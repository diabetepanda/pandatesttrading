"""Daily technical analysis calculation job producing `data/ta.json`."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import pickle
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
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
HISTORY_1D_FILENAME = "history_1d.json.gz"
MAX_HISTORY_1D_BARS = 2700
SHORT_LISTING_NOTE_KEYWORD = "上市或可用歷史較短"


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def _ym_label(date_str: str) -> str:
    """Format `YYYY-MM-DD` to `YYYY/MM`."""
    clean = str(date_str or "")[:7]
    return clean.replace("-", "/") if len(clean) == 7 else clean


def _span_label(start_date: str, end_date: str) -> str:
    """Format date span compactly as `YYYY/MM`, `YYYY/MM–MM`, or `YYYY/MM–YYYY/MM`."""
    s_ym = _ym_label(start_date)
    e_ym = _ym_label(end_date)
    if not s_ym or not e_ym:
        return s_ym or e_ym
    if s_ym == e_ym:
        return s_ym
    if s_ym[:4] == e_ym[:4]:
        return f"{s_ym}–{e_ym[5:7]}"
    return f"{s_ym}–{e_ym}"


def resample_1d_to_1w(bars: list[OHLCVBar]) -> tuple[list[OHLCVBar], list[int]]:
    """Aggregate chronological daily bars into ISO-week bars and record end daily indices."""
    if not bars:
        return [], []
    weekly: list[OHLCVBar] = []
    week_end_idxs: list[int] = []

    cur_key: tuple[int, int] | None = None
    bucket: list[tuple[int, OHLCVBar]] = []

    def _flush() -> None:
        if not bucket:
            return
        last_idx, last_bar = bucket[-1]
        weekly.append(
            OHLCVBar(
                ts_utc=last_bar.ts_utc,
                open=bucket[0][1].open,
                high=max(item[1].high for item in bucket),
                low=min(item[1].low for item in bucket),
                close=last_bar.close,
                volume=sum(max(0.0, item[1].volume) for item in bucket),
            )
        )
        week_end_idxs.append(last_idx)

    for idx, b in enumerate(bars):
        try:
            iso = date.fromisoformat(b.ts_utc[:10]).isocalendar()
            wk_key = (int(iso[0]), int(iso[1]))
        except Exception:
            wk_key = (idx // 5, 0)
        if cur_key is None or wk_key == cur_key:
            cur_key = wk_key
            bucket.append((idx, b))
        else:
            _flush()
            cur_key = wk_key
            bucket = [(idx, b)]
    _flush()
    return weekly, week_end_idxs


def find_swing_points(
    bars: list[OHLCVBar],
    left_right: int = 5,
) -> tuple[list[tuple[int, str, float]], list[tuple[int, str, float]]]:
    """Return confirmed swing highs and swing lows: list[(idx, date_str, price)]."""
    n = len(bars)
    k = max(1, left_right)
    highs_out: list[tuple[int, str, float]] = []
    lows_out: list[tuple[int, str, float]] = []
    if n < 2 * k + 1:
        return highs_out, lows_out

    hi_arr = [b.high for b in bars]
    lo_arr = [b.low for b in bars]
    for i in range(k, n - k):
        hi_i = hi_arr[i]
        lo_i = lo_arr[i]
        left_hi = max(hi_arr[i - k : i])
        right_hi = max(hi_arr[i + 1 : i + k + 1])
        if (hi_i >= left_hi and hi_i > right_hi) or (hi_i > left_hi and hi_i >= right_hi):
            highs_out.append((i, bars[i].ts_utc[:10], float(hi_i)))

        left_lo = min(lo_arr[i - k : i])
        right_lo = min(lo_arr[i + 1 : i + k + 1])
        if (lo_i <= left_lo and lo_i < right_lo) or (lo_i < left_lo and lo_i <= right_lo):
            lows_out.append((i, bars[i].ts_utc[:10], float(lo_i)))

    return highs_out, lows_out


def find_consolidation_zones(
    bars: list[OHLCVBar],
    atr20_series: list[float | None],
    fallback_atr: float | None,
    min_bars: int = 15,
    max_atr_amp: float = 2.0,
) -> list[dict[str, Any]]:
    """Detect tight multi-bar consolidation ranges (>= `min_bars`, close or range <= `max_atr_amp` * ATR)."""
    n = len(bars)
    if n < min_bars + 2:
        return []

    hi_arr = [b.high for b in bars]
    lo_arr = [b.low for b in bars]
    c_arr = [b.close for b in bars]

    # O(N) monotonic deque sliding window max/min + prefix sum for ATR20 mean
    dq_hi_max: deque[int] = deque()
    dq_lo_min: deque[int] = deque()
    dq_c_max: deque[int] = deque()
    dq_c_min: deque[int] = deque()
    atr_pref = [0.0] * (n + 1)
    atr_cnt = [0] * (n + 1)
    for k in range(n):
        v = atr20_series[k] if k < len(atr20_series) else None
        if v is not None and v > 0:
            atr_pref[k + 1] = atr_pref[k] + v
            atr_cnt[k + 1] = atr_cnt[k] + 1
        else:
            atr_pref[k + 1] = atr_pref[k]
            atr_cnt[k + 1] = atr_cnt[k]

    raw_boxes: list[dict[str, Any]] = []
    for i in range(n):
        win_start = i - min_bars + 1
        while dq_hi_max and dq_hi_max[0] < win_start:
            dq_hi_max.popleft()
        while dq_hi_max and hi_arr[dq_hi_max[-1]] <= hi_arr[i]:
            dq_hi_max.pop()
        dq_hi_max.append(i)

        while dq_lo_min and dq_lo_min[0] < win_start:
            dq_lo_min.popleft()
        while dq_lo_min and lo_arr[dq_lo_min[-1]] >= lo_arr[i]:
            dq_lo_min.pop()
        dq_lo_min.append(i)

        while dq_c_max and dq_c_max[0] < win_start:
            dq_c_max.popleft()
        while dq_c_max and c_arr[dq_c_max[-1]] <= c_arr[i]:
            dq_c_max.pop()
        dq_c_max.append(i)

        while dq_c_min and dq_c_min[0] < win_start:
            dq_c_min.popleft()
        while dq_c_min and c_arr[dq_c_min[-1]] >= c_arr[i]:
            dq_c_min.pop()
        dq_c_min.append(i)

        if win_start < 0:
            continue
        cnt = atr_cnt[i + 1] - atr_cnt[win_start]
        atr_ref = ((atr_pref[i + 1] - atr_pref[win_start]) / cnt) if cnt > 0 else fallback_atr
        if atr_ref is None or atr_ref <= 0:
            continue
        hi = hi_arr[dq_hi_max[0]]
        lo = lo_arr[dq_lo_min[0]]
        c_hi = c_arr[dq_c_max[0]]
        c_lo = c_arr[dq_c_min[0]]
        if (hi - lo) <= max_atr_amp * atr_ref or ((c_hi - c_lo) <= max_atr_amp * atr_ref and (hi - lo) <= (max_atr_amp + 0.6) * atr_ref):
            raw_boxes.append(
                {
                    "start_idx": win_start,
                    "end_idx": i,
                    "high": float(hi),
                    "low": float(lo),
                }
            )

    if not raw_boxes:
        return []

    merged: list[dict[str, Any]] = []
    for box in raw_boxes:
        if not merged:
            merged.append(dict(box))
            continue
        prev = merged[-1]
        if box["start_idx"] <= prev["end_idx"] + 2:
            prev["end_idx"] = max(prev["end_idx"], box["end_idx"])
            prev["high"] = max(prev["high"], box["high"])
            prev["low"] = min(prev["low"], box["low"])
        else:
            merged.append(dict(box))

    out: list[dict[str, Any]] = []
    for box in merged:
        s_i = box["start_idx"]
        e_i = box["end_idx"]
        span_len = e_i - s_i + 1
        if span_len < min_bars:
            continue
        s_dt = bars[s_i].ts_utc[:10]
        e_dt = bars[e_i].ts_utc[:10]
        atr_here = atr20_series[e_i] if (e_i < len(atr20_series) and atr20_series[e_i]) else fallback_atr
        if atr_here and (box["high"] - box["low"]) < 0.35 * atr_here:
            continue
        out.append(
            {
                "start_idx": s_i,
                "end_idx": e_i,
                "start_date": s_dt,
                "end_date": e_dt,
                "span_label": _span_label(s_dt, e_dt),
                "high": box["high"],
                "low": box["low"],
                "bars_len": span_len,
            }
        )
    return out


def find_volume_nodes(
    bars_1y: list[OHLCVBar],
    base_offset: int,
    atr20: float | None,
    cur_px: float,
    max_nodes: int = 6,
) -> list[dict[str, Any]]:
    """Build 1-year Volume Profile and extract top separated High-Volume Nodes."""
    if len(bars_1y) < 30:
        return []
    pos_vol_bars = [b for b in bars_1y if b.volume is not None and math.isfinite(b.volume) and b.volume > 0]
    if len(pos_vol_bars) < 30:
        return []
    total_vol = sum(b.volume for b in pos_vol_bars)
    if total_vol <= 0:
        return []

    p_min = min(b.low for b in bars_1y if b.low > 0)
    p_max = max(b.high for b in bars_1y)
    if p_max <= p_min or p_min <= 0:
        return []

    if atr20 is not None and atr20 > 0:
        nbins = max(24, min(60, round((p_max - p_min) / max(atr20 * 0.5, (p_max - p_min) / 48.0))))
    else:
        nbins = 36
    bw = (p_max - p_min) / nbins
    if bw <= 0:
        return []

    bin_vols = [0.0] * nbins
    bin_last_idx = [base_offset] * nbins

    for rel_i, b in enumerate(bars_1y):
        v = b.volume if (b.volume is not None and math.isfinite(b.volume) and b.volume > 0) else 0.0
        if v <= 0:
            continue
        lo_i = max(0, min(nbins - 1, int((b.low - p_min) / bw)))
        hi_i = max(0, min(nbins - 1, int((b.high - p_min) / bw)))
        if hi_i < lo_i:
            lo_i = hi_i
        spread = hi_i - lo_i + 1
        share = v / spread
        abs_i = base_offset + rel_i
        for bi in range(lo_i, hi_i + 1):
            bin_vols[bi] += share
            if abs_i > bin_last_idx[bi]:
                bin_last_idx[bi] = abs_i

    mean_v = sum(bin_vols) / nbins
    if mean_v <= 0:
        return []

    peaks: list[tuple[int, float]] = []
    for i in range(nbins):
        v_i = bin_vols[i]
        left_ok = i == 0 or v_i >= bin_vols[i - 1]
        right_ok = i == nbins - 1 or v_i >= bin_vols[i + 1]
        if left_ok and right_ok and v_i >= 1.25 * mean_v:
            peaks.append((i, v_i))

    if not peaks:
        best_i = max(range(nbins), key=lambda idx: bin_vols[idx])
        if bin_vols[best_i] > 0:
            peaks.append((best_i, bin_vols[best_i]))

    peaks.sort(key=lambda x: x[1], reverse=True)
    min_sep = (0.8 * atr20) if (atr20 is not None and atr20 > 0) else (cur_px * 0.015)

    chosen: list[dict[str, Any]] = []
    for bi, bv in peaks:
        mid_px = p_min + (bi + 0.5) * bw
        if any(abs(mid_px - c["price"]) < min_sep for c in chosen):
            continue
        chosen.append(
            {
                "price": float(mid_px),
                "vol_ratio": float(bv / mean_v),
                "ref_idx": int(bin_last_idx[bi]),
                "span_label": _span_label(bars_1y[0].ts_utc[:10], bars_1y[-1].ts_utc[:10]),
            }
        )
        if len(chosen) >= max_nodes:
            break
    return chosen


def _evaluate_zone_touches(
    bars: list[OHLCVBar],
    zone_px: float,
    atr20: float | None,
    avg_vol_60: float,
    market: str,
    is_resistance_side: bool,
) -> dict[str, Any]:
    """Measure touch count, recency-weighted touches, reversal amplitude, recency, and touch volume."""
    n = len(bars)
    has_atr = atr20 is not None and math.isfinite(atr20) and atr20 > 0
    tol = (0.5 * atr20) if has_atr else (zone_px * 0.008)
    atr_norm = atr20 if has_atr else max(zone_px * 0.01, 1e-8)
    yr_bars = 365.0 if market == "crypto" else 252.0

    touch_indices: list[int] = []
    last_t = -999
    for idx, b in enumerate(bars):
        if b.low <= zone_px + tol and b.high >= zone_px - tol:
            if idx - last_t >= 5:
                touch_indices.append(idx)
                last_t = idx

    max_rev_atr = 0.0
    weighted_touches = 0.0
    weighted_rev_atr = 0.0
    max_vol_ratio = 0.0
    for t_idx in touch_indices:
        age_decay = math.exp(-max(0, (n - 1) - t_idx) / (1.5 * yr_bars))
        weighted_touches += age_decay
        end_j = min(n, t_idx + 21)
        if end_j > t_idx + 1:
            if is_resistance_side:
                fav = max(0.0, zone_px - min(bars[j].low for j in range(t_idx + 1, end_j)))
            else:
                fav = max(0.0, max(bars[j].high for j in range(t_idx + 1, end_j)) - zone_px)
            rev_atr = fav / atr_norm
            if rev_atr > max_rev_atr:
                max_rev_atr = rev_atr
            w_rev = rev_atr * age_decay
            if w_rev > weighted_rev_atr:
                weighted_rev_atr = w_rev
        if avg_vol_60 > 0 and bars[t_idx].volume is not None and math.isfinite(bars[t_idx].volume):
            vr = bars[t_idx].volume / avg_vol_60
            if vr > max_vol_ratio:
                max_vol_ratio = vr

    latest_touch = touch_indices[-1] if touch_indices else 0
    days_ago = max(0, (n - 1) - latest_touch)
    recency = math.exp(-days_ago / yr_bars)

    return {
        "touches": len(touch_indices),
        "weighted_touches": weighted_touches,
        "max_rev_atr": round(max_rev_atr, 2),
        "weighted_rev_atr": weighted_rev_atr,
        "latest_touch_idx": latest_touch,
        "recency": recency,
        "max_vol_ratio": max_vol_ratio,
    }


def _build_fibo_extension_fallbacks(
    bars: list[OHLCVBar],
    swing_highs: list[tuple[int, str, float]],
    swing_lows: list[tuple[int, str, float]],
    cur_px: float,
    near_excl: float,
    side: str,
) -> list[dict[str, Any]]:
    """Compute Fibonacci 1.272 and 1.618 extension fallback levels when historical side is empty."""
    if len(bars) < 10:
        return []
    win_len = min(len(bars), 252)
    win = bars[-win_len:]
    win_hi = max(b.high for b in win)
    win_lo = min(b.low for b in win)
    hi_date = max((b for b in win if b.high == win_hi), key=lambda x: x.ts_utc).ts_utc[:10]
    lo_date = min((b for b in win if b.low == win_lo), key=lambda x: x.ts_utc).ts_utc[:10]

    # Prefer major confirmed swing wave inside recent 252 bars if wide enough (>= 35% of window range)
    cutoff = len(bars) - win_len
    r_sh = [s for s in swing_highs if s[0] >= cutoff]
    r_sl = [s for s in swing_lows if s[0] >= cutoff]
    if r_sh and r_sl:
        hi_cand = max(r_sh, key=lambda x: x[2])
        lo_cand = min(r_sl, key=lambda x: x[2])
        if (hi_cand[2] - lo_cand[2]) >= 0.35 * max(1e-12, win_hi - win_lo):
            win_hi, hi_date = hi_cand[2], hi_cand[1]
            win_lo, lo_date = lo_cand[2], lo_cand[1]

    diff = win_hi - win_lo
    if diff <= 1e-12:
        return []

    items: list[dict[str, Any]] = []
    for ratio in (1.272, 1.618):
        if side == "resistance":
            px = win_lo + ratio * diff
            if px <= cur_px + near_excl:
                continue
            wave_desc = f"波段 {lo_date}→{hi_date}"
        else:
            px = win_hi - ratio * diff
            if px <= 0 or px >= cur_px - near_excl:
                continue
            wave_desc = f"波段 {hi_date}→{lo_date}"
        items.append(
            {
                "price": float(px),
                "label": f"Fibo {ratio:.3f}（延伸參考）",
                "algo": f"Fibonacci {ratio:.3f} 延伸參考（{wave_desc}；無歷史點位時之補充參考，最多 1 星）",
                "strength": 1,
                "sources_count": 1,
                "dist_pct": round((px - cur_px) / cur_px * 100.0, 2),
                "far": True,
                "is_extension": True,
            }
        )
    return items


def _select_sr_zones_tiered(
    raw_candidates: list[dict[str, Any]],
    bars: list[OHLCVBar],
    cur_px: float,
    atr20: float | None,
    market: str,
    swing_highs: list[tuple[int, str, float]],
    swing_lows: list[tuple[int, str, float]],
    max_per_side: int = 3,
    is_short_listing: bool = False,
) -> dict[str, Any]:
    """Filter >=0.3*ATR, merge <0.5*ATR, score 1-3 stars, search 4*ATR -> 8*ATR -> all, fallback Fibo ext."""
    if cur_px <= 0:
        return {
            "resistances": [],
            "supports": [],
            "resistance_status": "無明確壓力",
            "support_status": "無明確支撐",
            "resistance_note": "現價無效",
            "support_note": "現價無效",
        }

    n = len(bars)
    has_atr = atr20 is not None and math.isfinite(atr20) and atr20 > 0
    near_excl = 0.3 * atr20 if has_atr else cur_px * 1e-4
    merge_gap = 0.5 * atr20 if has_atr else cur_px * 5e-4
    recent_vols = [b.volume for b in bars[-60:] if b.volume is not None and math.isfinite(b.volume) and b.volume > 0]
    avg_vol_60 = (sum(recent_vols) / len(recent_vols)) if recent_vols else 0.0
    yr_bars = 365.0 if market == "crypto" else 252.0

    # Score individual candidates first so cluster lead selection picks strongest member
    valid_res: list[dict[str, Any]] = []
    valid_sup: list[dict[str, Any]] = []
    for c in raw_candidates:
        px = c.get("price")
        if px is None or not math.isfinite(px) or px <= 0:
            continue
        if abs(px - cur_px) <= near_excl:
            continue
        ref_idx = int(c.get("ref_idx", n - 1))
        days_ago = max(0, (n - 1) - ref_idx)
        rec = math.exp(-days_ago / yr_bars)
        m_score = float(c.get("base_weight", 1.0)) + 1.5 * rec + min(1.0, float(c.get("vol_ratio", 0.0)) * 0.4)
        cand = dict(c, member_score=m_score)
        if px > cur_px:
            valid_res.append(cand)
        else:
            valid_sup.append(cand)

    def _cluster_and_score(items: list[dict[str, Any]], ascending: bool) -> list[dict[str, Any]]:
        if not items:
            return []
        ordered = sorted(items, key=lambda x: x["price"], reverse=not ascending)
        clusters: list[list[dict[str, Any]]] = []
        for it in ordered:
            if not clusters:
                clusters.append([it])
                continue
            anchor_px = clusters[-1][0]["price"]
            if abs(it["price"] - anchor_px) < merge_gap:
                clusters[-1].append(it)
            else:
                clusters.append([it])

        zones: list[dict[str, Any]] = []
        is_res_side = ascending
        for cl in clusters:
            # Keep strongest candidate per distinct dedup_key so same level isn't double-counted
            by_key: dict[str, dict[str, Any]] = {}
            for m in cl:
                dk = str(m.get("dedup_key") or m["label"])
                if dk not in by_key or m["member_score"] > by_key[dk]["member_score"]:
                    by_key[dk] = m
            uniq_members = sorted(by_key.values(), key=lambda x: x["member_score"], reverse=True)
            avg_px = sum(m["price"] for m in uniq_members) / len(uniq_members)

            # Safeguard against cluster mean slipping inside 0.3*ATR or crossing current price
            if is_res_side and avg_px <= cur_px + near_excl:
                avg_px = max(m["price"] for m in uniq_members)
            elif (not is_res_side) and avg_px >= cur_px - near_excl:
                avg_px = min(m["price"] for m in uniq_members)

            stats = _evaluate_zone_touches(
                bars=bars,
                zone_px=avg_px,
                atr20=atr20,
                avg_vol_60=avg_vol_60,
                market=market,
                is_resistance_side=is_res_side,
            )
            max_mem_ref = max(int(m.get("ref_idx", 0)) for m in uniq_members)
            best_days_ago = max(0, (n - 1) - max(max_mem_ref, stats["latest_touch_idx"]))
            best_recency = math.exp(-best_days_ago / yr_bars)

            touch_pts = min(2.5, stats["weighted_touches"] * 0.65)
            rev_pts = min(2.0, stats["weighted_rev_atr"] * 0.30)
            rec_pts = 1.5 * best_recency
            src_cnt = len(uniq_members)
            conf_pts = min(2.5, max(0, src_cnt - 1) * 0.9)
            has_hvn = any(m.get("kind") == "volume_node" for m in uniq_members)
            hvn_max = max((float(m.get("vol_ratio", 0.0)) for m in uniq_members), default=0.0)
            vol_ratio_peak = max(hvn_max, stats["max_vol_ratio"])
            vol_pts = min(1.5, 0.6 + min(0.9, max(0.0, (hvn_max - 1.0) * 0.45))) if has_hvn else min(0.8, max(0.0, (vol_ratio_peak - 1.1) * 0.4))

            total_score = touch_pts + rev_pts + rec_pts + conf_pts + vol_pts
            strength = 3 if total_score >= 7.8 else (2 if total_score >= 5.6 else 1)

            lead = uniq_members[0]
            base_lbl = lead["label"]
            if src_cnt > 1:
                base_lbl = f"{base_lbl} ＋{src_cnt - 1} 項"

            dist_atr = (abs(avg_px - cur_px) / atr20) if has_atr else 0.0
            is_far = has_atr and dist_atr > 4.0
            display_lbl = f"{base_lbl}（較遠）" if is_far else base_lbl

            algo_parts: list[str] = []
            seen_algos: set[str] = set()
            for m in uniq_members:
                if m["algo"] not in seen_algos:
                    seen_algos.add(m["algo"])
                    algo_parts.append(m["algo"])
            stat_note = f"測試 {stats['touches']} 次／最大反轉 {stats['max_rev_atr']}×ATR"
            if is_far:
                stat_note += f"／較遠（{dist_atr:.1f}×ATR）"

            zones.append(
                {
                    "price": float(avg_px),
                    "label": display_lbl,
                    "algo": "；".join(algo_parts) + f"（{stat_note}）",
                    "strength": strength,
                    "sources_count": src_cnt,
                    "dist_pct": round((avg_px - cur_px) / cur_px * 100.0, 2),
                    "dist_atr": round(dist_atr, 2),
                    "far": bool(is_far),
                    "score": round(total_score, 2),
                    "touches": stats["touches"],
                    "is_extension": False,
                }
            )
        zones.sort(key=lambda z: abs(z["price"] - cur_px))
        return zones

    all_res_zones = _cluster_and_score(valid_res, ascending=True)
    all_sup_zones = _cluster_and_score(valid_sup, ascending=False)

    def _pick_tiered(zones: list[dict[str, Any]], ascending: bool) -> list[dict[str, Any]]:
        if not zones:
            return []
        if not has_atr:
            chosen = zones[:max_per_side]
        else:
            t1 = [z for z in zones if z["dist_atr"] <= 4.0]
            t2 = [z for z in zones if 4.0 < z["dist_atr"] <= 8.0]
            t3 = [z for z in zones if z["dist_atr"] > 8.0]
            chosen = list(t1[:max_per_side])
            if len(chosen) < max_per_side:
                chosen.extend(t2[: max_per_side - len(chosen)])
            if len(chosen) < max_per_side:
                chosen.extend(t3[: max_per_side - len(chosen)])
        chosen.sort(key=lambda z: z["price"], reverse=not ascending)
        return chosen

    res_out = _pick_tiered(all_res_zones, ascending=True)
    sup_out = _pick_tiered(all_sup_zones, ascending=False)

    res_status = "ok"
    res_note = ""
    if not res_out:
        res_status = "無明確壓力"
        prior_hi = max(b.high for b in bars[:-10]) if n > 15 else max(b.high for b in bars)
        if is_short_listing:
            res_note = f"{SHORT_LISTING_NOTE_KEYWORD}（未滿 2 年），上方尚無歷史價位"
        elif n < 500:
            res_note = f"日線資料更新中（目前僅可用 {n} 根），上方尚無完整歷史壓力"
        elif cur_px >= max(b.high for b in bars) - near_excl:
            res_note = "現價創歷史新高或逼近歷史最高點，上方無歷史成交價位"
        elif cur_px >= prior_hi - near_excl:
            recent_ath = max(b.high for b in bars)
            res_note = f"近 10 日剛突破歷史前高（最高曾觸及 {recent_ath:,.2f}），上方尚無已確認歷史壓力"
        else:
            res_note = "可用歷史區間上方無有效支壓價位"
        res_out = _build_fibo_extension_fallbacks(
            bars=bars,
            swing_highs=swing_highs,
            swing_lows=swing_lows,
            cur_px=cur_px,
            near_excl=near_excl,
            side="resistance",
        )

    sup_status = "ok"
    sup_note = ""
    if not sup_out:
        sup_status = "無明確支撐"
        prior_lo = min(b.low for b in bars[:-10]) if n > 15 else min(b.low for b in bars)
        post_day0_lo = min(b.low for b in bars[1:-10]) if n > 20 else prior_lo
        if is_short_listing:
            sup_note = f"{SHORT_LISTING_NOTE_KEYWORD}（未滿 2 年），下方尚無歷史價位"
        elif n < 500:
            sup_note = f"日線資料更新中（目前僅可用 {n} 根），下方尚無完整歷史支撐"
        elif cur_px <= min(b.low for b in bars) + near_excl:
            sup_note = "現價創歷史新低或逼近歷史最低點，下方無歷史成交價位"
        elif cur_px <= prior_lo + near_excl:
            recent_atl = min(b.low for b in bars)
            sup_note = f"近 10 日剛跌破歷史前低（最低曾觸及 {recent_atl:,.4g}），下方尚無已確認歷史支撐"
        elif cur_px <= post_day0_lo + near_excl:
            sup_note = f"現價已跌破上市首日以外之所有歷史低點（最低僅上市首日 {bars[0].low:,.4g}），下方無已成交支撐區"
        else:
            sup_note = "可用歷史區間下方無有效支壓價位"
        sup_out = _build_fibo_extension_fallbacks(
            bars=bars,
            swing_highs=swing_highs,
            swing_lows=swing_lows,
            cur_px=cur_px,
            near_excl=near_excl,
            side="support",
        )

    return {
        "resistances": res_out,
        "supports": sup_out,
        "resistance_status": res_status,
        "support_status": sup_status,
        "resistance_note": res_note,
        "support_note": sup_note,
    }


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

    cutoff = max(0, len(bars) - 180)
    recent_sh = [s for s in swing_highs if s[0] >= cutoff]
    recent_sl = [s for s in swing_lows if s[0] >= cutoff]
    sh = recent_sh[-1] if recent_sh else (swing_highs[-1] if swing_highs else None)
    sl = recent_sl[-1] if recent_sl else (swing_lows[-1] if swing_lows else None)

    cur_px = bars[-1].close
    buf = atr20 if (atr20 is not None and atr20 > 0) else 0.0

    use_major = False
    if sh is None or sl is None or sh[2] <= sl[2]:
        use_major = True
    elif major_range > 0 and (sh[2] - sl[2]) < 0.40 * major_range:
        use_major = True
    elif cur_px < sl[2] - buf or cur_px > sh[2] + buf:
        use_major = True

    if use_major:
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
    weekly_bars: list[OHLCVBar] | None = None,
) -> dict[str, Any]:
    """Evaluate daily SMA20/50/100/200, weekly SMA20/50, and ADX14 into trend state."""
    closes = [b.close for b in bars]
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    sma100 = _sma(closes, 100)
    sma200 = _sma(closes, 200)

    w_bars = weekly_bars if weekly_bars is not None else resample_1d_to_1w(bars)[0]
    w_closes = [b.close for b in w_bars]
    w_sma20 = _sma(w_closes, 20)
    w_sma50 = _sma(w_closes, 50)

    adx14 = compute_adx(bars[-250:], period=14, market="crypto", interval="1d")

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
        "sma100": round(sma100, 6) if sma100 is not None else None,
        "sma200": round(sma200, 6) if sma200 is not None else None,
        "weekly_sma20": round(w_sma20, 6) if w_sma20 is not None else None,
        "weekly_sma50": round(w_sma50, 6) if w_sma50 is not None else None,
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
    """Causal historical test of 5-day hold rates within 1*ATR using the redesigned S/R sources."""
    n = len(bars)
    if n < 40:
        return {
            "years": round(n / 252.0, 1) if market != "crypto" else round(n / 365.0, 1),
            "support_hold_rate_pct": None,
            "support_held": 0,
            "support_samples": 0,
            "support_insufficient": True,
            "support_label": "樣本不足（0 次）",
            "resistance_hold_rate_pct": None,
            "resistance_held": 0,
            "resistance_samples": 0,
            "resistance_insufficient": True,
            "resistance_label": "樣本不足（0 次）",
        }

    atr_raw = compute_atr_series(bars, period=20, market="crypto", interval="1d")
    atr20: list[float | None] = [None] * n
    for idx_a, v in enumerate(atr_raw):
        if 20 + idx_a < n:
            atr20[20 + idx_a] = v

    # Confirmed causal swings (k=5 and k=10), consolidation boxes, and prefix-sum closes for causal SMAs
    sh5, sl5 = find_swing_points(bars, left_right=left_right)
    sh10, sl10 = find_swing_points(bars, left_right=10)
    boxes = find_consolidation_zones(bars, atr20, fallback_atr=None, min_bars=15, max_atr_amp=2.0)

    levels_by_confirm: dict[int, list[float]] = {}
    for p, _, px in sh5 + sl5:
        levels_by_confirm.setdefault(p + left_right, []).append(px)
    for p, _, px in sh10 + sl10:
        levels_by_confirm.setdefault(p + 10, []).append(px)
    for bx in boxes:
        levels_by_confirm.setdefault(bx["end_idx"], []).extend([bx["high"], bx["low"]])

    closes = [b.close for b in bars]
    pref_sum = [0.0] * (n + 1)
    for i, c in enumerate(closes):
        pref_sum[i + 1] = pref_sum[i] + c

    structural_pool: list[float] = []
    last_sup_touch = -999
    last_res_touch = -999
    sup_samples = 0
    sup_held = 0
    res_samples = 0
    res_held = 0

    for t in range(25, n - hold_days):
        if t in levels_by_confirm:
            structural_pool.extend(levels_by_confirm[t])
            if len(structural_pool) > 24:
                structural_pool = structural_pool[-24:]

        av = atr20[t]
        if av is None or av <= 0:
            continue

        prev_c = bars[t - 1].close
        cur_lo = bars[t].low
        cur_hi = bars[t].high
        near_excl = 0.3 * av

        # Combine historical structural levels (including role-reversal naturally) + causal long SMAs
        cand_levels = list(structural_pool)
        for period in (50, 100, 200):
            if t >= period:
                cand_levels.append((pref_sum[t] - pref_sum[t - period]) / period)

        if cand_levels and (t - last_sup_touch) >= hold_days:
            below_sups = [lv for lv in cand_levels if prev_c - lv > near_excl]
            if below_sups:
                nearest_s = max(below_sups)
                if cur_lo <= nearest_s + 0.25 * av:
                    sup_samples += 1
                    last_sup_touch = t
                    if all(bars[j].close >= (nearest_s - 1.0 * av) for j in range(t, t + hold_days)):
                        sup_held += 1

        if cand_levels and (t - last_res_touch) >= hold_days:
            above_ress = [lv for lv in cand_levels if lv - prev_c > near_excl]
            if above_ress:
                nearest_r = min(above_ress)
                if cur_hi >= nearest_r - 0.25 * av:
                    res_samples += 1
                    last_res_touch = t
                    if all(bars[j].close <= (nearest_r + 1.0 * av) for j in range(t, t + hold_days)):
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
    is_short_listing: bool = False,
    recompute_backtest: bool = True,
) -> dict[str, Any] | None:
    """Compute complete technical analysis snapshot for a single symbol."""
    eff_bars = sorted({b.ts_utc[:10]: b for b in (long_bars_1d or bars_1d)}.values(), key=lambda x: x.ts_utc)
    if bars_1d:
        merged = {b.ts_utc[:10]: b for b in eff_bars}
        for b in bars_1d:
            merged[b.ts_utc[:10]] = b
        eff_bars = sorted(merged.values(), key=lambda x: x.ts_utc)

    n = len(eff_bars)
    if n < 25:
        return None

    cur_px = float(latest_price) if (latest_price is not None and latest_price > 0) else float(eff_bars[-1].close)
    latest_date = eff_bars[-1].ts_utc[:10]

    atr20 = compute_atr(eff_bars, period=20, market="crypto", interval="1d")
    atr_raw = compute_atr_series(eff_bars, period=20, market="crypto", interval="1d")
    atr20_series: list[float | None] = [None] * n
    for idx_a, v in enumerate(atr_raw):
        if 20 + idx_a < n:
            atr20_series[20 + idx_a] = v

    weekly_bars, week_end_idxs = resample_1d_to_1w(eff_bars)
    swing5_h, swing5_l = find_swing_points(eff_bars, left_right=5)
    swing10_h, swing10_l = find_swing_points(eff_bars, left_right=10)
    w_swing_h, w_swing_l = find_swing_points(weekly_bars, left_right=3)

    fibo = compute_fibonacci_retracement(eff_bars, swing5_h, swing5_l, atr20=atr20)
    trend = classify_trend(eff_bars, cur_px, market=market, weekly_bars=weekly_bars)

    # Collect genuine structural and long-term MA S/R candidates (no round numbers!)
    raw_candidates: list[dict[str, Any]] = []
    lb_3y = 1095 if market == "crypto" else 756
    cutoff_3y = max(0, n - lb_3y)
    lb_1y = 365 if market == "crypto" else 252
    cutoff_1y = max(0, n - lb_1y)
    has_atr = atr20 is not None and math.isfinite(atr20) and atr20 > 0
    atr_dedup_tol = 0.25 * atr20 if has_atr else cur_px * 0.005

    # 1. Long-term swing highs/lows (k=10 over full history + k=5 in recent 3y + weekly k=3)
    recorded_sh: list[tuple[int, float]] = []
    for idx, dt_s, px in swing10_h:
        ym = _ym_label(dt_s)
        flipped = px < cur_px
        lbl = f"{ym} 波段高（前壓力）" if flipped else f"{ym} 波段高"
        algo = f"{dt_s} 長週期波段高點（10日確認{'；突破轉支撐' if flipped else ''}）"
        raw_candidates.append(
            {
                "price": px,
                "label": lbl,
                "algo": algo,
                "kind": "swing_high_role_flip" if flipped else "swing_high",
                "dedup_key": f"sh:{ym}",
                "ref_idx": idx,
                "base_weight": 1.35,
            }
        )
        recorded_sh.append((idx, px))

    for idx, dt_s, px in swing5_h:
        if idx < cutoff_3y and len(eff_bars) > lb_3y:
            continue
        if any(abs(idx - r_i) <= 4 and abs(px - r_p) <= atr_dedup_tol for r_i, r_p in recorded_sh):
            continue
        ym = _ym_label(dt_s)
        flipped = px < cur_px
        lbl = f"{ym} 波段高（前壓力）" if flipped else f"{ym} 波段高"
        algo = f"{dt_s} 波段高點（5日確認{'；突破轉支撐' if flipped else ''}）"
        raw_candidates.append(
            {
                "price": px,
                "label": lbl,
                "algo": algo,
                "kind": "swing_high_role_flip" if flipped else "swing_high",
                "dedup_key": f"sh:{ym}",
                "ref_idx": idx,
                "base_weight": 1.15,
            }
        )
        recorded_sh.append((idx, px))

    for w_idx, dt_s, px in w_swing_h:
        d_idx = week_end_idxs[w_idx] if w_idx < len(week_end_idxs) else n - 1
        if any(abs(d_idx - r_i) <= 14 and abs(px - r_p) <= 0.35 * (atr20 or cur_px * 0.02) for r_i, r_p in recorded_sh):
            continue
        ym = _ym_label(dt_s)
        flipped = px < cur_px
        lbl = f"{ym} 週線波段高（前壓力）" if flipped else f"{ym} 週線波段高"
        algo = f"{dt_s} 週線波段高點（3週確認{'；突破轉支撐' if flipped else ''}）"
        raw_candidates.append(
            {
                "price": px,
                "label": lbl,
                "algo": algo,
                "kind": "weekly_swing_high_role_flip" if flipped else "weekly_swing_high",
                "dedup_key": f"wsh:{ym}",
                "ref_idx": d_idx,
                "base_weight": 1.25,
            }
        )

    recorded_sl: list[tuple[int, float]] = []
    for idx, dt_s, px in swing10_l:
        ym = _ym_label(dt_s)
        flipped = px > cur_px
        lbl = f"{ym} 波段低（前支撐）" if flipped else f"{ym} 波段低"
        algo = f"{dt_s} 長週期波段低點（10日確認{'；跌破轉壓力' if flipped else ''}）"
        raw_candidates.append(
            {
                "price": px,
                "label": lbl,
                "algo": algo,
                "kind": "swing_low_role_flip" if flipped else "swing_low",
                "dedup_key": f"sl:{ym}",
                "ref_idx": idx,
                "base_weight": 1.35,
            }
        )
        recorded_sl.append((idx, px))

    for idx, dt_s, px in swing5_l:
        if idx < cutoff_3y and len(eff_bars) > lb_3y:
            continue
        if any(abs(idx - r_i) <= 4 and abs(px - r_p) <= atr_dedup_tol for r_i, r_p in recorded_sl):
            continue
        ym = _ym_label(dt_s)
        flipped = px > cur_px
        lbl = f"{ym} 波段低（前支撐）" if flipped else f"{ym} 波段低"
        algo = f"{dt_s} 波段低點（5日確認{'；跌破轉壓力' if flipped else ''}）"
        raw_candidates.append(
            {
                "price": px,
                "label": lbl,
                "algo": algo,
                "kind": "swing_low_role_flip" if flipped else "swing_low",
                "dedup_key": f"sl:{ym}",
                "ref_idx": idx,
                "base_weight": 1.15,
            }
        )
        recorded_sl.append((idx, px))

    for w_idx, dt_s, px in w_swing_l:
        d_idx = week_end_idxs[w_idx] if w_idx < len(week_end_idxs) else n - 1
        if any(abs(d_idx - r_i) <= 14 and abs(px - r_p) <= 0.35 * (atr20 or cur_px * 0.02) for r_i, r_p in recorded_sl):
            continue
        ym = _ym_label(dt_s)
        flipped = px > cur_px
        lbl = f"{ym} 週線波段低（前支撐）" if flipped else f"{ym} 週線波段低"
        algo = f"{dt_s} 週線波段低點（3週確認{'；跌破轉壓力' if flipped else ''}）"
        raw_candidates.append(
            {
                "price": px,
                "label": lbl,
                "algo": algo,
                "kind": "weekly_swing_low_role_flip" if flipped else "weekly_swing_low",
                "dedup_key": f"wsl:{ym}",
                "ref_idx": d_idx,
                "base_weight": 1.25,
            }
        )

    # 2. Prior Consolidation Zones (15+ daily bars, tight range)
    boxes = find_consolidation_zones(eff_bars, atr20_series, fallback_atr=atr20, min_bars=15, max_atr_amp=2.0)
    for bx in boxes:
        span = bx["span_label"]
        top_px = bx["high"]
        bot_px = bx["low"]
        top_flipped = top_px < cur_px
        bot_flipped = bot_px > cur_px
        raw_candidates.append(
            {
                "price": top_px,
                "label": f"盤整區上緣（前壓力，{span}）" if top_flipped else f"盤整區上緣（{span}）",
                "algo": f"前期盤整區間上緣（{bx['start_date']}~{bx['end_date']}，共 {bx['bars_len']} 根{'；突破轉支撐' if top_flipped else ''}）",
                "kind": "consolidation_role_flip" if top_flipped else "consolidation_high",
                "dedup_key": f"box_hi:{span}",
                "ref_idx": bx["end_idx"],
                "base_weight": 1.25,
            }
        )
        raw_candidates.append(
            {
                "price": bot_px,
                "label": f"盤整區下緣（前支撐，{span}）" if bot_flipped else f"盤整區下緣（{span}）",
                "algo": f"前期盤整區間下緣（{bx['start_date']}~{bx['end_date']}，共 {bx['bars_len']} 根{'；跌破轉壓力' if bot_flipped else ''}）",
                "kind": "consolidation_role_flip" if bot_flipped else "consolidation_low",
                "dedup_key": f"box_lo:{span}",
                "ref_idx": bx["end_idx"],
                "base_weight": 1.25,
            }
        )

    # 3. 1Y Volume Profile Dense Levels (skipped if volume data absent)
    vol_nodes = find_volume_nodes(
        eff_bars[cutoff_1y:],
        base_offset=cutoff_1y,
        atr20=atr20,
        cur_px=cur_px,
        max_nodes=6,
    )
    for idx_vn, vn in enumerate(vol_nodes):
        raw_candidates.append(
            {
                "price": vn["price"],
                "label": "高量價位",
                "algo": f"近 1 年成交量密集價位（{vn['span_label']}，相對均量 {vn['vol_ratio']:.1f}x）",
                "kind": "volume_node",
                "dedup_key": f"vol:{idx_vn}",
                "ref_idx": vn["ref_idx"],
                "vol_ratio": vn["vol_ratio"],
                "base_weight": 1.2,
            }
        )

    # 4. Long-Term Moving Averages (Daily SMA50/100/200 + Weekly SMA20/50)
    for ma_key, ma_lbl, ma_desc in (
        ("sma50", "SMA50", "日線 50 日均線 SMA50"),
        ("sma100", "SMA100", "日線 100 日均線 SMA100"),
        ("sma200", "SMA200", "日線 200 日均線 SMA200"),
        ("weekly_sma20", "週線 SMA20", "週線 20 週均線（約百日均線趨勢帶）"),
        ("weekly_sma50", "週線 SMA50", "週線 50 週均線（長週期牛熊分界線）"),
    ):
        ma_val = trend.get(ma_key)
        if ma_val is not None:
            raw_candidates.append(
                {
                    "price": float(ma_val),
                    "label": ma_lbl,
                    "algo": ma_desc,
                    "kind": ma_key,
                    "dedup_key": ma_key,
                    "ref_idx": n - 1,
                    "base_weight": 1.4 if ma_key in ("sma200", "weekly_sma50") else 1.25,
                }
            )

    sr_result = _select_sr_zones_tiered(
        raw_candidates=raw_candidates,
        bars=eff_bars,
        cur_px=cur_px,
        atr20=atr20,
        market=market,
        swing_highs=swing5_h,
        swing_lows=swing5_l,
        max_per_side=3,
        is_short_listing=is_short_listing,
    )
    resistances = sr_result["resistances"]
    supports = sr_result["supports"]

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

    # 5. Compression (Bollinger BandWidth 20,2 percentile over 1y)
    bb_pct = compute_bollinger_bandwidth_percentile(
        eff_bars[-(lb_1y + 25) :], period=20, num_std=2.0, lookback=min(lb_1y, max(30, n - 20))
    )
    is_squeezing = bb_pct is not None and bb_pct < 20.0
    squeeze_label = "收斂中，可能變盤" if is_squeezing else ("波動擴張中" if (bb_pct is not None and bb_pct >= 80.0) else "波動正常")

    # 6. Pattern Recognition
    pattern = detect_chart_pattern(swing5_h, swing5_l, atr20, cur_px)

    # 7. Historical Causal Validation (reuse same-day backtest on intraday 15m scans when passed)
    if not recompute_backtest and existing_backtest and (
        (existing_backtest.get("support_samples") or 0) > 0
        or (existing_backtest.get("resistance_samples") or 0) > 0
    ):
        backtest = existing_backtest
    elif n < 500 and existing_backtest and (
        (existing_backtest.get("support_samples") or 0) > 0
        or (existing_backtest.get("resistance_samples") or 0) > 0
        or (existing_backtest.get("years") or 0) >= 2.0
    ):
        backtest = existing_backtest
    else:
        backtest = backtest_sr_hold_rates(eff_bars, market=market, left_right=5, hold_days=5)

    sq_short = "正在收斂" if is_squeezing else squeeze_label
    summary_parts = [trend["state"], sq_short]
    if sr_result["resistance_status"] == "無明確壓力":
        summary_parts.append("無明確壓力（延伸參考）")
    elif resistances:
        near_res = resistances[0]["price"]
        summary_parts.append(f"上方壓力 {near_res:,.4g}" if near_res < 100 else f"上方壓力 {near_res:,.2f}")
    if sr_result["support_status"] == "無明確支撐":
        summary_parts.append("無明確支撐（延伸參考）")
    elif supports:
        near_sup = supports[0]["price"]
        summary_parts.append(f"下方支撐 {near_sup:,.4g}" if near_sup < 100 else f"下方支撐 {near_sup:,.2f}")

    return {
        "symbol": symbol,
        "display_name": display_name,
        "market": market,
        "latest_price": cur_px,
        "latest_bar_date": latest_date,
        "bars_count": n,
        "summary": "，".join(summary_parts),
        "resistances": resistances,
        "supports": supports,
        "resistance_status": sr_result["resistance_status"],
        "support_status": sr_result["support_status"],
        "resistance_note": sr_result["resistance_note"],
        "support_note": sr_result["support_note"],
        "fibonacci": fibo,
        "volatility": vol_info,
        "trend": trend,
        "squeeze": {
            "bb_width_pct": bb_pct,
            "lookback_days": lb_1y,
            "is_squeezing": is_squeezing,
            "label": squeeze_label,
        },
        "pattern": pattern,
        "backtest": backtest,
    }


def _compact_num(val: float) -> float:
    if not math.isfinite(val):
        return 0.0
    abs_v = abs(val)
    if abs_v >= 100:
        return round(val, 4)
    if abs_v >= 1:
        return round(val, 5)
    return round(val, 8)


def _is_genuine_short_listing(bars: list[OHLCVBar], now: datetime) -> bool:
    """Return True only when full-history fetch shows asset has < 500 bars and listed within ~2.2 years."""
    if not (25 <= len(bars) < 500):
        return False
    try:
        first_dt = datetime.strptime(bars[0].ts_utc[:10], "%Y-%m-%d").date()
        age_days = (now.date() - first_dt).days
        return 0 <= age_days <= 800
    except Exception:
        return False


def _has_round_text(lvl: dict[str, Any]) -> bool:
    lbl = str(lvl.get("label", ""))
    algo = str(lvl.get("algo", ""))
    return ("整數" in lbl) or ("心理關卡" in lbl) or ("整數" in algo) or ("心理關卡" in algo)


def _sanitize_ta_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy a TA entry while stripping any residual round-number levels."""
    cleaned = dict(entry)
    for key in ("resistances", "supports"):
        raw_list = cleaned.get(key)
        if isinstance(raw_list, list):
            cleaned[key] = [dict(x) for x in raw_list if isinstance(x, dict) and not _has_round_text(x)]
    return cleaned


