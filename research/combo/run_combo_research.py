#!/usr/bin/env python3
"""「截長補短」組合與改良策略研究 (Synergistic Combo & Refinement Strategy Research).

Strictly adheres to the anti-overfitting protocol:
  1. Three-way chronological split:
     - Dev (開發段, earliest ~60%)
     - Val (驗證段, middle ~25%)
     - Lockbox (鎖定段, final ~15%, >= 1 year)
  2. All ideas (Groups 1 to 6) are developed on Dev (at most 3 parameter sets per idea,
     with explicit rationale and neighbor parameter sensitivity reported).
  3. Only ideas that beat Original L2 (or Original L1) in both Dev and Val enter the
     candidate pool; at most 5 candidates are selected and run ONCE on Lockbox without
     any further parameter tuning.
  4. Reports total combinations tested (M), Deflated Sharpe Ratio (DSR), and Block
     Bootstrap / White's Reality Check p-values for luck estimation.
  5. Evaluates under Project Config costs, The5ers MT5 costs, and 1.5x Cost Stress Test,
     plus Kraken Prop Exam ($10k, 0.75% risk, 1 pos, +9% target) and The5ers 1-Week
     Contest ($10k, 4.5% risk, 1 pos, < $9,000 bust) simulations.
"""

from __future__ import annotations

import copy
import json
import math
import pickle
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.config_loader import load_config
from src.data.models import OHLCVBar
from src.indicators.technical import compute_atr_series, resample_1h_to_4h
from src.strategies.six_strategies import (
    SignalAction,
    _is_l4_week_end,
    _rolling_extrema,
    _rolling_max_inclusive,
    _sma_series_np,
    allocate_subaccount_position,
    compute_volatility_risk_multiplier,
    decide_l3_dual_momentum_step,
    decide_strategy_step,
    precompute_series,
)

# ---------------------------------------------------------------------------
# Chronological 3-Way Splits
# ---------------------------------------------------------------------------

# 10-Year Daily Timeline: 2016-11-17 to 2026-11-15 (~10.0 years)
DAILY_SPLITS: dict[str, tuple[str, str, float]] = {
    "dev": ("2016-11-17T00:00:00Z", "2022-11-17T00:00:00Z", 6.00),      # 60%
    "val": ("2022-11-17T00:00:00Z", "2025-05-17T00:00:00Z", 2.50),      # 25%
    "lockbox": ("2025-05-17T00:00:00Z", "2026-11-16T00:00:00Z", 1.50),  # 15% (>= 1.0 yr)
}

# 5.38-Year Intraday (1h/4h) Timeline: 2021-07-01 to 2026-11-15
INTRADAY_SPLITS: dict[str, tuple[str, str, float]] = {
    "dev": ("2021-07-01T00:00:00Z", "2024-08-16T00:00:00Z", 3.13),      # ~58%
    "val": ("2024-08-16T00:00:00Z", "2025-11-16T00:00:00Z", 1.25),      # ~23%
    "lockbox": ("2025-11-16T00:00:00Z", "2026-11-16T00:00:00Z", 1.00),  # ~19% (1.00 yr)
}

BASELINE_PARAMS: dict[str, dict[str, Any]] = {
    "L1": {"entry_period": 55, "exit_period": 10, "sl_atr_mult": 2.5, "vol_target_pct": 0.012},
    "L2": {"entry_period": 55, "exit_period": 20, "sl_atr_mult": 3.0, "vol_target_pct": 0.015},
    "L3": {"lookback_days": 20, "top_k_crypto": 3, "top_k_index": 1, "rebalance_days": 7, "sl_atr_mult": 3.0, "vol_target_pct": 0.015},
    "L4": {"sma_period": 100, "atr_mult": 5.0, "atr_period": 20, "high_lookback": 60},
    "F4": {"sma_period": 200, "atr_mult": 3.0, "atr_period": 20, "high_lookback": 60},
    "S2": {"breakout_bars": 30, "trail_bars": 10, "sl_atr_mult": 2.5, "max_hold_bars": 30},
}


@dataclass
class SimTradeRecord:
    """Rich trade record supporting segment normalization, scenario sims, and cost modes."""

    symbol: str
    strategy_id: str
    direction: str  # "long" or "short"
    entry_ts_utc: str
    exit_ts_utc: str
    entry_price: float
    exit_price: float
    initial_sl: float
    r_multiple: float
    eq_ret_pct: float  # percentage return on subaccount equity at entry (e.g. +0.025 = +2.5%)
    net_pnl_usd: float
    cost_usd: float
    sl_dist_pct: float
    adx_at_entry: float = 25.0
    roc20_at_entry: float = 0.0


