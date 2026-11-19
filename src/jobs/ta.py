"""Daily technical analysis calculation job producing `data/ta.json`."""

from __future__ import annotations

import argparse
import json
import math
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config_loader import load_config
from src.data.adapters import FallbackDataFetcher
from src.data.models import OHLCVBar
from src.indicators.technical import (
    compute_adx,
    compute_atr,
    compute_atr_series,
    compute_bollinger_bandwidth_percentile,
)

TA_DISCLAIMER = "以上為依歷史數據與技術指標計算的參考點位，不是預測，也不是投資建議。"
ROOT_DIR = Path(__file__).resolve().parent.parent.parent


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def find_swing_points(
    bars: list[OHLCVBar],
    left_right: int = 5,
) -> tuple[list[tuple[int, str, float]], list[tuple[int, str, float]]]:
    """Return confirmed swing highs and swing lows: list[(idx, date_str, price)]."""
    n = len(bars)
    k = max(1, left_right)
    highs: list[tuple[int, str, float]] = []
    lows: list[tuple[int, str, float]] = []
    if n < 2 * k + 1:
        return highs, lows

    for i in range(k, n - k):
        hi_i = bars[i].high
        lo_i = bars[i].low
        left_hi = max(bars[j].high for j in range(i - k, i))
        right_hi = max(bars[j].high for j in range(i + 1, i + k + 1))
        # Strictly > left or >= left when right is strictly lower (prevents flat double-count)
        if hi_i >= left_hi and hi_i > right_hi:
            highs.append((i, bars[i].ts_utc[:10], float(hi_i)))
        elif hi_i > left_hi and hi_i >= right_hi:
            highs.append((i, bars[i].ts_utc[:10], float(hi_i)))

        left_lo = min(bars[j].low for j in range(i - k, i))
        right_lo = min(bars[j].low for j in range(i + 1, i + k + 1))
        if lo_i <= left_lo and lo_i < right_lo:
            lows.append((i, bars[i].ts_utc[:10], float(lo_i)))
        elif lo_i < left_lo and lo_i <= right_lo:
            lows.append((i, bars[i].ts_utc[:10], float(lo_i)))

    return highs, lows