def _load_history_1d_store(out_dir: Path) -> dict[str, dict[str, Any]]:
    """Load compressed per-symbol daily OHLCV archive from `out_dir/history_1d.json.gz`."""
    hist_path = out_dir / HISTORY_1D_FILENAME
    if not hist_path.exists():
        return {}
    try:
        doc = json.loads(gzip.decompress(hist_path.read_bytes()).decode("utf-8"))
        syms_raw = doc.get("symbols") if isinstance(doc, dict) else None
        if not isinstance(syms_raw, dict):
            return {}
        store: dict[str, dict[str, Any]] = {}
        for sym, rec in syms_raw.items():
            if not isinstance(rec, dict):
                continue
            bars: list[OHLCVBar] = []
            for row in rec.get("bars") or []:
                if not isinstance(row, (list, tuple)) or len(row) < 5:
                    continue
                d_str = str(row[0])[:10]
                if len(d_str) != 10:
                    continue
                v = float(row[5]) if len(row) >= 6 and row[5] is not None else 0.0
                bars.append(
                    OHLCVBar(
                        ts_utc=f"{d_str}T00:00:00Z",
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=v,
                    )
                )
            bars.sort(key=lambda b: b.ts_utc)
            store[sym] = {
                "short_listing": bool(rec.get("short_listing", False)),
                "full_fetch_at_utc": rec.get("full_fetch_at_utc"),
                "bars": bars,
            }
        return store
    except Exception:
        return {}


