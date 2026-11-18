#!/usr/bin/env python3
"""15m Contest Short-Term Strategy Research & Backtester.

Evaluates 4 intraday 15m strategies (A, B, C, D) across the 18-symbol contest universe
using existing repo data sources (Binance monthly archives & Dukascopy with fast-fail
probe + PAXGUSDT gold backup matching src/jobs/weekly.py).

Strict rules:
- 70% IS / 30% OOS chronological split (parameters chosen on IS only; OOS frozen)
- Entry at next 15m bar open (adjusted for half-spread + slippage)
- Fixed SL & TP at entry, never trailed
- Max hold 16 bars (4 hours), closed at 16th bar close
- Conservative intra-bar tie-break: if SL and TP both hit in the same bar -> SL
- Stress test: spread * 1.5
- 1-position-at-a-time tie-break: highest IS (strategy, symbol) expectancy_r,
  then smallest stop distance %
- 1-week contest simulation in OOS: $10,000 start, 4.5% risk/trade, max 1 open pos,
  5% daily loss stop, < $9,000 bust
"""

from __future__ import annotations

import json
import math
import pickle
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.config_loader import load_config
from src.data.adapters import DukascopyArchiveFetcher
from src.data.models import OHLCVBar
from src.indicators.technical import compute_atr_series, resample_15m_to_1h

# ---------------------------------------------------------------------------
# Universe & Cost Configuration
# ---------------------------------------------------------------------------

TARGET_SYMBOLS: list[dict[str, str]] = [
    {"symbol": "DAX40", "market": "index", "session": "london"},
    {"symbol": "JPN225", "market": "index", "session": "tokyo"},
    {"symbol": "NAS100", "market": "index", "session": "ny"},
    {"symbol": "SP500", "market": "index", "session": "ny"},
    {"symbol": "UK100", "market": "index", "session": "london"},
    {"symbol": "US30", "market": "index", "session": "ny"},
    {"symbol": "EURUSD", "market": "forex", "session": "london"},
    {"symbol": "GBPUSD", "market": "forex", "session": "london"},
    {"symbol": "USDJPY", "market": "forex", "session": "london"},
    {"symbol": "USDCHF", "market": "forex", "session": "london"},
    {"symbol": "USDCAD", "market": "forex", "session": "london"},
    {"symbol": "AUDUSD", "market": "forex", "session": "london"},
    {"symbol": "NZDUSD", "market": "forex", "session": "london"},
    {"symbol": "XAUUSD", "market": "commodity", "session": "ny"},
    {"symbol": "XAGUSD", "market": "commodity", "session": "ny"},
    {"symbol": "WTI", "market": "commodity", "session": "ny"},
    {"symbol": "BTCUSD", "market": "crypto", "session": "utc0"},
    {"symbol": "ETHUSD", "market": "crypto", "session": "utc0"},
]

BINANCE_SYMBOL_ALIAS: dict[str, str] = {
    "BTCUSD": "BTCUSDT",
    "ETHUSD": "ETHUSDT",
    "XAUUSD": "PAXGUSDT",  # gold_backup_symbol in config/config.yaml & weekly.py
}

CONFIG_SYMBOL_ALIAS: dict[str, str] = {
    "BTCUSD": "BTCUSDT",
    "ETHUSD": "ETHUSDT",
}

LONDON_TZ = ZoneInfo("Europe/London")
NY_TZ = ZoneInfo("America/New_York")

MAX_HOLD_BARS = 16
MIN_HISTORY_YEARS = 3.0
IS_RATIO = 0.70

# Parameter candidates (3 variants per strategy; selected on IS only)
PARAM_GRID: dict[str, list[dict[str, Any]]] = {
    "A": [
        {"name": "A1_k2.0", "dist_atr": 2.0, "sl_buf_atr": 0.2, "min_tp_r": 1.0},
        {"name": "A2_k2.5", "dist_atr": 2.5, "sl_buf_atr": 0.2, "min_tp_r": 1.0},
        {"name": "A3_k3.0", "dist_atr": 3.0, "sl_buf_atr": 0.2, "min_tp_r": 1.0},
    ],
    "B": [
        {"name": "B1_tp1.5_rng2.0", "max_range_atr": 2.0, "tp_r": 1.5},
        {"name": "B2_tp2.0_rng2.0", "max_range_atr": 2.0, "tp_r": 2.0},
        {"name": "B3_tp1.5_rng2.5", "max_range_atr": 2.5, "tp_r": 1.5},
    ],
    "C": [
        {"name": "C1_z0.3_tp1.5", "zone_atr": 0.3, "sl_buf_atr": 0.2, "tp_r": 1.5},
        {"name": "C2_z0.3_tp2.0", "zone_atr": 0.3, "sl_buf_atr": 0.2, "tp_r": 2.0},
        {"name": "C3_z0.5_tp1.5", "zone_atr": 0.5, "sl_buf_atr": 0.2, "tp_r": 1.5},
    ],
    "D": [
        {"name": "D1_p10_tp1.5", "bw_pct": 0.10, "tp_r": 1.5},
        {"name": "D2_p10_tp2.0", "bw_pct": 0.10, "tp_r": 2.0},
        {"name": "D3_p15_tp2.0", "bw_pct": 0.15, "tp_r": 2.0},
    ],
}

STRATEGY_TITLES: dict[str, str] = {
    "A": "A 跌深反轉",
    "B": "B 開盤區間突破",
    "C": "C 順勢回檔",
    "D": "D 布林擠壓突破",
}


# ---------------------------------------------------------------------------
# Data Loading
# ---------------------------------------------------------------------------

