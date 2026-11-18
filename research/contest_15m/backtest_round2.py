#!/usr/bin/env python3
"""Round-2 Short-Term Contest Strategy Research & Backtester (15m + 1h, Strategies A-G).

Applies The5ers MT5 realistic costs from research/contest_15m/costs_config.yaml
(0.001% per side commission + typical MT5 point spreads), plus two control runs:
  (a) zero-cost (0 spread, 0 fee)
  (b) stress test (spread * 1.5)

Tests 7 strategies (A-D from Round 1 + new E Pivot Bounce, F Order Block, G Liquidity Sweep)
across both 15m (max_hold=16) and 1h (max_hold=24) timeframes over >= 3 years of data
(70% IS parameter selection / 30% OOS frozen evaluation).

Includes 1-week rolling contest simulation ($10k start, 4.5% risk/trade, max 1 open pos,
5% daily loss stop, < $9,000 bust) plus Signal Desk's L2 baseline under the exact same rules.
"""

from __future__ import annotations

import json
import math
import pickle
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.backtest.screener import screen_all_six_strategies
from src.config_loader import load_config
from src.data.models import OHLCVBar
from src.indicators.technical import compute_atr_series, resample_15m_to_1h, resample_1h_to_4h
from src.strategies.six_strategies import decide_strategy_step, precompute_series

LONDON_TZ = ZoneInfo("Europe/London")
NY_TZ = ZoneInfo("America/New_York")
TOKYO_TZ = ZoneInfo("Asia/Tokyo")

MIN_HISTORY_YEARS = 3.0
IS_RATIO = 0.70

MAX_HOLD_BY_TF: dict[str, int] = {
    "15m": 16,  # 4 hours
    "1h": 24,   # 24 hours
}

STRATEGY_TITLES: dict[str, str] = {
    "A": "A 跌深反轉",
    "B": "B 開盤區間突破",
    "C": "C 順勢回檔",
    "D": "D 布林擠壓突破",
    "E": "E 支撐壓力反彈",
    "F": "F 訂單塊 (Order Block)",
    "G": "G 掃停損反轉 (Liquidity Sweep)",
}

PARAM_GRID: dict[str, list[dict[str, Any]]] = {
    "A": [
        {"name": "A_k2.0_tp1.0", "dist_atr": 2.0, "sl_buf_atr": 0.2, "tp_r": 1.0},
        {"name": "A_k2.0_tp1.5", "dist_atr": 2.0, "sl_buf_atr": 0.2, "tp_r": 1.5},
        {"name": "A_k2.5_tp1.0", "dist_atr": 2.5, "sl_buf_atr": 0.2, "tp_r": 1.0},
        {"name": "A_k2.5_tp1.5", "dist_atr": 2.5, "sl_buf_atr": 0.2, "tp_r": 1.5},
        {"name": "A_k2.5_tp2.0", "dist_atr": 2.5, "sl_buf_atr": 0.2, "tp_r": 2.0},
        {"name": "A_k3.0_tp1.0", "dist_atr": 3.0, "sl_buf_atr": 0.2, "tp_r": 1.0},
        {"name": "A_k3.0_tp1.5", "dist_atr": 3.0, "sl_buf_atr": 0.2, "tp_r": 1.5},
        {"name": "A_k3.0_tp2.0", "dist_atr": 3.0, "sl_buf_atr": 0.2, "tp_r": 2.0},
    ],
    "B": [
        {"name": "B_rng2.0_tp1.0", "max_range_atr": 2.0, "tp_r": 1.0},
        {"name": "B_rng2.0_tp1.5", "max_range_atr": 2.0, "tp_r": 1.5},
        {"name": "B_rng2.0_tp2.0", "max_range_atr": 2.0, "tp_r": 2.0},
        {"name": "B_rng2.5_tp1.0", "max_range_atr": 2.5, "tp_r": 1.0},
        {"name": "B_rng2.5_tp1.5", "max_range_atr": 2.5, "tp_r": 1.5},
        {"name": "B_rng2.5_tp2.0", "max_range_atr": 2.5, "tp_r": 2.0},
    ],
    "C": [
        {"name": "C_z0.3_tp1.0", "zone_atr": 0.3, "sl_buf_atr": 0.2, "tp_r": 1.0},
        {"name": "C_z0.3_tp1.5", "zone_atr": 0.3, "sl_buf_atr": 0.2, "tp_r": 1.5},
        {"name": "C_z0.3_tp2.0", "zone_atr": 0.3, "sl_buf_atr": 0.2, "tp_r": 2.0},
        {"name": "C_z0.5_tp1.0", "zone_atr": 0.5, "sl_buf_atr": 0.2, "tp_r": 1.0},
        {"name": "C_z0.5_tp1.5", "zone_atr": 0.5, "sl_buf_atr": 0.2, "tp_r": 1.5},
        {"name": "C_z0.5_tp2.0", "zone_atr": 0.5, "sl_buf_atr": 0.2, "tp_r": 2.0},
    ],
    "D": [
        {"name": "D_p10_tp1.0", "bw_pct": 0.10, "tp_r": 1.0},
        {"name": "D_p10_tp1.5", "bw_pct": 0.10, "tp_r": 1.5},
        {"name": "D_p10_tp2.0", "bw_pct": 0.10, "tp_r": 2.0},
        {"name": "D_p15_tp1.0", "bw_pct": 0.15, "tp_r": 1.0},
        {"name": "D_p15_tp1.5", "bw_pct": 0.15, "tp_r": 1.5},
        {"name": "D_p15_tp2.0", "bw_pct": 0.15, "tp_r": 2.0},
    ],
    "E": [
        {"name": "E_N60_k3_tp1.0", "lookback": 60, "pivot_k": 3, "touch_atr": 0.25, "sl_buf_atr": 0.2, "tp_r": 1.0},
        {"name": "E_N60_k3_tp1.5", "lookback": 60, "pivot_k": 3, "touch_atr": 0.25, "sl_buf_atr": 0.2, "tp_r": 1.5},
        {"name": "E_N60_k3_tp2.0", "lookback": 60, "pivot_k": 3, "touch_atr": 0.25, "sl_buf_atr": 0.2, "tp_r": 2.0},
        {"name": "E_N100_k5_tp1.0", "lookback": 100, "pivot_k": 5, "touch_atr": 0.25, "sl_buf_atr": 0.2, "tp_r": 1.0},
        {"name": "E_N100_k5_tp1.5", "lookback": 100, "pivot_k": 5, "touch_atr": 0.25, "sl_buf_atr": 0.2, "tp_r": 1.5},
        {"name": "E_N100_k5_tp2.0", "lookback": 100, "pivot_k": 5, "touch_atr": 0.25, "sl_buf_atr": 0.2, "tp_r": 2.0},
    ],
    "F": [
        {"name": "F_d1.5_tp1.0", "disp_atr": 1.5, "sl_buf_atr": 0.2, "max_age": 48, "tp_r": 1.0},
        {"name": "F_d1.5_tp1.5", "disp_atr": 1.5, "sl_buf_atr": 0.2, "max_age": 48, "tp_r": 1.5},
        {"name": "F_d1.5_tp2.0", "disp_atr": 1.5, "sl_buf_atr": 0.2, "max_age": 48, "tp_r": 2.0},
        {"name": "F_d2.0_tp1.0", "disp_atr": 2.0, "sl_buf_atr": 0.2, "max_age": 48, "tp_r": 1.0},
        {"name": "F_d2.0_tp1.5", "disp_atr": 2.0, "sl_buf_atr": 0.2, "max_age": 48, "tp_r": 1.5},
        {"name": "F_d2.0_tp2.0", "disp_atr": 2.0, "sl_buf_atr": 0.2, "max_age": 48, "tp_r": 2.0},
    ],
    "G": [
        {"name": "G_N60_k3_tp1.0", "lookback": 60, "pivot_k": 3, "sl_buf_atr": 0.2, "tp_r": 1.0},
        {"name": "G_N60_k3_tp1.5", "lookback": 60, "pivot_k": 3, "sl_buf_atr": 0.2, "tp_r": 1.5},
        {"name": "G_N60_k3_tp2.0", "lookback": 60, "pivot_k": 3, "sl_buf_atr": 0.2, "tp_r": 2.0},
        {"name": "G_N100_k5_tp1.0", "lookback": 100, "pivot_k": 5, "sl_buf_atr": 0.2, "tp_r": 1.0},
        {"name": "G_N100_k5_tp1.5", "lookback": 100, "pivot_k": 5, "sl_buf_atr": 0.2, "tp_r": 1.5},
        {"name": "G_N100_k5_tp2.0", "lookback": 100, "pivot_k": 5, "sl_buf_atr": 0.2, "tp_r": 2.0},
    ],
}


# ---------------------------------------------------------------------------
# Data Loading
# ---------------------------------------------------------------------------