def _save_history_1d_store(out_dir: Path, store: dict[str, dict[str, Any]], now_iso: str) -> None:
    """Persist compressed daily OHLCV archive to `out_dir/history_1d.json.gz`."""
    out_syms: dict[str, Any] = {}
    for sym in sorted(store.keys()):
        rec = store[sym]
        bars: list[OHLCVBar] = rec.get("bars") or []
        if not bars:
            continue
        by_day = {b.ts_utc[:10]: b for b in bars}
        ordered = [by_day[d] for d in sorted(by_day.keys())[-MAX_HISTORY_1D_BARS:]]
        compact_rows = [
            [
                b.ts_utc[:10],
                _compact_num(b.open),
                _compact_num(b.high),
                _compact_num(b.low),
                _compact_num(b.close),
                _compact_num(b.volume),
            ]
            for b in ordered
        ]
        out_syms[sym] = {
            "short_listing": bool(rec.get("short_listing", False)),
            "full_fetch_at_utc": rec.get("full_fetch_at_utc"),
            "bars": compact_rows,
        }
    payload = {"version": 1, "updated_at_utc": now_iso, "symbols": out_syms}
    raw_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    gz_bytes = gzip.compress(raw_bytes, compresslevel=9)
    hist_path = out_dir / HISTORY_1D_FILENAME
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    hist_path.write_bytes(gz_bytes)