def _load_binance_merged_15m(binance_sym: str, cache_dir: Path) -> list[OHLCVBar]:
    """Load merged Binance monthly 15m cache + tail file matching weekly.py."""
    merged_dir = cache_dir / "merged"
    if not merged_dir.exists():
        return []
    main_files = sorted(merged_dir.glob(f"{binance_sym}_15m_*.pkl"))
    tail_files = sorted(merged_dir.glob(f"tail_{binance_sym}_15m_*.pkl"))
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


def load_universe_data(config: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """Load >= 3y 15m bars for each target symbol; skip unreachable ones cleanly."""
    bt_cfg = config.get("backtest", {})
    bin_cache = Path(bt_cfg.get("archive_cache_dir", ".cache/binance_monthly"))
    duka_cache = Path(bt_cfg.get("dukascopy_cache_dir", ".cache/dukascopy"))
    duka = DukascopyArchiveFetcher(cache_dir=duka_cache, timeout_sec=3)

    inst_by_sym = {inst["symbol"]: inst for inst in config.get("instruments", [])}
    default_slip_bps = float(config.get("risk", {}).get("default_slippage_bps", 1.0))

    loaded: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, str]] = []

    for meta in TARGET_SYMBOLS:
        sym = meta["symbol"]
        mkt = meta["market"]
        cfg_sym = CONFIG_SYMBOL_ALIAS.get(sym, sym)
        inst_cfg = inst_by_sym.get(cfg_sym, {})
        spread_bps = float(inst_cfg.get("assumed_spread_bps", 3.0))
        fee_rate = float(inst_cfg.get("fee_rate", 0.0005 if mkt == "crypto" else 0.0002))
        max_lev = float(inst_cfg.get("max_leverage", 10.0))

        bars_15m: list[OHLCVBar] = []
        source_note = ""

        if sym in ("BTCUSD", "ETHUSD"):
            alias = BINANCE_SYMBOL_ALIAS[sym]
            bars_15m = _load_binance_merged_15m(alias, bin_cache)
            source_note = f"Binance {alias} 15m cache"
        else:
            # Try Dukascopy local cache or remote probe (fast-fail on HTTP 429/timeout)
            duka_bars = duka.fetch_history(sym, "15m", years=3.2)
            if duka_bars:
                bars_15m = duka_bars
                source_note = "Dukascopy 15m"
            elif sym == "XAUUSD":
                alias = BINANCE_SYMBOL_ALIAS["XAUUSD"]
                bars_15m = _load_binance_merged_15m(alias, bin_cache)
                source_note = f"Binance {alias} 15m 備援（Dukascopy 無法連線）"

        if not bars_15m:
            reason = "Dukascopy 無法連線（HTTP 429/逾時）且無本地 3 年 15m 快取"
            if sym in ("DAX40", "JPN225", "NAS100", "SP500", "UK100", "US30"):
                reason = "repo 僅有日線 YFinance 資料（15m 最多僅 60 天，未達 3 年門檻）且 Dukascopy 不可用"
            skipped.append({"symbol": sym, "market": mkt, "reason": reason})
            continue

        dt_start = datetime.strptime(bars_15m[0].ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        dt_end = datetime.strptime(bars_15m[-1].ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        span_years = (dt_end - dt_start).total_seconds() / (86400.0 * 365.25)
        if span_years < MIN_HISTORY_YEARS:
            skipped.append({
                "symbol": sym,
                "market": mkt,
                "reason": f"歷史資料僅 {span_years:.2f} 年，未達 3 年門檻",
            })
            continue

        loaded[sym] = {
            "symbol": sym,
            "market": mkt,
            "session": meta["session"],
            "bars": bars_15m,
            "span_years": round(span_years, 2),
            "start_utc": bars_15m[0].ts_utc,
            "end_utc": bars_15m[-1].ts_utc,
            "spread_bps": spread_bps,
            "fee_rate": fee_rate,
            "slippage_bps": default_slip_bps,
            "max_leverage": max_lev,
            "source_note": source_note,
        }

    return loaded, skipped


# ---------------------------------------------------------------------------
# Precomputed Indicators (Strict No-Lookahead)
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


@dataclass
class SymbolSeries:
    symbol: str
    market: str
    session: str
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
    bb_bw_rank_100: np.ndarray  # percentile [0, 1] among trailing 100 bars
    h1_close: np.ndarray        # latest CLOSED 1h bar close at 15m bar i close
    h1_ema50: np.ndarray        # latest CLOSED 1h bar EMA50 at 15m bar i close
    spread_bps: float
    fee_rate: float
    slippage_bps: float
    max_leverage: float


def precompute_symbol_series(info: dict[str, Any]) -> SymbolSeries:
    bars: list[OHLCVBar] = info["bars"]
    sym = info["symbol"]
    mkt = info["market"]
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

    # Wilder ATR(14) matching src/indicators/technical.py
    atr_list = compute_atr_series(bars, period=14, market=mkt, interval="15m")
    atr14 = np.full(n, np.nan, dtype=np.float64)
    if atr_list:
        atr14[14 : 14 + len(atr_list)] = np.array(atr_list, dtype=np.float64)

    # Bollinger Bands (20, 2) population std matching technical.py
    bb_mid = np.full(n, np.nan, dtype=np.float64)
    bb_upper = np.full(n, np.nan, dtype=np.float64)
    bb_lower = np.full(n, np.nan, dtype=np.float64)
    bb_bw = np.full(n, np.nan, dtype=np.float64)
    bb_bw_rank_100 = np.full(n, np.nan, dtype=np.float64)

    # Rolling mean & std via cumulative sums for O(N) speed
    csum = np.cumsum(np.insert(closes, 0, 0.0))
    csum2 = np.cumsum(np.insert(closes * closes, 0, 0.0))
    idx_end = np.arange(20, n + 1)
    means = (csum[idx_end] - csum[idx_end - 20]) / 20.0
    means2 = (csum2[idx_end] - csum2[idx_end - 20]) / 20.0
    vars_ = np.maximum(0.0, means2 - means * means)
    stds = np.sqrt(vars_)

    bb_mid[19:] = means
    bb_upper[19:] = means + 2.0 * stds
    bb_lower[19:] = means - 2.0 * stds
    with np.errstate(divide="ignore", invalid="ignore"):
        bb_bw[19:] = np.where(means > 1e-12, (4.0 * stds) / means, np.nan)

    # Rank of bb_bw[i] within last 100 bars [i-99..i]
    for i in range(118, n):
        cur = bb_bw[i]
        if math.isnan(cur):
            continue
        win = bb_bw[i - 99 : i + 1]
        # Fraction <= cur (10th percentile -> <= 0.10)
        bb_bw_rank_100[i] = float(np.sum(win <= cur)) / 100.0

    # 1h EMA50 mapped strictly from CLOSED 1h bars (1h close_sec <= 15m bar close_sec)
    last_dt = datetime.fromtimestamp(ts_sec[-1] + 900, tz=timezone.utc)
    bars_1h = resample_15m_to_1h(bars, now_utc=last_dt)
    h1_close = np.full(n, np.nan, dtype=np.float64)
    h1_ema50 = np.full(n, np.nan, dtype=np.float64)
    if len(bars_1h) >= 50:
        h1_c = np.array([b.close for b in bars_1h], dtype=np.float64)
        h1_e50 = _ema_numpy(h1_c, 50)
        h1_close_sec = np.array(
            [
                datetime.strptime(b.ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() + 3600.0
                for b in bars_1h
            ],
            dtype=np.float64,
        )
        # For each 15m bar i closing at ts_sec[i] + 900, find latest closed 1h bar
        bar15_close_sec = ts_sec + 900.0
        pos = np.searchsorted(h1_close_sec, bar15_close_sec, side="right") - 1
        valid = pos >= 0
        h1_close[valid] = h1_c[pos[valid]]
        h1_ema50[valid] = h1_e50[pos[valid]]

    return SymbolSeries(
        symbol=sym,
        market=mkt,
        session=info["session"],
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
        h1_close=h1_close,
        h1_ema50=h1_ema50,
        spread_bps=info["spread_bps"],
        fee_rate=info["fee_rate"],
        slippage_bps=info["slippage_bps"],
        max_leverage=info["max_leverage"],
    )


# ---------------------------------------------------------------------------
# Signal Generation (Per Strategy, Per Symbol)
# ---------------------------------------------------------------------------

@dataclass
class SignalCandidate:
    symbol: str
    strategy_id: str
    signal_bar_idx: int   # closed bar i emitting signal
    entry_bar_idx: int    # bar i+1 where entry occurs at open
    ts_utc: str           # entry bar timestamp
    direction: str        # "long" or "short"
    raw_open: float       # bar[i+1].open
    sl_px: float
    tp_px: float
    sl_dist_pct: float    # |entry_est - sl_px| / entry_est


def _opening_range_session_key_and_minute(ts_s: float, session: str) -> tuple[str, int]:
    """Return (session_date_str, minutes_since_session_open) for Strategy B."""
    dt_utc = datetime.fromtimestamp(ts_s, tz=timezone.utc)
    if session == "utc0":
        return dt_utc.strftime("%Y-%m-%d"), dt_utc.hour * 60 + dt_utc.minute
    if session == "london":
        dt_loc = dt_utc.astimezone(LONDON_TZ)
        open_dt = dt_loc.replace(hour=8, minute=0, second=0, microsecond=0)
        if dt_loc < open_dt:
            prev = open_dt - timedelta(days=1)
            return prev.strftime("%Y-%m-%d"), int((dt_loc - prev).total_seconds() // 60)
        return open_dt.strftime("%Y-%m-%d"), int((dt_loc - open_dt).total_seconds() // 60)
    # New York 09:30 ET
    dt_loc = dt_utc.astimezone(NY_TZ)
    open_dt = dt_loc.replace(hour=9, minute=30, second=0, microsecond=0)
    if dt_loc < open_dt:
        prev = open_dt - timedelta(days=1)
        return prev.strftime("%Y-%m-%d"), int((dt_loc - prev).total_seconds() // 60)
    return open_dt.strftime("%Y-%m-%d"), int((dt_loc - open_dt).total_seconds() // 60)


def generate_signals_for_symbol(
    ser: SymbolSeries,
    strategy_id: str,
    params: dict[str, Any],
    start_idx: int,
    end_idx: int,
) -> list[SignalCandidate]:
    """Generate all non-lookahead 15m signal candidates on bar i in [start_idx, end_idx)."""
    out: list[SignalCandidate] = []
    n = ser.n
    hi_bound = min(end_idx, n - 1)

    opens = ser.opens
    highs = ser.highs
    lows = ser.lows
    closes = ser.closes
    ema20 = ser.ema20
    atr14 = ser.atr14

    if strategy_id == "A":
        k = float(params["dist_atr"])
        sl_buf = float(params["sl_buf_atr"])
        min_tp_r = float(params["min_tp_r"])
        for i in range(max(25, start_idx), hi_bound):
            atr = atr14[i]
            e20 = ema20[i]
            if math.isnan(atr) or atr <= 0 or math.isnan(e20):
                continue
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            po, pc = opens[i - 1], closes[i - 1]
            body = abs(c - o)
            pbody = abs(pc - po)
            rng = h - l
            if rng <= 0:
                continue

            next_open = opens[i + 1]
            if next_open <= 0:
                continue

            # Long reversal (oversold below EMA20)
            max_below = max(e20 - l, e20 - lows[i - 1])
            if max_below > k * atr and c < e20:
                engulf_bull = (pc < po) and (c > o) and (o <= pc) and (c >= po) and (body > pbody)
                lower_wick = min(o, c) - l
                pin_bull = (c > o) and (lower_wick >= 2.0 * max(body, 0.05 * atr)) and (lower_wick >= 0.5 * rng)
                if engulf_bull or pin_bull:
                    swing_low = min(l, lows[i - 1], lows[i - 2])
                    sl_px = swing_low - sl_buf * atr
                    risk_est = next_open - sl_px
                    if risk_est > 1e-9:
                        tp_px = e20 if (e20 - next_open) >= min_tp_r * risk_est else (next_open + min_tp_r * risk_est)
                        out.append(SignalCandidate(
                            symbol=ser.symbol, strategy_id="A",
                            signal_bar_idx=i, entry_bar_idx=i + 1, ts_utc=ser.ts_str[i + 1],
                            direction="long", raw_open=next_open, sl_px=sl_px, tp_px=tp_px,
                            sl_dist_pct=risk_est / next_open * 100.0,
                        ))
                        continue

            # Short reversal (overbought above EMA20)
            max_above = max(h - e20, highs[i - 1] - e20)
            if max_above > k * atr and c > e20:
                engulf_bear = (pc > po) and (c < o) and (o >= pc) and (c <= po) and (body > pbody)
                upper_wick = h - max(o, c)
                pin_bear = (c < o) and (upper_wick >= 2.0 * max(body, 0.05 * atr)) and (upper_wick >= 0.5 * rng)
                if engulf_bear or pin_bear:
                    swing_high = max(h, highs[i - 1], highs[i - 2])
                    sl_px = swing_high + sl_buf * atr
                    risk_est = sl_px - next_open
                    if risk_est > 1e-9:
                        tp_px = e20 if (next_open - e20) >= min_tp_r * risk_est else (next_open - min_tp_r * risk_est)
                        out.append(SignalCandidate(
                            symbol=ser.symbol, strategy_id="A",
                            signal_bar_idx=i, entry_bar_idx=i + 1, ts_utc=ser.ts_str[i + 1],
                            direction="short", raw_open=next_open, sl_px=sl_px, tp_px=tp_px,
                            sl_dist_pct=risk_est / next_open * 100.0,
                        ))

    elif strategy_id == "B":
        max_rng_atr = float(params["max_range_atr"])
        tp_r = float(params["tp_r"])
        # Track opening range per session_date
        sessions: dict[str, dict[str, Any]] = {}
        for i in range(max(20, start_idx), hi_bound):
            atr = atr14[i]
            if math.isnan(atr) or atr <= 0:
                continue
            sess_date, mins = _opening_range_session_key_and_minute(ser.ts_sec[i], ser.session)
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
                if st["or_bars"] < 3:
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
                        out.append(SignalCandidate(
                            symbol=ser.symbol, strategy_id="B",
                            signal_bar_idx=i, entry_bar_idx=i + 1, ts_utc=ser.ts_str[i + 1],
                            direction="long", raw_open=next_open, sl_px=sl_px,
                            tp_px=next_open + tp_r * risk_est,
                            sl_dist_pct=risk_est / next_open * 100.0,
                        ))
                elif c < st["or_lo"]:
                    sl_px = st["or_hi"]
                    risk_est = sl_px - next_open
                    if risk_est > 1e-9:
                        st["traded"] = True
                        out.append(SignalCandidate(
                            symbol=ser.symbol, strategy_id="B",
                            signal_bar_idx=i, entry_bar_idx=i + 1, ts_utc=ser.ts_str[i + 1],
                            direction="short", raw_open=next_open, sl_px=sl_px,
                            tp_px=next_open - tp_r * risk_est,
                            sl_dist_pct=risk_est / next_open * 100.0,
                        ))

    elif strategy_id == "C":
        zone_atr = float(params["zone_atr"])
        sl_buf = float(params["sl_buf_atr"])
        tp_r = float(params["tp_r"])
        h1_c = ser.h1_close
        h1_e50 = ser.h1_ema50
        for i in range(max(200, start_idx), hi_bound):
            atr = atr14[i]
            e20 = ema20[i]
            hc = h1_c[i]
            he = h1_e50[i]
            if math.isnan(atr) or atr <= 0 or math.isnan(e20) or math.isnan(hc) or math.isnan(he):
                continue
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            next_open = opens[i + 1]
            if next_open <= 0:
                continue

            if hc > he:
                # Uptrend: pullback touched EMA20 +/- zone_atr * ATR and bullish close
                touched_zone = (l <= e20 + zone_atr * atr) and (c >= e20 - zone_atr * atr)
                bull_close = (c > o) and (c > closes[i - 1])
                if touched_zone and bull_close:
                    sl_px = min(l, lows[i - 1], lows[i - 2]) - sl_buf * atr
                    risk_est = next_open - sl_px
                    if risk_est > 1e-9:
                        out.append(SignalCandidate(
                            symbol=ser.symbol, strategy_id="C",
                            signal_bar_idx=i, entry_bar_idx=i + 1, ts_utc=ser.ts_str[i + 1],
                            direction="long", raw_open=next_open, sl_px=sl_px,
                            tp_px=next_open + tp_r * risk_est,
                            sl_dist_pct=risk_est / next_open * 100.0,
                        ))
            elif hc < he:
                # Downtrend: pullback touched EMA20 +/- zone_atr * ATR and bearish close
                touched_zone = (h >= e20 - zone_atr * atr) and (c <= e20 + zone_atr * atr)
                bear_close = (c < o) and (c < closes[i - 1])
                if touched_zone and bear_close:
                    sl_px = max(h, highs[i - 1], highs[i - 2]) + sl_buf * atr
                    risk_est = sl_px - next_open
                    if risk_est > 1e-9:
                        out.append(SignalCandidate(
                            symbol=ser.symbol, strategy_id="C",
                            signal_bar_idx=i, entry_bar_idx=i + 1, ts_utc=ser.ts_str[i + 1],
                            direction="short", raw_open=next_open, sl_px=sl_px,
                            tp_px=next_open - tp_r * risk_est,
                            sl_dist_pct=risk_est / next_open * 100.0,
                        ))

    elif strategy_id == "D":
        bw_pct = float(params["bw_pct"])
        tp_r = float(params["tp_r"])
        rank100 = ser.bb_bw_rank_100
        mid = ser.bb_mid
        upper = ser.bb_upper
        lower = ser.bb_lower
        for i in range(max(120, start_idx), hi_bound):
            r_prev = rank100[i - 1]
            r_prev2 = rank100[i - 2]
            m_val = mid[i]
            u_val = upper[i]
            l_val = lower[i]
            if math.isnan(r_prev) or math.isnan(m_val) or math.isnan(u_val) or math.isnan(l_val):
                continue
            # Squeeze occurred right before breakout (bar i-1 or i-2 in bottom bw_pct)
            squeezed = (r_prev <= bw_pct) or (not math.isnan(r_prev2) and r_prev2 <= bw_pct)
            if not squeezed:
                continue

            c = closes[i]
            pc = closes[i - 1]
            next_open = opens[i + 1]
            if next_open <= 0:
                continue

            if c > u_val and pc <= upper[i - 1]:
                sl_px = m_val
                risk_est = next_open - sl_px
                if risk_est > 1e-9:
                    out.append(SignalCandidate(
                        symbol=ser.symbol, strategy_id="D",
                        signal_bar_idx=i, entry_bar_idx=i + 1, ts_utc=ser.ts_str[i + 1],
                        direction="long", raw_open=next_open, sl_px=sl_px,
                        tp_px=next_open + tp_r * risk_est,
                        sl_dist_pct=risk_est / next_open * 100.0,
                    ))
            elif c < l_val and pc >= lower[i - 1]:
                sl_px = m_val
                risk_est = sl_px - next_open
                if risk_est > 1e-9:
                    out.append(SignalCandidate(
                        symbol=ser.symbol, strategy_id="D",
                        signal_bar_idx=i, entry_bar_idx=i + 1, ts_utc=ser.ts_str[i + 1],
                        direction="short", raw_open=next_open, sl_px=sl_px,
                        tp_px=next_open - tp_r * risk_est,
                        sl_dist_pct=risk_est / next_open * 100.0,
                    ))

    return out


# ---------------------------------------------------------------------------
# Trade Outcome Simulator (Single Trade Execution & Costs)
# ---------------------------------------------------------------------------

@dataclass
class SimTrade:
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
    exit_reason: str  # "sl", "tp", "time_stop"


def simulate_single_candidate(
    ser: SymbolSeries,
    cand: SignalCandidate,
    end_idx_limit: int,
    spread_mult: float = 1.0,
) -> SimTrade | None:
    """Simulate a single candidate trade from entry_bar_idx up to MAX_HOLD_BARS=16."""
    eb = cand.entry_bar_idx
    if eb >= min(ser.n, end_idx_limit):
        return None

    spread_bps = ser.spread_bps * spread_mult
    slip = ser.slippage_bps / 10000.0
    fee = ser.fee_rate
    direction = cand.direction

    raw_entry = (
        ser.opens[eb] * (1.0 + spread_bps / 20000.0) * (1.0 + slip)
        if direction == "long"
        else ser.opens[eb] * (1.0 - spread_bps / 20000.0) * (1.0 - slip)
    )
    if raw_entry <= 0:
        return None

    sl_px = cand.sl_px
    tp_px = cand.tp_px

    # Check SL validity after entry slippage/spread
    risk_per_unit = (raw_entry - sl_px) if direction == "long" else (sl_px - raw_entry)
    if risk_per_unit <= 1e-9:
        return None

    max_b = min(eb + MAX_HOLD_BARS, min(ser.n, end_idx_limit))
    if max_b <= eb:
        return None

    raw_exit = 0.0
    exit_bidx = max_b - 1
    exit_reason = "time_stop"

    for bidx in range(eb, max_b):
        o = ser.opens[bidx]
        h = ser.highs[bidx]
        l = ser.lows[bidx]
        c = ser.closes[bidx]

        # Conservative SL check FIRST; if both SL & TP hit on same bar -> SL
        if direction == "long":
            open_bid = o * (1.0 - spread_bps / 20000.0)
            if open_bid <= sl_px:
                raw_exit = open_bid * (1.0 - slip)
                exit_bidx = bidx
                exit_reason = "sl"
                break
            if l * (1.0 - spread_bps / 20000.0) <= sl_px:
                raw_exit = sl_px * (1.0 - slip)
                exit_bidx = bidx
                exit_reason = "sl"
                break
            if h * (1.0 - spread_bps / 20000.0) >= tp_px:
                raw_exit = tp_px * (1.0 - slip)
                exit_bidx = bidx
                exit_reason = "tp"
                break
        else:
            open_ask = o * (1.0 + spread_bps / 20000.0)
            if open_ask >= sl_px:
                raw_exit = open_ask * (1.0 + slip)
                exit_bidx = bidx
                exit_reason = "sl"
                break
            if h * (1.0 + spread_bps / 20000.0) >= sl_px:
                raw_exit = sl_px * (1.0 + slip)
                exit_bidx = bidx
                exit_reason = "sl"
                break
            if l * (1.0 + spread_bps / 20000.0) <= tp_px:
                raw_exit = tp_px * (1.0 + slip)
                exit_bidx = bidx
                exit_reason = "tp"
                break

        # Reached 16th bar without SL/TP -> close at bar close
        if bidx == eb + MAX_HOLD_BARS - 1:
            raw_exit = (
                c * (1.0 - spread_bps / 20000.0) * (1.0 - slip)
                if direction == "long"
                else c * (1.0 + spread_bps / 20000.0) * (1.0 + slip)
            )
            exit_bidx = bidx
            exit_reason = "time_stop"
            break
    else:
        # Truncated at boundary before 16 bars -> do not count partial future trade at IS/OOS cut
        return None

    gross_per_unit = (raw_exit - raw_entry) if direction == "long" else (raw_entry - raw_exit)
    fee_per_unit = (raw_entry + raw_exit) * fee
    net_per_unit = gross_per_unit - fee_per_unit
    r_mult = net_per_unit / risk_per_unit

    return SimTrade(
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
# Metrics & Portfolio Sequencing (1 Position At A Time)
# ---------------------------------------------------------------------------

def compute_trade_metrics(trades: list[SimTrade], risk_pct: float = 1.0) -> dict[str, Any]:
    """Compute trade count, win rate, avg win/loss R, expectancy R, PF, max DD%, max consec losses."""
    n = len(trades)
    if n == 0:
        return {
            "trades": 0,
            "win_rate_pct": 0.0,
            "avg_win_r": 0.0,
            "avg_loss_r": 0.0,
            "expectancy_r": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_pct": 0.0,
            "max_consec_losses": 0,
            "total_r": 0.0,
        }

    ordered = sorted(trades, key=lambda t: (t.exit_sec, t.entry_sec, t.symbol))
    wins = [t.r_multiple for t in ordered if t.r_multiple > 0]
    losses = [t.r_multiple for t in ordered if t.r_multiple <= 0]

    win_rate = len(wins) / n * 100.0
    avg_win_r = sum(wins) / len(wins) if wins else 0.0
    avg_loss_r = sum(losses) / len(losses) if losses else 0.0  # negative or 0
    exp_r = sum(t.r_multiple for t in ordered) / n

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 1e-9 else (99.0 if gross_win > 0 else 0.0)

    # Max consecutive losses & compounded DD (at risk_pct per 1R)
    consec = 0
    max_consec = 0
    eq = 10000.0
    peak = 10000.0
    max_dd = 0.0
    for t in ordered:
        if t.r_multiple <= 0:
            consec += 1
            if consec > max_consec:
                max_consec = consec
        else:
            consec = 0
        pnl = eq * (risk_pct / 100.0) * t.r_multiple
        eq = max(1.0, eq + pnl)
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100.0
        if dd > max_dd:
            max_dd = dd

    return {
        "trades": n,
        "win_rate_pct": round(win_rate, 2),
        "avg_win_r": round(avg_win_r, 3),
        "avg_loss_r": round(avg_loss_r, 3),
        "expectancy_r": round(exp_r, 4),
        "profit_factor": round(pf, 3),
        "max_drawdown_pct": round(max_dd, 2),
        "max_consec_losses": max_consec,
        "total_r": round(sum(t.r_multiple for t in ordered), 2),
    }


def select_single_position_sequence(
    all_trades: list[SimTrade],
    rank_score_by_key: dict[tuple[str, str], float],
) -> list[SimTrade]:
    """Filter simulated trades to 1 open position at a time using IS ranking rule."""
    if not all_trades:
        return []
    by_entry: dict[float, list[SimTrade]] = defaultdict(list)
    for t in all_trades:
        by_entry[t.entry_sec].append(t)

    selected: list[SimTrade] = []
    busy_until_sec = -1.0
    for entry_sec in sorted(by_entry.keys()):
        if entry_sec <= busy_until_sec:
            continue
        batch = by_entry[entry_sec]
        # Highest IS expectancy R for (strategy, symbol), tie-break smallest sl_dist_pct
        best = max(
            batch,
            key=lambda t: (rank_score_by_key.get((t.strategy_id, t.symbol), -999.0), -t.sl_dist_pct),
        )
        selected.append(best)
        busy_until_sec = best.exit_sec
    return selected


# ---------------------------------------------------------------------------
# 1-Week Rolling Contest Simulator (OOS)
# ---------------------------------------------------------------------------

def simulate_weekly_contest_oos(
    trades_seq: list[SimTrade],
    series_map: dict[str, SymbolSeries],
    oos_start_sec: float,
    oos_end_sec: float,
    risk_pct: float = 4.5,
    daily_loss_limit_pct: float = 5.0,
    bust_equity: float = 9000.0,
    start_capital: float = 10000.0,
    step_days: int = 7,
) -> dict[str, Any]:
    """Run rolling 7-day contest windows over OOS period."""
    week_sec = 7 * 86400.0
    step_sec = step_days * 86400.0

    # Pre-index trades by entry_sec
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

            # Position sizing: 4.5% risk of current equity, capped by instrument max_leverage
            risk_usd = eq * (risk_pct / 100.0)
            sl_frac = tr.sl_dist_pct / 100.0
            max_lev = series_map[tr.symbol].max_leverage if tr.symbol in series_map else 10.0
            max_notional = eq * max_lev
            planned_notional = risk_usd / max(1e-9, sl_frac)
            if planned_notional > max_notional:
                # Cap leverage, scaling actual risk_usd down proportionally
                effective_risk_usd = max_notional * sl_frac
            else:
                effective_risk_usd = risk_usd

            pnl = effective_risk_usd * tr.r_multiple
            # Worst-case intra-trade check: if SL hit or negative excursion pushes eq < bust_equity
            # Also check worst intra-trade equity if SL was hit before TP/close
            worst_trade_eq = eq - effective_risk_usd if tr.exit_reason == "sl" else eq + min(0.0, pnl)
            if worst_trade_eq < bust_equity:
                eq = worst_trade_eq
                busted = True
                break

            eq += pnl
            if eq < bust_equity:
                busted = True
                break

            # Check if UTC day equity dropped >= 5% from day_start_eq
            exit_day = datetime.fromtimestamp(tr.exit_sec, tz=timezone.utc).strftime("%Y-%m-%d")
            if exit_day != cur_day_key:
                cur_day_key = exit_day
                day_start_eq = eq - pnl  # start of that UTC day approximated cleanly
            if eq <= day_start_eq * (1.0 - daily_loss_limit_pct / 100.0):
                stopped_day_key = exit_day

        if busted:
            bust_count += 1
        ret_pct = (eq - start_capital) / start_capital * 100.0
        returns_pct.append(ret_pct)
        cur_start += step_sec

    if not returns_pct:
        return {
            "weeks": 0,
            "median_return_pct": 0.0,
            "p90_return_pct": 0.0,
            "mean_return_pct": 0.0,
            "bust_rate_pct": 0.0,
        }

    arr = np.array(returns_pct, dtype=np.float64)
    return {
        "weeks": total_weeks,
        "median_return_pct": round(float(np.median(arr)), 2),
        "p90_return_pct": round(float(np.percentile(arr, 90)), 2),
        "mean_return_pct": round(float(np.mean(arr)), 2),
        "bust_rate_pct": round(bust_count / total_weeks * 100.0, 2),
    }


# ---------------------------------------------------------------------------
# Main Experiment Pipeline
# ---------------------------------------------------------------------------

def run_research() -> dict[str, Any]:
    config = load_config()
    loaded_data, skipped_symbols = load_universe_data(config)

    print(f"Loaded symbols ({len(loaded_data)}): {list(loaded_data.keys())}")
    print(f"Skipped symbols ({len(skipped_symbols)}): {[s['symbol'] for s in skipped_symbols]}")

    series_map: dict[str, SymbolSeries] = {}
    split_idx_map: dict[str, int] = {}
    for sym, info in loaded_data.items():
        ser = precompute_symbol_series(info)
        series_map[sym] = ser
        split_idx_map[sym] = int(ser.n * IS_RATIO)

    oos_starts = [series_map[s].ts_sec[split_idx_map[s]] for s in series_map]
    oos_ends = [series_map[s].ts_sec[-1] for s in series_map]
    global_oos_start_sec = max(oos_starts)
    global_oos_end_sec = min(oos_ends)

    strategy_summaries: dict[str, Any] = {}
    passed_strategies: list[str] = []

    for strat_id in ("A", "B", "C", "D"):
        candidates_cfg = PARAM_GRID[strat_id]
        param_evals: list[dict[str, Any]] = []

        for pcfg in candidates_cfg:
            # 1) Generate & simulate IS trades per symbol (unconstrained per symbol)
            is_raw_trades_by_sym: dict[str, list[SimTrade]] = {}
            is_all_trades: list[SimTrade] = []
            is_rank_map: dict[tuple[str, str], float] = {}

            for sym, ser in series_map.items():
                cut = split_idx_map[sym]
                cands = generate_signals_for_symbol(ser, strat_id, pcfg, 0, cut)
                tr_list: list[SimTrade] = []
                for c in cands:
                    st = simulate_single_candidate(ser, c, cut, spread_mult=1.0)
                    if st is not None:
                        tr_list.append(st)
                is_raw_trades_by_sym[sym] = tr_list
                is_all_trades.extend(tr_list)
                sym_m = compute_trade_metrics(tr_list)
                is_rank_map[(strat_id, sym)] = sym_m["expectancy_r"]

            # 2) Portfolio 1-position-at-a-time selection on IS
            is_seq = select_single_position_sequence(is_all_trades, is_rank_map)
            is_port_metrics = compute_trade_metrics(is_seq)
            param_evals.append({
                "params": pcfg,
                "is_port_metrics": is_port_metrics,
                "is_rank_map": {f"{k[0]}|{k[1]}": v for k, v in is_rank_map.items()},
                "is_raw_by_sym": {sym: compute_trade_metrics(tl) for sym, tl in is_raw_trades_by_sym.items()},
            })

        # Select best parameter set purely on IS (highest IS PF if >= 50 IS trades, else highest total_r)
        best_eval = max(
            param_evals,
            key=lambda e: (
                1 if e["is_port_metrics"]["trades"] >= 50 else 0,
                e["is_port_metrics"]["profit_factor"] if e["is_port_metrics"]["trades"] >= 50 else -999.0,
                e["is_port_metrics"]["total_r"],
            ),
        )
        best_params = best_eval["params"]
        frozen_rank_map: dict[tuple[str, str], float] = {
            (strat_id, sym): best_eval["is_raw_by_sym"][sym]["expectancy_r"]
            for sym in series_map
        }

        # Re-run IS normal + IS stress (spread x 1.5), and frozen OOS normal + OOS stress (spread x 1.5)
        def _eval_split(split_name: str, spread_mult: float) -> tuple[dict[str, Any], dict[str, Any], list[SimTrade]]:
            all_tr: list[SimTrade] = []
            by_sym_metrics: dict[str, Any] = {}
            for sym, ser in series_map.items():
                cut = split_idx_map[sym]
                s_idx = 0 if split_name == "IS" else cut
                e_idx = cut if split_name == "IS" else ser.n
                cands = generate_signals_for_symbol(ser, strat_id, best_params, s_idx, e_idx)
                tr_list: list[SimTrade] = []
                for c in cands:
                    st = simulate_single_candidate(ser, c, e_idx, spread_mult=spread_mult)
                    if st is not None:
                        tr_list.append(st)
                all_tr.extend(tr_list)
                by_sym_metrics[sym] = compute_trade_metrics(tr_list)
            seq = select_single_position_sequence(all_tr, frozen_rank_map)
            return compute_trade_metrics(seq), by_sym_metrics, seq

        is_norm_m, is_by_sym, is_seq_norm = _eval_split("IS", 1.0)
        is_stress_m, _, _ = _eval_split("IS", 1.5)
        oos_norm_m, oos_by_sym, oos_seq_norm = _eval_split("OOS", 1.0)
        oos_stress_m, oos_stress_by_sym, _ = _eval_split("OOS", 1.5)

        # Diagnostic: zero-cost OOS (spread=0, fee=0, slip=0) to separate edge vs friction
        oos_zero_all: list[SimTrade] = []
        for sym, ser in series_map.items():
            ser_z = SymbolSeries(**{**ser.__dict__, "spread_bps": 0.0, "fee_rate": 0.0, "slippage_bps": 0.0})
            cut = split_idx_map[sym]
            for c in generate_signals_for_symbol(ser_z, strat_id, best_params, cut, ser_z.n):
                st = simulate_single_candidate(ser_z, c, ser_z.n, spread_mult=1.0)
                if st is not None:
                    oos_zero_all.append(st)
        oos_zero_m = compute_trade_metrics(select_single_position_sequence(oos_zero_all, frozen_rank_map))

        # Pass threshold:
        # OOS trades >= 100, OOS PF >= 1.2, OOS expectancy > 0, OOS stress PF >= 1.05
        passed = (
            oos_norm_m["trades"] >= 100
            and oos_norm_m["profit_factor"] >= 1.2
            and oos_norm_m["expectancy_r"] > 0.0
            and oos_stress_m["profit_factor"] >= 1.05
        )
        if passed:
            passed_strategies.append(strat_id)

        # Weekly contest simulation on OOS (both 7-day step and 1-day rolling step)
        weekly_7d = simulate_weekly_contest_oos(
            oos_seq_norm, series_map, global_oos_start_sec, global_oos_end_sec, step_days=7
        )
        weekly_1d = simulate_weekly_contest_oos(
            oos_seq_norm, series_map, global_oos_start_sec, global_oos_end_sec, step_days=1
        )

        strategy_summaries[strat_id] = {
            "strategy_id": strat_id,
            "title": STRATEGY_TITLES[strat_id],
            "best_params": best_params,
            "param_candidates_is": [
                {"params": e["params"], "is_metrics": e["is_port_metrics"]} for e in param_evals
            ],
            "is_normal": is_norm_m,
            "is_stress": is_stress_m,
            "oos_normal": oos_norm_m,
            "oos_stress": oos_stress_m,
            "oos_zero_cost": oos_zero_m,
            "is_by_symbol": is_by_sym,
            "oos_by_symbol": oos_by_sym,
            "oos_stress_by_symbol": oos_stress_by_sym,
            "passed": passed,
            "weekly_contest_7d_step": weekly_7d,
            "weekly_contest_1d_step": weekly_1d,
            "_oos_all_raw_trades": oos_seq_norm,
        }

    # Combined multi-strategy contest portfolio (if >= 1 strategy or across all 4 using IS rank tie-break)
    combined_oos_raw: list[SimTrade] = []
    combined_rank_map: dict[tuple[str, str], float] = {}
    source_strats = passed_strategies if passed_strategies else ["A", "B", "C", "D"]
    for sid in source_strats:
        best_p = strategy_summaries[sid]["best_params"]
        for sym, ser in series_map.items():
            combined_rank_map[(sid, sym)] = strategy_summaries[sid]["is_by_symbol"][sym]["expectancy_r"]
            cut = split_idx_map[sym]
            for c in generate_signals_for_symbol(ser, sid, best_p, cut, ser.n):
                st = simulate_single_candidate(ser, c, ser.n, spread_mult=1.0)
                if st is not None:
                    combined_oos_raw.append(st)

    combined_seq = select_single_position_sequence(combined_oos_raw, combined_rank_map)
    combined_weekly_7d = simulate_weekly_contest_oos(
        combined_seq, series_map, global_oos_start_sec, global_oos_end_sec, step_days=7
    )
    combined_weekly_1d = simulate_weekly_contest_oos(
        combined_seq, series_map, global_oos_start_sec, global_oos_end_sec, step_days=1
    )

    # Strip non-JSON-serializable raw trades list before saving
    for sid in strategy_summaries:
        strategy_summaries[sid].pop("_oos_all_raw_trades", None)

    result = {
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
                "spread_bps": info["spread_bps"],
                "fee_rate": info["fee_rate"],
                "source_note": info["source_note"],
            }
            for s, info in loaded_data.items()
        },
        "skipped_symbols": skipped_symbols,
        "global_oos_start_utc": datetime.fromtimestamp(global_oos_start_sec, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "global_oos_end_utc": datetime.fromtimestamp(global_oos_end_sec, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "passed_strategies": passed_strategies,
        "strategies": strategy_summaries,
        "combined_oos_metrics": compute_trade_metrics(combined_seq),
        "combined_weekly_7d": combined_weekly_7d,
        "combined_weekly_1d": combined_weekly_1d,
    }

    out_json = Path(__file__).resolve().parent / "results.json"
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    res = run_research()
    print("\n=== OOS SUMMARY ===")
    for sid, sdata in res["strategies"].items():
        m = sdata["oos_normal"]
        st = sdata["oos_stress"]
        w7 = sdata["weekly_contest_7d_step"]
        print(
            f"{sid} ({sdata['best_params']['name']}): trades={m['trades']}, WR={m['win_rate_pct']}%, "
            f"expR={m['expectancy_r']}, PF={m['profit_factor']}, stressPF={st['profit_factor']}, "
            f"PASSED={sdata['passed']} | 1w_med={w7['median_return_pct']}%, p90={w7['p90_return_pct']}%, bust={w7['bust_rate_pct']}%"
        )
    print(f"\nPassed strategies: {res['passed_strategies']}")