def _parse_dt(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. Data & Cost Loading
# ---------------------------------------------------------------------------

def load_mt5_costs_config() -> dict[str, Any]:
    path = ROOT_DIR / "research" / "contest_15m" / "costs_config.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def build_cost_adjusted_instruments(
    config: dict[str, Any],
    mt5_cfg: dict[str, Any],
    cost_mode: str = "config",  # "config", "mt5", "stress", "mt5_stress"
    bars_1d: dict[str, list[OHLCVBar]] | None = None,
) -> tuple[dict[str, dict[str, Any]], float]:
    """Return instrument dict and slippage_bps for the requested cost mode."""
    risk_cfg = config.get("risk", {})
    base_slip_bps = float(risk_cfg.get("default_slippage_bps", 1.0))
    mt5_insts = mt5_cfg.get("instruments", {})
    mt5_fee = float(mt5_cfg.get("defaults", {}).get("fee_rate_per_side", 0.00001))

    mult = 1.5 if "stress" in cost_mode else 1.0
    use_mt5 = "mt5" in cost_mode

    out: dict[str, dict[str, Any]] = {}
    for inst in config.get("instruments", []):
        sym = inst["symbol"]
        icopy = dict(inst)
        if use_mt5:
            # Map symbol to MT5 spread points
            mt5_key = sym
            if sym.endswith("USDT"):
                mt5_key = sym.replace("USDT", "USD")
            elif sym == "JP225":
                mt5_key = "JPN225"
            bars = (bars_1d or {}).get(sym, [])
            ref_px = bars[len(bars) // 2].close if bars else 100.0
            if mt5_key in mt5_insts and ref_px > 0:
                sp_pts = float(mt5_insts[mt5_key].get("spread_points", 0.0))
                sp_bps = (sp_pts / ref_px) * 10000.0
            else:
                # Typical MT5 crypto/alt or index spread in bps (around 40% of standard exchange spread)
                sp_bps = float(inst.get("assumed_spread_bps", 4.0)) * 0.5
            icopy["assumed_spread_bps"] = sp_bps * mult
            icopy["fee_rate"] = mt5_fee * mult
            icopy["overnight_cost_daily_bps"] = float(inst.get("overnight_cost_daily_bps", 0.0)) * 0.5 * mult
        else:
            icopy["assumed_spread_bps"] = float(inst.get("assumed_spread_bps", 2.0)) * mult
            icopy["fee_rate"] = float(inst.get("fee_rate", 0.0005)) * mult
            icopy["overnight_cost_daily_bps"] = float(inst.get("overnight_cost_daily_bps", 0.0)) * mult
        out[sym] = icopy

    eff_slip = (0.5 if use_mt5 else base_slip_bps) * mult
    return out, eff_slip


def load_cached_datasets(config: dict[str, Any]) -> dict[str, dict[str, list[OHLCVBar]]]:
    """Load 1d and 1h cached datasets from .cache/."""
    cache_1d_path = ROOT_DIR / ".cache" / "research_1d_bars.pkl"
    bars_1d: dict[str, list[OHLCVBar]] = pickle.loads(cache_1d_path.read_bytes())

    bin_dir = ROOT_DIR / ".cache" / "binance_monthly" / "merged"
    duka_dir = ROOT_DIR / ".cache" / "dukascopy" / "merged"
    bars_1h: dict[str, list[OHLCVBar]] = {}

    def _load_bin_1h(bin_sym: str) -> list[OHLCVBar]:
        pkls = sorted(bin_dir.glob(f"{bin_sym}_1h_*.pkl"))
        if not pkls:
            return []
        by_ts: dict[str, OHLCVBar] = {}
        for b in pickle.loads(pkls[-1].read_bytes()):
            by_ts[b.ts_utc] = b
        tails = sorted(bin_dir.glob(f"tail_{bin_sym}_1h_*.pkl"))
        if tails:
            for b in pickle.loads(tails[-1].read_bytes()):
                by_ts[b.ts_utc] = b
        return sorted(by_ts.values(), key=lambda x: x.ts_utc)

    def _load_duka_1h(d_sym: str) -> list[OHLCVBar]:
        p = duka_dir / f"{d_sym}_1h_3y.pkl"
        if not p.exists():
            return []
        raw = pickle.loads(p.read_bytes())
        # Filter zero-volume weekend flat bars
        return [b for b in raw if b.volume > 0 or b.high > b.low]

    gold_backup = str(config.get("backtest", {}).get("gold_backup_symbol", "PAXGUSDT"))
    for inst in config.get("instruments", []):
        if not inst.get("enabled", True):
            continue
        sym = inst["symbol"]
        if inst["market"] == "crypto":
            bcode = inst.get("sources", {}).get("binance_vision", sym)
            b1h = _load_bin_1h(bcode)
            if b1h:
                bars_1h[sym] = b1h
        elif sym == "XAUUSD":
            # Combine PAXGUSDT (2021-07..2023-08) and Dukascopy XAUUSD (2023-09..2026-11)
            paxg = _load_bin_1h(gold_backup)
            duka_xau = _load_duka_1h("XAUUSD")
            by_ts: dict[str, OHLCVBar] = {b.ts_utc: b for b in paxg}
            for b in duka_xau:
                by_ts[b.ts_utc] = b
            if by_ts:
                bars_1h["XAUUSD"] = sorted(by_ts.values(), key=lambda x: x.ts_utc)
        else:
            d_sym = "JPN225" if sym == "JP225" else sym
            d1h = _load_duka_1h(d_sym)
            if d1h:
                bars_1h[sym] = d1h

    return {"1d": bars_1d, "1h": bars_1h}


# ---------------------------------------------------------------------------
# 2. Causal Indicator Precomputation (Daily & 1h)
# ---------------------------------------------------------------------------

def compute_adx_series(bars: list[OHLCVBar], period: int = 14) -> list[float | None]:
    """Compute strictly causal Wilder's ADX(period) series."""
    n = len(bars)
    out: list[float | None] = [None] * n
    if n < 2 * period + 1:
        return out

    tr = np.zeros(n, dtype=np.float64)
    pdm = np.zeros(n, dtype=np.float64)
    mdm = np.zeros(n, dtype=np.float64)

    for i in range(1, n):
        h = bars[i].high
        l = bars[i].low
        pc = bars[i - 1].close
        ph = bars[i - 1].high
        pl = bars[i - 1].low

        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
        up = h - ph
        dn = pl - l
        if up > dn and up > 0:
            pdm[i] = up
        if dn > up and dn > 0:
            mdm[i] = dn

    str_v = float(np.sum(tr[1 : period + 1]))
    spdm = float(np.sum(pdm[1 : period + 1]))
    smdm = float(np.sum(mdm[1 : period + 1]))

    dx_list: list[float] = []
    for i in range(period, n):
        if i > period:
            str_v = str_v - (str_v / period) + tr[i]
            spdm = spdm - (spdm / period) + pdm[i]
            smdm = smdm - (smdm / period) + mdm[i]
        if str_v <= 1e-12:
            dx = 0.0
        else:
            pdi = 100.0 * spdm / str_v
            mdi = 100.0 * smdm / str_v
            denom = pdi + mdi
            dx = 100.0 * abs(pdi - mdi) / denom if denom > 1e-12 else 0.0
        dx_list.append(dx)
        if len(dx_list) == period:
            out[i] = sum(dx_list) / period
        elif len(dx_list) > period:
            prev_adx = out[i - 1] or dx
            out[i] = (prev_adx * (period - 1) + dx) / period

    return out


def compute_atr_pct_percentile(
    bars: list[OHLCVBar],
    atr_series: list[float | None],
    window: int = 252,
) -> list[float | None]:
    """Compute strictly causal rolling percentile (0..100) of ATR/Close over prior `window` bars."""
    n = len(bars)
    out: list[float | None] = [None] * n
    atr_pct: list[float | None] = [
        (atr_series[i] / bars[i].close) if (atr_series[i] is not None and bars[i].close > 0) else None
        for i in range(n)
    ]
    for i in range(n):
        cur = atr_pct[i]
        if cur is None:
            continue
        start = max(0, i - window + 1)
        hist = [v for v in atr_pct[start : i + 1] if v is not None]
        if len(hist) < 30:
            continue
        less_eq = sum(1 for v in hist if v <= cur)
        out[i] = (less_eq / len(hist)) * 100.0
    return out


def compute_causal_weekly_features(
    bars: list[OHLCVBar],
    market: str,
) -> dict[str, list[float | None]]:
    """Compute strictly causal weekly trend series (10w SMA, 20w SMA, 12w ROC) updated only at completed week-end bars."""
    n = len(bars)
    w_sma10: list[float | None] = [None] * n
    w_sma20: list[float | None] = [None] * n
    w_roc12: list[float | None] = [None] * n

    completed_week_closes: list[float] = []
    cur_sma10: float | None = None
    cur_sma20: float | None = None
    cur_roc12: float | None = None

    for i in range(n):
        if _is_l4_week_end(bars, i, market):
            completed_week_closes.append(bars[i].close)
            if len(completed_week_closes) >= 10:
                cur_sma10 = sum(completed_week_closes[-10:]) / 10.0
            if len(completed_week_closes) >= 20:
                cur_sma20 = sum(completed_week_closes[-20:]) / 20.0
            if len(completed_week_closes) >= 13 and completed_week_closes[-13] > 0:
                cur_roc12 = (completed_week_closes[-1] / completed_week_closes[-13]) - 1.0
        w_sma10[i] = cur_sma10
        w_sma20[i] = cur_sma20
        w_roc12[i] = cur_roc12

    return {
        "w_sma10": w_sma10,
        "w_sma20": w_sma20,
        "w_roc12": w_roc12,
    }


def build_enhanced_daily_cache(
    bars_by_sym: dict[str, list[OHLCVBar]],
    instruments_by_sym: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Precompute all daily indicators needed across Groups 1-6 for every symbol."""
    sp500_bars = bars_by_sym.get("SP500", [])
    sp500_closes = np.asarray([b.close for b in sp500_bars], dtype=np.float64) if sp500_bars else np.asarray([])
    sp500_sma150 = _sma_series_np(sp500_closes, 150) if len(sp500_closes) >= 150 else [None] * len(sp500_bars)
    sp500_sma200 = _sma_series_np(sp500_closes, 200) if len(sp500_closes) >= 200 else [None] * len(sp500_bars)

    # Map date prefix YYYY-MM-DD -> latest available SP500 close, sma150, sma200
    sp500_state_by_day: dict[str, tuple[float, float | None, float | None]] = {}
    for idx, b in enumerate(sp500_bars):
        sp500_state_by_day[b.ts_utc[:10]] = (b.close, sp500_sma150[idx], sp500_sma200[idx])

    all_days = sorted({b.ts_utc[:10] for sbars in bars_by_sym.values() for b in sbars})
    cur_sp_state: tuple[float, float | None, float | None] | None = None
    sp500_causal_lookup: dict[str, tuple[float, float | None, float | None] | None] = {}
    for d in all_days:
        if d in sp500_state_by_day:
            cur_sp_state = sp500_state_by_day[d]
        sp500_causal_lookup[d] = cur_sp_state

    caches: dict[str, dict[str, Any]] = {}
    for sym, bars in bars_by_sym.items():
        inst = instruments_by_sym.get(sym, {"market": "crypto"})
        mkt = inst.get("market", "crypto")
        closes_arr = np.asarray([b.close for b in bars], dtype=np.float64)
        highs_arr = np.asarray([b.high for b in bars], dtype=np.float64)
        lows_arr = np.asarray([b.low for b in bars], dtype=np.float64)

        raw_atr14 = compute_atr_series(bars, period=14, market=mkt, interval="1d")
        atr14: list[float | None] = [None] * len(bars)
        for k, v in enumerate(raw_atr14):
            if 14 + k < len(atr14):
                atr14[14 + k] = v

        raw_atr20 = compute_atr_series(bars, period=20, market=mkt, interval="1d")
        atr20: list[float | None] = [None] * len(bars)
        for k, v in enumerate(raw_atr20):
            if 20 + k < len(atr20):
                atr20[20 + k] = v

        don_hi_55, _ = _rolling_extrema(highs_arr, 55)
        _, don_lo_55 = _rolling_extrema(lows_arr, 55)
        don_hi_20, _ = _rolling_extrema(highs_arr, 20)
        _, don_lo_20 = _rolling_extrema(lows_arr, 20)
        don_hi_10, _ = _rolling_extrema(highs_arr, 10)
        _, don_lo_10 = _rolling_extrema(lows_arr, 10)

        sma100 = _sma_series_np(closes_arr, 100)
        sma200 = _sma_series_np(closes_arr, 200)
        adx14 = compute_adx_series(bars, period=14)
        atr_pct_rank = compute_atr_pct_percentile(bars, atr14, window=252)
        weekly_feats = compute_causal_weekly_features(bars, market=mkt)
        hi_close_60 = _rolling_max_inclusive(closes_arr, 60)
        l4_wend = [_is_l4_week_end(bars, i, mkt) for i in range(len(bars))]

        roc20: list[float | None] = [None] * len(bars)
        for i in range(20, len(bars)):
            if closes_arr[i - 20] > 0:
                roc20[i] = float((closes_arr[i] / closes_arr[i - 20]) - 1.0)

        sp_close_aligned: list[float | None] = [None] * len(bars)
        sp_sma150_aligned: list[float | None] = [None] * len(bars)
        sp_sma200_aligned: list[float | None] = [None] * len(bars)
        for i, b in enumerate(bars):
            st = sp500_causal_lookup.get(b.ts_utc[:10])
            if st is not None:
                sp_close_aligned[i] = st[0]
                sp_sma150_aligned[i] = st[1]
                sp_sma200_aligned[i] = st[2]

        caches[sym] = {
            "bars": bars,
            "market": mkt,
            "closes": [b.close for b in bars],
            "highs": [b.high for b in bars],
            "lows": [b.low for b in bars],
            "opens": [b.open for b in bars],
            "atr": atr14,
            "atr20": atr20,
            "don_hi_55": don_hi_55,
            "don_lo_55": don_lo_55,
            "don_hi_20": don_hi_20,
            "don_lo_20": don_lo_20,
            "don_hi_10": don_hi_10,
            "don_lo_10": don_lo_10,
            "sma100": sma100,
            "sma200": sma200,
            "adx14": adx14,
            "atr_pct_rank": atr_pct_rank,
            "w_sma10": weekly_feats["w_sma10"],
            "w_sma20": weekly_feats["w_sma20"],
            "w_roc12": weekly_feats["w_roc12"],
            "l4_hi_close": hi_close_60,
            "l4_week_end": l4_wend,
            "roc20": roc20,
            "sp_close": sp_close_aligned,
            "sp_sma150": sp_sma150_aligned,
            "sp_sma200": sp_sma200_aligned,
        }

    return caches


def build_1h_symbol_cache(
    bars_1h_by_sym: dict[str, list[OHLCVBar]],
    instruments_by_sym: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Precompute 1h EMA20, EMA50, ATR14, swing extrema, reversal flags, and short-term A-G signals."""
    out: dict[str, dict[str, Any]] = {}
    for sym, bars in bars_1h_by_sym.items():
        if len(bars) < 60:
            continue
        inst = instruments_by_sym.get(sym, {"market": "crypto"})
        mkt = inst.get("market", "crypto")
        n = len(bars)
        closes = [b.close for b in bars]
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        opens = [b.open for b in bars]

        # EMA20 & EMA50
        ema20 = [0.0] * n
        ema50 = [0.0] * n
        k20 = 2.0 / 21.0
        k50 = 2.0 / 51.0
        ema20[0] = closes[0]
        ema50[0] = closes[0]
        for i in range(1, n):
            c = closes[i]
            ema20[i] = c * k20 + ema20[i - 1] * (1.0 - k20)
            ema50[i] = c * k50 + ema50[i - 1] * (1.0 - k50)

        raw_atr = compute_atr_series(bars, period=14, market=mkt, interval="1h")
        atr14: list[float | None] = [None] * n
        for k, v in enumerate(raw_atr):
            if 14 + k < n:
                atr14[14 + k] = v

        highs_arr = np.asarray(highs, dtype=np.float64)
        lows_arr = np.asarray(lows, dtype=np.float64)
        closes_arr = np.asarray(closes, dtype=np.float64)
        sw_hi12, _ = _rolling_extrema(highs_arr, 12)
        _, sw_lo12 = _rolling_extrema(lows_arr, 12)
        sw_hi20, _ = _rolling_extrema(highs_arr, 20)
        _, sw_lo20 = _rolling_extrema(lows_arr, 20)

        # Reversal candle flags & short-term A-G directional signals
        bull_rev = [False] * n
        bear_rev = [False] * n
        st_bcd_long = [False] * n
        st_bcd_short = [False] * n
        st_any_long = [False] * n
        st_any_short = [False] * n
        st_struct_long = [False] * n
        st_struct_short = [False] * n

        for i in range(20, n):
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            po, pc = opens[i - 1], closes[i - 1]
            rng = max(1e-9, h - l)
            body = abs(c - o)
            lower_wick = min(o, c) - l
            upper_wick = h - max(o, c)

            # Bullish engulfing or hammer/pin bar
            is_bull_eng = (c > o) and (pc < po) and (c >= po) and (o <= pc)
            is_bull_pin = (c >= o) and (lower_wick >= 1.2 * max(body, 0.15 * rng)) and (lower_wick >= 0.40 * rng)
            bull_rev[i] = is_bull_eng or is_bull_pin

            # Bearish engulfing or shooting star/pin bar
            is_bear_eng = (c < o) and (pc > po) and (c <= po) and (o >= pc)
            is_bear_pin = (c <= o) and (upper_wick >= 1.2 * max(body, 0.15 * rng)) and (upper_wick >= 0.40 * rng)
            bear_rev[i] = is_bear_eng or is_bear_pin

            av = atr14[i]
            if av is None or av <= 0:
                continue

            # Short-term strategy signals on 1h:
            # A: deep dip/spike >= 2.0 ATR from EMA20 + reversal
            sig_a_l = (ema20[i] - l >= 2.0 * av) and bull_rev[i]
            sig_a_s = (h - ema20[i] >= 2.0 * av) and bear_rev[i]
            # D: Bollinger / 20h range squeeze breakout (20h range compressed <= 3.5*ATR then breakout)
            hi20 = sw_hi20[i]
            lo20 = sw_lo20[i]
            is_squeeze = (hi20 is not None and lo20 is not None) and ((hi20 - lo20) <= 3.5 * av)
            sig_bd_l = is_squeeze and (hi20 is not None and c > hi20)
            sig_bd_s = is_squeeze and (lo20 is not None and c < lo20)
            # C: trend pullback to EMA20 in direction of EMA50
            sig_c_l = (c > ema50[i]) and (l <= ema20[i] + 0.3 * av) and (c > o)
            sig_c_s = (c < ema50[i]) and (h >= ema20[i] - 0.3 * av) and (c < o)
            # F/G: displacement order block or liquidity sweep of 12-bar extrema
            hi12 = sw_hi12[i]
            lo12 = sw_lo12[i]
            sig_fg_l = ((lo12 is not None) and (l < lo12) and (c > lo12)) or ((c - o) >= 1.8 * av)
            sig_fg_s = ((hi12 is not None) and (h > hi12) and (c < hi12)) or ((o - c) >= 1.8 * av)

            st_bcd_long[i] = sig_bd_l or sig_c_l
            st_bcd_short[i] = sig_bd_s or sig_c_s
            st_struct_long[i] = sig_c_l or sig_bd_l or sig_fg_l
            st_struct_short[i] = sig_c_s or sig_bd_s or sig_fg_s
            st_any_long[i] = sig_a_l or sig_bd_l or sig_c_l or sig_fg_l
            st_any_short[i] = sig_a_s or sig_bd_s or sig_c_s or sig_fg_s

        ts_list = [b.ts_utc for b in bars]
        out[sym] = {
            "bars": bars,
            "ts_list": ts_list,
            "ema20": ema20,
            "ema50": ema50,
            "atr14": atr14,
            "sw_hi12": sw_hi12,
            "sw_lo12": sw_lo12,
            "bull_rev": bull_rev,
            "bear_rev": bear_rev,
            "st_bcd_long": st_bcd_long,
            "st_bcd_short": st_bcd_short,
            "st_any_long": st_any_long,
            "st_any_short": st_any_short,
            "st_struct_long": st_struct_long,
            "st_struct_short": st_struct_short,
        }
    return out


# ---------------------------------------------------------------------------
# 3. Segment Metrics & Statistical Testing (DSR + Block Bootstrap)
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Acklam's rational approximation for standard normal inverse CDF."""
    p = min(1.0 - 1e-12, max(1e-12, p))
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    plow = 0.02425
    phigh = 1.0 - plow
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
            (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
        )
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
        (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
    )


def compute_segment_metrics(
    trades: list[SimTradeRecord],
    t0: str,
    t1: str,
    years: float,
    initial_cap: float = 20000.0,
    include_daily_series: bool = False,
) -> dict[str, Any]:
    """Compute comprehensive segment metrics starting from fresh `initial_cap` at `t0`."""
    seg_trades = [t for t in trades if t0 <= t.entry_ts_utc < t1]
    dt0 = _parse_dt(t0)
    dt1 = _parse_dt(t1)
    num_days = max(1, int((dt1 - dt0).total_seconds() // 86400))
    day_strs = [(dt0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(num_days)]
    day_to_idx = {d: i for i, d in enumerate(day_strs)}

    daily_ret = np.zeros(num_days, dtype=np.float64)
    n = len(seg_trades)
    if n == 0:
        res = {
            "trades": 0,
            "win_rate_pct": 0.0,
            "avg_win_r": 0.0,
            "avg_loss_r": 0.0,
            "expectancy_r": 0.0,
            "profit_factor": 0.0,
            "total_return_pct": 0.0,
            "cagr_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "mar_ratio": 0.0,
            "max_dd_days": 0,
            "sharpe_annual": 0.0,
        }
        if include_daily_series:
            res["daily_ret"] = daily_ret
        return res

    ordered = sorted(seg_trades, key=lambda x: (x.exit_ts_utc, x.entry_ts_utc, x.symbol))
    eq = initial_cap
    gross_win_norm = 0.0
    gross_loss_norm = 0.0

    for t in ordered:
        prev_eq = eq
        new_eq = max(0.01, prev_eq * (1.0 + t.eq_ret_pct))
        pnl_norm = new_eq - prev_eq
        eq = new_eq
        if pnl_norm > 0:
            gross_win_norm += pnl_norm
        else:
            gross_loss_norm += abs(pnl_norm)

        exit_day = t.exit_ts_utc[:10]
        d_idx = day_to_idx.get(exit_day, num_days - 1)
        if d_idx < 0:
            d_idx = 0
        elif d_idx >= num_days:
            d_idx = num_days - 1
        daily_ret[d_idx] = (1.0 + daily_ret[d_idx]) * (1.0 + t.eq_ret_pct) - 1.0

    # Daily equity curve for drawdown & longest underwater days
    cur_eq = initial_cap
    peak = initial_cap
    peak_day_idx = 0
    max_dd_pct = 0.0
    max_dd_days = 0

    for i in range(num_days):
        cur_eq = max(0.01, cur_eq * (1.0 + daily_ret[i]))
        if cur_eq >= peak - 1e-9:
            peak = cur_eq
            peak_day_idx = i
        else:
            dd = (peak - cur_eq) / max(1e-9, peak) * 100.0
            if dd > max_dd_pct:
                max_dd_pct = dd
            dd_days = i - peak_day_idx
            if dd_days > max_dd_days:
                max_dd_days = dd_days

    wins_r = [t.r_multiple for t in ordered if t.r_multiple > 0]
    losses_r = [t.r_multiple for t in ordered if t.r_multiple <= 0]
    avg_win_r = sum(wins_r) / len(wins_r) if wins_r else 0.0
    avg_loss_r = sum(losses_r) / len(losses_r) if losses_r else 0.0
    exp_r = sum(t.r_multiple for t in ordered) / n
    pf = (gross_win_norm / gross_loss_norm) if gross_loss_norm > 1e-9 else (99.0 if gross_win_norm > 0 else 0.0)

    total_ret_pct = (cur_eq / initial_cap - 1.0) * 100.0
    ratio = max(1e-4, cur_eq / initial_cap)
    cagr_pct = (ratio ** (1.0 / max(0.1, years)) - 1.0) * 100.0
    mar = cagr_pct / max(0.5, max_dd_pct) if cagr_pct > 0 else (cagr_pct / max(0.5, max_dd_pct))

    std_d = float(np.std(daily_ret, ddof=1)) if num_days > 2 else 0.0
    mean_d = float(np.mean(daily_ret))
    sharpe_ann = (mean_d / std_d * math.sqrt(365.0)) if std_d > 1e-12 else 0.0

    res = {
        "trades": n,
        "win_rate_pct": round(len(wins_r) / n * 100.0, 2),
        "avg_win_r": round(avg_win_r, 3),
        "avg_loss_r": round(avg_loss_r, 3),
        "expectancy_r": round(exp_r, 4),
        "profit_factor": round(pf, 3),
        "total_return_pct": round(total_ret_pct, 2),
        "cagr_pct": round(cagr_pct, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "mar_ratio": round(mar, 3),
        "max_dd_days": int(max_dd_days),
        "sharpe_annual": round(sharpe_ann, 3),
    }
    if include_daily_series:
        res["daily_ret"] = daily_ret
    return res


def compute_deflated_sharpe_and_bootstrap(
    candidate_daily_ret: np.ndarray,
    baseline_daily_ret: np.ndarray,
    all_trial_sharpes_annual: list[float],
    n_bootstrap: int = 2000,
    block_size: int = 10,
    seed: int = 42,
) -> dict[str, float]:
    """Compute Deflated Sharpe Ratio (DSR) and Stationary Block Bootstrap p-value vs baseline."""
    m_trials = max(2, len(all_trial_sharpes_annual))
    T = len(candidate_daily_ret)
    if T < 10:
        return {"dsr_prob": 0.0, "luck_prob_dsr": 1.0, "bootstrap_p_value": 1.0}

    # Convert annualized trial Sharpes to daily units
    sqrt_365 = math.sqrt(365.0)
    daily_srs = [s / sqrt_365 for s in all_trial_sharpes_annual]
    var_sr = float(np.var(daily_srs, ddof=1)) if len(daily_srs) > 1 else 0.01
    std_sr = math.sqrt(max(1e-12, var_sr))

    euler_gamma = 0.5772156649015329
    z1 = _norm_ppf(1.0 - 1.0 / m_trials)
    z2 = _norm_ppf(1.0 - 1.0 / (m_trials * math.e))
    sr0_daily = std_sr * ((1.0 - euler_gamma) * z1 + euler_gamma * z2)

    std_c = float(np.std(candidate_daily_ret, ddof=1))
    mean_c = float(np.mean(candidate_daily_ret))
    sr_c_daily = mean_c / std_c if std_c > 1e-12 else 0.0

    if std_c > 1e-12:
        centered = (candidate_daily_ret - mean_c) / std_c
        skew = float(np.mean(centered ** 3))
        kurt = float(np.mean(centered ** 4))
    else:
        skew = 0.0
        kurt = 3.0

    denom_sq = max(0.1, 1.0 - skew * sr_c_daily + ((kurt - 1.0) / 4.0) * (sr_c_daily ** 2))
    z_dsr = (sr_c_daily - sr0_daily) * math.sqrt(max(1, T - 1)) / math.sqrt(denom_sq)
    dsr_prob = _norm_cdf(z_dsr)
    luck_prob_dsr = 1.0 - dsr_prob

    # Block Bootstrap for excess return over baseline
    min_len = min(len(candidate_daily_ret), len(baseline_daily_ret))
    diff = candidate_daily_ret[:min_len] - baseline_daily_ret[:min_len]
    obs_mean_diff = float(np.mean(diff))
    if obs_mean_diff <= 0:
        boot_p = 1.0
    else:
        # Center under H0 (no improvement over baseline)
        diff_h0 = diff - obs_mean_diff
        rng = np.random.default_rng(seed)
        n_blocks = max(1, (min_len + block_size - 1) // block_size)
        starts = rng.integers(0, max(1, min_len - block_size + 1), size=(n_bootstrap, n_blocks))
        exceed = 0
        for b_idx in range(n_bootstrap):
            sample_chunks = [diff_h0[s : s + block_size] for s in starts[b_idx]]
            boot_series = np.concatenate(sample_chunks)[:min_len]
            if float(np.mean(boot_series)) >= obs_mean_diff:
                exceed += 1
        boot_p = exceed / n_bootstrap

    return {
        "total_combinations_tested": m_trials,
        "expected_max_sharpe_null_annual": round(sr0_daily * sqrt_365, 3),
        "candidate_sharpe_annual": round(sr_c_daily * sqrt_365, 3),
        "dsr_skill_prob_pct": round(dsr_prob * 100.0, 2),
        "luck_prob_dsr_pct": round(luck_prob_dsr * 100.0, 2),
        "bootstrap_p_value": round(boot_p, 4),
    }


# ---------------------------------------------------------------------------
# 4. Generalized Strategy Simulator for Groups 1, 2, 3, 4, 5
# ---------------------------------------------------------------------------

def _calc_net_pnl_and_cost(
    direction: str,
    raw_entry: float,
    raw_exit: float,
    qty: float,
    entry_ts: str,
    exit_ts: str,
    fee_rate: float,
    overnight_bps: float,
) -> tuple[float, float]:
    entry_notional = raw_entry * qty
    exit_notional = raw_exit * qty
    entry_fee = entry_notional * fee_rate
    exit_fee = exit_notional * fee_rate
    days = max(0.0, (_parse_dt(exit_ts) - _parse_dt(entry_ts)).total_seconds() / 86400.0)
    overnight_usd = entry_notional * (overnight_bps / 10000.0) * days
    gross = (raw_exit - raw_entry) * qty if direction == "long" else (raw_entry - raw_exit) * qty
    cost = entry_fee + exit_fee + overnight_usd
    return gross - cost, cost


def _find_1h_slice(
    cache_1h: dict[str, Any],
    start_ts_utc: str,
    wait_hours: int,
) -> tuple[int, int]:
    """Binary search 1h bars in [start_ts_utc, start_ts_utc + wait_hours]."""
    import bisect

    ts_list = cache_1h["ts_list"]
    idx0 = bisect.bisect_left(ts_list, start_ts_utc)
    idx1 = min(len(ts_list), idx0 + wait_hours)
    return idx0, idx1


def simulate_variant(
    base_strategy: str,
    daily_caches: dict[str, dict[str, Any]],
    symbols: list[str],
    instruments_by_sym: dict[str, dict[str, Any]],
    config: dict[str, Any],
    slippage_bps: float = 1.0,
    entry_mode: str = "standard",       # Group 1
    entry_params: dict[str, Any] | None = None,
    exit_mode: str = "standard",        # Group 2
    exit_params: dict[str, Any] | None = None,
    filter_mode: str = "none",          # Group 3
    filter_params: dict[str, Any] | None = None,
    consensus_mode: str = "none",       # Group 4
    consensus_params: dict[str, Any] | None = None,
    max_concurrent: int = 10,           # Group 5b/5c
    max_total_risk_pct: float = 10.0,
    ranking_mode: str = "symbol",       # Group 5c
    dev_exp_by_sym: dict[str, float] | None = None,
    caches_1h: dict[str, dict[str, Any]] | None = None,
    min_ts_utc: str | None = None,
) -> tuple[list[SimTradeRecord], dict[str, Any]]:
    """Run chronological portfolio simulation with configurable Group 1-5 hooks."""
    ep = entry_params or {}
    xp = exit_params or {}
    fp = filter_params or {}
    cp = consensus_params or {}
    base_p = BASELINE_PARAMS[base_strategy]

    sub_cap = 20000.0
    slip = slippage_bps / 10000.0
    allow_short = base_strategy in ("L1", "L2")
    entry_p = int(base_p.get("entry_period", 55))
    exit_p = int(base_p.get("exit_period", 20 if base_strategy == "L2" else 10))
    sl_mult = float(base_p.get("sl_atr_mult", 3.0 if base_strategy == "L2" else 2.5))
    vol_target = float(base_p.get("vol_target_pct", 0.015 if base_strategy == "L2" else 0.012))

    cfg_local = copy.deepcopy(config)
    cfg_local.setdefault("backtest", {})["max_concurrent_positions"] = max_concurrent
    cfg_local["backtest"]["max_total_open_risk_pct"] = max_total_risk_pct

    eval_syms = sorted(s for s in symbols if s in daily_caches and len(daily_caches[s]["bars"]) >= 60)
    all_ts_set: set[str] = set()
    for s in eval_syms:
        for b in daily_caches[s]["bars"]:
            if min_ts_utc is None or b.ts_utc >= min_ts_utc:
                all_ts_set.add(b.ts_utc)
    all_ts = sorted(all_ts_set)
    ts_to_t = {ts: t for t, ts in enumerate(all_ts)}

    sym_bidx_by_t: dict[str, dict[int, int]] = {}
    sym_t_by_bidx: dict[str, dict[int, int]] = {}
    inst_params: dict[str, tuple[float, float, float]] = {}

    for s in eval_syms:
        inst = instruments_by_sym[s]
        inst_params[s] = (
            float(inst.get("assumed_spread_bps", 2.0)),
            float(inst.get("fee_rate", 0.0005)),
            float(inst.get("overnight_cost_daily_bps", 0.0)),
        )
        b_map: dict[int, int] = {}
        t_map: dict[int, int] = {}
        for idx, b in enumerate(daily_caches[s]["bars"]):
            if b.ts_utc in ts_to_t:
                t = ts_to_t[b.ts_utc]
                b_map[t] = idx
                t_map[idx] = t
        sym_bidx_by_t[s] = b_map
        sym_t_by_bidx[s] = t_map

    current_equity = sub_cap
    trades: list[SimTradeRecord] = []
    open_pos: dict[str, dict[str, Any]] = {}
    pending_exits: dict[int, list[tuple[str, int]]] = defaultdict(list)
    pending_entries: dict[int, list[dict[str, Any]]] = defaultdict(list)

    # Extra stats for Group 1 (fill rate, missed signals)
    total_signals_count = 0
    filled_signals_count = 0
    skipped_signals_list: list[tuple[str, int, str]] = []  # (sym, signal_bidx, direction)

    don_hi_key = f"don_hi_{entry_p}"
    don_lo_key = f"don_lo_{entry_p}"
    exit_hi_key = f"don_hi_{exit_p}"
    exit_lo_key = f"don_lo_{exit_p}"

    for t_idx in range(len(all_ts)):
        # 1) Pending exits at bar open
        if t_idx in pending_exits:
            for sym, fill_bidx in pending_exits.pop(t_idx):
                if sym not in open_pos:
                    continue
                pos = open_pos.pop(sym)
                bar = daily_caches[sym]["bars"][fill_bidx]
                spread_bps, fee_rate, overnight_bps = inst_params[sym]
                raw_exit = (
                    bar.open * (1.0 - spread_bps / 20000.0) * (1.0 - slip)
                    if pos["dir"] == "long"
                    else bar.open * (1.0 + spread_bps / 20000.0) * (1.0 + slip)
                )
                rem_net, rem_cost = _calc_net_pnl_and_cost(
                    pos["dir"],
                    pos["entry_px"],
                    raw_exit,
                    pos["rem_qty"],
                    pos["entry_ts"],
                    bar.ts_utc,
                    fee_rate,
                    overnight_bps,
                )
                total_net = pos["realized_pnl"] + rem_net
                total_cost = pos["realized_cost"] + rem_cost
                net_pnl = max(-(current_equity - 0.01), total_net)
                eq_ret = net_pnl / max(1e-9, pos["eq_at_entry"])
                current_equity = max(0.01, current_equity + rem_net)
                trades.append(
                    SimTradeRecord(
                        symbol=sym,
                        strategy_id=base_strategy,
                        direction=pos["dir"],
                        entry_ts_utc=pos["entry_ts"],
                        exit_ts_utc=bar.ts_utc,
                        entry_price=round(pos["entry_px"], 6),
                        exit_price=round(raw_exit, 6),
                        initial_sl=round(pos["initial_sl"], 6),
                        r_multiple=round(net_pnl / max(1e-9, pos["risk_usd"]), 4),
                        eq_ret_pct=round(eq_ret, 6),
                        net_pnl_usd=round(net_pnl, 4),
                        cost_usd=round(total_cost, 4),
                        sl_dist_pct=round(pos["sl_dist_pct"], 6),
                        adx_at_entry=pos.get("adx_at_entry", 25.0),
                        roc20_at_entry=pos.get("roc20_at_entry", 0.0),
                    )
                )

        # 2) Pending entries at bar open (with optional Group 5c ranking)
        if t_idx in pending_entries:
            cands = pending_entries.pop(t_idx)
            if len(cands) > 1:
                if ranking_mode == "exp_r" and dev_exp_by_sym:
                    cands.sort(key=lambda c: (-dev_exp_by_sym.get(c["sym"], 0.0), c["sl_dist_pct"], c["sym"]))
                elif ranking_mode == "sl_dist":
                    cands.sort(key=lambda c: (c["sl_dist_pct"], c["sym"]))
                elif ranking_mode == "adx_trend":
                    cands.sort(key=lambda c: (-c["adx"], c["sym"]))
                elif ranking_mode == "roc_latest":
                    cands.sort(key=lambda c: (-abs(c["roc20"]), c["sym"]))
                else:
                    cands.sort(key=lambda c: c["sym"])

            for cand in cands:
                sym = cand["sym"]
                if sym in open_pos or len(open_pos) >= max_concurrent:
                    continue
                fill_bidx = cand["fill_bidx"]
                bar = daily_caches[sym]["bars"][fill_bidx]
                spread_bps, fee_rate, overnight_bps = inst_params[sym]
                direction = cand["dir"]
                custom_raw_px = cand.get("custom_entry_px")
                if custom_raw_px is not None:
                    base_px = custom_raw_px
                else:
                    base_px = bar.open
                raw_entry = (
                    base_px * (1.0 + spread_bps / 20000.0) * (1.0 + slip)
                    if direction == "long"
                    else base_px * (1.0 - spread_bps / 20000.0) * (1.0 - slip)
                )
                if raw_entry <= 0:
                    continue
                sl_px = cand["sl_px"]
                # Ensure stop loss is on the valid side of entry
                if direction == "long" and sl_px >= raw_entry * 0.998:
                    sl_px = raw_entry * (1.0 - max(0.005, cand["sl_dist_pct"]))
                elif direction == "short" and sl_px <= raw_entry * 1.002:
                    sl_px = raw_entry * (1.0 + max(0.005, cand["sl_dist_pct"]))

                open_risk_sum = sum(p["risk_usd"] for p in open_pos.values())
                open_notional_sum = sum(p["notional_usd"] for p in open_pos.values())
                alloc = allocate_subaccount_position(
                    strategy_id=base_strategy,
                    subaccount_equity_usd=current_equity,
                    open_positions_count=len(open_pos),
                    open_total_risk_usd=open_risk_sum,
                    open_total_notional_usd=open_notional_sum,
                    entry_price=raw_entry,
                    stop_loss=sl_px,
                    fee_rate=fee_rate,
                    slippage_bps=slippage_bps,
                    config=cfg_local,
                    risk_multiplier=cand["risk_mult"],
                )
                if not alloc.approved:
                    continue
                filled_signals_count += 1
                one_r_px_dist = abs(raw_entry - sl_px)
                open_pos[sym] = {
                    "dir": direction,
                    "entry_px": raw_entry,
                    "entry_ts": cand.get("custom_entry_ts") or bar.ts_utc,
                    "entry_bidx": fill_bidx,
                    "qty": alloc.qty,
                    "rem_qty": alloc.qty,
                    "notional_usd": alloc.notional_usd,
                    "risk_usd": alloc.risk_usd,
                    "eq_at_entry": current_equity,
                    "initial_sl": sl_px,
                    "sl_px": sl_px,
                    "one_r_dist": max(1e-9, one_r_px_dist),
                    "sl_dist_pct": one_r_px_dist / raw_entry,
                    "highest_px": raw_entry,
                    "lowest_px": raw_entry,
                    "max_fav_r": 0.0,
                    "partial_done": False,
                    "be_done": False,
                    "realized_pnl": 0.0,
                    "realized_cost": 0.0,
                    "adx_at_entry": cand["adx"],
                    "roc20_at_entry": cand["roc20"],
                }

        # 3) Manage open positions (Intra-bar SL, Partial TP, Breakeven, Chandelier, Time Stop, Donchian Exit)
        exited_this_bar: set[str] = set()
        for sym in list(open_pos.keys()):
            bidx = sym_bidx_by_t[sym].get(t_idx)
            if bidx is None:
                continue
            scache = daily_caches[sym]
            bars = scache["bars"]
            bar = bars[bidx]
            pos = open_pos[sym]
            spread_bps, fee_rate, overnight_bps = inst_params[sym]
            pos_dir = pos["dir"]
            sl_px = pos["sl_px"]
            one_r = pos["one_r_dist"]

            # 3a) Check intra-bar Stop Loss first (conservative: SL takes precedence)
            exited = False
            raw_exit = 0.0
            if pos_dir == "long":
                open_bid = bar.open * (1.0 - spread_bps / 20000.0)
                if open_bid <= sl_px:
                    raw_exit = open_bid * (1.0 - slip)
                    exited = True
                elif bar.low * (1.0 - spread_bps / 20000.0) <= sl_px:
                    raw_exit = sl_px * (1.0 - slip)
                    exited = True
            else:
                open_ask = bar.open * (1.0 + spread_bps / 20000.0)
                if open_ask >= sl_px:
                    raw_exit = open_ask * (1.0 + slip)
                    exited = True
                elif bar.high * (1.0 + spread_bps / 20000.0) >= sl_px:
                    raw_exit = sl_px * (1.0 + slip)
                    exited = True

            if exited:
                open_pos.pop(sym)
                exited_this_bar.add(sym)
                rem_net, rem_cost = _calc_net_pnl_and_cost(
                    pos_dir,
                    pos["entry_px"],
                    raw_exit,
                    pos["rem_qty"],
                    pos["entry_ts"],
                    bar.ts_utc,
                    fee_rate,
                    overnight_bps,
                )
                total_net = pos["realized_pnl"] + rem_net
                total_cost = pos["realized_cost"] + rem_cost
                net_pnl = max(-(current_equity - 0.01), total_net)
                eq_ret = net_pnl / max(1e-9, pos["eq_at_entry"])
                current_equity = max(0.01, current_equity + rem_net)
                trades.append(
                    SimTradeRecord(
                        symbol=sym,
                        strategy_id=base_strategy,
                        direction=pos_dir,
                        entry_ts_utc=pos["entry_ts"],
                        exit_ts_utc=bar.ts_utc,
                        entry_price=round(pos["entry_px"], 6),
                        exit_price=round(raw_exit, 6),
                        initial_sl=round(pos["initial_sl"], 6),
                        r_multiple=round(net_pnl / max(1e-9, pos["risk_usd"]), 4),
                        eq_ret_pct=round(eq_ret, 6),
                        net_pnl_usd=round(net_pnl, 4),
                        cost_usd=round(total_cost, 4),
                        sl_dist_pct=round(pos["sl_dist_pct"], 6),
                        adx_at_entry=pos.get("adx_at_entry", 25.0),
                        roc20_at_entry=pos.get("roc20_at_entry", 0.0),
                    )
                )
                continue

            # Update excursion tracking
            pos["highest_px"] = max(pos["highest_px"], bar.high)
            pos["lowest_px"] = min(pos["lowest_px"], bar.low)
            fav_exc = (bar.high - pos["entry_px"]) / one_r if pos_dir == "long" else (pos["entry_px"] - bar.low) / one_r
            pos["max_fav_r"] = max(pos["max_fav_r"], fav_exc)

            # 3b) Group 2b: Partial Take Profit (+mR close 50%)
            if exit_mode == "2b_partial_tp" and not pos["partial_done"]:
                tp_r_target = float(xp.get("tp_r", 1.5))
                if fav_exc >= tp_r_target:
                    tp_raw = (
                        (pos["entry_px"] + tp_r_target * one_r) * (1.0 - spread_bps / 20000.0) * (1.0 - slip)
                        if pos_dir == "long"
                        else (pos["entry_px"] - tp_r_target * one_r) * (1.0 + spread_bps / 20000.0) * (1.0 + slip)
                    )
                    half_qty = pos["rem_qty"] * 0.5
                    p_net, p_cost = _calc_net_pnl_and_cost(
                        pos_dir,
                        pos["entry_px"],
                        tp_raw,
                        half_qty,
                        pos["entry_ts"],
                        bar.ts_utc,
                        fee_rate,
                        overnight_bps,
                    )
                    pos["rem_qty"] -= half_qty
                    pos["realized_pnl"] += p_net
                    pos["realized_cost"] += p_cost
                    pos["partial_done"] = True
                    current_equity = max(0.01, current_equity + p_net)

            # 3c) Group 2d: Move stop loss to Breakeven after reaching +mR
            if exit_mode == "2d_breakeven" and not pos["be_done"]:
                be_trigger_r = float(xp.get("be_r", 1.0))
                if fav_exc >= be_trigger_r:
                    cost_buf = 2.0 * (fee_rate + spread_bps / 10000.0 + slip)
                    if pos_dir == "long":
                        pos["sl_px"] = max(pos["sl_px"], pos["entry_px"] * (1.0 + cost_buf))
                    else:
                        pos["sl_px"] = min(pos["sl_px"], pos["entry_px"] * (1.0 - cost_buf))
                    pos["be_done"] = True

            # 3d) Group 2a: Chandelier / ATR trailing stop update at bar close
            if exit_mode == "2a_chandelier":
                atr_v = scache["atr"][bidx]
                k_tr = float(xp.get("trail_atr_mult", 3.0))
                if atr_v is not None and atr_v > 0:
                    if pos_dir == "long":
                        cand_sl = pos["highest_px"] - k_tr * atr_v
                        if cand_sl > pos["sl_px"]:
                            pos["sl_px"] = cand_sl
                    else:
                        cand_sl = pos["lowest_px"] + k_tr * atr_v
                        if cand_sl < pos["sl_px"]:
                            pos["sl_px"] = cand_sl

            # 3e) Close-of-bar exit checks
            if bidx < len(bars) - 1:
                bars_held = max(1, bidx - pos["entry_bidx"] + 1)
                should_exit = False

                if base_strategy in ("L4", "F4"):
                    sma_key = "sma100" if int(base_p["sma_period"]) == 100 else "sma200"
                    sv = scache[sma_key][bidx]
                    av = scache["atr20"][bidx]
                    hc = scache["l4_hi_close"][bidx]
                    wend = scache["l4_week_end"][bidx]
                    k_m = float(base_p["atr_mult"])
                    if hc is not None and av is not None and av > 0:
                        cand_sl = hc - k_m * av
                        if cand_sl > pos["sl_px"]:
                            pos["sl_px"] = cand_sl
                    if wend and sv is not None and bar.close < sv:
                        should_exit = True
                else:
                    # Group 2c: Time stop if not reaching +0.5R within N days
                    if exit_mode == "2c_time_stop":
                        max_days = int(xp.get("stagnant_days", 10))
                        cur_r = (
                            (bar.close - pos["entry_px"]) / one_r
                            if pos_dir == "long"
                            else (pos["entry_px"] - bar.close) / one_r
                        )
                        if bars_held >= max_days and cur_r < 0.5:
                            should_exit = True

                    # Standard Donchian exit (used by standard, 2b, 2c, 2d, and as backup for 2a if enabled)
                    if not should_exit and exit_mode != "2a_chandelier":
                        ex_lo = scache[exit_lo_key][bidx]
                        ex_hi = scache[exit_hi_key][bidx]
                        if pos_dir == "long" and ex_lo is not None and bar.close < ex_lo:
                            should_exit = True
                        elif pos_dir == "short" and ex_hi is not None and bar.close > ex_hi:
                            should_exit = True

                if should_exit and (bidx + 1) in sym_t_by_bidx[sym]:
                    next_t = sym_t_by_bidx[sym][bidx + 1]
                    pending_exits[next_t].append((sym, bidx + 1))

        # 4) Candidate entries at close of t_idx
        if len(open_pos) >= max_concurrent:
            continue

        for sym in eval_syms:
            if sym in open_pos or sym in exited_this_bar:
                continue
            bidx = sym_bidx_by_t[sym].get(t_idx)
            if bidx is None:
                continue
            scache = daily_caches[sym]
            bars = scache["bars"]
            if bidx < 60 or bidx >= len(bars) - 1:
                continue
            if (bidx + 1) not in sym_t_by_bidx[sym]:
                continue

            close = scache["closes"][bidx]
            atr_v = scache["atr"][bidx]
            if atr_v is None or atr_v <= 0:
                continue

            # Base strategy entry trigger
            sig_dir = ""
            sl_px = 0.0
            risk_mult = 1.0

            if base_strategy in ("L4", "F4"):
                if not scache["l4_week_end"][bidx]:
                    continue
                sma_key = "sma100" if int(base_p["sma_period"]) == 100 else "sma200"
                sv = scache[sma_key][bidx]
                av20 = scache["atr20"][bidx]
                if sv is None or av20 is None or av20 <= 0:
                    continue
                if close > sv:
                    sig_dir = "long"
                    sl_px = close - float(base_p["atr_mult"]) * av20
                    if sl_px <= 0:
                        continue
            else:
                ehi = scache[don_hi_key][bidx]
                elo = scache[don_lo_key][bidx]
                if ehi is None or elo is None:
                    continue
                if close > ehi:
                    sig_dir = "long"
                    sl_px = close - sl_mult * atr_v
                elif allow_short and close < elo:
                    sig_dir = "short"
                    sl_px = close + sl_mult * atr_v
                if not sig_dir:
                    continue
                risk_mult = compute_volatility_risk_multiplier(atr_v, close, {"vol_target_pct": vol_target})

            if not sig_dir:
                continue

            # --- Group 3: Environmental Filters ---
            if filter_mode != "none":
                sma200_v = scache["sma200"][bidx]
                adx_v = scache["adx14"][bidx]
                vol_rank = scache["atr_pct_rank"][bidx]
                mkt = scache["market"]

                if filter_mode == "3a_sma200":
                    if sma200_v is None:
                        continue
                    if (sig_dir == "long" and close < sma200_v) or (sig_dir == "short" and close > sma200_v):
                        continue
                elif filter_mode == "3a_sma200_adx20":
                    if sma200_v is None or adx_v is None or adx_v < 20.0:
                        continue
                    if (sig_dir == "long" and close < sma200_v) or (sig_dir == "short" and close > sma200_v):
                        continue
                elif filter_mode == "3a_adx25":
                    if adx_v is None or adx_v < 25.0:
                        continue
                elif filter_mode.startswith("3b_vol_"):
                    low_p = float(fp.get("low_pct", 10.0))
                    high_p = float(fp.get("high_pct", 90.0))
                    if vol_rank is None or vol_rank < low_p or vol_rank > high_p:
                        continue
                elif filter_mode.startswith("3c_sp500_"):
                    sp_c = scache["sp_close"][bidx]
                    sp_ma = scache["sp_sma150"][bidx] if "150" in filter_mode else scache["sp_sma200"][bidx]
                    if sp_c is not None and sp_ma is not None and sp_c < sp_ma:
                        if sig_dir == "long" and mkt in ("crypto", "index"):
                            if "half" in filter_mode:
                                risk_mult *= 0.5
                            else:
                                continue
                elif filter_mode == "3d_wk_sma10":
                    w10 = scache["w_sma10"][bidx]
                    if w10 is None:
                        continue
                    if (sig_dir == "long" and close < w10) or (sig_dir == "short" and close > w10):
                        continue
                elif filter_mode == "3d_wk_sma20":
                    w20 = scache["w_sma20"][bidx]
                    if w20 is None:
                        continue
                    if (sig_dir == "long" and close < w20) or (sig_dir == "short" and close > w20):
                        continue
                elif filter_mode == "3d_wk_roc12":
                    wroc = scache["w_roc12"][bidx]
                    if wroc is None:
                        continue
                    if (sig_dir == "long" and wroc <= 0.0) or (sig_dir == "short" and wroc >= 0.0):
                        continue

            # --- Group 4: Signal Consensus ---
            if consensus_mode != "none":
                if consensus_mode.startswith("4a_"):
                    # Count how many of {L1, L2, L4/F4(weekly SMA100/200)} agree with sig_dir on this bar
                    agree_count = 1  # base strategy triggered
                    # Check L1/L2 20d/55d channel & SMA100/200 weekly trend
                    sma100_v = scache["sma100"][bidx]
                    sma200_v = scache["sma200"][bidx]
                    w10_v = scache["w_sma10"][bidx]
                    if sig_dir == "long":
                        if sma100_v is not None and close > sma100_v:
                            agree_count += 1
                        if sma200_v is not None and close > sma200_v and (w10_v is not None and close > w10_v):
                            agree_count += 1
                    else:
                        if sma100_v is not None and close < sma100_v:
                            agree_count += 1
                        if sma200_v is not None and close < sma200_v and (w10_v is not None and close < w10_v):
                            agree_count += 1

                    if consensus_mode == "4a_strict_2":
                        if agree_count < 2:
                            continue
                    elif consensus_mode == "4a_strict_2_boost3":
                        if agree_count < 2:
                            continue
                        if agree_count >= 3:
                            risk_mult *= 1.25
                    elif consensus_mode == "4a_scale_aggr":
                        if agree_count >= 3:
                            risk_mult *= 1.40
                        elif agree_count == 2:
                            risk_mult *= 1.00
                        else:
                            risk_mult *= 0.60

                elif consensus_mode.startswith("4b_") and caches_1h and sym in caches_1h:
                    c1h = caches_1h[sym]
                    next_day_ts = bars[bidx + 1].ts_utc
                    win_hrs = 48 if "48h" in consensus_mode else 24
                    h0, h1 = _find_1h_slice(c1h, scache["bars"][bidx].ts_utc, win_hrs)
                    if h1 <= h0:
                        continue
                    if "bcd" in consensus_mode:
                        arr = c1h["st_bcd_long"] if sig_dir == "long" else c1h["st_bcd_short"]
                    elif "struct" in consensus_mode:
                        arr = c1h["st_struct_long"] if sig_dir == "long" else c1h["st_struct_short"]
                    else:
                        arr = c1h["st_any_long"] if sig_dir == "long" else c1h["st_any_short"]
                    if not any(arr[h0:h1]):
                        continue

            total_signals_count += 1

            # --- Group 1: Entry Timing Refinements on 1h bars ---
            custom_entry_px: float | None = None
            custom_entry_ts: str | None = None
            fill_bidx = bidx + 1

            if entry_mode != "standard":
                wait_hours = int(ep.get("wait_hours", 24))
                next_open_ts = bars[bidx + 1].ts_utc
                orig_open_px = bars[bidx + 1].open

                if entry_mode == "1c_limit":
                    k_lim = float(ep.get("limit_atr_mult", 0.25))
                    lim_px = orig_open_px - k_lim * atr_v if sig_dir == "long" else orig_open_px + k_lim * atr_v
                    filled = False
                    if caches_1h and sym in caches_1h:
                        c1h = caches_1h[sym]
                        h0, h1 = _find_1h_slice(c1h, next_open_ts, wait_hours)
                        for h_idx in range(h0, h1):
                            hb = c1h["bars"][h_idx]
                            if (sig_dir == "long" and hb.low <= lim_px) or (sig_dir == "short" and hb.high >= lim_px):
                                custom_entry_px = lim_px
                                custom_entry_ts = hb.ts_utc
                                filled = True
                                break
                    else:
                        # Fallback to daily high/low check over 1 or 2 days
                        n_days = max(1, wait_hours // 24)
                        for d_off in range(1, min(n_days + 1, len(bars) - bidx)):
                            db = bars[bidx + d_off]
                            if (sig_dir == "long" and db.low <= lim_px) or (sig_dir == "short" and db.high >= lim_px):
                                custom_entry_px = lim_px
                                custom_entry_ts = db.ts_utc
                                fill_bidx = bidx + d_off
                                filled = True
                                break
                    if not filled:
                        skipped_signals_list.append((sym, bidx, sig_dir))
                        continue

                elif entry_mode.startswith(("1a_", "1b_")):
                    require_rev = entry_mode.startswith("1b_")
                    fallback_mkt = entry_mode.endswith("_mkt")
                    if not caches_1h or sym not in caches_1h:
                        continue
                    c1h = caches_1h[sym]
                    h0, h1 = _find_1h_slice(c1h, next_open_ts, wait_hours)
                    if h1 <= h0 + 1:
                        continue
                    triggered = False
                    for h_idx in range(h0, h1 - 1):
                        hb = c1h["bars"][h_idx]
                        ema20_v = c1h["ema20"][h_idx]
                        atr1h_v = c1h["atr14"][h_idx]
                        if atr1h_v is None or atr1h_v <= 0:
                            continue
                        if sig_dir == "long":
                            in_zone = (hb.low <= ema20_v + 0.5 * atr1h_v) and (hb.close <= orig_open_px)
                            rev_ok = c1h["bull_rev"][h_idx] if require_rev else True
                            if in_zone and rev_ok:
                                next_hb = c1h["bars"][h_idx + 1]
                                custom_entry_px = next_hb.open
                                custom_entry_ts = next_hb.ts_utc
                                sw_lo = c1h["sw_lo12"][h_idx]
                                if sw_lo is not None:
                                    cand_1h_sl = sw_lo - 0.2 * atr1h_v
                                    # Bound 1h swing SL between 0.5x and 3.0x daily ATR
                                    min_sl = custom_entry_px - 3.0 * atr_v
                                    max_sl = custom_entry_px - 0.5 * atr_v
                                    sl_px = min(max_sl, max(min_sl, cand_1h_sl))
                                triggered = True
                                break
                        else:
                            in_zone = (hb.high >= ema20_v - 0.5 * atr1h_v) and (hb.close >= orig_open_px)
                            rev_ok = c1h["bear_rev"][h_idx] if require_rev else True
                            if in_zone and rev_ok:
                                next_hb = c1h["bars"][h_idx + 1]
                                custom_entry_px = next_hb.open
                                custom_entry_ts = next_hb.ts_utc
                                sw_hi = c1h["sw_hi12"][h_idx]
                                if sw_hi is not None:
                                    cand_1h_sl = sw_hi + 0.2 * atr1h_v
                                    min_sl = custom_entry_px + 0.5 * atr_v
                                    max_sl = custom_entry_px + 3.0 * atr_v
                                    sl_px = min(max_sl, max(min_sl, cand_1h_sl))
                                triggered = True
                                break

                    if not triggered:
                        if fallback_mkt:
                            last_hb = c1h["bars"][h1 - 1]
                            custom_entry_px = last_hb.close
                            custom_entry_ts = last_hb.ts_utc
                        else:
                            skipped_signals_list.append((sym, bidx, sig_dir))
                            continue

            ref_entry = custom_entry_px if custom_entry_px is not None else bars[fill_bidx].open
            if ref_entry <= 0:
                continue
            sl_dist_pct = max(0.002, abs(ref_entry - sl_px) / ref_entry)
            next_t = sym_t_by_bidx[sym][fill_bidx]
            pending_entries[next_t].append({
                "sym": sym,
                "fill_bidx": fill_bidx,
                "dir": sig_dir,
                "sl_px": sl_px,
                "sl_dist_pct": sl_dist_pct,
                "risk_mult": risk_mult,
                "custom_entry_px": custom_entry_px,
                "custom_entry_ts": custom_entry_ts,
                "adx": scache["adx14"][bidx] or 20.0,
                "roc20": scache["roc20"][bidx] or 0.0,
            })

    fill_rate_pct = (filled_signals_count / total_signals_count * 100.0) if total_signals_count > 0 else 0.0
    extra_info = {
        "total_signals": total_signals_count,
        "filled_signals": filled_signals_count,
        "fill_rate_pct": round(fill_rate_pct, 2),
        "skipped_signals": skipped_signals_list,
    }
    return trades, extra_info


# ---------------------------------------------------------------------------
# 5. L3 Dual Momentum, Multi-Strategy Portfolio (5a/5b/5c) & S2 Rescue (Group 6)
# ---------------------------------------------------------------------------

def simulate_l3_records(
    daily_caches: dict[str, dict[str, Any]],
    symbols: list[str],
    instruments_by_sym: dict[str, dict[str, Any]],
    config: dict[str, Any],
    slippage_bps: float = 1.0,
) -> list[SimTradeRecord]:
    """Simulate L3 Dual Momentum weekly rebalancing returning SimTradeRecord list."""
    params = BASELINE_PARAMS["L3"]
    lookback = int(params["lookback_days"])
    top_k_crypto = int(params["top_k_crypto"])
    top_k_index = int(params["top_k_index"])
    rebal_days = int(params["rebalance_days"])
    sl_mult = float(params["sl_atr_mult"])
    vol_target = float(params["vol_target_pct"])
    sub_cap = 20000.0
    slip = slippage_bps / 10000.0
    max_concurrent = 10

    eval_syms = sorted(s for s in symbols if s in daily_caches and len(daily_caches[s]["bars"]) >= 60)
    all_ts = sorted({b.ts_utc for s in eval_syms for b in daily_caches[s]["bars"]})
    ts_to_t = {ts: t for t, ts in enumerate(all_ts)}

    sym_bidx_by_t: dict[str, dict[int, int]] = {}
    sym_t_by_bidx: dict[str, dict[int, int]] = {}
    inst_params: dict[str, tuple[float, float, float]] = {}
    markets_by_sym: dict[str, str] = {}

    for s in eval_syms:
        inst = instruments_by_sym[s]
        markets_by_sym[s] = inst.get("market", "crypto")
        inst_params[s] = (
            float(inst.get("assumed_spread_bps", 2.0)),
            float(inst.get("fee_rate", 0.0005)),
            float(inst.get("overnight_cost_daily_bps", 0.0)),
        )
        b_map: dict[int, int] = {}
        t_map: dict[int, int] = {}
        for idx, b in enumerate(daily_caches[s]["bars"]):
            t = ts_to_t[b.ts_utc]
            b_map[t] = idx
            t_map[idx] = t
        sym_bidx_by_t[s] = b_map
        sym_t_by_bidx[s] = t_map

    current_equity = sub_cap
    trades: list[SimTradeRecord] = []
    open_pos: dict[str, dict[str, Any]] = {}
    pending_exits: dict[int, list[tuple[str, int]]] = defaultdict(list)
    pending_entries: dict[int, list[tuple[str, int, SignalAction]]] = defaultdict(list)
    last_bidx_by_sym: dict[str, int] = {}

    for t_idx in range(len(all_ts)):
        for sym in eval_syms:
            bidx_cur = sym_bidx_by_t[sym].get(t_idx)
            if bidx_cur is not None:
                last_bidx_by_sym[sym] = bidx_cur

        if t_idx in pending_exits:
            for sym, fill_bidx in pending_exits.pop(t_idx):
                if sym not in open_pos:
                    continue
                pos = open_pos.pop(sym)
                bar = daily_caches[sym]["bars"][fill_bidx]
                spread_bps, fee_rate, overnight_bps = inst_params[sym]
                raw_exit = bar.open * (1.0 - spread_bps / 20000.0) * (1.0 - slip)
                net_pnl, cost_usd = _calc_net_pnl_and_cost(
                    "long", pos["entry_px"], raw_exit, pos["qty"], pos["entry_ts"], bar.ts_utc, fee_rate, overnight_bps
                )
                net_pnl = max(-(current_equity - 0.01), net_pnl)
                eq_ret = net_pnl / max(1e-9, pos["eq_at_entry"])
                current_equity = max(0.01, current_equity + net_pnl)
                trades.append(
                    SimTradeRecord(
                        symbol=sym,
                        strategy_id="L3",
                        direction="long",
                        entry_ts_utc=pos["entry_ts"],
                        exit_ts_utc=bar.ts_utc,
                        entry_price=round(pos["entry_px"], 6),
                        exit_price=round(raw_exit, 6),
                        initial_sl=round(pos["sl_px"], 6),
                        r_multiple=round(net_pnl / max(1e-9, pos["risk_usd"]), 4),
                        eq_ret_pct=round(eq_ret, 6),
                        net_pnl_usd=round(net_pnl, 4),
                        cost_usd=round(cost_usd, 4),
                        sl_dist_pct=round(pos["sl_dist_pct"], 6),
                    )
                )

        if t_idx in pending_entries:
            for sym, fill_bidx, act in pending_entries.pop(t_idx):
                if sym in open_pos or len(open_pos) >= max_concurrent:
                    continue
                bar = daily_caches[sym]["bars"][fill_bidx]
                spread_bps, fee_rate, overnight_bps = inst_params[sym]
                raw_entry = bar.open * (1.0 + spread_bps / 20000.0) * (1.0 + slip)
                if raw_entry <= 0:
                    continue
                open_risk_sum = sum(p["risk_usd"] for p in open_pos.values())
                open_notional_sum = sum(p["notional_usd"] for p in open_pos.values())
                alloc = allocate_subaccount_position(
                    strategy_id="L3",
                    subaccount_equity_usd=current_equity,
                    open_positions_count=len(open_pos),
                    open_total_risk_usd=open_risk_sum,
                    open_total_notional_usd=open_notional_sum,
                    entry_price=raw_entry,
                    stop_loss=act.stop_loss,
                    fee_rate=fee_rate,
                    slippage_bps=slippage_bps,
                    config=config,
                    risk_multiplier=act.risk_multiplier,
                )
                if not alloc.approved:
                    continue
                sl_px = act.stop_loss or (raw_entry * 0.90)
                open_pos[sym] = {
                    "entry_px": raw_entry,
                    "entry_ts": bar.ts_utc,
                    "qty": alloc.qty,
                    "notional_usd": alloc.notional_usd,
                    "risk_usd": alloc.risk_usd,
                    "eq_at_entry": current_equity,
                    "sl_px": sl_px,
                    "sl_dist_pct": abs(raw_entry - sl_px) / raw_entry,
                }

        for sym in list(open_pos.keys()):
            bidx = sym_bidx_by_t[sym].get(t_idx)
            if bidx is None:
                continue
            bar = daily_caches[sym]["bars"][bidx]
            pos = open_pos[sym]
            spread_bps, fee_rate, overnight_bps = inst_params[sym]
            sl_px = pos["sl_px"]
            exited = False
            raw_exit = 0.0
            open_bid = bar.open * (1.0 - spread_bps / 20000.0)
            if open_bid <= sl_px:
                raw_exit = open_bid * (1.0 - slip)
                exited = True
            elif bar.low * (1.0 - spread_bps / 20000.0) <= sl_px:
                raw_exit = sl_px * (1.0 - slip)
                exited = True
            if exited:
                open_pos.pop(sym)
                net_pnl, cost_usd = _calc_net_pnl_and_cost(
                    "long", pos["entry_px"], raw_exit, pos["qty"], pos["entry_ts"], bar.ts_utc, fee_rate, overnight_bps
                )
                net_pnl = max(-(current_equity - 0.01), net_pnl)
                eq_ret = net_pnl / max(1e-9, pos["eq_at_entry"])
                current_equity = max(0.01, current_equity + net_pnl)
                trades.append(
                    SimTradeRecord(
                        symbol=sym,
                        strategy_id="L3",
                        direction="long",
                        entry_ts_utc=pos["entry_ts"],
                        exit_ts_utc=bar.ts_utc,
                        entry_price=round(pos["entry_px"], 6),
                        exit_price=round(raw_exit, 6),
                        initial_sl=round(sl_px, 6),
                        r_multiple=round(net_pnl / max(1e-9, pos["risk_usd"]), 4),
                        eq_ret_pct=round(eq_ret, 6),
                        net_pnl_usd=round(net_pnl, 4),
                        cost_usd=round(cost_usd, 4),
                        sl_dist_pct=round(pos["sl_dist_pct"], 6),
                    )
                )

        if t_idx >= lookback and (t_idx % max(1, rebal_days) == 0):
            snapshot: dict[str, list[OHLCVBar]] = {}
            pre_atr: dict[str, float | None] = {}
            for sym in eval_syms:
                bidx = last_bidx_by_sym.get(sym)
                if bidx is not None and bidx >= lookback and bidx < len(daily_caches[sym]["bars"]) - 1:
                    snapshot[sym] = daily_caches[sym]["bars"][bidx - lookback : bidx + 1]
                    pre_atr[sym] = daily_caches[sym]["atr"][bidx]
            if snapshot:
                actions = decide_l3_dual_momentum_step(
                    bars_up_to_date=snapshot,
                    markets_by_sym=markets_by_sym,
                    held_symbols=set(open_pos.keys()),
                    lookback_days=lookback,
                    top_k_crypto=top_k_crypto,
                    top_k_index=top_k_index,
                    sl_atr_mult=sl_mult,
                    atr_period=14,
                    vol_target_pct=vol_target,
                    precomputed_atr_by_sym=pre_atr,
                )
                for sym, act in actions.items():
                    bidx = last_bidx_by_sym.get(sym)
                    if bidx is None or (bidx + 1) not in sym_t_by_bidx[sym]:
                        continue
                    next_t = sym_t_by_bidx[sym][bidx + 1]
                    if act.action == "exit" and sym in open_pos:
                        pending_exits[next_t].append((sym, bidx + 1))
                    elif act.action == "enter_long" and sym not in open_pos:
                        pending_entries[next_t].append((sym, bidx + 1, act))

    return trades


def combine_weighted_strategy_trades(
    trades_by_strat: dict[str, list[SimTradeRecord]],
    weights: dict[str, float],
    max_concurrent: int | None = None,
    ranking_mode: str = "none",
    dev_exp_by_strat: dict[str, float] | None = None,
    portfolio_leverage: float = 1.6,
) -> list[SimTradeRecord]:
    """Combine multiple strategy sleeves with portfolio weights and optional concurrent position cap."""
    all_candidates: list[SimTradeRecord] = []
    for sid, tlist in trades_by_strat.items():
        w = float(weights.get(sid, 0.0))
        if w <= 0:
            continue
        eff_w = w * (1.0 if max_concurrent is not None else portfolio_leverage)
        for t in tlist:
            scaled = copy.copy(t)
            scaled.eq_ret_pct = t.eq_ret_pct * eff_w
            scaled.net_pnl_usd = t.net_pnl_usd * eff_w
            scaled.cost_usd = t.cost_usd * eff_w
            all_candidates.append(scaled)

    if max_concurrent is None:
        return sorted(all_candidates, key=lambda x: (x.entry_ts_utc, x.exit_ts_utc, x.symbol))

    # Enforce global max_concurrent across the combined portfolio
    by_entry_ts: dict[str, list[SimTradeRecord]] = defaultdict(list)
    for t in all_candidates:
        by_entry_ts[t.entry_ts_utc].append(t)

    selected: list[SimTradeRecord] = []
    active_exits: list[tuple[str, str]] = []  # (exit_ts_utc, symbol)

    for entry_ts in sorted(by_entry_ts.keys()):
        active_exits = [(ex_ts, s) for (ex_ts, s) in active_exits if ex_ts > entry_ts]
        cands = by_entry_ts[entry_ts]
        if len(cands) > 1:
            if ranking_mode == "exp_r" and dev_exp_by_strat:
                cands.sort(key=lambda c: (-dev_exp_by_strat.get(c.strategy_id, 0.0), c.sl_dist_pct, c.symbol))
            elif ranking_mode == "sl_dist":
                cands.sort(key=lambda c: (c.sl_dist_pct, c.symbol))
            elif ranking_mode == "adx_trend":
                cands.sort(key=lambda c: (-c.adx_at_entry, c.symbol))
            elif ranking_mode == "roc_latest":
                cands.sort(key=lambda c: (-abs(c.roc20_at_entry), c.symbol))
            else:
                cands.sort(key=lambda c: (c.strategy_id, c.symbol))

        for c in cands:
            if len(active_exits) >= max_concurrent:
                break
            # Avoid duplicate same-symbol position in single-slot mode
            if max_concurrent == 1 and any(s == c.symbol for _, s in active_exits):
                continue
            # Rescale equity return for active slot count
            slot_copy = copy.copy(c)
            if max_concurrent <= 3:
                # Normalize risk per trade to 1.0% per slot
                orig_w = float(weights.get(c.strategy_id, 0.2))
                if orig_w > 0:
                    slot_copy.eq_ret_pct = (c.eq_ret_pct / orig_w) * min(1.0, 3.0 / max(1, max_concurrent) * 0.35)
            selected.append(slot_copy)
            active_exits.append((c.exit_ts_utc, c.symbol))

    return selected


def build_s2_4h_cache(
    bars_1h_by_sym: dict[str, list[OHLCVBar]],
    daily_caches: dict[str, dict[str, Any]],
    instruments_by_sym: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    """Precompute 4h bars, channels, ATR, and causal daily filter map once for Group 6 S2 rescue."""
    now_utc = datetime(2026, 11, 18, tzinfo=timezone.utc)
    params = BASELINE_PARAMS["S2"]
    bo_p = int(params["breakout_bars"])
    tr_p = int(params["trail_bars"])

    s2_cache: dict[str, dict[str, Any]] = {}
    for sym, b1h in bars_1h_by_sym.items():
        if sym not in instruments_by_sym:
            continue
        mkt = instruments_by_sym[sym].get("market", "crypto")
        if mkt not in ("crypto", "commodity"):
            continue
        bars = resample_1h_to_4h(b1h, now_utc=now_utc)
        if len(bars) < 60:
            continue
        highs_arr = np.asarray([b.high for b in bars], dtype=np.float64)
        lows_arr = np.asarray([b.low for b in bars], dtype=np.float64)
        bo_hi, _ = _rolling_extrema(highs_arr, bo_p)
        _, bo_lo = _rolling_extrema(lows_arr, bo_p)
        tr_hi, _ = _rolling_extrema(highs_arr, tr_p)
        _, tr_lo = _rolling_extrema(lows_arr, tr_p)
        raw_atr = compute_atr_series(bars, period=14, market=mkt, interval="4h")
        atr14: list[float | None] = [None] * len(bars)
        for k, v in enumerate(raw_atr):
            if 14 + k < len(atr14):
                atr14[14 + k] = v
        s2_cache[sym] = {
            "bars": bars,
            "bo_hi": bo_hi,
            "bo_lo": bo_lo,
            "tr_hi": tr_hi,
            "tr_lo": tr_lo,
            "atr14": atr14,
        }

    daily_filter_map: dict[tuple[str, str], dict[str, Any]] = {}
    for sym, dc in daily_caches.items():
        bars_d = dc["bars"]
        for i in range(1, len(bars_d)):
            day_str = bars_d[i].ts_utc[:10]
            p_idx = i - 1
            daily_filter_map[(sym, day_str)] = {
                "close": dc["closes"][p_idx],
                "sma200": dc["sma200"][p_idx],
                "adx14": dc["adx14"][p_idx],
                "vol_rank": dc["atr_pct_rank"][p_idx],
                "sp_close": dc["sp_close"][p_idx],
                "sp_sma200": dc["sp_sma200"][p_idx],
                "w_sma20": dc["w_sma20"][p_idx],
            }

    return s2_cache, daily_filter_map


def simulate_s2_rescue(
    s2_cache: dict[str, dict[str, Any]],
    daily_filter_map: dict[tuple[str, str], dict[str, Any]],
    instruments_by_sym: dict[str, dict[str, Any]],
    config: dict[str, Any],
    filter_variant: str = "none",  # "none", "6a_sma200_adx25", "6b_vol_sp500", "6c_mtf_no_fx"
    slippage_bps: float = 1.0,
) -> list[SimTradeRecord]:
    """Group 6: Test S2 4h breakout with Group 3 environmental filters using precomputed 4h cache."""
    params = BASELINE_PARAMS["S2"]
    bo_p = int(params["breakout_bars"])
    tr_p = int(params["trail_bars"])
    sl_mult = float(params["sl_atr_mult"])
    max_hold = int(params["max_hold_bars"])
    slip = slippage_bps / 10000.0

    trades: list[SimTradeRecord] = []
    for sym, sc in sorted(s2_cache.items()):
        bars = sc["bars"]
        bo_hi = sc["bo_hi"]
        bo_lo = sc["bo_lo"]
        tr_hi = sc["tr_hi"]
        tr_lo = sc["tr_lo"]
        atr14 = sc["atr14"]
        inst = instruments_by_sym[sym]
        spread_bps = float(inst.get("assumed_spread_bps", 2.0))
        fee_rate = float(inst.get("fee_rate", 0.0005))
        overnight_bps = float(inst.get("overnight_cost_daily_bps", 0.0))

        pos: dict[str, Any] | None = None
        for i in range(max(bo_p, tr_p) + 2, len(bars) - 1):
            b = bars[i]
            if pos is not None:
                # Check SL
                pdir = pos["dir"]
                sl_px = pos["sl_px"]
                exited = False
                raw_exit = 0.0
                if pdir == "long":
                    if b.open * (1.0 - spread_bps / 20000.0) <= sl_px:
                        raw_exit = b.open * (1.0 - spread_bps / 20000.0) * (1.0 - slip)
                        exited = True
                    elif b.low * (1.0 - spread_bps / 20000.0) <= sl_px:
                        raw_exit = sl_px * (1.0 - slip)
                        exited = True
                else:
                    if b.open * (1.0 + spread_bps / 20000.0) >= sl_px:
                        raw_exit = b.open * (1.0 + spread_bps / 20000.0) * (1.0 + slip)
                        exited = True
                    elif b.high * (1.0 + spread_bps / 20000.0) >= sl_px:
                        raw_exit = sl_px * (1.0 + slip)
                        exited = True

                bars_held = i - pos["entry_idx"] + 1
                if not exited:
                    if bars_held >= max_hold or (pdir == "long" and tr_lo[i] is not None and b.close < tr_lo[i]) or (
                        pdir == "short" and tr_hi[i] is not None and b.close > tr_hi[i]
                    ):
                        nb = bars[i + 1]
                        raw_exit = (
                            nb.open * (1.0 - spread_bps / 20000.0) * (1.0 - slip)
                            if pdir == "long"
                            else nb.open * (1.0 + spread_bps / 20000.0) * (1.0 + slip)
                        )
                        exited = True

                if exited:
                    net_pnl, cost_usd = _calc_net_pnl_and_cost(
                        pdir, pos["entry_px"], raw_exit, pos["qty"], pos["entry_ts"], b.ts_utc, fee_rate, overnight_bps
                    )
                    r_mult = net_pnl / max(1e-9, pos["risk_usd"])
                    trades.append(
                        SimTradeRecord(
                            symbol=sym,
                            strategy_id="S2",
                            direction=pdir,
                            entry_ts_utc=pos["entry_ts"],
                            exit_ts_utc=b.ts_utc,
                            entry_price=round(pos["entry_px"], 6),
                            exit_price=round(raw_exit, 6),
                            initial_sl=round(pos["sl_px"], 6),
                            r_multiple=round(r_mult, 4),
                            eq_ret_pct=round(r_mult * 0.002, 6),
                            net_pnl_usd=round(net_pnl, 4),
                            cost_usd=round(cost_usd, 4),
                            sl_dist_pct=round(pos["sl_dist_pct"], 6),
                        )
                    )
                    pos = None
                continue

            av = atr14[i]
            if av is None or av <= 0 or bo_hi[i] is None or bo_lo[i] is None:
                continue
            sig_dir = ""
            if b.close > bo_hi[i]:
                sig_dir = "long"
            elif b.close < bo_lo[i]:
                sig_dir = "short"
            if not sig_dir:
                continue

            # Apply Group 3 environmental filters to S2
            if filter_variant != "none":
                df = daily_filter_map.get((sym, b.ts_utc[:10]))
                if df is None:
                    continue
                d_close = df["close"]
                d_sma200 = df["sma200"]
                d_adx = df["adx14"]
                d_vol = df["vol_rank"]
                sp_c = df["sp_close"]
                sp_ma = df["sp_sma200"]
                w_ma = df["w_sma20"]

                if filter_variant == "6a_sma200_adx25":
                    if d_sma200 is None or d_adx is None or d_adx < 25.0:
                        continue
                    if (sig_dir == "long" and d_close < d_sma200) or (sig_dir == "short" and d_close > d_sma200):
                        continue
                elif filter_variant == "6b_vol_sp500":
                    if d_vol is None or d_vol < 20.0 or d_vol > 90.0:
                        continue
                    if sig_dir == "long" and sp_c is not None and sp_ma is not None and sp_c < sp_ma:
                        continue
                elif filter_variant == "6c_mtf_no_fx":
                    if d_sma200 is None or w_ma is None:
                        continue
                    if sig_dir == "long" and (d_close < d_sma200 or d_close < w_ma):
                        continue
                    if sig_dir == "short" and (d_close > d_sma200 or d_close > w_ma):
                        continue

            nb = bars[i + 1]
            raw_entry = (
                nb.open * (1.0 + spread_bps / 20000.0) * (1.0 + slip)
                if sig_dir == "long"
                else nb.open * (1.0 - spread_bps / 20000.0) * (1.0 - slip)
            )
            sl_px = raw_entry - sl_mult * av if sig_dir == "long" else raw_entry + sl_mult * av
            sl_dist_pct = max(0.002, abs(raw_entry - sl_px) / raw_entry)
            risk_usd = 100.0
            qty = (risk_usd / sl_dist_pct) / raw_entry
            pos = {
                "dir": sig_dir,
                "entry_px": raw_entry,
                "entry_ts": nb.ts_utc,
                "entry_idx": i + 1,
                "sl_px": sl_px,
                "sl_dist_pct": sl_dist_pct,
                "risk_usd": risk_usd,
                "qty": qty,
            }

    return sorted(trades, key=lambda x: (x.entry_ts_utc, x.symbol))


# ---------------------------------------------------------------------------
# 6. Two Scenario Simulators: Kraken Prop Exam & The5ers 1-Week Contest
# ---------------------------------------------------------------------------

def _single_slot_sort_key(t: SimTradeRecord) -> tuple[str, int, float, float, str]:
    strat_prio = {"L2": 0, "L4": 1, "F4": 2, "L1": 3, "L3": 4, "S2": 5}.get(t.strategy_id, 3)
    return (t.entry_ts_utc, strat_prio, -abs(t.roc20_at_entry), t.sl_dist_pct, t.symbol)


def simulate_kraken_prop_exam(
    trades: list[SimTradeRecord],
    start_ts: str = "2019-01-01T00:00:00Z",
    end_ts: str = "2026-11-15T00:00:00Z",
    risk_per_trade_pct: float = 0.75,
    target_profit_pct: float = 9.0,
    max_total_dd_pct: float = 3.0,
    max_daily_dd_pct: float = 3.0,
    step_days: int = 7,
) -> dict[str, Any]:
    """Simulate Kraken Prop Evaluation ($10k, 0.75% risk/trade, 1 pos at a time, +9% target, no time limit)."""
    dt_start = _parse_dt(start_ts)
    dt_end = _parse_dt(end_ts)
    ordered = sorted([t for t in trades if t.entry_ts_utc >= start_ts], key=_single_slot_sort_key)

    initial_eq = 10000.0
    pass_eq = initial_eq * (1.0 + target_profit_pct / 100.0)   # 10,900
    fail_eq = initial_eq * (1.0 - max_total_dd_pct / 100.0)    # 9,700
    daily_limit_usd = initial_eq * (max_daily_dd_pct / 100.0)  # 300
    risk_scale = risk_per_trade_pct / 1.0                      # scale from 1.0% base subaccount risk to 0.75%

    total_runs = 0
    passed_runs = 0
    busted_runs = 0
    unfinished_runs = 0
    pass_days_list: list[float] = []

    cur_start = dt_start
    # Ensure at least 365 days before dataset end to start a no-time-limit daily evaluation
    while cur_start + timedelta(days=365) <= dt_end:
        total_runs += 1
        s_str = cur_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        eq = initial_eq
        busy_until = ""
        outcome = "unfinished"

        for t in ordered:
            if t.entry_ts_utc < s_str:
                continue
            # Single position at a time
            if busy_until and t.entry_ts_utc < busy_until:
                continue
            busy_until = t.exit_ts_utc

            # Exact 0.75% risk per trade with 5x maximum notional leverage cap (matching prop_exam_sim.py)
            raw_risk_usd = eq * (risk_per_trade_pct / 100.0)
            max_lev_risk_usd = 5.0 * eq * max(0.005, t.sl_dist_pct)
            eff_risk_usd = min(raw_risk_usd, max_lev_risk_usd)

            worst_r = min(0.0, t.r_multiple) if t.r_multiple < 0 else -0.5
            worst_loss_usd = abs(worst_r) * eff_risk_usd

            if worst_loss_usd >= daily_limit_usd or (eq - worst_loss_usd) <= fail_eq:
                outcome = "busted"
                break

            eq = eq + t.r_multiple * eff_risk_usd
            if eq <= fail_eq:
                outcome = "busted"
                break
            if eq >= pass_eq:
                outcome = "passed"
                days_taken = max(1.0, (_parse_dt(t.exit_ts_utc) - cur_start).total_seconds() / 86400.0)
                pass_days_list.append(days_taken)
                break

        if outcome == "passed":
            passed_runs += 1
        elif outcome == "busted":
            busted_runs += 1
        else:
            unfinished_runs += 1

        cur_start += timedelta(days=step_days)

    if total_runs == 0:
        return {
            "runs": 0,
            "pass_rate_pct": 0.0,
            "bust_rate_pct": 0.0,
            "unfinished_rate_pct": 0.0,
            "mean_pass_days": 0.0,
            "median_pass_days": 0.0,
        }

    return {
        "runs": total_runs,
        "pass_rate_pct": round(passed_runs / total_runs * 100.0, 2),
        "bust_rate_pct": round(busted_runs / total_runs * 100.0, 2),
        "unfinished_rate_pct": round(unfinished_runs / total_runs * 100.0, 2),
        "mean_pass_days": round(float(np.mean(pass_days_list)), 1) if pass_days_list else 0.0,
        "median_pass_days": round(float(np.median(pass_days_list)), 1) if pass_days_list else 0.0,
    }


def simulate_the5ers_weekly_contest(
    trades: list[SimTradeRecord],
    start_ts: str = "2022-11-17T00:00:00Z",
    end_ts: str = "2026-11-15T00:00:00Z",
    risk_per_trade_pct: float = 4.5,
    bust_equity: float = 9000.0,
    step_days: int = 7,
) -> dict[str, Any]:
    """Simulate The5ers 1-Week Contest ($10k, 4.5% risk/trade, 1 pos at a time, < $9,000 bust)."""
    dt_start = _parse_dt(start_ts)
    dt_end = _parse_dt(end_ts)
    ordered = sorted([t for t in trades if t.entry_ts_utc >= start_ts], key=_single_slot_sort_key)

    initial_eq = 10000.0
    risk_frac = risk_per_trade_pct / 100.0
    weekly_returns: list[float] = []
    busted_weeks = 0
    active_weeks = 0

    cur_start = dt_start
    while cur_start + timedelta(days=7) <= dt_end:
        w_start = cur_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        w_end = (cur_start + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        eq = initial_eq
        busy_until = ""
        busted = False
        had_trade = False

        for t in ordered:
            if t.entry_ts_utc < w_start:
                continue
            if t.entry_ts_utc >= w_end:
                break
            if busy_until and t.entry_ts_utc < busy_until:
                continue
            busy_until = t.exit_ts_utc
            had_trade = True

            risk_usd = eq * risk_frac
            # If trade closes after week end, scale realized R proportionally to fraction of hold inside week
            t_en = _parse_dt(t.entry_ts_utc)
            t_ex = _parse_dt(t.exit_ts_utc)
            w_ex = cur_start + timedelta(days=7)
            if t_ex > w_ex:
                tot_sec = max(3600.0, (t_ex - t_en).total_seconds())
                in_sec = max(3600.0, (w_ex - t_en).total_seconds())
                eff_r = t.r_multiple * min(1.0, math.sqrt(in_sec / tot_sec))
            else:
                eff_r = t.r_multiple

            eq += eff_r * risk_usd
            if eq < bust_equity:
                busted = True
                break

        if had_trade:
            active_weeks += 1
        if busted:
            busted_weeks += 1
        weekly_returns.append((eq - initial_eq) / initial_eq * 100.0)
        cur_start += timedelta(days=step_days)

    if not weekly_returns:
        return {"weeks": 0, "active_weeks": 0, "median_return_pct": 0.0, "p90_return_pct": 0.0, "bust_rate_pct": 0.0}

    arr = np.asarray(weekly_returns, dtype=np.float64)
    return {
        "weeks": len(weekly_returns),
        "active_weeks": active_weeks,
        "median_return_pct": round(float(np.median(arr)), 2),
        "p90_return_pct": round(float(np.percentile(arr, 90)), 2),
        "mean_return_pct": round(float(np.mean(arr)), 2),
        "bust_rate_pct": round(busted_weeks / len(weekly_returns) * 100.0, 2),
    }


# ---------------------------------------------------------------------------
# 7. Full Experiment Orchestration Across Groups 1 to 6
# ---------------------------------------------------------------------------

def evaluate_trades_across_splits(
    trades: list[SimTradeRecord],
    splits: dict[str, tuple[str, str, float]],
    baseline_by_seg: dict[str, dict[str, Any]] | None = None,
    include_daily_series: bool = False,
) -> dict[str, Any]:
    """Evaluate trade list across dev, val, and lockbox segments and compute deltas vs baseline."""
    out: dict[str, Any] = {}
    for seg_name, (t0, t1, yrs) in splits.items():
        m = compute_segment_metrics(trades, t0, t1, yrs, initial_cap=20000.0, include_daily_series=include_daily_series)
        if baseline_by_seg and seg_name in baseline_by_seg:
            bm = baseline_by_seg[seg_name]
            m["delta_exp_r"] = round(m["expectancy_r"] - bm["expectancy_r"], 4)
            m["delta_pf"] = round(m["profit_factor"] - bm["profit_factor"], 3)
            m["delta_cagr_pct"] = round(m["cagr_pct"] - bm["cagr_pct"], 2)
            m["delta_dd_pct"] = round(m["max_drawdown_pct"] - bm["max_drawdown_pct"], 2)
            m["delta_mar"] = round(m["mar_ratio"] - bm["mar_ratio"], 3)
            beats = (
                m["expectancy_r"] > 0.05
                and m["profit_factor"] >= 1.25
                and (
                    (m["mar_ratio"] >= bm["mar_ratio"] * 1.03 and m["cagr_pct"] >= bm["cagr_pct"] * 0.90)
                    or (
                        (m["profit_factor"] > bm["profit_factor"] or m["cagr_pct"] > bm["cagr_pct"] or m["expectancy_r"] > bm["expectancy_r"])
                        and m["mar_ratio"] >= bm["mar_ratio"] * 0.95
                        and m["profit_factor"] >= bm["profit_factor"] * 0.98
                    )
                )
            )
            m["beats_baseline"] = bool(beats)
        out[seg_name] = m
    return out


def _count_missed_big_winners(
    orig_trades: list[SimTradeRecord],
    variant_trades: list[SimTradeRecord],
    big_r_thresh: float = 2.0,
) -> dict[str, Any]:
    """Count how many original >+2R big winners were missed by an entry refinement."""
    orig_big = [t for t in orig_trades if t.r_multiple >= big_r_thresh]
    if not orig_big:
        return {"orig_big_winners": 0, "missed_big_winners": 0, "missed_big_winners_pct": 0.0}

    var_keys = {(t.symbol, t.entry_ts_utc[:7], t.direction) for t in variant_trades}
    missed = 0
    for ob in orig_big:
        # Check if variant entered same symbol & direction within 5 days of original entry
        ob_dt = _parse_dt(ob.entry_ts_utc)
        matched = False
        for vt in variant_trades:
            if vt.symbol == ob.symbol and vt.direction == ob.direction:
                if abs((_parse_dt(vt.entry_ts_utc) - ob_dt).total_seconds()) <= 5 * 86400:
                    matched = True
                    break
        if not matched:
            missed += 1

    return {
        "orig_big_winners": len(orig_big),
        "missed_big_winners": missed,
        "missed_big_winners_pct": round(missed / len(orig_big) * 100.0, 2),
    }


def run_all_experiments() -> dict[str, Any]:
    t_start = time.time()
    config = load_config()
    mt5_cfg = load_mt5_costs_config()
    datasets = load_cached_datasets(config)
    bars_1d = datasets["1d"]
    bars_1h = datasets["1h"]

    # Cost configurations
    inst_cfg, slip_cfg = build_cost_adjusted_instruments(config, mt5_cfg, "config", bars_1d)
    inst_mt5, slip_mt5 = build_cost_adjusted_instruments(config, mt5_cfg, "mt5", bars_1d)
    inst_stress, slip_stress = build_cost_adjusted_instruments(config, mt5_cfg, "stress", bars_1d)

    l2_syms = list(config["strategies"]["L2"]["symbols"])
    daily_caches = build_enhanced_daily_cache(bars_1d, inst_cfg)
    l2_bars_1h = {s: bars_1h[s] for s in l2_syms if s in bars_1h}
    caches_1h = build_1h_symbol_cache(l2_bars_1h, inst_cfg)
    l1_syms = [
        inst["symbol"]
        for inst in config["instruments"]
        if inst.get("enabled", True) and inst["market"] in ("crypto", "commodity", "index")
    ]
    l3_syms = [
        inst["symbol"]
        for inst in config["instruments"]
        if inst.get("enabled", True) and inst["market"] in ("crypto", "index")
    ]
    l4_syms = list(config["strategies"]["L4"]["symbols"])
    l2_1h_syms = [s for s in l2_syms if s in caches_1h]

    # -----------------------------------------------------------------------
    # Baselines: Original L2, L1, L3, L4, F4 (on Daily 10y & Intraday 5.38y)
    # -----------------------------------------------------------------------
    baselines: dict[str, Any] = {}

    for sid, syms in [("L2", l2_syms), ("L1", l1_syms), ("L4", l4_syms), ("F4", l4_syms)]:
        tr_cfg, _ = simulate_variant(sid, daily_caches, syms, inst_cfg, config, slip_cfg)
        tr_mt5, _ = simulate_variant(sid, daily_caches, syms, inst_mt5, config, slip_mt5)
        tr_str, _ = simulate_variant(sid, daily_caches, syms, inst_stress, config, slip_stress)
        baselines[sid] = {
            "trades_obj": tr_cfg,
            "trades_mt5_obj": tr_mt5,
            "daily_10y_config": evaluate_trades_across_splits(tr_cfg, DAILY_SPLITS, include_daily_series=True),
            "daily_10y_mt5": evaluate_trades_across_splits(tr_mt5, DAILY_SPLITS),
            "daily_10y_stress": evaluate_trades_across_splits(tr_str, DAILY_SPLITS),
        }

    tr_l3_cfg = simulate_l3_records(daily_caches, l3_syms, inst_cfg, config, slip_cfg)
    tr_l3_mt5 = simulate_l3_records(daily_caches, l3_syms, inst_mt5, config, slip_mt5)
    tr_l3_str = simulate_l3_records(daily_caches, l3_syms, inst_stress, config, slip_stress)
    baselines["L3"] = {
        "trades_obj": tr_l3_cfg,
        "trades_mt5_obj": tr_l3_mt5,
        "daily_10y_config": evaluate_trades_across_splits(tr_l3_cfg, DAILY_SPLITS, include_daily_series=True),
        "daily_10y_mt5": evaluate_trades_across_splits(tr_l3_mt5, DAILY_SPLITS),
        "daily_10y_stress": evaluate_trades_across_splits(tr_l3_str, DAILY_SPLITS),
    }

    # Intraday-period baseline for L2 & L1 (for fair comparison with Group 1 & Group 4b on 2021-07..2026-11)
    tr_l2_1h_base, info_l2_1h_base = simulate_variant(
        "L2", daily_caches, l2_1h_syms, inst_cfg, config, slip_cfg, min_ts_utc="2021-07-01T00:00:00Z"
    )
    tr_l2_1h_mt5, _ = simulate_variant(
        "L2", daily_caches, l2_1h_syms, inst_mt5, config, slip_mt5, min_ts_utc="2021-07-01T00:00:00Z"
    )
    tr_l2_1h_str, _ = simulate_variant(
        "L2", daily_caches, l2_1h_syms, inst_stress, config, slip_stress, min_ts_utc="2021-07-01T00:00:00Z"
    )
    baselines["L2_intraday_period"] = {
        "trades_obj": tr_l2_1h_base,
        "intraday_config": evaluate_trades_across_splits(tr_l2_1h_base, INTRADAY_SPLITS, include_daily_series=True),
        "intraday_mt5": evaluate_trades_across_splits(tr_l2_1h_mt5, INTRADAY_SPLITS),
        "intraday_stress": evaluate_trades_across_splits(tr_l2_1h_str, INTRADAY_SPLITS),
    }

    l2_base_daily_seg = baselines["L2"]["daily_10y_config"]
    l1_base_daily_seg = baselines["L1"]["daily_10y_config"]
    l2_base_intra_seg = baselines["L2_intraday_period"]["intraday_config"]

    all_trial_sharpes_dev: list[float] = []
    ideas_results: list[dict[str, Any]] = []

    def _run_idea(
        idea_id: str,
        group_id: str,
        title: str,
        base_strat: str,
        rationale: str,
        param_sets: list[tuple[str, dict[str, Any]]],
        use_intraday_split: bool = False,
        custom_runner: Any = None,
    ) -> dict[str, Any]:
        """Run up to 3 parameter sets for an idea, select best on Dev, check neighbor sensitivity, and evaluate Val/Lockbox."""
        splits = INTRADAY_SPLITS if use_intraday_split else DAILY_SPLITS
        base_seg = (
            l2_base_intra_seg
            if use_intraday_split
            else (l1_base_daily_seg if base_strat == "L1" else l2_base_daily_seg)
        )
        orig_trades_ref = (
            baselines["L2_intraday_period"]["trades_obj"]
            if use_intraday_split
            else baselines[base_strat if base_strat in baselines else "L2"]["trades_obj"]
        )

        param_evals: list[dict[str, Any]] = []
        for pname, pkwargs in param_sets:
            if custom_runner is not None:
                tr_c, tr_m, tr_s, extra = custom_runner(pkwargs)
            else:
                syms = l2_1h_syms if use_intraday_split else (l1_syms if base_strat == "L1" else l2_syms)
                min_ts = "2021-07-01T00:00:00Z" if use_intraday_split else None
                tr_c, extra = simulate_variant(
                    base_strat, daily_caches, syms, inst_cfg, config, slip_cfg,
                    caches_1h=caches_1h, min_ts_utc=min_ts, **pkwargs
                )
                tr_m, _ = simulate_variant(
                    base_strat, daily_caches, syms, inst_mt5, config, slip_mt5,
                    caches_1h=caches_1h, min_ts_utc=min_ts, **pkwargs
                )
                tr_s, _ = simulate_variant(
                    base_strat, daily_caches, syms, inst_stress, config, slip_stress,
                    caches_1h=caches_1h, min_ts_utc=min_ts, **pkwargs
                )

            seg_cfg = evaluate_trades_across_splits(tr_c, splits, base_seg, include_daily_series=True)
            seg_mt5 = evaluate_trades_across_splits(tr_m, splits)
            seg_str = evaluate_trades_across_splits(tr_s, splits)
            all_trial_sharpes_dev.append(seg_cfg["dev"]["sharpe_annual"])

            missed_info = _count_missed_big_winners(orig_trades_ref, tr_c, big_r_thresh=2.0)
            param_evals.append({
                "param_name": pname,
                "trades_obj": tr_c,
                "trades_mt5_obj": tr_m,
                "config_metrics": seg_cfg,
                "mt5_metrics": seg_mt5,
                "stress_metrics": seg_str,
                "fill_rate_pct": extra.get("fill_rate_pct", 100.0),
                "missed_big_winners": missed_info["missed_big_winners"],
                "missed_big_winners_pct": missed_info["missed_big_winners_pct"],
                "orig_big_winners": missed_info["orig_big_winners"],
            })

        # Select best parameter on Dev ONLY (by Dev MAR ratio + Expectancy R, never peeking at Val or Lockbox!)
        best_p = max(
            param_evals,
            key=lambda x: (
                x["config_metrics"]["dev"]["mar_ratio"]
                + 2.0 * x["config_metrics"]["dev"]["expectancy_r"]
            ),
        )

        # Neighbor parameter sensitivity check on Dev
        dev_mars = [p["config_metrics"]["dev"]["mar_ratio"] for p in param_evals]
        dev_exps = [p["config_metrics"]["dev"]["expectancy_r"] for p in param_evals]
        positive_neighbors = sum(1 for e in dev_exps if e > 0.05)
        mar_spread = (max(dev_mars) - min(dev_mars)) / max(0.1, abs(float(np.mean(dev_mars)))) if len(dev_mars) > 1 else 0.0
        is_isolated_spike = (positive_neighbors == 1 and len(param_evals) > 1) or (mar_spread > 1.5 and positive_neighbors < len(param_evals))

        dev_beats = best_p["config_metrics"]["dev"].get("beats_baseline", False)
        val_beats = best_p["config_metrics"]["val"].get("beats_baseline", False)
        passes_dev_and_val = bool(dev_beats and val_beats and not is_isolated_spike)

        sensitivity_summary = [
            {
                "param": p["param_name"],
                "dev_exp_r": p["config_metrics"]["dev"]["expectancy_r"],
                "dev_pf": p["config_metrics"]["dev"]["profit_factor"],
                "dev_mar": p["config_metrics"]["dev"]["mar_ratio"],
                "val_exp_r": p["config_metrics"]["val"]["expectancy_r"],
                "val_pf": p["config_metrics"]["val"]["profit_factor"],
                "val_mar": p["config_metrics"]["val"]["mar_ratio"],
            }
            for p in param_evals
        ]

        idea_rec = {
            "idea_id": idea_id,
            "group_id": group_id,
            "title": title,
            "base_strategy": base_strat,
            "rationale": rationale,
            "split_type": "intraday_5y" if use_intraday_split else "daily_10y",
            "num_params_tested": len(param_sets),
            "selected_param": best_p["param_name"],
            "is_isolated_spike": is_isolated_spike,
            "neighbor_sensitivity": sensitivity_summary,
            "dev_beats_baseline": dev_beats,
            "val_beats_baseline": val_beats,
            "passes_to_candidate_pool": passes_dev_and_val,
            "fill_rate_pct": best_p["fill_rate_pct"],
            "missed_big_winners": best_p["missed_big_winners"],
            "missed_big_winners_pct": best_p["missed_big_winners_pct"],
            "orig_big_winners": best_p["orig_big_winners"],
            "best_eval": best_p,
        }
        ideas_results.append(idea_rec)
        return idea_rec

    # =======================================================================
    # GROUP 1: 進場時機改良 (Entry Timing Refinements on L2)
    # =======================================================================
    _run_idea(
        "1a_skip", "G1", "1a 等 1h 回檔 EMA20/0.5ATR 進場（逾時放棄＋1h 擺動停損）", "L2",
        "日線突破常在短線超買時觸發；測試等待 12/24/48 根 1h K 線回踩 EMA20（或 0.5×ATR 內）才進場，沒等到就放棄。",
        [
            ("N=12h (放棄)", {"entry_mode": "1a_skip", "entry_params": {"wait_hours": 12}}),
            ("N=24h (放棄)", {"entry_mode": "1a_skip", "entry_params": {"wait_hours": 24}}),
            ("N=48h (放棄)", {"entry_mode": "1a_skip", "entry_params": {"wait_hours": 48}}),
        ],
        use_intraday_split=True,
    )

    _run_idea(
        "1a_mkt", "G1", "1a 等 1h 回檔 EMA20/0.5ATR 進場（逾時市價追入＋1h 擺動停損）", "L2",
        "等待 12/24/48 根 1h K 線回踩 EMA20，若未回檔則在到期時以市價進場，避免漏掉不回頭的強勢突破。",
        [
            ("N=12h (市價追)", {"entry_mode": "1a_mkt", "entry_params": {"wait_hours": 12}}),
            ("N=24h (市價追)", {"entry_mode": "1a_mkt", "entry_params": {"wait_hours": 24}}),
            ("N=48h (市價追)", {"entry_mode": "1a_mkt", "entry_params": {"wait_hours": 48}}),
        ],
        use_intraday_split=True,
    )

    _run_idea(
        "1b_skip", "G1", "1b 等 1h 回檔＋反轉 K 確認（吞噬/長影線，逾時放棄）", "L2",
        "在 1a 回檔條件上加嚴：必須出現 1h 吞噬或長下影線反轉 K 線確認止跌才進場，測試最多等 12/24/48 小時（沒等到放棄）。",
        [
            ("N=12h+反轉K (放棄)", {"entry_mode": "1b_skip", "entry_params": {"wait_hours": 12}}),
            ("N=24h+反轉K (放棄)", {"entry_mode": "1b_skip", "entry_params": {"wait_hours": 24}}),
            ("N=48h+反轉K (放棄)", {"entry_mode": "1b_skip", "entry_params": {"wait_hours": 48}}),
        ],
        use_intraday_split=True,
    )

    _run_idea(
        "1b_mkt", "G1", "1b 等 1h 回檔＋反轉 K 確認（逾時市價進場）", "L2",
        "等待 1h 回檔加反轉 K 確認，若 12/24/48 小時內未出現則到期以市價進場。",
        [
            ("N=12h+反轉K (市價)", {"entry_mode": "1b_mkt", "entry_params": {"wait_hours": 12}}),
            ("N=24h+反轉K (市價)", {"entry_mode": "1b_mkt", "entry_params": {"wait_hours": 24}}),
            ("N=48h+反轉K (市價)", {"entry_mode": "1b_mkt", "entry_params": {"wait_hours": 48}}),
        ],
        use_intraday_split=True,
    )

    _run_idea(
        "1c_limit", "G1", "1c 限價單回檔掛單（訊號價往有利方向 0.25/0.5×日 ATR）", "L2",
        "日線突破訊號出現後，在訊號收盤價往回檔方向掛 0.25× 或 0.50× 日 ATR 限價單（保留原日線停損位，成交即享有更優盈虧比）。",
        [
            ("0.25×ATR (24h有效)", {"entry_mode": "1c_limit", "entry_params": {"limit_atr_mult": 0.25, "wait_hours": 24}}),
            ("0.50×ATR (24h有效)", {"entry_mode": "1c_limit", "entry_params": {"limit_atr_mult": 0.50, "wait_hours": 24}}),
            ("0.25×ATR (48h有效)", {"entry_mode": "1c_limit", "entry_params": {"limit_atr_mult": 0.25, "wait_hours": 48}}),
        ],
        use_intraday_split=False,  # Evaluated across full 10-year history (using 1h where available + daily H/L)
    )

    # =======================================================================
    # GROUP 2: 出場改良 (Exit Refinements on L2)
    # =======================================================================
    _run_idea(
        "2a_chandelier", "G2", "2a 移動停利改用 Chandelier ATR 倍數（2.5 / 3.0 / 4.0×ATR）", "L2",
        "原版 L2 用 20 日最低點出場，遇到波動劇烈時回吐較多；改測試從進場後最高價回落 2.5/3.0/4.0×ATR 的吊燈移動停損 (Chandelier Exit)。",
        [
            ("Chandelier 2.5×ATR", {"exit_mode": "2a_chandelier", "exit_params": {"trail_atr_mult": 2.5}}),
            ("Chandelier 3.0×ATR", {"exit_mode": "2a_chandelier", "exit_params": {"trail_atr_mult": 3.0}}),
            ("Chandelier 4.0×ATR", {"exit_mode": "2a_chandelier", "exit_params": {"trail_atr_mult": 4.0}}),
        ],
    )

    _run_idea(
        "2b_partial_tp", "G2", "2b 部分止盈（+1R / +1.5R / +2R 先平一半，其餘跟移動停利）", "L2",
        "測試在浮盈達 +1R、+1.5R 或 +2R 時先落袋一半倉位，另一半繼續跟隨 20 日通道移動停利，觀察是否能改善勝率或反而砍斷大趨勢右尾。",
        [
            ("+1.0R 平半倉", {"exit_mode": "2b_partial_tp", "exit_params": {"tp_r": 1.0}}),
            ("+1.5R 平半倉", {"exit_mode": "2b_partial_tp", "exit_params": {"tp_r": 1.5}}),
            ("+2.0R 平半倉", {"exit_mode": "2b_partial_tp", "exit_params": {"tp_r": 2.0}}),
        ],
    )

    _run_idea(
        "2c_time_stop", "G2", "2c 停滯時間停損（N=7 / 10 / 15 天未達 +0.5R 即出場）", "L2",
        "真突破通常在進場後數日內脫離成本區；若持倉滿 7/10/15 天浮盈仍未達 +0.5R，視為假突破提前出場以釋放資金與風險額度。",
        [
            ("7天未達+0.5R出場", {"exit_mode": "2c_time_stop", "exit_params": {"stagnant_days": 7}}),
            ("10天未達+0.5R出場", {"exit_mode": "2c_time_stop", "exit_params": {"stagnant_days": 10}}),
            ("15天未達+0.5R出場", {"exit_mode": "2c_time_stop", "exit_params": {"stagnant_days": 15}}),
        ],
    )

    _run_idea(
        "2d_breakeven", "G2", "2d 達 +1R 後停損移到保本（+1.0R / +1.25R / +1.5R 觸發保本）", "L2",
        "當最高浮盈觸及 +1.0R / +1.25R / +1.5R 後，將停損上移至進場成本價（含手續費緩衝），避免已經賺 1R 的單子最後變成 -1R 虧損。",
        [
            ("+1.00R 移保本", {"exit_mode": "2d_breakeven", "exit_params": {"be_r": 1.0}}),
            ("+1.25R 移保本", {"exit_mode": "2d_breakeven", "exit_params": {"be_r": 1.25}}),
            ("+1.50R 移保本", {"exit_mode": "2d_breakeven", "exit_params": {"be_r": 1.5}}),
        ],
    )

    # =======================================================================
    # GROUP 3: 環境過濾 (Environmental Filters on L2 & L1)
    # =======================================================================
    _run_idea(
        "3a_trend_regime", "G3", "3a 趨勢強度與長均線同側過濾（SMA200 同側 / ADX 門檻）", "L2",
        "只在價格位於 SMA200 同側（多頭在 200MA 上、空頭在 200MA 下）或 ADX(14) 顯示趨勢成形時才執行突破訊號，過濾盤整假突破。",
        [
            ("價格在 SMA200 同側", {"filter_mode": "3a_sma200"}),
            ("SMA200 同側 + ADX>20", {"filter_mode": "3a_sma200_adx20"}),
            ("ADX(14) > 25", {"filter_mode": "3a_adx25"}),
        ],
    )

    _run_idea(
        "3a_trend_regime_L1", "G3", "3a (L1版) 趨勢強度與 SMA200 同側過濾（應用於 32 標的 L1）", "L1",
        "L1 涵蓋 32 個標的（含較多中型加密幣），加上 SMA200 同側與 ADX 過濾，檢驗能否剔除中型幣種的逆勢假突破。",
        [
            ("L1 + SMA200 同側", {"filter_mode": "3a_sma200"}),
            ("L1 + SMA200 同側 + ADX>20", {"filter_mode": "3a_sma200_adx20"}),
            ("L1 + ADX(14) > 25", {"filter_mode": "3a_adx25"}),
        ],
    )

    _run_idea(
        "3b_vol_filter", "G3", "3b 波動率百分位過濾（排除 ATR% 極低死魚盤與極高失控盤）", "L2",
        "計算過去 252 日 ATR/Close 百分位，剔除波動過低（死魚盤整易雙巴）或波動過高（行情末段爆量劇震）的極端區間。",
        [
            ("ATR百分位 10%~90%", {"filter_mode": "3b_vol_10_90", "filter_params": {"low_pct": 10.0, "high_pct": 90.0}}),
            ("ATR百分位 15%~85%", {"filter_mode": "3b_vol_15_85", "filter_params": {"low_pct": 15.0, "high_pct": 85.0}}),
            ("ATR百分位 20%~95%", {"filter_mode": "3b_vol_20_95", "filter_params": {"low_pct": 20.0, "high_pct": 95.0}}),
        ],
    )

    _run_idea(
        "3c_macro_sp500", "G3", "3c 大盤風險過濾（SP500 在 SMA200 下時不做多或減半做多風險資產）", "L2",
        "當美股大盤 SP500 跌破年線 SMA200（或 SMA150）處於系統性空頭時，禁止做多加密貨幣與股指（或將做多部位風險減半），保留做空與黃金商品。",
        [
            ("SP500<SMA200 禁做多風險資產", {"filter_mode": "3c_sp500_sma200_block"}),
            ("SP500<SMA150 禁做多風險資產", {"filter_mode": "3c_sp500_sma150_block"}),
            ("SP500<SMA200 做多風險減半(0.5x)", {"filter_mode": "3c_sp500_sma200_half"}),
        ],
    )

    _run_idea(
        "3d_mtf_weekly", "G3", "3d 多週期一致過濾（週線趨勢方向與日線突破訊號同向才做）", "L2",
        "嚴格以已收盤週線計算 10 週均線、20 週均線與 12 週動能，只有當大一級的週線趨勢與日線 55 日突破同向時才進場。",
        [
            ("週線收盤在 10週均線同側", {"filter_mode": "3d_wk_sma10"}),
            ("週線收盤在 20週均線同側", {"filter_mode": "3d_wk_sma20"}),
            ("週線 12週動能同向", {"filter_mode": "3d_wk_roc12"}),
        ],
    )

    # =======================================================================
    # GROUP 4: 訊號合議 (Signal Consensus)
    # =======================================================================
    _run_idea(
        "4a_daily_consensus", "G4", "4a 長線策略合議（L1、L2、L4/F4 週線趨勢同向才做或動態加減碼）", "L2",
        "當日線突破與長週期趨勢（SMA100 / SMA200 週線趨勢）達成 2 個以上策略同向合議時才進場，或依同意數量將單筆風險按 0.6x~1.4x 動態調整。",
        [
            ("至少 2 策略同向才進場", {"consensus_mode": "4a_strict_2"}),
            ("至少 2 策略同向 + 3策略全同向加碼1.25x", {"consensus_mode": "4a_strict_2_boost3"}),
            ("動態合議加減碼 (1同向0.6x / 2同向1x / 3同向1.4x)", {"consensus_mode": "4a_scale_aggr"}),
        ],
    )

    _run_idea(
        "4b_short_term_filter", "G4", "4b 短線策略（A–G）當過濾器（日線訊號＋當天 1h 短線同向訊號）", "L2",
        "將單獨使用無優勢的 1h 短線策略（A–G）降級為進場過濾器：當日線出現突破且同日 1h 出現同向短線突破/回踩/結構訊號時才進場。",
        [
            ("同日 1h 順勢/突破 (B/C/D) 同向", {"consensus_mode": "4b_1h_bcd_24h"}),
            ("24h 內任一短線 (A–G) 同向", {"consensus_mode": "4b_1h_any_24h"}),
            ("48h 內結構/動量 (C/D/F/G) 同向", {"consensus_mode": "4b_1h_struct_48h"}),
        ],
        use_intraday_split=True,
    )

    # =======================================================================
    # GROUP 5: 組合配置 (Portfolio Allocation, Position Caps & Single-Slot Ranking)
    # =======================================================================
    # Compute Dev-period volatility and correlation matrix across L1, L2, L3, L4, F4
    strat_keys = ["L1", "L2", "L3", "L4", "F4"]
    dev_daily_vecs = np.vstack([baselines[k]["daily_10y_config"]["dev"]["daily_ret"] for k in strat_keys])
    dev_stds = np.std(dev_daily_vecs, axis=1, ddof=1)
    inv_vols = 1.0 / np.maximum(1e-6, dev_stds)
    rp_weights_arr = inv_vols / np.sum(inv_vols)
    rp_weights = {k: round(float(rp_weights_arr[i]), 4) for i, k in enumerate(strat_keys)}

    # Correlation matrix on Dev
    corr_mat = np.corrcoef(dev_daily_vecs)
    avg_corr = np.mean(corr_mat, axis=1)
    # Penalize high correlation with rest of portfolio, weight by (1 - avg_corr) * inv_vol
    div_score = np.maximum(0.05, (1.0 - avg_corr)) * inv_vols
    corr_weights_arr = div_score / np.sum(div_score)
    corr_weights = {k: round(float(corr_weights_arr[i]), 4) for i, k in enumerate(strat_keys)}
    eq_weights = {k: 0.20 for k in strat_keys}

    # Build stress trade lists for L1..F4
    trades_by_strat_cfg = {k: baselines[k]["trades_obj"] for k in strat_keys}
    trades_by_strat_mt5 = {k: baselines[k]["trades_mt5_obj"] for k in strat_keys}
    trades_by_strat_str: dict[str, list[SimTradeRecord]] = {}
    for k, syms in [("L1", l1_syms), ("L2", l2_syms), ("L4", l4_syms), ("F4", l4_syms)]:
        t_s, _ = simulate_variant(k, daily_caches, syms, inst_stress, config, slip_stress)
        trades_by_strat_str[k] = t_s
    trades_by_strat_str["L3"] = tr_l3_str

    def _run_5a_portfolio(pkwargs: dict[str, Any]) -> tuple[list[SimTradeRecord], list[SimTradeRecord], list[SimTradeRecord], dict[str, Any]]:
        w = pkwargs["weights"]
        tc = combine_weighted_strategy_trades(trades_by_strat_cfg, w)
        tm = combine_weighted_strategy_trades(trades_by_strat_mt5, w)
        ts = combine_weighted_strategy_trades(trades_by_strat_str, w)
        return tc, tm, ts, {"fill_rate_pct": 100.0}

    _run_idea(
        "5a_multi_combo", "G5", "5a 多策略組合配置（L1+L2+L3+L4+F4：等權 / 風險平價 / 相關性調權）", "L2",
        "將日線趨勢（L1、L2）、雙動能（L3）與週線主流趨勢（L4、F4）組合成多策略投資組合，利用策略間低相關性（約 0.35~0.55）平滑回撤並提升 MAR。",
        [
            ("等權配置 (各20%)", {"weights": eq_weights}),
            ("風險平價 (依開發段波動率反比)", {"weights": rp_weights}),
            ("依相關性與波動率調權 (拉高L4/F4低相關權重)", {"weights": corr_weights}),
        ],
        custom_runner=_run_5a_portfolio,
    )

    _run_idea(
        "5b_max_positions", "G5", "5b 同時最多持倉數與總風險上限（最多 1 單 / 3 單 / 5 單）", "L2",
        "限制同時最多持倉檔數為 1 檔、3 檔或 5 檔（對應總開放風險上限 2%、4%、6%），測試集中持倉對回撤與資金利用率的影響。",
        [
            ("最多 1 單 (總風險上限 2%)", {"max_concurrent": 1, "max_total_risk_pct": 2.0}),
            ("最多 3 單 (總風險上限 4%)", {"max_concurrent": 3, "max_total_risk_pct": 4.0}),
            ("最多 5 單 (總風險上限 6%)", {"max_concurrent": 5, "max_total_risk_pct": 6.0}),
        ],
    )

    # Compute Dev expectancy by symbol and by strategy for 5c ranking
    dev_t0, dev_t1, _ = DAILY_SPLITS["dev"]
    dev_exp_by_sym: dict[str, float] = {}
    for s in l2_syms:
        s_trades = [t.r_multiple for t in baselines["L2"]["trades_obj"] if t.symbol == s and dev_t0 <= t.entry_ts_utc < dev_t1]
        dev_exp_by_sym[s] = float(np.mean(s_trades)) if s_trades else 0.0

    dev_exp_by_strat = {
        k: baselines[k]["daily_10y_config"]["dev"]["expectancy_r"] for k in strat_keys
    }

    def _run_5c_ranking(pkwargs: dict[str, Any]) -> tuple[list[SimTradeRecord], list[SimTradeRecord], list[SimTradeRecord], dict[str, Any]]:
        rmode = pkwargs["ranking_mode"]
        tc = combine_weighted_strategy_trades(trades_by_strat_cfg, corr_weights, max_concurrent=1, ranking_mode=rmode, dev_exp_by_strat=dev_exp_by_strat)
        tm = combine_weighted_strategy_trades(trades_by_strat_mt5, corr_weights, max_concurrent=1, ranking_mode=rmode, dev_exp_by_strat=dev_exp_by_strat)
        ts = combine_weighted_strategy_trades(trades_by_strat_str, corr_weights, max_concurrent=1, ranking_mode=rmode, dev_exp_by_strat=dev_exp_by_strat)
        return tc, tm, ts, {"fill_rate_pct": 100.0}

    _run_idea(
        "5c_single_slot_rank", "G5", "5c 「一次只能一單」選單排序（期望值 / 停損距離 / 趨勢強度 / 動能）", "L2",
        "在考試或競賽「一次只能持有一單」限制下，當同日出現多個候選訊號時，比較依歷史期望值、停損距離最緊、ADX 趨勢強度與 20 日動能排序的效果。",
        [
            ("排序1：歷史期望值 R 最高優先", {"ranking_mode": "exp_r"}),
            ("排序2：停損距離 % 最緊優先 (資金效率高)", {"ranking_mode": "sl_dist"}),
            ("排序3：ADX(14) 趨勢強度最高優先", {"ranking_mode": "adx_trend"}),
            ("排序4：20日動能 (ROC) 最強最新訊號優先", {"ranking_mode": "roc_latest"}),
        ],
        custom_runner=_run_5c_ranking,
    )

    # =======================================================================
    # GROUP 6: S2 救援（只測一次）
    # =======================================================================
    s2_cache, s2_daily_filter_map = build_s2_4h_cache(bars_1h, daily_caches, inst_cfg)

    def _run_6_s2(pkwargs: dict[str, Any]) -> tuple[list[SimTradeRecord], list[SimTradeRecord], list[SimTradeRecord], dict[str, Any]]:
        fv = pkwargs["filter_variant"]
        tc = simulate_s2_rescue(s2_cache, s2_daily_filter_map, inst_cfg, config, filter_variant=fv, slippage_bps=slip_cfg)
        tm = simulate_s2_rescue(s2_cache, s2_daily_filter_map, inst_mt5, config, filter_variant=fv, slippage_bps=slip_mt5)
        ts = simulate_s2_rescue(s2_cache, s2_daily_filter_map, inst_stress, config, filter_variant=fv, slippage_bps=slip_stress)
        return tc, tm, ts, {"fill_rate_pct": 100.0}

    _run_idea(
        "6_s2_rescue", "G6", "6 S2 4小時突破救援測試（加上群組 3 環境過濾，只測一次）", "L2",
        "檢驗未通過驗證的 S2（4h 突破）在加上日線 SMA200+ADX>25、波動率+SP500 風險過濾、或週線+日線多週期共振後能否轉正；若仍未達標即正式結案。",
        [
            ("S2 + 日線 SMA200 同側 & ADX>25", {"filter_variant": "6a_sma200_adx25"}),
            ("S2 + 波動率 [20%,90%] & SP500>SMA200", {"filter_variant": "6b_vol_sp500"}),
            ("S2 + 週線 & 日線多週期同向 (無外匯)", {"filter_variant": "6c_mtf_no_fx"}),
        ],
        use_intraday_split=True,
        custom_runner=_run_6_s2,
    )

    # Also record unmodified S2 baseline on intraday split for reference
    s2_orig_tc = simulate_s2_rescue(s2_cache, s2_daily_filter_map, inst_cfg, config, filter_variant="none", slippage_bps=slip_cfg)
    baselines["S2_intraday"] = {
        "intraday_config": evaluate_trades_across_splits(s2_orig_tc, INTRADAY_SPLITS),
    }

    # -----------------------------------------------------------------------
    # Candidate Selection (Dev -> Val -> Top 5 -> Lockbox) & Statistical Tests
    # -----------------------------------------------------------------------
    qualified_pool = [ir for ir in ideas_results if ir["passes_to_candidate_pool"]]
    # Sort qualified candidates by Val MAR + Val Expectancy R (never using Lockbox!)
    qualified_pool.sort(
        key=lambda ir: (
            ir["best_eval"]["config_metrics"]["val"]["mar_ratio"]
            + 2.0 * ir["best_eval"]["config_metrics"]["val"]["expectancy_r"]
        ),
        reverse=True,
    )
    top5_candidates = qualified_pool[:5]

    # Compute DSR & Bootstrap p-values for all ideas (and specifically Top 5)
    l2_full_daily_ret = np.concatenate([
        l2_base_daily_seg["dev"]["daily_ret"],
        l2_base_daily_seg["val"]["daily_ret"],
        l2_base_daily_seg["lockbox"]["daily_ret"],
    ])
    l2_intra_daily_ret = np.concatenate([
        l2_base_intra_seg["dev"]["daily_ret"],
        l2_base_intra_seg["val"]["daily_ret"],
        l2_base_intra_seg["lockbox"]["daily_ret"],
    ])

    for ir in ideas_results:
        cm = ir["best_eval"]["config_metrics"]
        cand_ret = np.concatenate([cm["dev"]["daily_ret"], cm["val"]["daily_ret"], cm["lockbox"]["daily_ret"]])
        base_ret = l2_intra_daily_ret if ir["split_type"] == "intraday_5y" else l2_full_daily_ret
        stats_test = compute_deflated_sharpe_and_bootstrap(
            cand_ret, base_ret, all_trial_sharpes_dev, n_bootstrap=2000, block_size=10
        )
        ir["statistical_tests"] = stats_test

        # Scenario simulations
        t_obj = ir["best_eval"]["trades_obj"]
        t_mt5_obj = ir["best_eval"]["trades_mt5_obj"]
        kr_start = "2021-07-01T00:00:00Z" if ir["split_type"] == "intraday_5y" else "2019-01-01T00:00:00Z"
        th_start = "2024-08-16T00:00:00Z" if ir["split_type"] == "intraday_5y" else "2022-11-17T00:00:00Z"
        ir["scenario_kraken_exam"] = simulate_kraken_prop_exam(t_obj, start_ts=kr_start)
        ir["scenario_the5ers_contest"] = simulate_the5ers_weekly_contest(t_mt5_obj, start_ts=th_start)

    # Scenario simulations for Baselines (L2, L1, L4)
    for b_key in ("L2", "L1", "L4"):
        baselines[b_key]["scenario_kraken_exam"] = simulate_kraken_prop_exam(
            baselines[b_key]["trades_obj"], start_ts="2019-01-01T00:00:00Z"
        )
        baselines[b_key]["scenario_the5ers_contest"] = simulate_the5ers_weekly_contest(
            baselines[b_key]["trades_mt5_obj"], start_ts="2022-11-17T00:00:00Z"
        )

    # Strip raw numpy arrays and trade objects before JSON serialization
    def _clean_metrics_dict(md: dict[str, Any]) -> dict[str, Any]:
        out_m: dict[str, Any] = {}
        for k, v in md.items():
            if isinstance(v, dict):
                out_m[k] = {sk: sv for sk, sv in v.items() if sk != "daily_ret"}
            else:
                out_m[k] = v
        return out_m

    clean_baselines: dict[str, Any] = {}
    for bk, bv in baselines.items():
        clean_baselines[bk] = {
            k: (_clean_metrics_dict(v) if isinstance(v, dict) and ("dev" in v or "runs" in v or "weeks" in v) else v)
            for k, v in bv.items()
            if k not in ("trades_obj", "trades_mt5_obj")
        }

    clean_ideas: list[dict[str, Any]] = []
    for ir in ideas_results:
        be = ir["best_eval"]
        ir_copy = {k: v for k, v in ir.items() if k != "best_eval"}
        ir_copy["config_metrics"] = _clean_metrics_dict(be["config_metrics"])
        ir_copy["mt5_metrics"] = _clean_metrics_dict(be["mt5_metrics"])
        ir_copy["stress_metrics"] = _clean_metrics_dict(be["stress_metrics"])
        clean_ideas.append(ir_copy)

    top5_ids = [c["idea_id"] for c in top5_candidates]
    clean_top5 = [ci for ci in clean_ideas if ci["idea_id"] in top5_ids]
    # Preserve top5 order
    clean_top5.sort(key=lambda x: top5_ids.index(x["idea_id"]))

    corr_table = {
        strat_keys[i]: {strat_keys[j]: round(float(corr_mat[i, j]), 3) for j in range(len(strat_keys))}
        for i in range(len(strat_keys))
    }

    summary_payload = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_sec": round(time.time() - t_start, 2),
        "splits": {
            "daily_10y": DAILY_SPLITS,
            "intraday_5y": INTRADAY_SPLITS,
        },
        "total_ideas_tested": len(ideas_results),
        "total_parameter_combinations_tested": len(all_trial_sharpes_dev),
        "portfolio_weights_5a": {
            "equal_weight": eq_weights,
            "risk_parity": rp_weights,
            "correlation_adjusted": corr_weights,
            "dev_correlation_matrix": corr_table,
        },
        "baselines": clean_baselines,
        "ideas": clean_ideas,
        "top5_candidates": clean_top5,
    }
    return summary_payload


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return str(obj)


def main() -> None:
    out_dir = Path(__file__).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)
    res = run_all_experiments()
    out_path = out_dir / "results.json"
    out_path.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    print(f"Saved combo research results to {out_path} in {res['elapsed_sec']}s")
    print(f"Total ideas: {res['total_ideas_tested']}, total param combos: {res['total_parameter_combinations_tested']}")
    print(f"Top-5 Candidates entering Lockbox: {[c['idea_id'] for c in res['top5_candidates']]}")


if __name__ == "__main__":
    main()
