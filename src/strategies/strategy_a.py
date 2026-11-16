"""Strategy A: Low-volatility range mean-reversion (SPEC Section 11)."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from src.data.models import OHLCVBar
from src.data.quality import _parse_iso_utc, is_market_open
from src.engine.simulator import CandidateSignal
from src.indicators.technical import (
    compute_adx,
    compute_atr,
    compute_atr_ratio,
    compute_bollinger_bandwidth_percentile,
    compute_rsi,
    resample_15m_to_1h,
)
from src.signals.models import AddLevel, InputSnapshot


@dataclass(frozen=True)
class StrategyAEvaluation:
    """Result of evaluating one instrument under Strategy A."""

    symbol: str
    market: str
    decision: str  # "signal" or "watch"
    candidate: CandidateSignal | None
    watch_reason: str
    filter_checks: dict[str, bool]
    indicators: dict[str, float | None]
    low_liquidity_hours: list[int]
    hourly_profile: dict[str, float]
    session_end_utc: str | None


class StrategyA:
    """Implements all Strategy A low-liquidity range filters and signal generation."""

    def __init__(self, config: dict[str, Any], events: list[dict[str, Any]] | None = None) -> None:
        self.config = config
        self.cfg = config.get("strategy_a", {})
        self.sched_cfg = config.get("schedule", {})
        self.events = events or []

    def compute_low_liquidity_profile(
        self,
        bars_15m: list[OHLCVBar],
        market: str,
    ) -> tuple[list[int], dict[str, float]]:
        """Identify bottom 1/3 UTC hours by volume (crypto) or range (forex/commodity)."""
        if not bars_15m:
            return [], {}
        lookback_days = int(self.cfg.get("lookback_days_liquidity", 45))
        latest_dt = _parse_iso_utc(bars_15m[-1].ts_utc)
        cutoff = latest_dt - timedelta(days=lookback_days)

        buckets: dict[int, list[float]] = defaultdict(list)
        for b in bars_15m:
            dt = _parse_iso_utc(b.ts_utc)
            if dt < cutoff:
                continue
            metric = b.volume if market == "crypto" else (b.high - b.low)
            buckets[dt.hour].append(metric)

        if not buckets:
            return [], {}

        hourly_avg: dict[int, float] = {
            hr: sum(vals) / len(vals) for hr, vals in buckets.items() if vals
        }
        pct = float(self.cfg.get("low_liquidity_percentile", 33.33))
        n_low = max(1, math.floor(len(hourly_avg) * pct / 100.0 + 1e-9))
        sorted_hours = sorted(hourly_avg.keys(), key=lambda h: (hourly_avg[h], h))
        low_hours = sorted(sorted_hours[:n_low])
        profile_str = {f"{hr:02d}": round(val, 6) for hr, val in sorted(hourly_avg.items())}
        return low_hours, profile_str

    def _in_settlement_window(self, dt: datetime, market: str) -> bool:
        if market not in ("forex", "commodity"):
            return False
        avoid_min = int(self.cfg.get("settlement_avoid_minutes", 30))
        minutes_of_day = dt.hour * 60 + dt.minute
        # Check ±avoid_min around 22:00 UTC (1320m) and 00:00 UTC (0m / 1440m)
        targets = (0, 22 * 60, 24 * 60)
        return any(abs(minutes_of_day - t) <= avoid_min for t in targets)

    def _is_hour_eligible(self, dt: datetime, market: str, low_liq_hours: list[int]) -> bool:
        if self._in_settlement_window(dt, market):
            return False
        if market == "crypto" and dt.weekday() >= 5:
            return True
        return dt.hour in low_liq_hours

    def is_low_liquidity_window(
        self,
        now_utc: datetime,
        market: str,
        low_liq_hours: list[int],
    ) -> tuple[bool, str | None, str]:
        """Check whether now_utc is inside an eligible low-liquidity window with >= 30m left."""
        if self._in_settlement_window(now_utc, market):
            return False, None, "避開外匯/商品每日結算與換日前後時段"

        if not self._is_hour_eligible(now_utc, market, low_liq_hours):
            return False, None, f"非低流動性時段（UTC {now_utc.hour:02d}:00）"

        # Estimate contiguous session end hour
        cursor = now_utc.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        for _ in range(48):
            if not self._is_hour_eligible(cursor, market, low_liq_hours):
                break
            cursor += timedelta(hours=1)

        pre_close_min = int(self.cfg.get("pre_session_end_close_minutes", 30))
        if (cursor - now_utc).total_seconds() / 60.0 <= pre_close_min:
            return False, cursor.strftime("%Y-%m-%dT%H:%M:%SZ"), "低流動性時段即將結束（不足 30 分鐘）"

        return True, cursor.strftime("%Y-%m-%dT%H:%M:%SZ"), "ok"

    def check_high_impact_events(
        self,
        now_utc: datetime,
        symbol: str,
    ) -> tuple[bool, str]:
        """Ensure no high-impact event within future 4h or past 60m."""
        before_hrs = float(self.cfg.get("event_before_hours", 4))
        after_min = float(self.cfg.get("event_after_minutes", 60))

        for ev in self.events:
            if str(ev.get("impact", "")).lower() != "high":
                continue
            ccy = str(ev.get("currency", "ALL")).upper()
            if ccy not in ("ALL", "USD") and ccy not in symbol.upper():
                continue
            ts_str = ev.get("time_utc")
            if not ts_str:
                continue
            ev_dt = _parse_iso_utc(ts_str)
            diff_min = (ev_dt - now_utc).total_seconds() / 60.0
            if -after_min <= diff_min <= before_hrs * 60.0:
                return False, f"高影響經濟事件前後避險（{ev.get('title', 'Event')}）"
        return True, "ok"

    def detect_range(self, closed_15m: list[OHLCVBar], atr_1h: float) -> dict[str, Any] | None:
        """Detect valid 48-bar 15m range with >= 2 edge touches and 1.0~2.5x ATR height."""
        n_bars = int(self.cfg.get("range_bars_15m", 48))
        if len(closed_15m) < n_bars or atr_1h <= 0:
            return None
        win = closed_15m[-n_bars:]
        range_high = max(b.high for b in win)
        range_low = min(b.low for b in win)
        height = range_high - range_low
        if height <= 0:
            return None

        min_mult = float(self.cfg.get("range_height_atr_min", 1.0))
        max_mult = float(self.cfg.get("range_height_atr_max", 2.5))
        height_atr = height / atr_1h
        if not (min_mult <= height_atr <= max_mult):
            return None

        edge_pct = float(self.cfg.get("entry_edge_pct", 15.0)) / 100.0
        upper_thresh = range_high - edge_pct * height
        lower_thresh = range_low + edge_pct * height
        min_touches = int(self.cfg.get("min_edge_touches", 2))

        upper_touches = sum(1 for b in win if b.high >= upper_thresh)
        lower_touches = sum(1 for b in win if b.low <= lower_thresh)
        if upper_touches < min_touches or lower_touches < min_touches:
            return None

        return {
            "range_high": range_high,
            "range_low": range_low,
            "height": height,
            "height_atr": height_atr,
            "upper_touches": upper_touches,
            "lower_touches": lower_touches,
        }

    def evaluate_instrument(
        self,
        instrument: dict[str, Any],
        bars_15m: list[OHLCVBar],
        report_1m: dict[str, Any],
        report_15m: dict[str, Any],
        now_utc: datetime,
        peer_metrics: dict[str, dict[str, Any]] | None = None,
        crypto_meta: dict[str, float] | None = None,
    ) -> StrategyAEvaluation:
        """Evaluate all Strategy A filters and return either a CandidateSignal or watch reason."""
        now = now_utc.astimezone(timezone.utc)
        symbol = instrument["symbol"]
        market = instrument["market"]
        corr_group = instrument.get("correlation_group", "")

        checks: dict[str, bool] = {}
        indicators_out: dict[str, float | None] = {}

        # 0) Strictly filter closed 15m bars only (no lookahead)
        closed_15m = sorted(
            [b for b in bars_15m if _parse_iso_utc(b.ts_utc) + timedelta(minutes=15) <= now],
            key=lambda b: b.ts_utc,
        )
        low_liq_hours, hourly_profile = self.compute_low_liquidity_profile(closed_15m, market)

        def _watch(reason: str, session_end: str | None = None) -> StrategyAEvaluation:
            return StrategyAEvaluation(
                symbol=symbol,
                market=market,
                decision="watch",
                candidate=None,
                watch_reason=reason,
                filter_checks=checks,
                indicators=indicators_out,
                low_liquidity_hours=low_liq_hours,
                hourly_profile=hourly_profile,
                session_end_utc=session_end,
            )

        # 1) Enabled markets & market open check
        enabled_markets = set(self.cfg.get("enabled_markets", ["crypto", "forex", "commodity"]))
        if market not in enabled_markets:
            checks["market_enabled"] = False
            return _watch(f"市場類別 {market} 未啟用策略 A")
        checks["market_enabled"] = True

        if not is_market_open(market, now):
            checks["market_open"] = False
            return _watch("目前為休市時段")
        checks["market_open"] = True

        # 2) Data quality check (must be ok for both 1m and 15m; unstable data excluded without fake fill)
        st_1m = report_1m.get("status", "failed")
        st_15m = report_15m.get("status", "failed")
        if st_1m != "ok" or st_15m != "ok":
            checks["data_quality_ok"] = False
            parts = []
            if st_1m != "ok":
                parts.append(f"1m={st_1m}")
            if st_15m != "ok":
                parts.append(f"15m={st_15m}")
            return _watch(f"資料不穩或異常（{', '.join(parts)}），依規格排除掃描")
        checks["data_quality_ok"] = True

        # 3) Schedule delay check (latest closed 15m bar <= max_candle_delay_minutes)
        if not closed_15m:
            checks["schedule_timely"] = False
            return _watch("無已收盤 15m K 線")
        latest_close_dt = _parse_iso_utc(closed_15m[-1].ts_utc) + timedelta(minutes=15)
        delay_min = (now - latest_close_dt).total_seconds() / 60.0
        max_delay_min = float(self.sched_cfg.get("max_candle_delay_minutes", 20))
        if delay_min > max_delay_min:
            checks["schedule_timely"] = False
            return _watch(f"排程延遲（最新收盤 15m K 線距今 {delay_min:.1f} 分鐘 > {max_delay_min:.0f} 分鐘）")
        checks["schedule_timely"] = True

        # 4) Low liquidity window check
        liq_ok, session_end_utc, liq_msg = self.is_low_liquidity_window(now, market, low_liq_hours)
        checks["low_liquidity_session"] = liq_ok
        if not liq_ok:
            return _watch(liq_msg, session_end_utc)

        # 5) High impact events check
        ev_ok, ev_msg = self.check_high_impact_events(now, symbol)
        checks["no_high_impact_event"] = ev_ok
        if not ev_ok:
            return _watch(ev_msg, session_end_utc)

        # 6) Crypto derivatives check (mandatory for crypto; missing data -> insufficient data)
        if market == "crypto":
            if (
                crypto_meta is None
                or crypto_meta.get("funding_rate") is None
                or crypto_meta.get("oi_change_1h_pct") is None
            ):
                checks["crypto_derivatives_ok"] = False
                return _watch("加密貨幣資金費率或未平倉量資料不足，無法驗證衍生品狀態", session_end_utc)
            max_fund = float(self.cfg.get("max_abs_funding_rate", 0.001))
            max_oi = float(self.cfg.get("max_oi_surge_pct", 10.0))
            fr = abs(float(crypto_meta["funding_rate"]))
            oi_chg = abs(float(crypto_meta["oi_change_1h_pct"]))
            deriv_ok = (fr <= max_fund) and (oi_chg <= max_oi)
            checks["crypto_derivatives_ok"] = deriv_ok
            if not deriv_ok:
                return _watch(f"加密貨幣資金費率或未平倉量異常（FR={fr:.4f}, OI變動={oi_chg:.1f}%）", session_end_utc)
        else:
            checks["crypto_derivatives_ok"] = True

        # 7) Correlated peer violent move check
        if peer_metrics and peer_metrics.get(corr_group, {}).get("violent_move"):
            checks["correlated_peers_calm"] = False
            return _watch(f"相關性群組 {corr_group} 同步劇烈波動，禁止逆勢操作", session_end_utc)
        checks["correlated_peers_calm"] = True

        # 8) 1H indicator regime filters (ADX < 20, BB bandwidth < 30%, ATR ratio < 1.2)
        bars_1h = resample_15m_to_1h(closed_15m, now_utc=now)
        adx_period = int(self.cfg.get("adx_period", 14))
        adx_max = float(self.cfg.get("adx_max", 20.0))
        bb_period = int(self.cfg.get("bb_period", 20))
        bb_std = float(self.cfg.get("bb_std", 2.0))
        bb_pct_max = float(self.cfg.get("bb_percentile_max", 30.0))
        atr_period = int(self.cfg.get("atr_period", 14))
        atr_base_bars = int(self.cfg.get("atr_baseline_days", 20)) * 24
        atr_ratio_max = float(self.cfg.get("atr_ratio_max", 1.2))

        adx_val = compute_adx(bars_1h, period=adx_period, market=market, interval="1h")
        bb_pct = compute_bollinger_bandwidth_percentile(bars_1h, period=bb_period, num_std=bb_std, lookback=720)
        atr_1h = compute_atr(bars_1h, period=atr_period, market=market, interval="1h")
        atr_ratio = compute_atr_ratio(bars_1h, atr_period=atr_period, baseline_bars=atr_base_bars, market=market, interval="1h")
        rsi_val = compute_rsi(closed_15m, period=int(self.cfg.get("rsi_period", 14)))

        indicators_out.update(
            {
                "adx_1h": round(adx_val, 2) if adx_val is not None else None,
                "bb_percentile": round(bb_pct, 2) if bb_pct is not None else None,
                "atr_1h": round(atr_1h, 6) if atr_1h is not None else None,
                "atr_ratio": round(atr_ratio, 3) if atr_ratio is not None else None,
                "rsi_14": round(rsi_val, 2) if rsi_val is not None else None,
                "last_close_15m": closed_15m[-1].close,
            }
        )

        if adx_val is None or bb_pct is None or atr_1h is None or atr_ratio is None or rsi_val is None:
            checks["indicators_ready"] = False
            return _watch(f"歷史 1H K 線不足（目前 {len(bars_1h)} 根），無法完成指標計算", session_end_utc)
        checks["indicators_ready"] = True

        adx_ok = adx_val < adx_max
        checks["adx_below_max"] = adx_ok
        if not adx_ok:
            return _watch(f"1H ADX(14)={adx_val:.1f} >= {adx_max}，處於趨勢狀態", session_end_utc)

        bb_ok = bb_pct < bb_pct_max
        checks["bb_low_percentile"] = bb_ok
        if not bb_ok:
            return _watch(f"布林帶寬百分位 {bb_pct:.1f}% >= {bb_pct_max}%，波動未收斂", session_end_utc)

        atr_ok = atr_ratio < atr_ratio_max
        checks["atr_ratio_below_max"] = atr_ok
        if not atr_ok:
            return _watch(f"ATR 相對均值倍數 {atr_ratio:.2f} >= {atr_ratio_max}，波動過大", session_end_utc)

        # 9) 48-bar 15m range check
        rng = self.detect_range(closed_15m, atr_1h=atr_1h)
        if rng is None:
            checks["valid_range"] = False
            return _watch("近 48 根 15m K 線未形成有效盤整區間（高度或測試次數不符）", session_end_utc)
        checks["valid_range"] = True

        # 10) Entry trigger (lower 15% + RSI < 35 -> long; upper 15% + RSI > 65 -> short)
        edge_frac = float(self.cfg.get("entry_edge_pct", 15.0)) / 100.0
        rsi_ob = float(self.cfg.get("rsi_overbought", 65.0))
        rsi_os = float(self.cfg.get("rsi_oversold", 35.0))
        sl_buf = float(self.cfg.get("sl_atr_buffer", 0.5))
        add_mult = float(self.cfg.get("add_atr_mult", 0.35))
        max_hold_bars = int(self.cfg.get("max_hold_bars_15m", 24))

        last_close = closed_15m[-1].close
        r_low = rng["range_low"]
        r_high = rng["range_high"]
        r_h = rng["height"]
        lower_bound_top = r_low + edge_frac * r_h
        upper_bound_bot = r_high - edge_frac * r_h

        direction: str | None = None
        if last_close <= lower_bound_top and rsi_val < rsi_os:
            direction = "long"
        elif last_close >= upper_bound_bot and rsi_val > rsi_ob:
            direction = "short"

        if direction is None:
            checks["entry_trigger"] = False
            return _watch(
                f"未觸及入場條件（收盤 {last_close:g}，區間 [{r_low:g}, {r_high:g}]，RSI={rsi_val:.1f}）",
                session_end_utc,
            )
        checks["entry_trigger"] = True

        if direction == "long":
            entry_low = round(r_low, 6)
            entry_high = round(lower_bound_top, 6)
            stop_loss = round(r_low - sl_buf * atr_1h, 6)
            tp1 = round((r_low + r_high) / 2.0, 6)
            tp2 = round(r_high - 0.10 * r_h, 6)
            add_px = round(max(stop_loss + 0.1 * atr_1h, last_close - add_mult * atr_1h), 6)
            inv_cond = f"15m 有效跌破區間下緣 {r_low:g}（止損 {stop_loss:g}）"
            entry_desc = f"價格進入區間下緣 {self.cfg.get('entry_edge_pct', 15)}% 範圍且 RSI(14)={rsi_val:.1f} < {rsi_os}"
        else:
            entry_low = round(upper_bound_bot, 6)
            entry_high = round(r_high, 6)
            stop_loss = round(r_high + sl_buf * atr_1h, 6)
            tp1 = round((r_low + r_high) / 2.0, 6)
            tp2 = round(r_low + 0.10 * r_h, 6)
            add_px = round(min(stop_loss - 0.1 * atr_1h, last_close + add_mult * atr_1h), 6)
            inv_cond = f"15m 有效突破區間上緣 {r_high:g}（止損 {stop_loss:g}）"
            entry_desc = f"價格進入區間上緣 {self.cfg.get('entry_edge_pct', 15)}% 範圍且 RSI(14)={rsi_val:.1f} > {rsi_ob}"

        entry_mid = (entry_low + entry_high) / 2.0
        risk_dist = (entry_mid - stop_loss) if direction == "long" else (stop_loss - entry_mid)
        rew1 = (tp1 - entry_mid) if direction == "long" else (entry_mid - tp1)
        rew2 = (tp2 - entry_mid) if direction == "long" else (entry_mid - tp2)
        tp1_min = float(self.cfg.get("tp1_rr_min", 1.5))
        tp2_min = float(self.cfg.get("tp2_rr_min", 2.0))
        rr1 = rew1 / max(1e-12, risk_dist)
        rr2 = rew2 / max(1e-12, risk_dist)
        if risk_dist <= 0 or rr1 < tp1_min - 1e-9 or rr2 < tp2_min - 1e-9:
            checks["rr_ok"] = False
            return _watch(
                f"風險報酬比不足（TP1={rr1:.2f} 需>={tp1_min}, TP2={rr2:.2f} 需>={tp2_min}）",
                session_end_utc,
            )
        checks["rr_ok"] = True

        reasons = (
            f"低流動性時段（UTC {now.hour:02d}:00，時段結束 {session_end_utc}）",
            f"1H ADX(14)={adx_val:.1f} < {adx_max}",
            f"布林帶寬百分位={bb_pct:.1f}% < {bb_pct_max}%",
            f"ATR 倍數={atr_ratio:.2f} < {atr_ratio_max}",
            "未來 4 小時無高影響事件且距上次事件 > 60 分鐘",
            f"48 根 15m 區間 [{r_low:g}, {r_high:g}]（高度 {rng['height_atr']:.2f}x ATR，上測 {rng['upper_touches']} 次、下測 {rng['lower_touches']} 次）",
            entry_desc,
        )

        snap = InputSnapshot(
            snapshot_id="PENDING",
            signal_id="PENDING",
            captured_at_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            symbol=symbol,
            kline_summary={
                "latest_15m_ts_utc": closed_15m[-1].ts_utc,
                "latest_15m_close": last_close,
                "range_low": r_low,
                "range_high": r_high,
            },
            indicators={k: v for k, v in indicators_out.items() if v is not None},
            filter_checks=checks,
        )

        candidate = CandidateSignal(
            strategy="A",
            symbol=symbol,
            direction=direction,
            entry_low=entry_low,
            entry_high=entry_high,
            add_levels=(AddLevel(stage=2, add_type="mean_add", price=add_px, note="ATR 逆勢補倉"),),
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            invalidation_price=stop_loss,
            invalidation_cond=inv_cond,
            reasons=reasons,
            snapshot=snap,
            atr=atr_1h,
            atr_ratio=atr_ratio,
            time_stop_bars=max_hold_bars,
        )

        return StrategyAEvaluation(
            symbol=symbol,
            market=market,
            decision="signal",
            candidate=candidate,
            watch_reason="",
            filter_checks=checks,
            indicators=indicators_out,
            low_liquidity_hours=low_liq_hours,
            hourly_profile=hourly_profile,
            session_end_utc=session_end_utc,
        )