def _merge_sr_levels(
    raw_levels: list[tuple[float, str, str]],
    cur_px: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Deduplicate identical price levels and split into resistances (>= cur) and supports (< cur)."""
    tol = max(1e-9, abs(cur_px) * 1e-6)
    merged: list[dict[str, Any]] = []

    for px, algo, kind in raw_levels:
        if px is None or not math.isfinite(px) or px <= 0:
            continue
        matched = None
        for m in merged:
            if abs(m["price"] - px) <= tol:
                matched = m
                break
        if matched is not None:
            if algo not in matched["algos"]:
                matched["algos"].append(algo)
        else:
            merged.append({"price": float(px), "algos": [algo], "kind": kind})

    resistances: list[dict[str, Any]] = []
    supports: list[dict[str, Any]] = []
    for item in merged:
        is_res = item["price"] >= cur_px
        algos = list(item["algos"])
        if is_res and item["kind"] == "swing_low":
            algos = [a.replace("擺動低點（", "前期擺動低點（跌破轉壓力 ") for a in algos]
        elif (not is_res) and item["kind"] == "swing_high":
            algos = [a.replace("擺動高點（", "前期擺動高點（突破轉支撐 ") for a in algos]
        entry = {
            "price": item["price"],
            "algo": "／".join(algos),
        }
        if is_res:
            resistances.append(entry)
        else:
            supports.append(entry)

    # Nearest to farthest
    resistances.sort(key=lambda x: x["price"])
    supports.sort(key=lambda x: x["price"], reverse=True)
    return resistances, supports


def compute_fibonacci_retracement(
    bars: list[OHLCVBar],
    swing_highs: list[tuple[int, str, float]],
    swing_lows: list[tuple[int, str, float]],
    atr20: float | None = None,
) -> dict[str, Any] | None:
    """Compute 38.2%, 50%, 61.8% retracement on the most recent major swing wave."""
    if len(bars) < 10:
        return None

    win_len = min(len(bars), 120)
    win = bars[-win_len:]
    offset = len(bars) - win_len
    win_hi_rel = max(range(win_len), key=lambda i: win[i].high)
    win_lo_rel = min(range(win_len), key=lambda i: win[i].low)
    major_sh = (offset + win_hi_rel, win[win_hi_rel].ts_utc[:10], float(win[win_hi_rel].high))
    major_sl = (offset + win_lo_rel, win[win_lo_rel].ts_utc[:10], float(win[win_lo_rel].low))
    major_range = major_sh[2] - major_sl[2]

    # Check latest confirmed k=5 swings in recent 180 bars
    cutoff = max(0, len(bars) - 180)
    recent_sh = [s for s in swing_highs if s[0] >= cutoff]
    recent_sl = [s for s in swing_lows if s[0] >= cutoff]
    sh = recent_sh[-1] if recent_sh else (swing_highs[-1] if swing_highs else None)
    sl = recent_sl[-1] if recent_sl else (swing_lows[-1] if swing_lows else None)

    cur_px = bars[-1].close
    buf = atr20 if (atr20 is not None and atr20 > 0) else 0.0

    # Use recent confirmed wave only if it's meaningful (>= 40% of 120d range) and still encompasses price
    use_major = False
    if sh is None or sl is None or sh[2] <= sl[2]:
        use_major = True
    elif major_range > 0 and (sh[2] - sl[2]) < 0.40 * major_range:
        use_major = True
    elif cur_px < sl[2] - buf or cur_px > sh[2] + buf:
        use_major = True

    if use_major:
        # Prefer prominent confirmed swing high/low inside 120d if close to major extremes, else major extreme
        sh = max(recent_sh, key=lambda s: s[2]) if recent_sh and max(s[2] for s in recent_sh) >= major_sh[2] - buf else major_sh
        sl = min(recent_sl, key=lambda s: s[2]) if recent_sl and min(s[2] for s in recent_sl) <= major_sl[2] + buf else major_sl
        if sh[2] <= sl[2]:
            sh, sl = major_sh, major_sl

    hi_idx, hi_date, hi_px = sh
    lo_idx, lo_date, lo_px = sl
    diff = hi_px - lo_px
    if diff <= 1e-12:
        return None

    if hi_idx >= lo_idx:
        # Recent primary wave was upward (low -> high); retracement measures pullback support
        direction = "up"
        direction_label = "上漲波段回撤"
        start_date, start_price = lo_date, lo_px
        end_date, end_price = hi_date, hi_px
        lvls = {
            "38.2%": hi_px - 0.382 * diff,
            "50.0%": hi_px - 0.500 * diff,
            "61.8%": hi_px - 0.618 * diff,
        }
    else:
        # Recent primary wave was downward (high -> low); retracement measures rebound resistance
        direction = "down"
        direction_label = "下跌波段反彈"
        start_date, start_price = hi_date, hi_px
        end_date, end_price = lo_date, lo_px
        lvls = {
            "38.2%": lo_px + 0.382 * diff,
            "50.0%": lo_px + 0.500 * diff,
            "61.8%": lo_px + 0.618 * diff,
        }

    return {
        "direction": direction,
        "direction_label": direction_label,
        "start_date": start_date,
        "start_price": start_price,
        "end_date": end_date,
        "end_price": end_price,
        "levels": lvls,
    }


def classify_trend(
    bars: list[OHLCVBar],
    cur_px: float,
    market: str,
) -> dict[str, Any]:
    """Evaluate SMA20/50/200 alignment + ADX14 into 上升趨勢 / 下降趨勢 / 盤整."""
    closes = [b.close for b in bars]
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    sma200 = _sma(closes, 200)
    # Use continuous daily Wilder ADX (market="crypto" skips intraday settlement-break suppression on 1d bars)
    adx14 = compute_adx(bars, period=14, market="crypto", interval="1d")

    state = "盤整"
    detail_parts: list[str] = []

    if sma20 is not None and sma50 is not None and sma200 is not None:
        if sma20 > sma50 > sma200 and cur_px > sma50:
            if adx14 is None or adx14 >= 20.0:
                state = "上升趨勢"
                detail_parts.append("SMA20 > SMA50 > SMA200 多頭排列")
            else:
                state = "盤整"
                detail_parts.append("均線多頭排列但 ADX < 20 動能偏弱")
        elif sma20 < sma50 < sma200 and cur_px < sma50:
            if adx14 is None or adx14 >= 20.0:
                state = "下降趨勢"
                detail_parts.append("SMA20 < SMA50 < SMA200 空頭排列")
            else:
                state = "盤整"
                detail_parts.append("均線空頭排列但 ADX < 20 動能偏弱")
        elif adx14 is not None and adx14 >= 25.0 and cur_px > sma20 > sma50:
            state = "上升趨勢"
            detail_parts.append("價格位於 SMA20/50 之上且 ADX ≥ 25")
        elif adx14 is not None and adx14 >= 25.0 and cur_px < sma20 < sma50:
            state = "下降趨勢"
            detail_parts.append("價格位於 SMA20/50 之下且 ADX ≥ 25")
        else:
            state = "盤整"
            detail_parts.append("均線交錯或尚未形成一致方向")
    elif sma20 is not None and sma50 is not None:
        if cur_px > sma20 > sma50 and (adx14 is None or adx14 >= 20.0):
            state = "上升趨勢"
            detail_parts.append("短中期均線多頭排列 (歷史未滿 200 日)")
        elif cur_px < sma20 < sma50 and (adx14 is None or adx14 >= 20.0):
            state = "下降趨勢"
            detail_parts.append("短中期均線空頭排列 (歷史未滿 200 日)")
        else:
            detail_parts.append("均線糾結盤整 (歷史未滿 200 日)")
    else:
        detail_parts.append("日線資料不足以計算完整均線")

    if adx14 is not None:
        detail_parts.append(f"ADX14 = {round(adx14, 1)}")

    return {
        "state": state,
        "sma20": round(sma20, 6) if sma20 is not None else None,
        "sma50": round(sma50, 6) if sma50 is not None else None,
        "sma200": round(sma200, 6) if sma200 is not None else None,
        "adx14": round(adx14, 2) if adx14 is not None else None,
        "detail": "，".join(detail_parts),
    }


def detect_chart_pattern(
    swing_highs: list[tuple[int, str, float]],
    swing_lows: list[tuple[int, str, float]],
    atr20: float | None,
    cur_px: float,
) -> dict[str, str]:
    """Identify 三角收斂 / 箱型 / 上升通道 / 下降通道 / 無明顯型態 from recent swing lines."""
    recent_hi = swing_highs[-3:] if len(swing_highs) >= 2 else swing_highs
    recent_lo = swing_lows[-3:] if len(swing_lows) >= 2 else swing_lows

    if len(recent_hi) < 2 or len(recent_lo) < 2 or atr20 is None or atr20 <= 0:
        return {
            "name": "無明顯型態",
            "note": "近期確認擺動高低點不足兩組，無法連線構成幾何型態（僅供參考）",
        }

    h_first, h_last = recent_hi[0], recent_hi[-1]
    l_first, l_last = recent_lo[0], recent_lo[-1]

    delta_hi = h_last[2] - h_first[2]
    delta_lo = l_last[2] - l_first[2]

    # Normalize slope across the swing span relative to ATR20
    tol = 0.8 * atr20
    hi_dir = 1 if delta_hi > tol else (-1 if delta_hi < -tol else 0)
    lo_dir = 1 if delta_lo > tol else (-1 if delta_lo < -tol else 0)

    width_first = h_first[2] - l_first[2]
    width_last = h_last[2] - l_last[2]
    is_narrowing = (width_first > 0) and (width_last < 0.85 * width_first)

    if (hi_dir == -1 and lo_dir == 1) or (is_narrowing and ((hi_dir == -1 and lo_dir == 0) or (hi_dir == 0 and lo_dir == 1))):
        return {
            "name": "三角收斂",
            "note": "擺動高點下移／低點墊高，波動區間逐步收窄（僅供參考）",
        }
    if hi_dir == 1 and lo_dir == 1:
        return {
            "name": "上升通道",
            "note": "擺動高點與擺動低點同步墊高，呈現上升通道軌跡（僅供參考）",
        }
    if hi_dir == -1 and lo_dir == -1:
        return {
            "name": "下降通道",
            "note": "擺動高點與擺動低點同步走低，呈現下降通道軌跡（僅供參考）",
        }
    if hi_dir == 0 and lo_dir == 0 and cur_px <= h_last[2] + tol and cur_px >= l_last[2] - tol:
        return {
            "name": "箱型",
            "note": "擺動高點與低點落在相近水平區間內來回震盪（僅供參考）",
        }

    return {
        "name": "無明顯型態",
        "note": "近期擺動高低點連線未構成標準三角、箱型或通道（僅供參考）",
    }


def backtest_sr_hold_rates(
    bars: list[OHLCVBar],
    market: str,
    left_right: int = 5,
    hold_days: int = 5,
) -> dict[str, Any]:
    """Causal 10-year historical test of 5-day support/resistance hold rates within 1*ATR."""
    n = len(bars)
    if n < 80:
        return {
            "years": round(n / 252.0, 1) if market != "crypto" else round(n / 365.0, 1),
            "support_hold_rate_pct": None,
            "support_held": 0,
            "support_samples": 0,
            "support_insufficient": True,
            "support_label": "樣本不足 (0 次)",
            "resistance_hold_rate_pct": None,
            "resistance_held": 0,
            "resistance_samples": 0,
            "resistance_insufficient": True,
            "resistance_label": "樣本不足 (0 次)",
        }

    atr_raw = compute_atr_series(bars, period=20, market="crypto", interval="1d")
    atr20: list[float | None] = [None] * n
    for idx_a, v in enumerate(atr_raw):
        if 20 + idx_a < n:
            atr20[20 + idx_a] = v

    swing_highs, swing_lows = find_swing_points(bars, left_right=left_right)
    # Confirmation bar for pivot at index p is p + left_right (strictly causal)
    sh_by_confirm: dict[int, list[float]] = {}
    for p, _, px in swing_highs:
        sh_by_confirm.setdefault(p + left_right, []).append(px)

    sl_by_confirm: dict[int, list[float]] = {}
    for p, _, px in swing_lows:
        sl_by_confirm.setdefault(p + left_right, []).append(px)

    active_supports: list[float] = []
    active_resistances: list[float] = []
    last_sup_touch = -999
    last_res_touch = -999

    sup_samples = 0
    sup_held = 0
    res_samples = 0
    res_held = 0

    for t in range(25, n - hold_days):
        if t in sl_by_confirm:
            for px in sl_by_confirm[t]:
                active_supports.append(px)
            active_supports = active_supports[-3:]
        if t in sh_by_confirm:
            for px in sh_by_confirm[t]:
                active_resistances.append(px)
            active_resistances = active_resistances[-3:]

        av = atr20[t]
        if av is None or av <= 0:
            continue

        prev_c = bars[t - 1].close
        cur_lo = bars[t].low
        cur_hi = bars[t].high

        # Check touch of nearest active swing low support (approaching from above)
        if active_supports and (t - last_sup_touch) >= hold_days:
            below_sups = [s for s in active_supports if prev_c > s]
            if below_sups:
                nearest_s = max(below_sups)
                if cur_lo <= nearest_s + 0.25 * av:
                    sup_samples += 1
                    last_sup_touch = t
                    held = all(bars[j].close >= (nearest_s - 1.0 * av) for j in range(t, t + hold_days))
                    if held:
                        sup_held += 1

        # Check touch of nearest active swing high resistance (approaching from below)
        if active_resistances and (t - last_res_touch) >= hold_days:
            above_ress = [r for r in active_resistances if prev_c < r]
            if above_ress:
                nearest_r = min(above_ress)
                if cur_hi >= nearest_r - 0.25 * av:
                    res_samples += 1
                    last_res_touch = t
                    held = all(bars[j].close <= (nearest_r + 1.0 * av) for j in range(t, t + hold_days))
                    if held:
                        res_held += 1

    years_approx = round((n / 365.0) if market == "crypto" else (n / 252.0), 1)
    sup_rate = round(sup_held / sup_samples * 100.0, 1) if sup_samples > 0 else None
    res_rate = round(res_held / res_samples * 100.0, 1) if res_samples > 0 else None

    sup_insuff = sup_samples < 30
    res_insuff = res_samples < 30

    sup_label = (
        f"{sup_rate}%（{sup_samples} 次，{'樣本不足' if sup_insuff else f'{years_approx} 年'}）"
        if sup_rate is not None
        else "樣本不足（0 次）"
    )
    res_label = (
        f"{res_rate}%（{res_samples} 次，{'樣本不足' if res_insuff else f'{years_approx} 年'}）"
        if res_rate is not None
        else "樣本不足（0 次）"
    )

    return {
        "years": years_approx,
        "support_hold_rate_pct": sup_rate,
        "support_held": sup_held,
        "support_samples": sup_samples,
        "support_insufficient": sup_insuff,
        "support_label": sup_label,
        "resistance_hold_rate_pct": res_rate,
        "resistance_held": res_held,
        "resistance_samples": res_samples,
        "resistance_insufficient": res_insuff,
        "resistance_label": res_label,
    }


def analyze_symbol_ta(
    symbol: str,
    display_name: str,
    market: str,
    bars_1d: list[OHLCVBar],
    latest_price: float | None = None,
    long_bars_1d: list[OHLCVBar] | None = None,
    existing_backtest: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Compute complete technical analysis snapshot for a single symbol."""
    eff_bars = sorted({b.ts_utc[:10]: b for b in (long_bars_1d or bars_1d)}.values(), key=lambda x: x.ts_utc)
    if bars_1d:
        merged = {b.ts_utc[:10]: b for b in eff_bars}
        for b in bars_1d:
            merged[b.ts_utc[:10]] = b
        eff_bars = sorted(merged.values(), key=lambda x: x.ts_utc)

    if len(eff_bars) < 25:
        return None

    cur_px = float(latest_price) if (latest_price is not None and latest_price > 0) else float(eff_bars[-1].close)
    latest_date = eff_bars[-1].ts_utc[:10]

    # 1. Support & Resistance (3 swing highs, 3 swing lows, 20d H/L, 60d H/L)
    swing_highs, swing_lows = find_swing_points(eff_bars, left_right=5)
    raw_levels: list[tuple[float, str, str]] = []
    for _, dt_s, px in swing_highs[-3:]:
        raw_levels.append((px, f"擺動高點（5日確認 {dt_s}）", "swing_high"))
    for _, dt_s, px in swing_lows[-3:]:
        raw_levels.append((px, f"擺動低點（5日確認 {dt_s}）", "swing_low"))

    win20 = eff_bars[-20:]
    raw_levels.append((max(b.high for b in win20), "近 20 日最高", "20d_high"))
    raw_levels.append((min(b.low for b in win20), "近 20 日最低", "20d_low"))

    if len(eff_bars) >= 60:
        win60 = eff_bars[-60:]
        raw_levels.append((max(b.high for b in win60), "近 60 日最高", "60d_high"))
        raw_levels.append((min(b.low for b in win60), "近 60 日最低", "60d_low"))

    resistances, supports = _merge_sr_levels(raw_levels, cur_px)

    # 3. Volatility Range (ATR20 * sqrt(days))
    atr20 = compute_atr(eff_bars, period=20, market="crypto", interval="1d")

    # 2. Fibonacci Retracement (computed with ATR20 scale check)
    fibo = compute_fibonacci_retracement(eff_bars, swing_highs, swing_lows, atr20=atr20)
    week_days = 7 if market == "crypto" else 5
    vol_info: dict[str, Any] = {
        "atr20": atr20,
        "tomorrow": None,
        "week": None,
    }
    if atr20 is not None and atr20 > 0:
        sq_wk = math.sqrt(week_days)
        vol_info["tomorrow"] = {
            "low": max(0.0, cur_px - atr20),
            "high": cur_px + atr20,
            "days": 1,
        }
        vol_info["week"] = {
            "low": max(0.0, cur_px - atr20 * sq_wk),
            "high": cur_px + atr20 * sq_wk,
            "days": week_days,
        }

    # 4. Trend State
    trend = classify_trend(eff_bars, cur_px, market=market)

    # 5. Compression (Bollinger BandWidth 20,2 percentile over 1y)
    lb_year = 365 if market == "crypto" else 252
    bb_pct = compute_bollinger_bandwidth_percentile(
        eff_bars, period=20, num_std=2.0, lookback=min(lb_year, max(30, len(eff_bars) - 20))
    )
    is_squeezing = bb_pct is not None and bb_pct < 20.0
    squeeze_label = "收斂中，可能變盤" if is_squeezing else ("波動擴張中" if (bb_pct is not None and bb_pct >= 80.0) else "波動正常")

    # 6. Pattern Recognition
    pattern = detect_chart_pattern(swing_highs, swing_lows, atr20, cur_px)

    # 7. Historical 10Y Validation
    if len(eff_bars) < 500 and existing_backtest and (existing_backtest.get("years") or 0) > 3.0:
        backtest = existing_backtest
    else:
        backtest = backtest_sr_hold_rates(eff_bars, market=market, left_right=5, hold_days=5)

    # One-line summary
    sq_short = "正在收斂" if is_squeezing else squeeze_label
    near_res = resistances[0]["price"] if resistances else None
    near_sup = supports[0]["price"] if supports else None
    summary_parts = [trend["state"], sq_short]
    if near_res is not None:
        summary_parts.append(f"上方壓力 {near_res:,.4g}" if near_res < 100 else f"上方壓力 {near_res:,.2f}")
    if near_sup is not None:
        summary_parts.append(f"下方支撐 {near_sup:,.4g}" if near_sup < 100 else f"下方支撐 {near_sup:,.2f}")

    return {
        "symbol": symbol,
        "display_name": display_name,
        "market": market,
        "latest_price": cur_px,
        "latest_bar_date": latest_date,
        "bars_count": len(eff_bars),
        "summary": "，".join(summary_parts),
        "resistances": resistances,
        "supports": supports,
        "fibonacci": fibo,
        "volatility": vol_info,
        "trend": trend,
        "squeeze": {
            "bb_width_pct": bb_pct,
            "lookback_days": lb_year,
            "is_squeezing": is_squeezing,
            "label": squeeze_label,
        },
        "pattern": pattern,
        "backtest": backtest,
    }


def _load_research_1d_cache() -> dict[str, list[OHLCVBar]]:
    p = ROOT_DIR / ".cache" / "research_1d_bars.pkl"
    if not p.exists():
        return {}
    try:
        loaded = pickle.loads(p.read_bytes())
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def generate_ta_file(
    out_dir: Path,
    config: dict[str, Any] | None = None,
    bars_1d_by_sym: dict[str, list[OHLCVBar]] | None = None,
    latest_prices: dict[str, float] | None = None,
    data_fetcher: FallbackDataFetcher | None = None,
    now_utc: datetime | None = None,
    fetch_10y_if_missing: bool = False,
) -> dict[str, Any]:
    """Build technical analysis for all active instruments and write `out_dir/ta.json`."""
    cfg = config or load_config()
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    research_1d = _load_research_1d_cache()
    existing_symbols: dict[str, Any] = {}
    ta_path = out_dir / "ta.json"
    if ta_path.exists():
        try:
            old_doc = json.loads(ta_path.read_text(encoding="utf-8"))
            if isinstance(old_doc, dict) and isinstance(old_doc.get("symbols"), dict):
                existing_symbols = old_doc["symbols"]
        except Exception:
            existing_symbols = {}

    bars_map = bars_1d_by_sym or {}
    marks = latest_prices or {}
    instruments = [inst for inst in cfg.get("instruments", []) if inst.get("enabled", True)]

    out_symbols: dict[str, Any] = {}
    for inst in instruments:
        sym = inst["symbol"]
        mkt = inst["market"]
        disp = inst.get("display_name", sym)

        live_bars = list(bars_map.get(sym, []))
        cached_10y = list(research_1d.get(sym, []))

        if fetch_10y_if_missing and data_fetcher is not None and (len(cached_10y) + len(live_bars)) < 500:
            try:
                res_long = data_fetcher.fetch(inst, "1d", limit=2600, now_utc=now)
                if res_long.ok and res_long.bars:
                    cached_10y = res_long.bars
            except Exception:
                pass

        old_sym_ta = existing_symbols.get(sym) or {}
        old_bt = old_sym_ta.get("backtest") if isinstance(old_sym_ta, dict) else None

        ta_entry = analyze_symbol_ta(
            symbol=sym,
            display_name=disp,
            market=mkt,
            bars_1d=live_bars,
            latest_price=marks.get(sym),
            long_bars_1d=cached_10y,
            existing_backtest=old_bt,
        )
        if ta_entry is not None:
            out_symbols[sym] = ta_entry

    payload = {
        "updated_at_utc": now_iso,
        "disclaimer": TA_DISCLAIMER,
        "symbols_count": len(out_symbols),
        "symbols": out_symbols,
    }
    ta_path.parent.mkdir(parents=True, exist_ok=True)
    ta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate daily technical analysis data/ta.json")
    parser.add_argument("--out-dir", type=Path, default=Path("data"))
    parser.add_argument("--fetch-10y", action="store_true", help="Fetch 10y 1d history if local cache missing")
    args = parser.parse_args()

    cfg = load_config()
    fetcher = FallbackDataFetcher(timeout_sec=10) if args.fetch_10y else None
    payload = generate_ta_file(
        out_dir=args.out_dir,
        config=cfg,
        data_fetcher=fetcher,
        fetch_10y_if_missing=args.fetch_10y,
    )
    print(f"Generated {args.out_dir / 'ta.json'} for {payload['symbols_count']} symbols.")


if __name__ == "__main__":
    main()