def _merge_daily_bars(*bar_lists: list[OHLCVBar]) -> list[OHLCVBar]:
    merged: dict[str, OHLCVBar] = {}
    for bars in bar_lists:
        for b in bars:
            merged[b.ts_utc[:10]] = b
    ordered = [merged[d] for d in sorted(merged.keys())]
    return ordered[-MAX_HISTORY_1D_BARS:] if len(ordered) > MAX_HISTORY_1D_BARS else ordered


def _load_research_1d_cache() -> dict[str, list[OHLCVBar]]:
    out: dict[str, list[OHLCVBar]] = {}
    for fname in ("research_1d_bars.pkl", "ta_1d_bars.pkl"):
        p = ROOT_DIR / ".cache" / fname
        if not p.exists():
            continue
        try:
            loaded = pickle.loads(p.read_bytes())
            if isinstance(loaded, dict):
                out.update(loaded)
        except Exception:
            pass
    return out


def _save_ta_1d_cache(extra: dict[str, list[OHLCVBar]]) -> None:
    if not extra:
        return
    cache_path = ROOT_DIR / ".cache" / "ta_1d_bars.pkl"
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, list[OHLCVBar]] = {}
        if cache_path.exists():
            loaded = pickle.loads(cache_path.read_bytes())
            if isinstance(loaded, dict):
                existing = loaded
        existing.update(extra)
        cache_path.write_bytes(pickle.dumps(existing))
    except Exception:
        pass