def load_costs_config() -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "costs_config.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_binance_merged(binance_sym: str, interval: str, cache_dir: Path) -> list[OHLCVBar]:
    merged_dir = cache_dir / "merged"
    if not merged_dir.exists():
        return []
    main_files = sorted(merged_dir.glob(f"{binance_sym}_{interval}_*.pkl"))
    tail_files = sorted(merged_dir.glob(f"tail_{binance_sym}_{interval}_*.pkl"))
    bars_map: dict[str, OHLCVBar] = {}
    for fp in main_files + tail_files:
        try:
            loaded = pickle.loads(fp.read_bytes())
            if isinstance(loaded, list):
                for b in loaded:
                    if isinstance(b, OHLCVBar) and b.ts_utc:
                        bars_map[b.ts_utc] = b
        except Exception:
            continue
    return sorted(bars_map.values(), key=lambda x: x.ts_utc)


def _load_dukascopy_merged(sym: str, interval: str, cache_dir: Path) -> list[OHLCVBar]:
    p = cache_dir / "merged" / f"{sym}_{interval}_3y.pkl"
    if not p.exists():
        return []
    try:
        loaded = pickle.loads(p.read_bytes())
        return loaded if isinstance(loaded, list) else []
    except Exception:
        return []


def load_timeframe_universe(
    timeframe: str,
    costs_cfg: dict[str, Any],
    repo_cfg: dict[str, Any],
    common_start_utc: str = "2023-09-01T00:00:00Z",
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """Load >= 3y cached bars for given timeframe ('15m' or '1h') across all 18 target symbols."""
    bt_cfg = repo_cfg.get("backtest", {})
    bin_cache = Path(bt_cfg.get("archive_cache_dir", ".cache/binance_monthly"))
    duka_cache = Path(bt_cfg.get("dukascopy_cache_dir", ".cache/dukascopy"))

    defaults = costs_cfg.get("defaults", {})
    fee_rate = float(defaults.get("fee_rate_per_side", 0.00001))
    slip_bps = float(defaults.get("slippage_bps_per_side", 0.0))

    loaded: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, str]] = []

    for sym, inst in costs_cfg.get("instruments", {}).items():
        mkt = inst["market"]
        bars: list[OHLCVBar] = []
        source_note = ""

        if mkt == "crypto":
            alias = inst.get("binance_alias", f"{sym}T")
            bars = _load_binance_merged(alias, timeframe, bin_cache)
            if bars:
                # Align crypto window to >= common_start_utc so IS/OOS period aligns with Dukascopy symbols
                aligned = [b for b in bars if b.ts_utc >= common_start_utc]
                bars = aligned if len(aligned) > 1000 else bars
                source_note = f"Binance {alias} {timeframe} 快取"
        else:
            bars = _load_dukascopy_merged(sym, timeframe, duka_cache)
            if bars:
                # Filter out closed-market/weekend zero-volume flat padding bars from Dukascopy
                bars = [b for b in bars if b.volume > 0]
                source_note = f"Dukascopy {inst['dukascopy_pair']} {timeframe}"
            elif sym == "XAUUSD":
                # Fallback to PAXGUSDT only if Dukascopy XAUUSD not present
                paxg = _load_binance_merged("PAXGUSDT", timeframe, bin_cache)
                if paxg:
                    aligned = [b for b in paxg if b.ts_utc >= common_start_utc]
                    bars = aligned if len(aligned) > 1000 else paxg
                    source_note = f"Binance PAXGUSDT {timeframe} 備援（Dukascopy 未取得）"

        if not bars:
            skipped.append({"symbol": sym, "market": mkt, "reason": f"無 {timeframe} 本地快取資料"})
            continue

        dt_start = datetime.strptime(bars[0].ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        dt_end = datetime.strptime(bars[-1].ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        span_years = (dt_end - dt_start).total_seconds() / (86400.0 * 365.25)
        if span_years < MIN_HISTORY_YEARS:
            skipped.append({
                "symbol": sym,
                "market": mkt,
                "reason": f"{timeframe} 歷史長度僅 {span_years:.2f} 年，未達 3 年門檻",
            })
            continue

        loaded[sym] = {
            "symbol": sym,
            "market": mkt,
            "session": inst["session"],
            "timeframe": timeframe,
            "bars": bars,
            "span_years": round(span_years, 2),
            "start_utc": bars[0].ts_utc,
            "end_utc": bars[-1].ts_utc,
            "spread_points": float(inst["spread_points"]),
            "fee_rate": fee_rate,
            "slippage_bps": slip_bps,
            "max_leverage": float(inst.get("max_leverage", 20.0)),
            "source_note": source_note,
        }

    return loaded, skipped


# ---------------------------------------------------------------------------
# Precomputed Indicators & Pivots
# ---------------------------------------------------------------------------

def _ema_numpy(arr: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(arr), np.nan, dtype=np.float64)
    if len(arr) < period:
        return out
    alpha = 2.0 / (period + 1.0)
    out[period - 1] = float(np.mean(arr[:period]))
    val = out[period - 1]
    for i in range(period, len(arr)):
        val = arr[i] * alpha + val * (1.0 - alpha)
        out[i] = val
    return out


def _precompute_pivots(
    highs: np.ndarray,
    lows: np.ndarray,
    k: int,
) -> tuple[dict[int, list[tuple[int, float]]], dict[int, list[tuple[int, float]]]]:
    """Return pivot highs & lows indexed by confirm_idx = p + k -> list[(p, price)]."""
    n = len(highs)
    hi_by_confirm: dict[int, list[tuple[int, float]]] = defaultdict(list)
    lo_by_confirm: dict[int, list[tuple[int, float]]] = defaultdict(list)

    for p in range(k, n - k):
        wh = highs[p - k : p + k + 1]
        if highs[p] == np.max(wh) and highs[p] > highs[p - 1]:
            hi_by_confirm[p + k].append((p, float(highs[p])))
        elif highs[p] == np.max(wh) and np.sum(wh == highs[p]) == 1:
            hi_by_confirm[p + k].append((p, float(highs[p])))

        wl = lows[p - k : p + k + 1]
        if lows[p] == np.min(wl) and lows[p] < lows[p - 1]:
            lo_by_confirm[p + k].append((p, float(lows[p])))
        elif lows[p] == np.min(wl) and np.sum(wl == lows[p]) == 1:
            lo_by_confirm[p + k].append((p, float(lows[p])))

    return hi_by_confirm, lo_by_confirm


@dataclass
class SymbolSeriesR2:
    symbol: str
    market: str
    session: str
    timeframe: str
    n: int
    ts_str: list[str]
    ts_sec: np.ndarray
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray
    ema20: np.ndarray
    atr14: np.ndarray
    bb_mid: np.ndarray
    bb_upper: np.ndarray
    bb_lower: np.ndarray
    bb_bw: np.ndarray
    bb_bw_rank_100: np.ndarray
    htf_close: np.ndarray
    htf_ema50: np.ndarray
    pivots_hi_by_k: dict[int, dict[int, list[tuple[int, float]]]]
    pivots_lo_by_k: dict[int, dict[int, list[tuple[int, float]]]]
    spread_points: float
    fee_rate: float
    slippage_bps: float
    max_leverage: float


def precompute_symbol_series_r2(info: dict[str, Any]) -> SymbolSeriesR2:
    bars: list[OHLCVBar] = info["bars"]
    sym = info["symbol"]
    mkt = info["market"]
    tf = info["timeframe"]
    n = len(bars)

    ts_str = [b.ts_utc for b in bars]
    ts_sec = np.array(
        [datetime.strptime(b.ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() for b in bars],
        dtype=np.float64,
    )
    opens = np.array([b.open for b in bars], dtype=np.float64)
    highs = np.array([b.high for b in bars], dtype=np.float64)
    lows = np.array([b.low for b in bars], dtype=np.float64)
    closes = np.array([b.close for b in bars], dtype=np.float64)

    ema20 = _ema_numpy(closes, 20)
    atr_list = compute_atr_series(bars, period=14, market=mkt, interval=tf)
    atr14 = np.full(n, np.nan, dtype=np.float64)
    if atr_list:
        atr14[14 : 14 + len(atr_list)] = np.array(atr_list, dtype=np.float64)

    # Bollinger Bands (20, 2)
    bb_mid = np.full(n, np.nan, dtype=np.float64)
    bb_upper = np.full(n, np.nan, dtype=np.float64)
    bb_lower = np.full(n, np.nan, dtype=np.float64)
    bb_bw = np.full(n, np.nan, dtype=np.float64)
    bb_bw_rank_100 = np.full(n, np.nan, dtype=np.float64)

    csum = np.cumsum(np.insert(closes, 0, 0.0))
    csum2 = np.cumsum(np.insert(closes * closes, 0, 0.0))
    idx_end = np.arange(20, n + 1)
    means = (csum[idx_end] - csum[idx_end - 20]) / 20.0
    means2 = (csum2[idx_end] - csum2[idx_end - 20]) / 20.0
    stds = np.sqrt(np.maximum(0.0, means2 - means * means))

    bb_mid[19:] = means
    bb_upper[19:] = means + 2.0 * stds
    bb_lower[19:] = means - 2.0 * stds
    with np.errstate(divide="ignore", invalid="ignore"):
        bb_bw[19:] = np.where(means > 1e-12, (4.0 * stds) / means, np.nan)

    for i in range(118, n):
        cur = bb_bw[i]
        if math.isnan(cur):
            continue
        win = bb_bw[i - 99 : i + 1]
        bb_bw_rank_100[i] = float(np.sum(win <= cur)) / 100.0

    # Higher-timeframe EMA50 strictly closed before bar close (1h for 15m; 4h for 1h)
    bar_dur_sec = 900.0 if tf == "15m" else 3600.0
    htf_dur_sec = 3600.0 if tf == "15m" else 14400.0
    last_dt = datetime.fromtimestamp(ts_sec[-1] + bar_dur_sec, tz=timezone.utc)
    if tf == "15m":
        htf_bars = resample_15m_to_1h(bars, now_utc=last_dt)
    else:
        htf_bars = resample_1h_to_4h(bars, now_utc=last_dt)

    htf_close = np.full(n, np.nan, dtype=np.float64)
    htf_ema50 = np.full(n, np.nan, dtype=np.float64)
    if len(htf_bars) >= 50:
        hc = np.array([b.close for b in htf_bars], dtype=np.float64)
        he = _ema_numpy(hc, 50)
        htf_close_sec = np.array(
            [
                datetime.strptime(b.ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
                + htf_dur_sec
                for b in htf_bars
            ],
            dtype=np.float64,
        )
        bar_close_sec = ts_sec + bar_dur_sec
        pos = np.searchsorted(htf_close_sec, bar_close_sec, side="right") - 1
        valid = pos >= 0
        htf_close[valid] = hc[pos[valid]]
        htf_ema50[valid] = he[pos[valid]]

    pivots_hi_by_k: dict[int, dict[int, list[tuple[int, float]]]] = {}
    pivots_lo_by_k: dict[int, dict[int, list[tuple[int, float]]]] = {}
    for k in (3, 5):
        hi_d, lo_d = _precompute_pivots(highs, lows, k)
        pivots_hi_by_k[k] = hi_d
        pivots_lo_by_k[k] = lo_d

    return SymbolSeriesR2(
        symbol=sym,
        market=mkt,
        session=info["session"],
        timeframe=tf,
        n=n,
        ts_str=ts_str,
        ts_sec=ts_sec,
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        ema20=ema20,
        atr14=atr14,
        bb_mid=bb_mid,
        bb_upper=bb_upper,
        bb_lower=bb_lower,
        bb_bw=bb_bw,
        bb_bw_rank_100=bb_bw_rank_100,
        htf_close=htf_close,
        htf_ema50=htf_ema50,
        pivots_hi_by_k=pivots_hi_by_k,
        pivots_lo_by_k=pivots_lo_by_k,
        spread_points=info["spread_points"],
        fee_rate=info["fee_rate"],
        slippage_bps=info["slippage_bps"],
        max_leverage=info["max_leverage"],
    )


# ---------------------------------------------------------------------------
# Signal Generation (Strategies A-G)
# ---------------------------------------------------------------------------

@dataclass
class SignalCandidateR2:
    symbol: str
    strategy_id: str
    signal_bar_idx: int
    entry_bar_idx: int
    ts_utc: str
    direction: str
    raw_open: float
    sl_px: float
    tp_px: float
    sl_dist_pct: float


def _session_key_and_minute(ts_s: float, session: str) -> tuple[str, int]:
    dt_utc = datetime.fromtimestamp(ts_s, tz=timezone.utc)
    if session == "utc0":
        return dt_utc.strftime("%Y-%m-%d"), dt_utc.hour * 60 + dt_utc.minute
    if session == "london":
        dt_loc = dt_utc.astimezone(LONDON_TZ)
        open_dt = dt_loc.replace(hour=8, minute=0, second=0, microsecond=0)
    elif session == "tokyo":
        dt_loc = dt_utc.astimezone(TOKYO_TZ)
        open_dt = dt_loc.replace(hour=9, minute=0, second=0, microsecond=0)
    else:
        dt_loc = dt_utc.astimezone(NY_TZ)
        open_dt = dt_loc.replace(hour=9, minute=30 if session == "ny" else 0, second=0, microsecond=0)
        # Note: for 1h bars (timestamps at :00 UTC), NY 09:30 opens inside the 09:00 ET hourly bar
        open_dt = open_dt.replace(minute=0) if dt_loc.minute == 0 and session == "ny" else open_dt

    if dt_loc < open_dt:
        prev = open_dt - timedelta(days=1)
        return prev.strftime("%Y-%m-%d"), int((dt_loc - prev).total_seconds() // 60)
    return open_dt.strftime("%Y-%m-%d"), int((dt_loc - open_dt).total_seconds() // 60)


def _session_key_and_minute_exact(ts_s: float, session: str, tf: str) -> tuple[str, int]:
    dt_utc = datetime.fromtimestamp(ts_s, tz=timezone.utc)
    if session == "utc0":
        return dt_utc.strftime("%Y-%m-%d"), dt_utc.hour * 60 + dt_utc.minute
    if session == "london":
        dt_loc = dt_utc.astimezone(LONDON_TZ)
        open_dt = dt_loc.replace(hour=8, minute=0, second=0, microsecond=0)
    elif session == "tokyo":
        dt_loc = dt_utc.astimezone(TOKYO_TZ)
        open_dt = dt_loc.replace(hour=9, minute=0, second=0, microsecond=0)
    else:
        dt_loc = dt_utc.astimezone(NY_TZ)
        # On 1h bars (always :00), use 09:00 ET as first hourly bar; on 15m use 09:30 ET
        open_min = 30 if tf == "15m" else 0
        open_dt = dt_loc.replace(hour=9, minute=open_min, second=0, microsecond=0)
    if dt_loc < open_dt:
        prev = open_dt - timedelta(days=1)
        return prev.strftime("%Y-%m-%d"), int((dt_loc - prev).total_seconds() // 60)
    return open_dt.strftime("%Y-%m-%d"), int((dt_loc - open_dt).total_seconds() // 60)


def generate_signals_r2(
    ser: SymbolSeriesR2,
    strategy_id: str,
    params: dict[str, Any],
    start_idx: int,
    end_idx: int,
) -> list[SignalCandidateR2]:
    out: list[SignalCandidateR2] = []
    n = ser.n
    hi_bound = min(end_idx, n - 1)
    opens, highs, lows, closes = ser.opens, ser.highs, ser.lows, ser.closes
    ema20, atr14 = ser.ema20, ser.atr14

    if strategy_id == "A":
        k = float(params["dist_atr"])
        sl_buf = float(params["sl_buf_atr"])
        tp_r = float(params["tp_r"])
        for i in range(max(25, start_idx), hi_bound):
            atr, e20 = atr14[i], ema20[i]
            if math.isnan(atr) or atr <= 0 or math.isnan(e20):
                continue
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            po, pc = opens[i - 1], closes[i - 1]
            body, pbody, rng = abs(c - o), abs(pc - po), h - l
            if rng <= 0:
                continue
            next_open = opens[i + 1]
            if next_open <= 0:
                continue

            if max(e20 - l, e20 - lows[i - 1]) > k * atr and c < e20:
                engulf_bull = (pc < po) and (c > o) and (o <= pc) and (c >= po) and (body > pbody)
                lower_wick = min(o, c) - l
                pin_bull = (c > o) and (lower_wick >= 2.0 * max(body, 0.05 * atr)) and (lower_wick >= 0.5 * rng)
                if engulf_bull or pin_bull:
                    sl_px = min(l, lows[i - 1], lows[i - 2]) - sl_buf * atr
                    risk_est = next_open - sl_px
                    if risk_est > 1e-9:
                        out.append(SignalCandidateR2(
                            ser.symbol, "A", i, i + 1, ser.ts_str[i + 1], "long",
                            next_open, sl_px, next_open + tp_r * risk_est, risk_est / next_open * 100.0,
                        ))
                        continue

            if max(h - e20, highs[i - 1] - e20) > k * atr and c > e20:
                engulf_bear = (pc > po) and (c < o) and (o >= pc) and (c <= po) and (body > pbody)
                upper_wick = h - max(o, c)
                pin_bear = (c < o) and (upper_wick >= 2.0 * max(body, 0.05 * atr)) and (upper_wick >= 0.5 * rng)
                if engulf_bear or pin_bear:
                    sl_px = max(h, highs[i - 1], highs[i - 2]) + sl_buf * atr
                    risk_est = sl_px - next_open
                    if risk_est > 1e-9:
                        out.append(SignalCandidateR2(
                            ser.symbol, "A", i, i + 1, ser.ts_str[i + 1], "short",
                            next_open, sl_px, next_open - tp_r * risk_est, risk_est / next_open * 100.0,
                        ))

    elif strategy_id == "B":
        max_rng_atr = float(params["max_range_atr"])
        tp_r = float(params["tp_r"])
        min_or_bars = 3 if ser.timeframe == "15m" else 1
        sessions: dict[str, dict[str, Any]] = {}
        for i in range(max(20, start_idx), hi_bound):
            atr = atr14[i]
            if math.isnan(atr) or atr <= 0:
                continue
            sess_date, mins = _session_key_and_minute_exact(ser.ts_sec[i], ser.session, ser.timeframe)
            st = sessions.get(sess_date)
            if st is None:
                st = {"or_hi": -1e18, "or_lo": 1e18, "or_bars": 0, "traded": False, "skip": False}
                sessions[sess_date] = st

            if 0 <= mins < 60:
                st["or_hi"] = max(st["or_hi"], highs[i])
                st["or_lo"] = min(st["or_lo"], lows[i])
                st["or_bars"] += 1
                continue

            if 60 <= mins < 240 and not st["traded"] and not st["skip"]:
                if st["or_bars"] < min_or_bars:
                    st["skip"] = True
                    continue
                or_rng = st["or_hi"] - st["or_lo"]
                if or_rng <= 0 or or_rng > max_rng_atr * atr:
                    st["skip"] = True
                    continue
                c = closes[i]
                next_open = opens[i + 1]
                if next_open <= 0:
                    continue
                if c > st["or_hi"]:
                    sl_px = st["or_lo"]
                    risk_est = next_open - sl_px
                    if risk_est > 1e-9:
                        st["traded"] = True
                        out.append(SignalCandidateR2(
                            ser.symbol, "B", i, i + 1, ser.ts_str[i + 1], "long",
                            next_open, sl_px, next_open + tp_r * risk_est, risk_est / next_open * 100.0,
                        ))
                elif c < st["or_lo"]:
                    sl_px = st["or_hi"]
                    risk_est = sl_px - next_open
                    if risk_est > 1e-9:
                        st["traded"] = True
                        out.append(SignalCandidateR2(
                            ser.symbol, "B", i, i + 1, ser.ts_str[i + 1], "short",
                            next_open, sl_px, next_open - tp_r * risk_est, risk_est / next_open * 100.0,
                        ))

    elif strategy_id == "C":
        zone_atr = float(params["zone_atr"])
        sl_buf = float(params["sl_buf_atr"])
        tp_r = float(params["tp_r"])
        hc_arr, he_arr = ser.htf_close, ser.htf_ema50
        for i in range(max(200, start_idx), hi_bound):
            atr, e20, hc, he = atr14[i], ema20[i], hc_arr[i], he_arr[i]
            if math.isnan(atr) or atr <= 0 or math.isnan(e20) or math.isnan(hc) or math.isnan(he):
                continue
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            next_open = opens[i + 1]
            if next_open <= 0:
                continue
            if hc > he:
                if (l <= e20 + zone_atr * atr) and (c >= e20 - zone_atr * atr) and (c > o) and (c > closes[i - 1]):
                    sl_px = min(l, lows[i - 1], lows[i - 2]) - sl_buf * atr
                    risk_est = next_open - sl_px
                    if risk_est > 1e-9:
                        out.append(SignalCandidateR2(
                            ser.symbol, "C", i, i + 1, ser.ts_str[i + 1], "long",
                            next_open, sl_px, next_open + tp_r * risk_est, risk_est / next_open * 100.0,
                        ))
            elif hc < he:
                if (h >= e20 - zone_atr * atr) and (c <= e20 + zone_atr * atr) and (c < o) and (c < closes[i - 1]):
                    sl_px = max(h, highs[i - 1], highs[i - 2]) + sl_buf * atr
                    risk_est = sl_px - next_open
                    if risk_est > 1e-9:
                        out.append(SignalCandidateR2(
                            ser.symbol, "C", i, i + 1, ser.ts_str[i + 1], "short",
                            next_open, sl_px, next_open - tp_r * risk_est, risk_est / next_open * 100.0,
                        ))

    elif strategy_id == "D":
        bw_pct = float(params["bw_pct"])
        tp_r = float(params["tp_r"])
        rank100, mid, upper, lower = ser.bb_bw_rank_100, ser.bb_mid, ser.bb_upper, ser.bb_lower
        for i in range(max(120, start_idx), hi_bound):
            r_prev, r_prev2 = rank100[i - 1], rank100[i - 2]
            m_val, u_val, l_val = mid[i], upper[i], lower[i]
            if math.isnan(r_prev) or math.isnan(m_val) or math.isnan(u_val) or math.isnan(l_val):
                continue
            if not ((r_prev <= bw_pct) or (not math.isnan(r_prev2) and r_prev2 <= bw_pct)):
                continue
            c, pc, next_open = closes[i], closes[i - 1], opens[i + 1]
            if next_open <= 0:
                continue
            if c > u_val and pc <= upper[i - 1]:
                risk_est = next_open - m_val
                if risk_est > 1e-9:
                    out.append(SignalCandidateR2(
                        ser.symbol, "D", i, i + 1, ser.ts_str[i + 1], "long",
                        next_open, m_val, next_open + tp_r * risk_est, risk_est / next_open * 100.0,
                    ))
            elif c < l_val and pc >= lower[i - 1]:
                risk_est = m_val - next_open
                if risk_est > 1e-9:
                    out.append(SignalCandidateR2(
                        ser.symbol, "D", i, i + 1, ser.ts_str[i + 1], "short",
                        next_open, m_val, next_open - tp_r * risk_est, risk_est / next_open * 100.0,
                    ))

    elif strategy_id == "E":
        N = int(params["lookback"])
        k = int(params["pivot_k"])
        touch_atr = float(params["touch_atr"])
        sl_buf = float(params["sl_buf_atr"])
        tp_r = float(params["tp_r"])
        hi_map = ser.pivots_hi_by_k[k]
        lo_map = ser.pivots_lo_by_k[k]
        active_hi: deque[tuple[int, float]] = deque()
        active_lo: deque[tuple[int, float]] = deque()

        warm_start = max(k + 2, start_idx - N)
        for j in range(warm_start, start_idx):
            for p_idx, px in hi_map.get(j, ()):
                active_hi.append((p_idx, px))
            for p_idx, px in lo_map.get(j, ()):
                active_lo.append((p_idx, px))

        for i in range(max(N + k, start_idx), hi_bound):
            for p_idx, px in hi_map.get(i, ()):
                active_hi.append((p_idx, px))
            for p_idx, px in lo_map.get(i, ()):
                active_lo.append((p_idx, px))
            min_p = i - N
            while active_hi and active_hi[0][0] < min_p:
                active_hi.popleft()
            while active_lo and active_lo[0][0] < min_p:
                active_lo.popleft()

            atr = atr14[i]
            if math.isnan(atr) or atr <= 0:
                continue
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            next_open = opens[i + 1]
            if next_open <= 0:
                continue

            # Support bounce (long)
            if c > o and active_lo:
                matched_s = None
                best_dist = 1e18
                for _, s_px in active_lo:
                    if l <= s_px + touch_atr * atr and l >= s_px - 0.5 * atr and c > s_px:
                        d = abs(l - s_px)
                        if d < best_dist:
                            best_dist = d
                            matched_s = s_px
                if matched_s is not None:
                    sl_px = min(l, matched_s) - sl_buf * atr
                    risk_est = next_open - sl_px
                    if risk_est > 1e-9:
                        out.append(SignalCandidateR2(
                            ser.symbol, "E", i, i + 1, ser.ts_str[i + 1], "long",
                            next_open, sl_px, next_open + tp_r * risk_est, risk_est / next_open * 100.0,
                        ))
                        continue

            # Resistance bounce (short)
            if c < o and active_hi:
                matched_r = None
                best_dist = 1e18
                for _, r_px in active_hi:
                    if h >= r_px - touch_atr * atr and h <= r_px + 0.5 * atr and c < r_px:
                        d = abs(h - r_px)
                        if d < best_dist:
                            best_dist = d
                            matched_r = r_px
                if matched_r is not None:
                    sl_px = max(h, matched_r) + sl_buf * atr
                    risk_est = sl_px - next_open
                    if risk_est > 1e-9:
                        out.append(SignalCandidateR2(
                            ser.symbol, "E", i, i + 1, ser.ts_str[i + 1], "short",
                            next_open, sl_px, next_open - tp_r * risk_est, risk_est / next_open * 100.0,
                        ))

    elif strategy_id == "F":
        disp_atr = float(params["disp_atr"])
        sl_buf = float(params["sl_buf_atr"])
        max_age = int(params["max_age"])
        tp_r = float(params["tp_r"])
        # Active OBs: list of dicts {dir, lo, hi, created_idx}
        obs: list[dict[str, Any]] = []
        scan_begin = max(20, start_idx - max_age)

        for i in range(scan_begin, hi_bound):
            atr = atr14[i]
            if math.isnan(atr) or atr <= 0:
                continue
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]

            # 1) Retest / invalidation check ONLY on bars >= start_idx
            next_obs: list[dict[str, Any]] = []
            emitted_this_bar = False
            for ob in obs:
                if i - ob["created_idx"] > max_age:
                    continue
                if ob["dir"] == "long":
                    if c < ob["lo"]:
                        continue  # invalidated
                    if not emitted_this_bar and i >= start_idx and l <= ob["hi"] and c > o and c >= ob["lo"]:
                        next_open = opens[i + 1]
                        sl_px = ob["lo"] - sl_buf * atr
                        risk_est = next_open - sl_px
                        if next_open > 0 and risk_est > 1e-9:
                            out.append(SignalCandidateR2(
                                ser.symbol, "F", i, i + 1, ser.ts_str[i + 1], "long",
                                next_open, sl_px, next_open + tp_r * risk_est, risk_est / next_open * 100.0,
                            ))
                            emitted_this_bar = True
                            continue  # consumed on first retest
                    next_obs.append(ob)
                else:
                    if c > ob["hi"]:
                        continue  # invalidated
                    if not emitted_this_bar and i >= start_idx and h >= ob["lo"] and c < o and c <= ob["hi"]:
                        next_open = opens[i + 1]
                        sl_px = ob["hi"] + sl_buf * atr
                        risk_est = sl_px - next_open
                        if next_open > 0 and risk_est > 1e-9:
                            out.append(SignalCandidateR2(
                                ser.symbol, "F", i, i + 1, ser.ts_str[i + 1], "short",
                                next_open, sl_px, next_open - tp_r * risk_est, risk_est / next_open * 100.0,
                            ))
                            emitted_this_bar = True
                            continue  # consumed on first retest
                    next_obs.append(ob)
            obs = next_obs

            # 2) Detect new displacement finishing at bar i (1..3 bars)
            if i >= 4:
                for span in (1, 2, 3):
                    j = i - span + 1
                    ob_idx = j - 1
                    atr_j = atr14[j]
                    if math.isnan(atr_j) or atr_j <= 0:
                        continue
                    if (c - opens[j]) >= disp_atr * atr_j and closes[ob_idx] < opens[ob_idx]:
                        obs.append({"dir": "long", "lo": float(lows[ob_idx]), "hi": float(highs[ob_idx]), "created_idx": i})
                        break
                    if (opens[j] - c) >= disp_atr * atr_j and closes[ob_idx] > opens[ob_idx]:
                        obs.append({"dir": "short", "lo": float(lows[ob_idx]), "hi": float(highs[ob_idx]), "created_idx": i})
                        break

    elif strategy_id == "G":
        N = int(params["lookback"])
        k = int(params["pivot_k"])
        sl_buf = float(params["sl_buf_atr"])
        tp_r = float(params["tp_r"])
        hi_map = ser.pivots_hi_by_k[k]
        lo_map = ser.pivots_lo_by_k[k]
        active_hi: deque[tuple[int, float]] = deque()
        active_lo: deque[tuple[int, float]] = deque()
        consumed_hi: set[int] = set()
        consumed_lo: set[int] = set()

        warm_start = max(k + 2, start_idx - N)
        for j in range(warm_start, start_idx):
            for p_idx, px in hi_map.get(j, ()):
                active_hi.append((p_idx, px))
            for p_idx, px in lo_map.get(j, ()):
                active_lo.append((p_idx, px))

        for i in range(max(N + k, start_idx), hi_bound):
            for p_idx, px in hi_map.get(i, ()):
                active_hi.append((p_idx, px))
            for p_idx, px in lo_map.get(i, ()):
                active_lo.append((p_idx, px))
            min_p = i - N
            while active_hi and active_hi[0][0] < min_p:
                active_hi.popleft()
            while active_lo and active_lo[0][0] < min_p:
                active_lo.popleft()

            atr = atr14[i]
            if math.isnan(atr) or atr <= 0:
                continue
            h, l, c = highs[i], lows[i], closes[i]
            next_open = opens[i + 1]
            if next_open <= 0:
                continue

            # Bullish liquidity sweep (long): poke below recent unconsumed swing low and reclaim inside
            swept_lo = None
            for p_idx, s_lo in reversed(active_lo):
                if p_idx in consumed_lo:
                    continue
                one_bar = (l < s_lo) and (c > s_lo)
                two_bar = (lows[i - 1] < s_lo) and (closes[i - 1] <= s_lo) and (c > s_lo)
                if one_bar or two_bar:
                    swept_lo = (p_idx, min(l, lows[i - 1]) if two_bar else l)
                    break
            if swept_lo is not None:
                p_idx, extreme_lo = swept_lo
                consumed_lo.add(p_idx)
                sl_px = extreme_lo - sl_buf * atr
                risk_est = next_open - sl_px
                if risk_est > 1e-9:
                    out.append(SignalCandidateR2(
                        ser.symbol, "G", i, i + 1, ser.ts_str[i + 1], "long",
                        next_open, sl_px, next_open + tp_r * risk_est, risk_est / next_open * 100.0,
                    ))
                    continue

            # Bearish liquidity sweep (short): poke above recent unconsumed swing high and reclaim inside
            swept_hi = None
            for p_idx, s_hi in reversed(active_hi):
                if p_idx in consumed_hi:
                    continue
                one_bar = (h > s_hi) and (c < s_hi)
                two_bar = (highs[i - 1] > s_hi) and (closes[i - 1] >= s_hi) and (c < s_hi)
                if one_bar or two_bar:
                    swept_hi = (p_idx, max(h, highs[i - 1]) if two_bar else h)
                    break
            if swept_hi is not None:
                p_idx, extreme_hi = swept_hi
                consumed_hi.add(p_idx)
                sl_px = extreme_hi + sl_buf * atr
                risk_est = sl_px - next_open
                if risk_est > 1e-9:
                    out.append(SignalCandidateR2(
                        ser.symbol, "G", i, i + 1, ser.ts_str[i + 1], "short",
                        next_open, sl_px, next_open - tp_r * risk_est, risk_est / next_open * 100.0,
                    ))

    return out


# ---------------------------------------------------------------------------
# Trade Outcome Simulator (The5ers MT5 Spread Points & 0.001% Commission)
# ---------------------------------------------------------------------------

@dataclass
class SimTradeR2:
    symbol: str
    strategy_id: str
    direction: str
    entry_ts_utc: str
    exit_ts_utc: str
    entry_sec: float
    exit_sec: float
    entry_px: float
    exit_px: float
    sl_px: float
    tp_px: float
    sl_dist_pct: float
    r_multiple: float
    exit_reason: str


def simulate_candidate_r2(
    ser: SymbolSeriesR2,
    cand: SignalCandidateR2,
    end_idx_limit: int,
    max_hold_bars: int,
    spread_mult: float = 1.0,
    zero_cost: bool = False,
) -> SimTradeR2 | None:
    eb = cand.entry_bar_idx
    if eb >= min(ser.n, end_idx_limit):
        return None

    half_sp = 0.0 if zero_cost else (0.5 * ser.spread_points * spread_mult)
    slip = 0.0 if zero_cost else (ser.slippage_bps / 10000.0)
    fee = 0.0 if zero_cost else ser.fee_rate
    direction = cand.direction

    raw_entry = (
        (ser.opens[eb] + half_sp) * (1.0 + slip)
        if direction == "long"
        else (ser.opens[eb] - half_sp) * (1.0 - slip)
    )
    if raw_entry <= 0:
        return None

    sl_px, tp_px = cand.sl_px, cand.tp_px
    risk_per_unit = (raw_entry - sl_px) if direction == "long" else (sl_px - raw_entry)
    if risk_per_unit <= 1e-9:
        return None

    max_b = min(eb + max_hold_bars, min(ser.n, end_idx_limit))
    if max_b <= eb:
        return None

    raw_exit = 0.0
    exit_bidx = max_b - 1
    exit_reason = "time_stop"

    for bidx in range(eb, max_b):
        o, h, l, c = ser.opens[bidx], ser.highs[bidx], ser.lows[bidx], ser.closes[bidx]
        if direction == "long":
            open_bid = o - half_sp
            if open_bid <= sl_px:
                raw_exit = open_bid * (1.0 - slip)
                exit_bidx, exit_reason = bidx, "sl"
                break
            if (l - half_sp) <= sl_px:
                raw_exit = sl_px * (1.0 - slip)
                exit_bidx, exit_reason = bidx, "sl"
                break
            if (h - half_sp) >= tp_px:
                raw_exit = tp_px * (1.0 - slip)
                exit_bidx, exit_reason = bidx, "tp"
                break
        else:
            open_ask = o + half_sp
            if open_ask >= sl_px:
                raw_exit = open_ask * (1.0 + slip)
                exit_bidx, exit_reason = bidx, "sl"
                break
            if (h + half_sp) >= sl_px:
                raw_exit = sl_px * (1.0 + slip)
                exit_bidx, exit_reason = bidx, "sl"
                break
            if (l + half_sp) <= tp_px:
                raw_exit = tp_px * (1.0 + slip)
                exit_bidx, exit_reason = bidx, "tp"
                break

        if bidx == eb + max_hold_bars - 1:
            raw_exit = (
                (c - half_sp) * (1.0 - slip)
                if direction == "long"
                else (c + half_sp) * (1.0 + slip)
            )
            exit_bidx, exit_reason = bidx, "time_stop"
            break
    else:
        return None

    gross_per_unit = (raw_exit - raw_entry) if direction == "long" else (raw_entry - raw_exit)
    fee_per_unit = (raw_entry + raw_exit) * fee
    net_per_unit = gross_per_unit - fee_per_unit
    r_mult = net_per_unit / risk_per_unit

    return SimTradeR2(
        symbol=ser.symbol,
        strategy_id=cand.strategy_id,
        direction=direction,
        entry_ts_utc=ser.ts_str[eb],
        exit_ts_utc=ser.ts_str[exit_bidx],
        entry_sec=float(ser.ts_sec[eb]),
        exit_sec=float(ser.ts_sec[exit_bidx]),
        entry_px=raw_entry,
        exit_px=raw_exit,
        sl_px=sl_px,
        tp_px=tp_px,
        sl_dist_pct=risk_per_unit / raw_entry * 100.0,
        r_multiple=float(r_mult),
        exit_reason=exit_reason,
    )


# ---------------------------------------------------------------------------
# Metrics, Sequencing & 1-Week Contest Simulator
# ---------------------------------------------------------------------------

def compute_trade_metrics_r2(trades: list[SimTradeR2], risk_pct: float = 1.0) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {
            "trades": 0, "win_rate_pct": 0.0, "avg_win_r": 0.0, "avg_loss_r": 0.0,
            "expectancy_r": 0.0, "profit_factor": 0.0, "max_drawdown_pct": 0.0,
            "max_consec_losses": 0, "total_r": 0.0,
        }
    ordered = sorted(trades, key=lambda t: (t.exit_sec, t.entry_sec, t.symbol))
    wins = [t.r_multiple for t in ordered if t.r_multiple > 0]
    losses = [t.r_multiple for t in ordered if t.r_multiple <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 1e-9 else (99.0 if gross_win > 0 else 0.0)

    consec, max_consec = 0, 0
    eq, peak, max_dd = 10000.0, 10000.0, 0.0
    for t in ordered:
        if t.r_multiple <= 0:
            consec += 1
            max_consec = max(max_consec, consec)
        else:
            consec = 0
        eq = max(1.0, eq + eq * (risk_pct / 100.0) * t.r_multiple)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100.0)

    return {
        "trades": n,
        "win_rate_pct": round(len(wins) / n * 100.0, 2),
        "avg_win_r": round(sum(wins) / len(wins), 3) if wins else 0.0,
        "avg_loss_r": round(sum(losses) / len(losses), 3) if losses else 0.0,
        "expectancy_r": round(sum(t.r_multiple for t in ordered) / n, 4),
        "profit_factor": round(pf, 3),
        "max_drawdown_pct": round(max_dd, 2),
        "max_consec_losses": max_consec,
        "total_r": round(sum(t.r_multiple for t in ordered), 2),
    }


def select_single_position_sequence_r2(
    all_trades: list[SimTradeR2],
    rank_score_by_key: dict[tuple[str, str], float],
) -> list[SimTradeR2]:
    if not all_trades:
        return []
    by_entry: dict[float, list[SimTradeR2]] = defaultdict(list)
    for t in all_trades:
        by_entry[t.entry_sec].append(t)
    selected: list[SimTradeR2] = []
    busy_until = -1.0
    for entry_sec in sorted(by_entry.keys()):
        if entry_sec <= busy_until:
            continue
        best = max(
            by_entry[entry_sec],
            key=lambda t: (rank_score_by_key.get((t.strategy_id, t.symbol), -999.0), -t.sl_dist_pct),
        )
        selected.append(best)
        busy_until = best.exit_sec
    return selected


def simulate_weekly_contest_r2(
    trades_seq: list[SimTradeR2],
    max_lev_by_sym: dict[str, float],
    oos_start_sec: float,
    oos_end_sec: float,
    risk_pct: float = 4.5,
    daily_loss_limit_pct: float = 5.0,
    bust_equity: float = 9000.0,
    start_capital: float = 10000.0,
    step_days: int = 7,
) -> dict[str, Any]:
    week_sec = 7 * 86400.0
    step_sec = step_days * 86400.0
    ordered = sorted(trades_seq, key=lambda t: t.entry_sec)

    returns_pct: list[float] = []
    bust_count = 0
    total_weeks = 0
    cur_start = oos_start_sec

    while cur_start + week_sec <= oos_end_sec:
        cur_end = cur_start + week_sec
        total_weeks += 1
        eq = start_capital
        busted = False
        stopped_day_key: str | None = None
        cur_day_key: str | None = None
        day_start_eq = start_capital

        for tr in ordered:
            if tr.entry_sec < cur_start:
                continue
            if tr.exit_sec > cur_end:
                break

            entry_day = datetime.fromtimestamp(tr.entry_sec, tz=timezone.utc).strftime("%Y-%m-%d")
            if entry_day != cur_day_key:
                cur_day_key = entry_day
                day_start_eq = eq
                stopped_day_key = None
            if stopped_day_key == entry_day:
                continue

            risk_usd = eq * (risk_pct / 100.0)
            sl_frac = tr.sl_dist_pct / 100.0
            max_lev = max_lev_by_sym.get(tr.symbol, 20.0)
            planned_notional = risk_usd / max(1e-9, sl_frac)
            eff_risk_usd = min(risk_usd, eq * max_lev * sl_frac) if planned_notional > eq * max_lev else risk_usd

            pnl = eff_risk_usd * tr.r_multiple
            worst_eq = eq - eff_risk_usd if tr.exit_reason == "sl" else eq + min(0.0, pnl)
            if worst_eq < bust_equity:
                eq = worst_eq
                busted = True
                break
            eq += pnl
            if eq < bust_equity:
                busted = True
                break

            exit_day = datetime.fromtimestamp(tr.exit_sec, tz=timezone.utc).strftime("%Y-%m-%d")
            if exit_day != cur_day_key:
                cur_day_key = exit_day
                day_start_eq = eq - pnl
            if eq <= day_start_eq * (1.0 - daily_loss_limit_pct / 100.0):
                stopped_day_key = exit_day

        if busted:
            bust_count += 1
        returns_pct.append((eq - start_capital) / start_capital * 100.0)
        cur_start += step_sec

    if not returns_pct:
        return {"weeks": 0, "median_return_pct": 0.0, "p90_return_pct": 0.0, "mean_return_pct": 0.0, "bust_rate_pct": 0.0}
    arr = np.array(returns_pct, dtype=np.float64)
    return {
        "weeks": total_weeks,
        "median_return_pct": round(float(np.median(arr)), 2),
        "p90_return_pct": round(float(np.percentile(arr, 90)), 2),
        "mean_return_pct": round(float(np.mean(arr)), 2),
        "bust_rate_pct": round(bust_count / total_weeks * 100.0, 2),
    }


# ---------------------------------------------------------------------------
# L2 Signal Desk Baseline Simulation Under Identical 1-Week Contest Rules
# ---------------------------------------------------------------------------

def run_l2_contest_baseline(
    repo_cfg: dict[str, Any],
    oos_start_sec: float,
    oos_end_sec: float,
) -> dict[str, Any]:
    """Extract Signal Desk L2 daily trades from 1d cache and run 1-week contest simulation."""
    cache_1d = ROOT_DIR / ".cache" / "research_1d_bars.pkl"
    if not cache_1d.exists():
        return {"available": False}
    bars_1d: dict[str, list[OHLCVBar]] = pickle.loads(cache_1d.read_bytes())

    # Screen L2 on 1d bars to get exact IS-chosen params
    screen_res = screen_all_six_strategies({"1d": bars_1d}, repo_cfg)
    l2_row = next((r for r in screen_res if r["strategy_id"] == "L2"), None)
    if not l2_row or not l2_row.get("chosen_params"):
        return {"available": False}

    params = l2_row["chosen_params"]
    l2_cfg = repo_cfg["strategies"]["L2"]
    allowed_syms = set(l2_cfg.get("symbols", []))
    inst_by_sym = {i["symbol"]: i for i in repo_cfg["instruments"] if i.get("enabled", True)}
    slip = float(repo_cfg.get("risk", {}).get("default_slippage_bps", 1.0)) / 10000.0

    # Extract per-symbol L2 trades independently, then enforce max 1 position at a time
    all_l2_trades: list[SimTradeR2] = []
    max_lev_map: dict[str, float] = {}
    for sym in sorted(allowed_syms):
        bars = sorted(bars_1d.get(sym, []), key=lambda b: b.ts_utc)
        inst = inst_by_sym.get(sym)
        if not bars or not inst or len(bars) < 80:
            continue
        max_lev_map[sym] = float(inst.get("max_leverage", 10.0))
        spread_bps = float(inst.get("assumed_spread_bps", 3.0))
        fee_rate = float(inst.get("fee_rate", 0.0005))
        series = precompute_series("L2", bars, params, market=inst["market"], interval="1d", atr_period=14)

        pos = None
        pending_act = None
        for idx in range(len(bars)):
            b = bars[idx]
            b_sec = datetime.strptime(b.ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()

            if pending_act is not None and pos is None:
                d = pending_act.direction
                raw_entry = b.open * (1.0 + spread_bps / 20000.0) * (1.0 + slip) if d == "long" else b.open * (1.0 - spread_bps / 20000.0) * (1.0 - slip)
                sl_px = pending_act.stop_loss
                r_unit = (raw_entry - sl_px) if d == "long" else (sl_px - raw_entry)
                if raw_entry > 0 and r_unit > 1e-9:
                    pos = {"dir": d, "entry_px": raw_entry, "entry_ts": b.ts_utc, "entry_sec": b_sec, "entry_idx": idx, "sl_px": sl_px, "r_unit": r_unit}
                pending_act = None

            if pos is not None:
                d = pos["dir"]
                sl_px = pos["sl_px"]
                exited = False
                raw_exit = 0.0
                reason = "trail"
                if d == "long":
                    obid = b.open * (1.0 - spread_bps / 20000.0)
                    if obid <= sl_px:
                        raw_exit, exited, reason = obid * (1.0 - slip), True, "sl"
                    elif b.low * (1.0 - spread_bps / 20000.0) <= sl_px:
                        raw_exit, exited, reason = sl_px * (1.0 - slip), True, "sl"
                else:
                    oask = b.open * (1.0 + spread_bps / 20000.0)
                    if oask >= sl_px:
                        raw_exit, exited, reason = oask * (1.0 + slip), True, "sl"
                    elif b.high * (1.0 + spread_bps / 20000.0) >= sl_px:
                        raw_exit, exited, reason = sl_px * (1.0 + slip), True, "sl"

                if not exited and idx < len(bars) - 1:
                    act = decide_strategy_step("L2", series, idx, d, params, allow_short=True, bars_held=max(1, idx - pos["entry_idx"] + 1))
                    if act.action == "exit":
                        nb = bars[idx + 1]
                        raw_exit = nb.open * (1.0 - spread_bps / 20000.0) * (1.0 - slip) if d == "long" else nb.open * (1.0 + spread_bps / 20000.0) * (1.0 + slip)
                        n_sec = datetime.strptime(nb.ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
                        gross = (raw_exit - pos["entry_px"]) if d == "long" else (pos["entry_px"] - raw_exit)
                        net = gross - (pos["entry_px"] + raw_exit) * fee_rate
                        all_l2_trades.append(SimTradeR2(
                            sym, "L2", d, pos["entry_ts"], nb.ts_utc, pos["entry_sec"], n_sec,
                            pos["entry_px"], raw_exit, sl_px, raw_exit, pos["r_unit"] / pos["entry_px"] * 100.0,
                            net / pos["r_unit"], "trail",
                        ))
                        pos = None
                        continue

                if exited:
                    gross = (raw_exit - pos["entry_px"]) if d == "long" else (pos["entry_px"] - raw_exit)
                    net = gross - (pos["entry_px"] + raw_exit) * fee_rate
                    all_l2_trades.append(SimTradeR2(
                        sym, "L2", d, pos["entry_ts"], b.ts_utc, pos["entry_sec"], b_sec,
                        pos["entry_px"], raw_exit, sl_px, raw_exit, pos["r_unit"] / pos["entry_px"] * 100.0,
                        net / pos["r_unit"], reason,
                    ))
                    pos = None
                    continue

            if pos is None and idx < len(bars) - 1:
                act = decide_strategy_step("L2", series, idx, None, params, allow_short=True, bars_held=0)
                if act.action in ("enter_long", "enter_short"):
                    pending_act = act

    # Enforce 1-position-at-a-time over OOS window (for trades entering in OOS)
    oos_l2 = [t for t in all_l2_trades if oos_start_sec <= t.entry_sec < oos_end_sec]
    seq_l2 = select_single_position_sequence_r2(oos_l2, {("L2", s): 0.2 for s in max_lev_map})

    # Pre-index daily bars by symbol & timestamp for accurate 1-week MTM contest simulation
    daily_bars_by_sym: dict[str, list[tuple[float, OHLCVBar]]] = {}
    for sym in max_lev_map:
        daily_bars_by_sym[sym] = [
            (datetime.strptime(b.ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp(), b)
            for b in sorted(bars_1d.get(sym, []), key=lambda x: x.ts_utc)
        ]

    def _sim_l2_weekly(step_days: int) -> dict[str, Any]:
        week_sec = 7 * 86400.0
        step_sec = step_days * 86400.0
        ordered = sorted(seq_l2, key=lambda t: t.entry_sec)
        rets: list[float] = []
        busts = 0
        weeks = 0
        cur_start = oos_start_sec
        while cur_start + week_sec <= oos_end_sec:
            cur_end = cur_start + week_sec
            weeks += 1
            eq = 10000.0
            busted = False
            stopped_day: str | None = None
            cur_day: str | None = None
            day_start_eq = 10000.0

            for tr in ordered:
                if tr.entry_sec < cur_start:
                    continue
                if tr.entry_sec >= cur_end:
                    break
                entry_day = datetime.fromtimestamp(tr.entry_sec, tz=timezone.utc).strftime("%Y-%m-%d")
                if entry_day != cur_day:
                    cur_day = entry_day
                    day_start_eq = eq
                    stopped_day = None
                if stopped_day == entry_day:
                    continue

                risk_usd = eq * 0.045
                sl_frac = tr.sl_dist_pct / 100.0
                max_lev = max_lev_map.get(tr.symbol, 10.0)
                eff_risk = min(risk_usd, eq * max_lev * sl_frac) if (risk_usd / max(1e-9, sl_frac)) > eq * max_lev else risk_usd
                qty = eff_risk / max(1e-9, abs(tr.entry_px - tr.sl_px))
                fee_rate = float(inst_by_sym[tr.symbol].get("fee_rate", 0.0005))

                # Walk daily bars inside [tr.entry_sec, min(tr.exit_sec, cur_end)]
                bars_sym = daily_bars_by_sym.get(tr.symbol, [])
                for b_sec, b in bars_sym:
                    if b_sec < tr.entry_sec:
                        continue
                    if b_sec > cur_end or b_sec > tr.exit_sec:
                        break
                    d_str = b.ts_utc[:10]
                    if d_str != cur_day:
                        cur_day = d_str
                        day_start_eq = eq
                        stopped_day = None
                    worst_px = b.low if tr.direction == "long" else b.high
                    worst_float = ((worst_px - tr.entry_px) if tr.direction == "long" else (tr.entry_px - worst_px)) * qty
                    if eq + worst_float < 9000.0:
                        eq = eq + worst_float
                        busted = True
                        break
                if busted:
                    break

                if tr.exit_sec <= cur_end:
                    pnl = eff_risk * tr.r_multiple
                    eq += pnl
                    exit_day = tr.exit_ts_utc[:10]
                    if eq <= day_start_eq * 0.95:
                        stopped_day = exit_day
                else:
                    # Mark-to-market open position at cur_end
                    last_bar = None
                    for b_sec, b in bars_sym:
                        if b_sec <= cur_end:
                            last_bar = b
                        else:
                            break
                    if last_bar is not None:
                        mtm_gross = ((last_bar.close - tr.entry_px) if tr.direction == "long" else (tr.entry_px - last_bar.close)) * qty
                        mtm_fee = (tr.entry_px + last_bar.close) * qty * fee_rate
                        eq += (mtm_gross - mtm_fee)
                    break  # single open position extends to end of this 1-week contest

            if busted:
                busts += 1
            rets.append((eq - 10000.0) / 100.0)
            cur_start += step_sec

        if not rets:
            return {"weeks": 0, "median_return_pct": 0.0, "p90_return_pct": 0.0, "mean_return_pct": 0.0, "bust_rate_pct": 0.0}
        arr = np.array(rets, dtype=np.float64)
        return {
            "weeks": weeks,
            "median_return_pct": round(float(np.median(arr)), 2),
            "p90_return_pct": round(float(np.percentile(arr, 90)), 2),
            "mean_return_pct": round(float(np.mean(arr)), 2),
            "bust_rate_pct": round(busts / weeks * 100.0, 2),
        }

    return {
        "available": True,
        "selected_params": params,
        "oos_metrics_1pos": compute_trade_metrics_r2(seq_l2),
        "weekly_contest_7d_step": _sim_l2_weekly(7),
        "weekly_contest_1d_step": _sim_l2_weekly(1),
    }


# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------

def run_timeframe(tf: str, costs_cfg: dict[str, Any], repo_cfg: dict[str, Any]) -> dict[str, Any]:
    loaded_data, skipped = load_timeframe_universe(tf, costs_cfg, repo_cfg)
    print(f"\n=== [{tf}] Loaded {len(loaded_data)} symbols: {list(loaded_data.keys())} ===")
    if skipped:
        print(f"    [{tf}] Skipped {len(skipped)}: {[s['symbol'] for s in skipped]}")

    if not loaded_data:
        return {"timeframe": tf, "loaded_symbols": {}, "skipped_symbols": skipped, "strategies": {}, "passed_strategies": []}

    series_map: dict[str, SymbolSeriesR2] = {}
    split_idx_map: dict[str, int] = {}
    max_lev_map: dict[str, float] = {}
    for sym, info in loaded_data.items():
        ser = precompute_symbol_series_r2(info)
        series_map[sym] = ser
        split_idx_map[sym] = int(ser.n * IS_RATIO)
        max_lev_map[sym] = ser.max_leverage

    global_oos_start_sec = max(series_map[s].ts_sec[split_idx_map[s]] for s in series_map)
    global_oos_end_sec = min(series_map[s].ts_sec[-1] for s in series_map)
    max_hold = MAX_HOLD_BY_TF[tf]

    strategies_out: dict[str, Any] = {}
    passed_list: list[str] = []

    for sid in ("A", "B", "C", "D", "E", "F", "G"):
        param_evals: list[dict[str, Any]] = []
        for pcfg in PARAM_GRID[sid]:
            is_all: list[SimTradeR2] = []
            is_by_sym: dict[str, Any] = {}
            is_rank: dict[tuple[str, str], float] = {}
            for sym, ser in series_map.items():
                cut = split_idx_map[sym]
                cands = generate_signals_r2(ser, sid, pcfg, 0, cut)
                tr_list = [t for c in cands if (t := simulate_candidate_r2(ser, c, cut, max_hold, 1.0, False)) is not None]
                is_all.extend(tr_list)
                sm = compute_trade_metrics_r2(tr_list)
                is_by_sym[sym] = sm
                is_rank[(sid, sym)] = sm["expectancy_r"]
            seq = select_single_position_sequence_r2(is_all, is_rank)
            param_evals.append({
                "params": pcfg,
                "is_metrics": compute_trade_metrics_r2(seq),
                "is_by_sym": is_by_sym,
            })

        best_eval = max(
            param_evals,
            key=lambda e: (
                1 if e["is_metrics"]["trades"] >= 50 else 0,
                e["is_metrics"]["profit_factor"] if e["is_metrics"]["trades"] >= 50 else -999.0,
                e["is_metrics"]["total_r"],
            ),
        )
        best_p = best_eval["params"]
        frozen_rank = {(sid, sym): best_eval["is_by_sym"][sym]["expectancy_r"] for sym in series_map}

        def _eval(split: str, spread_mult: float, zero_cost: bool) -> tuple[dict[str, Any], dict[str, Any], list[SimTradeR2]]:
            all_tr: list[SimTradeR2] = []
            by_sym: dict[str, Any] = {}
            for sym, ser in series_map.items():
                cut = split_idx_map[sym]
                s_idx = 0 if split == "IS" else cut
                e_idx = cut if split == "IS" else ser.n
                cands = generate_signals_r2(ser, sid, best_p, s_idx, e_idx)
                tr_list = [t for c in cands if (t := simulate_candidate_r2(ser, c, e_idx, max_hold, spread_mult, zero_cost)) is not None]
                all_tr.extend(tr_list)
                by_sym[sym] = compute_trade_metrics_r2(tr_list)
            seq = select_single_position_sequence_r2(all_tr, frozen_rank)
            return compute_trade_metrics_r2(seq), by_sym, seq

        is_norm, is_sym_m, _ = _eval("IS", 1.0, False)
        is_stress, _, _ = _eval("IS", 1.5, False)
        is_zero, _, _ = _eval("IS", 0.0, True)
        oos_norm, oos_sym_m, oos_seq = _eval("OOS", 1.0, False)
        oos_stress, oos_stress_sym_m, _ = _eval("OOS", 1.5, False)
        oos_zero, oos_zero_sym_m, _ = _eval("OOS", 0.0, True)

        passed = (
            oos_norm["trades"] >= 100
            and oos_norm["profit_factor"] >= 1.2
            and oos_norm["expectancy_r"] > 0.0
            and oos_stress["profit_factor"] >= 1.05
        )
        if passed:
            passed_list.append(sid)

        w7 = simulate_weekly_contest_r2(oos_seq, max_lev_map, global_oos_start_sec, global_oos_end_sec, step_days=7)
        w1 = simulate_weekly_contest_r2(oos_seq, max_lev_map, global_oos_start_sec, global_oos_end_sec, step_days=1)

        strategies_out[sid] = {
            "strategy_id": sid,
            "title": STRATEGY_TITLES[sid],
            "best_params": best_p,
            "param_candidates_is": [{"params": e["params"], "is_metrics": e["is_metrics"]} for e in param_evals],
            "is_normal": is_norm,
            "is_stress": is_stress,
            "is_zero_cost": is_zero,
            "oos_normal": oos_norm,
            "oos_stress": oos_stress,
            "oos_zero_cost": oos_zero,
            "oos_by_symbol": oos_sym_m,
            "oos_stress_by_symbol": oos_stress_sym_m,
            "oos_zero_by_symbol": oos_zero_sym_m,
            "passed": passed,
            "weekly_contest_7d_step": w7,
            "weekly_contest_1d_step": w1,
        }
        print(
            f"  [{tf}] {sid} ({best_p['name']:18s}): "
            f"OOS n={oos_norm['trades']:4d} WR={oos_norm['win_rate_pct']:5.1f}% "
            f"expR={oos_norm['expectancy_r']:+.4f} PF={oos_norm['profit_factor']:.3f} "
            f"stressPF={oos_stress['profit_factor']:.3f} zeroPF={oos_zero['profit_factor']:.3f} "
            f"PASS={passed} | 1w_med={w7['median_return_pct']:+.2f}% bust={w7['bust_rate_pct']:.1f}%"
        )

    return {
        "timeframe": tf,
        "loaded_symbols": {
            s: {
                "symbol": info["symbol"],
                "market": info["market"],
                "bars_count": len(info["bars"]),
                "span_years": info["span_years"],
                "start_utc": info["start_utc"],
                "end_utc": info["end_utc"],
                "is_end_utc": series_map[s].ts_str[split_idx_map[s] - 1],
                "oos_start_utc": series_map[s].ts_str[split_idx_map[s]],
                "spread_points": info["spread_points"],
                "fee_rate": info["fee_rate"],
                "source_note": info["source_note"],
            }
            for s, info in loaded_data.items()
        },
        "skipped_symbols": skipped,
        "global_oos_start_sec": global_oos_start_sec,
        "global_oos_end_sec": global_oos_end_sec,
        "global_oos_start_utc": datetime.fromtimestamp(global_oos_start_sec, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "global_oos_end_utc": datetime.fromtimestamp(global_oos_end_sec, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "passed_strategies": passed_list,
        "strategies": strategies_out,
    }


def main() -> None:
    costs_cfg = load_costs_config()
    repo_cfg = load_config()

    res_1h = run_timeframe("1h", costs_cfg, repo_cfg)
    res_15m = run_timeframe("15m", costs_cfg, repo_cfg)

    oos_start_sec = res_1h.get("global_oos_start_sec") or res_15m.get("global_oos_start_sec")
    oos_end_sec = res_1h.get("global_oos_end_sec") or res_15m.get("global_oos_end_sec")
    l2_base = run_l2_contest_baseline(repo_cfg, oos_start_sec, oos_end_sec)
    print(f"\n=== L2 Baseline ===\n  {l2_base}")

    out = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timeframes": {"1h": res_1h, "15m": res_15m},
        "l2_baseline": l2_base,
    }
    out_path = Path(__file__).resolve().parent / "results_round2.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
