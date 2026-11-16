"""Strategy B: Long-term multi-factor trend following (SPEC Section 12)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from src.backtest.screener import _ema_series
from src.data.models import OHLCVBar
from src.data.quality import _parse_iso_utc
from src.engine.simulator import CandidateSignal
from src.indicators.technical import compute_atr, compute_rsi
from src.signals.models import AddLevel, InputSnapshot


@dataclass(frozen=True)
class StrategyBEvaluation:
    """Result of daily Strategy B multi-factor evaluation."""

    symbol: str
    market: str
    decision: str  # "signal" or "watch"
    candidate: CandidateSignal | None
    watch_reason: str
    filter_checks: dict[str, bool]
    indicators: dict[str, Any]


class StrategyB:
    """Multi-factor daily trend signal generator for screened instruments."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.cfg = config.get("strategy_b", {})

    def evaluate_instrument(
        self,
        instrument: dict[str, Any],
        bars_1d: list[OHLCVBar],
        backtest_entry: dict[str, Any] | None,
        report_1d: dict[str, Any],
        now_utc: datetime,
        peer_metrics: dict[str, dict[str, Any]] | None = None,
    ) -> StrategyBEvaluation:
        now = now_utc.astimezone(timezone.utc)
        symbol = instrument["symbol"]
        market = instrument["market"]
        corr_group = instrument.get("correlation_group", "")
        checks: dict[str, bool] = {}
        indicators: dict[str, Any] = {}

        def _watch(reason: str) -> StrategyBEvaluation:
            return StrategyBEvaluation(
                symbol=symbol,
                market=market,
                decision="watch",
                candidate=None,
                watch_reason=reason,
                filter_checks=checks,
                indicators=indicators,
            )

        # 1) Must pass weekly OOS screening
        if not backtest_entry or not backtest_entry.get("passed", False):
            checks["passed_screening"] = False
            reason = backtest_entry.get("reason", "尚未通過篩選") if backtest_entry else "無回測篩選資料"
            return _watch(f"未通過策略 B 樣本外篩選（{reason}）")
        checks["passed_screening"] = True

        # 2) Data quality check
        st_1d = report_1d.get("status", "failed")
        if st_1d in ("failed", "unstable"):
            checks["data_quality_ok"] = False
            return _watch(f"日線資料異常（{st_1d}）")
        checks["data_quality_ok"] = True

        # 3) Strictly closed daily bars only
        closed_1d = sorted(
            [b for b in bars_1d if _parse_iso_utc(b.ts_utc) + timedelta(days=1) <= now],
            key=lambda b: b.ts_utc,
        )
        if len(closed_1d) < 210:
            checks["history_ready"] = False
            return _watch(f"已收盤日線不足 210 根（目前 {len(closed_1d)} 根）")
        checks["history_ready"] = True

        closes = [b.close for b in closed_1d]
        highs = [b.high for b in closed_1d]
        lows = [b.low for b in closed_1d]
        vols = [b.volume for b in closed_1d]

        ema50 = _ema_series(closes, 50)[-1]
        ema200 = _ema_series(closes, 200)[-1]
        weekly_ema = _ema_series(closes, 100)[-1]
        ema12 = _ema_series(closes, 12)
        ema26 = _ema_series(closes, 26)
        macd_line = [a - b for a, b in zip(ema12, ema26)]
        macd_sig = _ema_series(macd_line, 9)
        macd_hist = macd_line[-1] - macd_sig[-1]

        atr_14 = compute_atr(closed_1d, period=14, market=market, interval="1d")
        rsi_14 = compute_rsi(closed_1d, period=14)
        if atr_14 is None or atr_14 <= 0 or rsi_14 is None:
            checks["indicators_ready"] = False
            return _watch("日線指標計算不足")
        checks["indicators_ready"] = True

        res_20 = max(highs[-21:-1])
        sup_20 = min(lows[-21:-1])
        avg_vol_20 = sum(vols[-21:-1]) / 20.0 if sum(vols[-21:-1]) > 0 else 0.0
        vol_ratio = (vols[-1] / avg_vol_20) if avg_vol_20 > 0 else 1.0
        last_close = closes[-1]

        trending = abs(ema50 - ema200) > atr_14
        regime = "trending" if trending else "consolidating"

        indicators.update(
            {
                "last_close": round(last_close, 6),
                "ema50": round(ema50, 6),
                "ema200": round(ema200, 6),
                "weekly_ema": round(weekly_ema, 6),
                "atr_14": round(atr_14, 6),
                "rsi_14": round(rsi_14, 2),
                "macd_hist": round(macd_hist, 6),
                "vol_ratio": round(vol_ratio, 2),
                "support_20": round(sup_20, 6),
                "resistance_20": round(res_20, 6),
                "regime": regime,
            }
        )

        # Peer check
        if peer_metrics and peer_metrics.get(corr_group, {}).get("counter_trend"):
            checks["peer_aligned"] = False
            return _watch(f"相關性群組 {corr_group} 走勢分歧，暫觀望")
        checks["peer_aligned"] = True

        # Multi-factor consensus check (trend + weekly + S/R breakout + MACD + RSI + volume)
        params = backtest_entry.get("chosen_params") or {}
        sl_mult = float(params.get("sl_atr_mult", self.cfg.get("sl_atr_min_mult", 2.5)))
        sl_mult = max(float(self.cfg.get("sl_atr_min_mult", 2.0)), min(float(self.cfg.get("sl_atr_max_mult", 3.0)), sl_mult))
        min_rr = float(self.cfg.get("min_rr", 2.0))

        long_ok = (
            last_close > ema50 > ema200
            and last_close > weekly_ema
            and last_close > res_20
            and macd_hist > 0
            and 50.0 <= rsi_14 <= 75.0
            and (market in ("forex",) or vol_ratio >= 0.8)
        )
        short_ok = (
            last_close < ema50 < ema200
            and last_close < weekly_ema
            and last_close < sup_20
            and macd_hist < 0
            and 25.0 <= rsi_14 <= 50.0
            and (market in ("forex",) or vol_ratio >= 0.8)
        )

        if not (long_ok or short_ok):
            checks["multifactor_consensus"] = False
            return _watch(f"多因子尚未全面共振（狀態：{regime}，EMA50={ema50:g}，RSI={rsi_14:.1f}，MACD柱={macd_hist:g}）")
        checks["multifactor_consensus"] = True

        direction = "long" if long_ok else "short"
        sl_dist = sl_mult * atr_14
        if direction == "long":
            entry_low = round(last_close - 0.2 * atr_14, 6)
            entry_high = round(last_close + 0.2 * atr_14, 6)
            stop_loss = round(last_close - sl_dist, 6)
            tp1 = round(last_close + min_rr * sl_dist, 6)
            tp2_desc = f"ATR({sl_mult}x) 追蹤止損或收盤跌破 EMA50 ({ema50:g})"
            add_levels = (
                AddLevel(stage=2, add_type="trend_add", price=round(last_close + 1.0 * atr_14, 6), note="獲利 >= 1R 順勢加倉"),
            )
            inv_cond = f"日線收盤跌破 EMA50 ({ema50:g}) 或止損 {stop_loss:g}"
        else:
            entry_low = round(last_close - 0.2 * atr_14, 6)
            entry_high = round(last_close + 0.2 * atr_14, 6)
            stop_loss = round(last_close + sl_dist, 6)
            tp1 = round(last_close - min_rr * sl_dist, 6)
            tp2_desc = f"ATR({sl_mult}x) 追蹤止損或收盤站回 EMA50 ({ema50:g})"
            add_levels = (
                AddLevel(stage=2, add_type="trend_add", price=round(last_close - 1.0 * atr_14, 6), note="獲利 >= 1R 順勢加倉"),
            )
            inv_cond = f"日線收盤站上 EMA50 ({ema50:g}) 或止損 {stop_loss:g}"

        reasons = (
            f"通過策略 B 週回測篩選（{backtest_entry.get('chosen_strategy')}）",
            f"多週期趨勢一致（收盤 {last_close:g} / EMA50 {ema50:g} / EMA200 {ema200:g} / 週線EMA {weekly_ema:g}）",
            f"20日關鍵支撐壓力突破（支撐 {sup_20:g} / 壓力 {res_20:g}）",
            f"動能共振（RSI={rsi_14:.1f}, MACD柱={macd_hist:g}, 量比={vol_ratio:.2f}）",
        )

        snap = InputSnapshot(
            snapshot_id="PENDING",
            signal_id="PENDING",
            captured_at_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            symbol=symbol,
            kline_summary={"latest_1d_ts_utc": closed_1d[-1].ts_utc, "latest_1d_close": last_close},
            indicators=indicators,
            filter_checks=checks,
        )

        candidate = CandidateSignal(
            strategy="B",
            symbol=symbol,
            direction=direction,
            entry_low=entry_low,
            entry_high=entry_high,
            add_levels=add_levels,
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2_desc,
            invalidation_price=stop_loss,
            invalidation_cond=inv_cond,
            reasons=reasons,
            snapshot=snap,
            atr=atr_14,
            atr_ratio=1.0,
            time_stop_bars=0,
        )
        return StrategyBEvaluation(
            symbol=symbol,
            market=market,
            decision="signal",
            candidate=candidate,
            watch_reason="",
            filter_checks=checks,
            indicators=indicators,
        )