def _load_scan_bars_1d(symbol: str) -> list[OHLCVBar]:
    p = ROOT_DIR / ".cache" / "scan_bars" / f"{symbol}_1d.pkl"
    if not p.exists():
        return []
    try:
        loaded = pickle.loads(p.read_bytes())
        if isinstance(loaded, dict) and isinstance(loaded.get("bars"), list):
            return list(loaded["bars"])
        if isinstance(loaded, list):
            return list(loaded)
    except Exception:
        pass
    return []


def _needs_full_history_fetch(
    merged_count: int,
    rec: dict[str, Any] | None,
    now: datetime,
) -> bool:
    if merged_count >= 2000:
        return False
    full_at_str = (rec or {}).get("full_fetch_at_utc")
    age_days: float | None = None
    if isinstance(full_at_str, str) and full_at_str:
        try:
            age_days = (now - datetime.fromisoformat(full_at_str.replace("Z", "+00:00"))).total_seconds() / 86400.0
        except Exception:
            age_days = None
    if merged_count < 500:
        if rec and rec.get("short_listing") and age_days is not None and age_days < 7.0:
            return False
        return True
    # Between 500 and 1999 bars: backfill only if not already checked recently within 30 days
    return age_days is None or age_days >= 30.0


def generate_ta_file(
    out_dir: Path,
    config: dict[str, Any] | None = None,
    bars_1d_by_sym: dict[str, list[OHLCVBar]] | None = None,
    latest_prices: dict[str, float] | None = None,
    data_fetcher: FallbackDataFetcher | None = None,
    now_utc: datetime | None = None,
    fetch_10y_if_missing: bool = False,
    update_history_store: bool = False,
    use_local_cache: bool = True,
) -> dict[str, Any]:
    """Build technical analysis for all active instruments and write `out_dir/ta.json`."""
    cfg = config or load_config()
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    history_store = _load_history_1d_store(out_dir)
    research_1d = _load_research_1d_cache() if use_local_cache else {}

    def _load_ta_symbols(p: Path) -> dict[str, Any]:
        if not p.exists():
            return {}
        try:
            old_doc = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(old_doc, dict) and isinstance(old_doc.get("symbols"), dict):
                return old_doc["symbols"]
        except Exception:
            pass
        return {}

    ta_path = out_dir / "ta.json"
    existing_symbols = _load_ta_symbols(ta_path)
    repo_ta_path = ROOT_DIR / "data" / "ta.json"
    fallback_symbols = _load_ta_symbols(repo_ta_path) if repo_ta_path.resolve() != ta_path.resolve() else {}

    bars_map = bars_1d_by_sym or {}
    marks = latest_prices or {}
    instruments = [inst for inst in cfg.get("instruments", []) if inst.get("enabled", True)]

    # Step 1: Collect live + archived bars and determine which symbols need 10y fetch (daily job)
    live_by_sym: dict[str, list[OHLCVBar]] = {}
    cached_by_sym: dict[str, list[OHLCVBar]] = {}
    fetch_targets: list[dict[str, Any]] = []

    for inst in instruments:
        sym = inst["symbol"]
        live_bars = list(bars_map.get(sym, []))
        if not live_bars and fetch_10y_if_missing and use_local_cache:
            live_bars = _load_scan_bars_1d(sym)
        live_by_sym[sym] = live_bars

        hist_rec = history_store.get(sym)
        hist_bars = list(hist_rec.get("bars") or []) if hist_rec else []
        local_10y = list(research_1d.get(sym, []))
        combined = _merge_daily_bars(local_10y, hist_bars, live_bars)
        cached_by_sym[sym] = _merge_daily_bars(local_10y, hist_bars)

        if fetch_10y_if_missing and data_fetcher is not None and _needs_full_history_fetch(len(combined), hist_rec, now):
            fetch_targets.append(inst)

    fetched_for_cache: dict[str, list[OHLCVBar]] = {}
    if fetch_targets and data_fetcher is not None:
        def _fetch_one(inst_item: dict[str, Any]) -> tuple[str, bool, list[OHLCVBar]]:
            s = inst_item["symbol"]
            try:
                res_long = data_fetcher.fetch(inst_item, "1d", limit=2600, now_utc=now)
                if res_long.ok and res_long.bars:
                    return s, True, list(res_long.bars)
            except Exception:
                pass
            return s, False, []

        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = [pool.submit(_fetch_one, inst_item) for inst_item in fetch_targets]
            for fut in as_completed(futs):
                s, ok_long, fetched_bars = fut.result()
                if ok_long and fetched_bars:
                    fetched_for_cache[s] = fetched_bars
                    prior_hist = cached_by_sym.get(s, [])
                    merged_long = _merge_daily_bars(prior_hist, fetched_bars, live_by_sym.get(s, []))
                    cached_by_sym[s] = merged_long
                    short_flag = _is_genuine_short_listing(merged_long, now)
                    history_store[s] = {
                        "short_listing": short_flag,
                        "full_fetch_at_utc": now_iso,
                        "bars": merged_long,
                    }

    # Step 2: Update compressed history archive when invoked by daily / explicit build
    if update_history_store:
        for inst in instruments:
            s = inst["symbol"]
            rec = history_store.get(s, {})
            merged_hist = _merge_daily_bars(cached_by_sym.get(s, []), live_by_sym.get(s, []))
            if merged_hist:
                history_store[s] = {
                    "short_listing": bool(rec.get("short_listing", False)) if len(merged_hist) < 500 else False,
                    "full_fetch_at_utc": rec.get("full_fetch_at_utc"),
                    "bars": merged_hist,
                }
        _save_history_1d_store(out_dir, history_store, now_iso=now_iso)

    if fetched_for_cache and use_local_cache:
        _save_ta_1d_cache(fetched_for_cache)

    # Step 3: Analyze each symbol with safeguard against short-bar overwrites
    out_symbols: dict[str, Any] = {}
    for inst in instruments:
        sym = inst["symbol"]
        mkt = inst["market"]
        disp = inst.get("display_name", sym)

        live_bars = live_by_sym.get(sym, [])
        long_bars = cached_by_sym.get(sym, [])
        eff_bars = _merge_daily_bars(long_bars, live_bars)
        hist_rec = history_store.get(sym)
        is_short_listing = bool(hist_rec and hist_rec.get("short_listing") and _is_genuine_short_listing(eff_bars, now))

        old_raw = existing_symbols.get(sym)
        fb_raw = fallback_symbols.get(sym)
        prev_existing = _sanitize_ta_entry(old_raw) if isinstance(old_raw, dict) else None
        prev_fallback = _sanitize_ta_entry(fb_raw) if isinstance(fb_raw, dict) else None

        # Choose valid prior reference (>=500 bars and not already marked updating if a cleaner one exists)
        prev_valid: dict[str, Any] | None = None
        for cand_prev in (prev_existing, prev_fallback):
            if cand_prev and (cand_prev.get("bars_count") or 0) >= 500:
                if cand_prev.get("resistances") or cand_prev.get("supports"):
                    prev_valid = cand_prev
                    break

        old_bt = (prev_existing or {}).get("backtest")
        if not (isinstance(old_bt, dict) and ((old_bt.get("support_samples") or 0) > 0 or (old_bt.get("resistance_samples") or 0) > 0)):
            fb_bt = (prev_fallback or {}).get("backtest")
            if isinstance(fb_bt, dict) and ((fb_bt.get("support_samples") or 0) > 0 or (fb_bt.get("resistance_samples") or 0) > 0):
                old_bt = fb_bt

        # Safeguard: Never overwrite a normal >=500-bar TA entry with incomplete (<500) fetch bars
        if len(eff_bars) < 500 and not is_short_listing and prev_valid is not None:
            preserved = _sanitize_ta_entry(prev_valid)
            preserved["data_status"] = "資料更新中"
            preserved["updating"] = True
            preserved["updating_notice"] = f"日線資料更新中（僅取得 {len(eff_bars)} 根），暫保留前次完整分析結果"
            out_symbols[sym] = preserved
            continue

        latest_eff_date = eff_bars[-1].ts_utc[:10] if eff_bars else ""
        same_day_cached = (
            not fetch_10y_if_missing
            and prev_existing is not None
            and prev_existing.get("latest_bar_date") == latest_eff_date
            and prev_existing.get("bars_count") == len(eff_bars)
            and isinstance(prev_existing.get("backtest"), dict)
            and (
                (prev_existing["backtest"].get("support_samples") or 0) > 0
                or (prev_existing["backtest"].get("resistance_samples") or 0) > 0
            )
        )

        ta_entry = analyze_symbol_ta(
            symbol=sym,
            display_name=disp,
            market=mkt,
            bars_1d=live_bars,
            latest_price=marks.get(sym),
            long_bars_1d=long_bars,
            existing_backtest=old_bt,
            is_short_listing=is_short_listing,
            recompute_backtest=not same_day_cached,
        )
        if ta_entry is not None:
            if len(eff_bars) < 500 and not is_short_listing:
                ta_entry["data_status"] = "資料更新中"
                ta_entry["updating"] = True
                ta_entry["updating_notice"] = f"長日線資料更新中（目前僅可用 {len(eff_bars)} 根）"
            else:
                ta_entry["data_status"] = "ok"
                ta_entry["updating"] = False
            out_symbols[sym] = _sanitize_ta_entry(ta_entry)
        elif prev_valid is not None:
            preserved = _sanitize_ta_entry(prev_valid)
            preserved["data_status"] = "資料更新中"
            preserved["updating"] = True
            preserved["updating_notice"] = f"日線資料更新中（可用 {len(eff_bars)} 根），暫保留前次分析結果"
            out_symbols[sym] = preserved

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
    parser.add_argument("--build-history", action="store_true", help="Persist history_1d.json.gz alongside ta.json")
    args = parser.parse_args()

    cfg = load_config()
    fetcher = FallbackDataFetcher(timeout_sec=10) if args.fetch_10y else None
    payload = generate_ta_file(
        out_dir=args.out_dir,
        config=cfg,
        data_fetcher=fetcher,
        fetch_10y_if_missing=args.fetch_10y,
        update_history_store=args.build_history or args.fetch_10y,
    )
    print(f"Generated {args.out_dir / 'ta.json'} for {payload['symbols_count']} symbols.")


if __name__ == "__main__":
    main()
