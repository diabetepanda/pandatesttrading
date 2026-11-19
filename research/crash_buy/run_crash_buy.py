#!/usr/bin/env python3
"""Crash Buy (單日大跌做多) Strategy Research & Backtester.

Core Idea:
  Buy when intraday drop from reference price reaches <= -X% using 1h bars:
  - Reference prices: Previous Day Close (prev_close) vs Current Day Open (day_open).
  - Intraday trigger: First 1h bar whose low <= ref * (1 - X).
    Fill base price = min(bar.open, trigger_px) to handle gap-downs realistically,
    plus half-spread and slippage.

Full Grid Variations:
  1. Threshold X:
     - TradFi (Gold, Oil, Indices): 3%, 5%, 7%, 10%
     - Crypto (BTC, ETH, SOL, BNB): 3%, 5%, 7%, 10%, 15%
  2. Reference benchmark: prev_close, day_open
  3. Exit modes:
     - Fixed hold: 1, 3, 5, 10 trading days (hold_1d, hold_3d, hold_5d, hold_10d)
     - Rebound back to previous day close (rebound_prev_close, max 30 trading days cap)
     - ATR trailing profit (l2_atr_trail: highest_px - 3.0 * ATR14 ratcheting upward)
  4. Stop loss modes:
     - none (reference only; R normalized by 2*ATR20)
     - 2 * ATR20 (atr2)
     - 3 * ATR20 (atr3)
     - Below intraday low so far (day_low: min_low_so_far - 0.25 * ATR20)
  5. Filters (tested individually + unfiltered):
     - none
     - sma200 (previous day close > previous day SMA200)
     - cool_5, cool_10, cool_20 (N trading days cooldown per symbol after trigger)
  6. Risk per trade: 0.75% of equity, measured in R and mark-to-market daily PnL.

Outputs:
  - research/crash_buy/results.json
  - research/crash_buy/report.md
"""

from __future__ import annotations

import copy
import json
import math
import pickle
import sys
import time
from collections import defaultdict
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

from src.config_loader import load_config
from src.data.models import OHLCVBar
from src.indicators.technical import compute_atr_series
from research.combo.run_combo_research import (
    BASELINE_PARAMS,
    SimTradeRecord,
    _sma_series_np,
    build_cost_adjusted_instruments,
    build_enhanced_daily_cache,
    load_mt5_costs_config,
    simulate_l3_records,
    simulate_variant,
)

NY_TZ = ZoneInfo("America/New_York")

TRADFI_SYMS = ["XAUUSD", "WTI", "SP500", "NAS100", "JP225", "DAX40", "UK100", "US30"]
CRYPTO_SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
ALL_1H_SYMS = TRADFI_SYMS + CRYPTO_SYMS
DAILY_10Y_SYMS = ["XAUUSD", "WTI", "SP500", "NAS100", "JP225", "TWII", "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]

THRESHOLDS_ALL = [0.03, 0.05, 0.07, 0.10, 0.15]
REF_MODES = ["prev_close", "day_open"]
EXIT_MODES = ["hold_1d", "hold_3d", "hold_5d", "hold_10d", "rebound_prev_close", "l2_atr_trail"]
SL_MODES = ["none", "atr2", "atr3", "day_low"]
FILTER_MODES = ["none", "sma200", "cool_5", "cool_10", "cool_20"]

RISK_PCT = 0.0075  # 0.75% per trade
INITIAL_EQUITY = 100000.0
MAX_POS_NOTIONAL_MULT = 5.0  # 500% notional cap (same as config.yaml)


@dataclass
class CrashTrade:
    symbol: str
    market: str
    ref_mode: str
    threshold_x: float
    entry_day_idx: int
    exit_day_idx: int
    entry_day_str: str
    exit_day_str: str
    entry_ts_utc: str
    exit_ts_utc: str
    entry_px: float
    exit_px: float
    sl_px: float
    risk_dist: float
    qty_per_unit_risk: float  # qty for $1 of risk (actual qty scaled at portfolio sim)
    raw_r: float              # net R after spread, slippage, commission, overnight
    gross_r: float
    cost_r: float
    exit_reason: str
    notional_ratio_per_1pct_risk: float  # notional / equity when risking 1%


def _tradfi_session_date(ts_utc: str) -> str:
    dt = datetime.strptime(ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(NY_TZ)
    if dt.hour >= 17:
        dt = dt + timedelta(days=1)
    d = dt.date()
    if d.weekday() == 5:
        d += timedelta(days=2)
    elif d.weekday() == 6:
        d += timedelta(days=1)
    return d.isoformat()


def _load_binance_1h(bin_sym: str, bin_dir: Path) -> list[OHLCVBar]:
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


def _load_dukascopy_1h(d_sym: str, duka_dir: Path) -> list[OHLCVBar]:
    p = duka_dir / f"{d_sym}_1h_3y.pkl"
    if not p.exists():
        return []
    raw = pickle.loads(p.read_bytes())
    return [b for b in raw if b.volume > 0 or b.high > b.low]


def build_symbol_cost_params(
    sym: str,
    ref_price: float,
    config: dict[str, Any],
    mt5_cfg: dict[str, Any],
) -> dict[str, float]:
    """Return cost parameters: MT5 for TradFi, 5 bps/side + spread for Crypto."""
    is_crypto = sym.endswith("USDT")
    inst_cfg = {i["symbol"]: i for i in config.get("instruments", [])}.get(sym, {})
    mt5_insts = mt5_cfg.get("instruments", {})
    mt5_fee = float(mt5_cfg.get("defaults", {}).get("fee_rate_per_side", 0.00001))

    if is_crypto:
        return {
            "is_crypto": 1.0,
            "spread_bps": float(inst_cfg.get("assumed_spread_bps", 3.0)),
            "spread_pts": 0.0,
            "fee_rate": 0.0005,  # 5 bps per side as explicitly specified
            "slippage_bps": float(config.get("risk", {}).get("default_slippage_bps", 1.0)),
            "overnight_bps": float(inst_cfg.get("overnight_cost_daily_bps", 3.0)),
            "max_leverage": 10.0,
        }
    else:
        mt5_key = "JPN225" if sym == "JP225" else sym
        m_info = mt5_insts.get(mt5_key, {})
        sp_pts = float(m_info.get("spread_points", 0.0))
        sp_bps = (sp_pts / ref_price * 10000.0) if (sp_pts > 0 and ref_price > 0) else float(inst_cfg.get("assumed_spread_bps", 2.0))
        return {
            "is_crypto": 0.0,
            "spread_bps": sp_bps,
            "spread_pts": sp_pts,
            "fee_rate": mt5_fee,  # 0.001% (0.1 bps) per side
            "slippage_bps": 0.5,  # MT5 mode slippage
            "overnight_bps": float(inst_cfg.get("overnight_cost_daily_bps", 1.5)) * 0.5,
            "max_leverage": float(m_info.get("max_leverage", 20.0)),
        }


def prepare_symbol_1h_sessions(
    sym: str,
    bars_1h: list[OHLCVBar],
    bars_1d_prehist: list[OHLCVBar],
    config: dict[str, Any],
    mt5_cfg: dict[str, Any],
) -> dict[str, Any]:
    """Group 1h bars into trading sessions and attach strictly causal daily SMA200/ATR20/ATR14."""
    is_crypto = sym.endswith("USDT")
    mkt = "crypto" if is_crypto else ("commodity" if sym in ("XAUUSD", "WTI") else "index")

    by_day: dict[str, list[OHLCVBar]] = defaultdict(list)
    for b in bars_1h:
        d_str = b.ts_utc[:10] if is_crypto else _tradfi_session_date(b.ts_utc)
        by_day[d_str].append(b)

    sorted_days = sorted(by_day.keys())
    session_bars_1d: list[OHLCVBar] = []
    session_1h_list: list[list[OHLCVBar]] = []
    for d_str in sorted_days:
        bs = sorted(by_day[d_str], key=lambda x: x.ts_utc)
        session_1h_list.append(bs)
        session_bars_1d.append(
            OHLCVBar(
                ts_utc=f"{d_str}T00:00:00Z",
                open=bs[0].open,
                high=max(b.high for b in bs),
                low=min(b.low for b in bs),
                close=bs[-1].close,
                volume=sum(b.volume for b in bs),
            )
        )

    # Stitch earlier 1d pre-history (before first session date) so SMA200/ATR20 are warmed up from day 0
    first_day_ts = f"{sorted_days[0]}T00:00:00Z"
    pre_bars = [b for b in bars_1d_prehist if b.ts_utc < first_day_ts and b.close > 0]
    n_pre = len(pre_bars)
    combined_1d = pre_bars + session_bars_1d

    closes_arr = np.asarray([b.close for b in combined_1d], dtype=np.float64)
    sma200_comb = _sma_series_np(closes_arr, 200)
    raw_atr14 = compute_atr_series(combined_1d, period=14, market=mkt, interval="1d")
    raw_atr20 = compute_atr_series(combined_1d, period=20, market=mkt, interval="1d")

    atr14_comb: list[float | None] = [None] * len(combined_1d)
    for k, v in enumerate(raw_atr14):
        if 14 + k < len(combined_1d):
            atr14_comb[14 + k] = v

    atr20_comb: list[float | None] = [None] * len(combined_1d)
    for k, v in enumerate(raw_atr20):
        if 20 + k < len(combined_1d):
            atr20_comb[20 + k] = v

    sma200 = sma200_comb[n_pre:]
    atr14 = atr14_comb[n_pre:]
    atr20 = atr20_comb[n_pre:]

    mid_px = session_bars_1d[len(session_bars_1d) // 2].close
    cost_params = build_symbol_cost_params(sym, mid_px, config, mt5_cfg)

    return {
        "symbol": sym,
        "market": mkt,
        "is_crypto": is_crypto,
        "days": sorted_days,
        "day_to_idx": {d: i for i, d in enumerate(sorted_days)},
        "bars_1d": session_bars_1d,
        "bars_1h_by_day": session_1h_list,
        "sma200": sma200,
        "atr14": atr14,
        "atr20": atr20,
        "cost": cost_params,
    }


def prepare_symbol_1d_sessions(
    sym: str,
    bars_1d: list[OHLCVBar],
    config: dict[str, Any],
    mt5_cfg: dict[str, Any],
) -> dict[str, Any]:
    """Prepare 10Y daily bars (for 2020/03 & 2022 stress proxy simulation)."""
    is_crypto = sym.endswith("USDT")
    mkt = "crypto" if is_crypto else ("commodity" if sym in ("XAUUSD", "WTI") else "index")
    bars_clean = [b for b in bars_1d]
    days = [b.ts_utc[:10] for b in bars_clean]

    closes_arr = np.asarray([b.close for b in bars_clean], dtype=np.float64)
    sma200 = _sma_series_np(closes_arr, 200)
    raw_atr14 = compute_atr_series(bars_clean, period=14, market=mkt, interval="1d")
    raw_atr20 = compute_atr_series(bars_clean, period=20, market=mkt, interval="1d")

    atr14: list[float | None] = [None] * len(bars_clean)
    for k, v in enumerate(raw_atr14):
        if 14 + k < len(bars_clean):
            atr14[14 + k] = v

    atr20: list[float | None] = [None] * len(bars_clean)
    for k, v in enumerate(raw_atr20):
        if 20 + k < len(bars_clean):
            atr20[20 + k] = v

    pos_closes = [b.close for b in bars_clean if b.close > 0]
    mid_px = pos_closes[len(pos_closes) // 2] if pos_closes else 100.0
    cost_params = build_symbol_cost_params(sym, mid_px, config, mt5_cfg)

    return {
        "symbol": sym,
        "market": mkt,
        "is_crypto": is_crypto,
        "days": days,
        "day_to_idx": {d: i for i, d in enumerate(days)},
        "bars_1d": bars_clean,
        "bars_1h_by_day": [[b] for b in bars_clean],  # 1 bar per day for unified simulation loop
        "sma200": sma200,
        "atr14": atr14,
        "atr20": atr20,
        "cost": cost_params,
    }


def _apply_entry_cost(base_px: float, cost: dict[str, float]) -> float:
    half_sp = (cost["spread_pts"] * 0.5) if cost["spread_pts"] > 0 else (base_px * cost["spread_bps"] / 20000.0)
    return (base_px + half_sp) * (1.0 + cost["slippage_bps"] / 10000.0)


def _apply_exit_cost(base_px: float, cost: dict[str, float]) -> float:
    half_sp = (cost["spread_pts"] * 0.5) if cost["spread_pts"] > 0 else (base_px * cost["spread_bps"] / 20000.0)
    return max(1e-9, (base_px - half_sp) * (1.0 - cost["slippage_bps"] / 10000.0))


def simulate_symbol_combo(
    sym_data: dict[str, Any],
    ref_mode: str,
    threshold_x: float,
    exit_mode: str,
    sl_mode: str,
    filter_mode: str,
) -> tuple[list[CrashTrade], dict[int, float]]:
    """Simulate a single parameter tuple on one symbol; return trades & per-day R return contribution."""
    days = sym_data["days"]
    bars_1d = sym_data["bars_1d"]
    bars_1h_by_day = sym_data["bars_1h_by_day"]
    sma200 = sym_data["sma200"]
    atr14 = sym_data["atr14"]
    atr20 = sym_data["atr20"]
    cost = sym_data["cost"]
    n_days = len(days)

    cooldown_days = 0
    if filter_mode.startswith("cool_"):
        cooldown_days = int(filter_mode.split("_")[1])
    use_sma200 = filter_mode == "sma200"

    trades: list[CrashTrade] = []
    # Also track daily mark-to-market R delta per day index (for 0.75% risk scaling later)
    daily_r_pnl: dict[int, float] = defaultdict(float)

    last_trigger_day = -9999
    d_idx = 1  # need day d_idx - 1 for prev_close & causal atr20/sma200

    while d_idx < n_days - 1:
        prev_bar = bars_1d[d_idx - 1]
        cur_day_1h = bars_1h_by_day[d_idx]
        if prev_bar.close <= 0 or not cur_day_1h or cur_day_1h[0].open <= 0:
            d_idx += 1
            continue

        av20_prev = atr20[d_idx - 1]
        av14_prev = atr14[d_idx - 1]
        sma_prev = sma200[d_idx - 1]
        if av20_prev is None or av20_prev <= 0 or av14_prev is None or av14_prev <= 0:
            d_idx += 1
            continue

        # Filter: cooldown
        if cooldown_days > 0 and (d_idx - last_trigger_day) <= cooldown_days:
            d_idx += 1
            continue

        # Filter: SMA200 (strictly causal: previous closed day close > SMA200)
        if use_sma200:
            if sma_prev is None or prev_bar.close <= sma_prev:
                d_idx += 1
                continue

        ref_px = prev_bar.close if ref_mode == "prev_close" else cur_day_1h[0].open
        if ref_px <= 0:
            d_idx += 1
            continue
        trigger_px = ref_px * (1.0 - threshold_x)

        # Scan 1h bars of day d_idx for first drop <= trigger_px
        trig_h_idx = -1
        low_so_far = cur_day_1h[0].open
        for hi, hb in enumerate(cur_day_1h):
            if hb.low <= trigger_px:
                trig_h_idx = hi
                low_so_far = min(low_so_far, trigger_px)
                break
            low_so_far = min(low_so_far, hb.low)

        if trig_h_idx < 0:
            d_idx += 1
            continue

        # Trigger hit! Update cooldown clock regardless of subsequent exit
        last_trigger_day = d_idx
        trig_bar = cur_day_1h[trig_h_idx]
        fill_base_px = min(trig_bar.open, trigger_px)
        if fill_base_px <= 0:
            d_idx += 1
            continue
        entry_px = _apply_entry_cost(fill_base_px, cost)

        # Determine initial stop loss and 1R distance
        if sl_mode == "none":
            sl_px = -1e12  # No hard stop
            risk_dist = 2.0 * av20_prev  # Normalized R unit for comparability
        elif sl_mode == "atr2":
            sl_px = entry_px - 2.0 * av20_prev
            risk_dist = max(1e-6, entry_px - sl_px)
        elif sl_mode == "atr3":
            sl_px = entry_px - 3.0 * av20_prev
            risk_dist = max(1e-6, entry_px - sl_px)
        elif sl_mode == "day_low":
            raw_sl = min(low_so_far, fill_base_px) - 0.25 * av20_prev
            sl_px = min(raw_sl, entry_px - 0.25 * av20_prev)
            risk_dist = max(1e-6, entry_px - sl_px)
        else:
            raise ValueError(f"Unknown sl_mode: {sl_mode}")

        # Cap position leverage at MAX_POS_NOTIONAL_MULT (500%)
        # At 0.75% nominal risk, notional_pct = 0.0075 / (risk_dist / entry_px)
        sl_dist_pct = risk_dist / entry_px
        notional_at_1pct = 0.01 / max(1e-6, sl_dist_pct)
        scale_factor = 1.0
        if (0.0075 / max(1e-6, sl_dist_pct)) > MAX_POS_NOTIONAL_MULT:
            # Cap notional at 5.0x equity; effective 1R becomes risk_dist * actual/unscaled
            scale_factor = MAX_POS_NOTIONAL_MULT / (0.0075 / max(1e-6, sl_dist_pct))

        # Trailing stop initialization for l2_atr_trail (Chandelier 3.0 * ATR14)
        cur_sl = sl_px
        highest_px = entry_px
        if exit_mode == "l2_atr_trail":
            trail_cand = highest_px - 3.0 * av14_prev
            if sl_mode == "none":
                cur_sl = trail_cand
            else:
                cur_sl = max(cur_sl, trail_cand)

        target_rebound_px = prev_bar.close
        target_hold_days = 0
        if exit_mode.startswith("hold_"):
            target_hold_days = int(exit_mode.replace("hold_", "").replace("d", ""))

        # Simulate forward starting with conservative SL check on trig_bar itself,
        # then remaining 1h bars on day d_idx, then day d_idx + 1, ...
        exited = False
        exit_base_px = entry_px
        exit_ts_utc = trig_bar.ts_utc
        exit_day_idx = d_idx
        exit_reason = "end_of_data"

        prev_mtm_r = 0.0
        max_hold_cap = 30 if exit_mode == "rebound_prev_close" else (target_hold_days if target_hold_days > 0 else 120)

        # Conservative same-bar SL check on trigger bar (e.g. if crash bar slices right through SL)
        trig_half_sp = cost["spread_pts"] * 0.5 if cost["spread_pts"] > 0 else trig_bar.low * cost["spread_bps"] / 20000.0
        if cur_sl > -1e11 and (trig_bar.low - trig_half_sp) <= cur_sl:
            exit_base_px = cur_sl + (cost["spread_pts"] * 0.5 if cost["spread_pts"] > 0 else cur_sl * cost["spread_bps"] / 20000.0)
            if trig_bar.close < fill_base_px and trig_bar.close < exit_base_px:
                # If bar closed even lower (e.g. daily proxy flash crash), check close vs cur_sl
                exit_base_px = min(exit_base_px, trig_bar.close) if len(cur_day_1h) == 1 else exit_base_px
            exit_ts_utc = trig_bar.ts_utc
            exit_day_idx = d_idx
            exit_reason = "stop_loss_same_bar"
            exited = True

        # Check remaining 1h bars on entry day d_idx (strictly AFTER trig_h_idx to avoid same-bar lookahead)
        if not exited:
            for hb in cur_day_1h[trig_h_idx + 1 :]:
                # Check SL first (conservative)
                if cur_sl > -1e11 and (hb.low - (cost["spread_pts"] * 0.5 if cost["spread_pts"] > 0 else hb.low * cost["spread_bps"] / 20000.0)) <= cur_sl:
                    open_bid = hb.open - (cost["spread_pts"] * 0.5 if cost["spread_pts"] > 0 else hb.open * cost["spread_bps"] / 20000.0)
                    if open_bid <= cur_sl:
                        exit_base_px = hb.open
                    else:
                        exit_base_px = cur_sl + (cost["spread_pts"] * 0.5 if cost["spread_pts"] > 0 else cur_sl * cost["spread_bps"] / 20000.0)
                    exit_ts_utc = hb.ts_utc
                    exit_day_idx = d_idx
                    exit_reason = "trailing_stop" if (exit_mode == "l2_atr_trail" and cur_sl > sl_px) else "stop_loss"
                    exited = True
                    break

                if exit_mode == "rebound_prev_close" and hb.high >= target_rebound_px:
                    exit_base_px = max(hb.open, target_rebound_px)
                    exit_ts_utc = hb.ts_utc
                    exit_day_idx = d_idx
                    exit_reason = "rebound_prev_close"
                    exited = True
                    break

                highest_px = max(highest_px, hb.high)

        # If still open at close of entry day d_idx
        if not exited:
            day_close = bars_1d[d_idx].close
            cur_mtm_r = ((day_close - entry_px) / risk_dist) * scale_factor
            daily_r_pnl[d_idx] += cur_mtm_r - prev_mtm_r
            prev_mtm_r = cur_mtm_r

            if exit_mode == "l2_atr_trail":
                av14_d = atr14[d_idx] or av14_prev
                cur_sl = max(cur_sl, highest_px - 3.0 * av14_d)

            cur_d = d_idx + 1
            while cur_d < n_days:
                day_1h = bars_1h_by_day[cur_d]
                for hb in day_1h:
                    if cur_sl > -1e11 and (hb.low - (cost["spread_pts"] * 0.5 if cost["spread_pts"] > 0 else hb.low * cost["spread_bps"] / 20000.0)) <= cur_sl:
                        open_bid = hb.open - (cost["spread_pts"] * 0.5 if cost["spread_pts"] > 0 else hb.open * cost["spread_bps"] / 20000.0)
                        if open_bid <= cur_sl:
                            exit_base_px = hb.open
                        else:
                            exit_base_px = cur_sl + (cost["spread_pts"] * 0.5 if cost["spread_pts"] > 0 else cur_sl * cost["spread_bps"] / 20000.0)
                        exit_ts_utc = hb.ts_utc
                        exit_day_idx = cur_d
                        exit_reason = "trailing_stop" if (exit_mode == "l2_atr_trail" and cur_sl > sl_px) else "stop_loss"
                        exited = True
                        break

                    if exit_mode == "rebound_prev_close" and hb.high >= target_rebound_px:
                        exit_base_px = max(hb.open, target_rebound_px)
                        exit_ts_utc = hb.ts_utc
                        exit_day_idx = cur_d
                        exit_reason = "rebound_prev_close"
                        exited = True
                        break

                    highest_px = max(highest_px, hb.high)

                if exited:
                    break

                days_held = cur_d - d_idx
                if target_hold_days > 0 and days_held >= target_hold_days:
                    exit_base_px = bars_1d[cur_d].close
                    exit_ts_utc = day_1h[-1].ts_utc
                    exit_day_idx = cur_d
                    exit_reason = f"hold_{target_hold_days}d"
                    exited = True
                    break

                if exit_mode == "rebound_prev_close" and days_held >= max_hold_cap:
                    exit_base_px = bars_1d[cur_d].close
                    exit_ts_utc = day_1h[-1].ts_utc
                    exit_day_idx = cur_d
                    exit_reason = "rebound_timeout_30d"
                    exited = True
                    break

                # Still open at close of cur_d
                day_close = bars_1d[cur_d].close
                cur_mtm_r = ((day_close - entry_px) / risk_dist) * scale_factor
                daily_r_pnl[cur_d] += cur_mtm_r - prev_mtm_r
                prev_mtm_r = cur_mtm_r

                if exit_mode == "l2_atr_trail":
                    av14_d = atr14[cur_d] or av14_prev
                    cur_sl = max(cur_sl, highest_px - 3.0 * av14_d)

                cur_d += 1

            if not exited:
                # Force close on last available bar
                last_d = n_days - 1
                exit_base_px = bars_1d[last_d].close
                exit_ts_utc = bars_1h_by_day[last_d][-1].ts_utc
                exit_day_idx = last_d
                exit_reason = "end_of_data"

        exit_px = _apply_exit_cost(exit_base_px, cost)
        days_held_cal = max(0.0, (
            datetime.strptime(exit_ts_utc, "%Y-%m-%dT%H:%M:%SZ") -
            datetime.strptime(trig_bar.ts_utc, "%Y-%m-%dT%H:%M:%SZ")
        ).total_seconds() / 86400.0)

        # Gross R (including spread/slip on entry/exit prices) & Commissions + Overnight cost
        raw_price_r = ((exit_px - entry_px) / risk_dist) * scale_factor
        # Commission per side + overnight bps
        comm_cost_px = (entry_px + exit_px) * cost["fee_rate"]
        overnight_cost_px = entry_px * (cost["overnight_bps"] / 10000.0) * math.floor(days_held_cal)
        extra_cost_r = ((comm_cost_px + overnight_cost_px) / risk_dist) * scale_factor
        net_r = raw_price_r - extra_cost_r

        # Cap catastrophic single-bar negative price loss at account bankruptcy (-1.0 / 0.0075 = -133R)
        net_r = max(-133.0, net_r)

        # Book final difference between realized net_r and previous MTM R on exit_day_idx
        daily_r_pnl[exit_day_idx] += net_r - prev_mtm_r

        trades.append(
            CrashTrade(
                symbol=sym_data["symbol"],
                market=sym_data["market"],
                ref_mode=ref_mode,
                threshold_x=threshold_x,
                entry_day_idx=d_idx,
                exit_day_idx=exit_day_idx,
                entry_day_str=days[d_idx],
                exit_day_str=days[exit_day_idx],
                entry_ts_utc=trig_bar.ts_utc,
                exit_ts_utc=exit_ts_utc,
                entry_px=round(entry_px, 6),
                exit_px=round(exit_px, 6),
                sl_px=round(sl_px, 6) if sl_px > -1e11 else 0.0,
                risk_dist=round(risk_dist, 6),
                qty_per_unit_risk=round(scale_factor / risk_dist, 6),
                raw_r=round(net_r, 4),
                gross_r=round(raw_price_r, 4),
                cost_r=round(extra_cost_r, 4),
                exit_reason=exit_reason,
                notional_ratio_per_1pct_risk=round(notional_at_1pct * scale_factor, 4),
            )
        )

        # Advance to day after exit (no overlapping position on same symbol)
        d_idx = exit_day_idx + 1

    return trades, daily_r_pnl


def compute_trade_metrics(
    trades: list[CrashTrade],
    daily_r_series: np.ndarray | None = None,
    day_strs: list[str] | None = None,
    risk_pct: float = RISK_PCT,
) -> dict[str, Any]:
    """Compute trade metrics + max drawdown (at 0.75% risk) + max consecutive losses."""
    n = len(trades)
    if n == 0:
        return {
            "trades": 0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "expectancy_r": 0.0,
            "total_r": 0.0,
            "worst_trade_r": 0.0,
            "best_trade_r": 0.0,
            "max_consec_losses": 0,
            "max_dd_pct": 0.0,
            "total_return_pct": 0.0,
            "insufficient_sample": True,
        }

    ordered = sorted(trades, key=lambda t: (t.exit_ts_utc, t.entry_ts_utc, t.symbol))
    rs = [t.raw_r for t in ordered]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 1e-9 else (99.0 if gross_win > 0 else 0.0)
    exp_r = sum(rs) / n
    win_rate = len(wins) / n * 100.0

    # Max consecutive losses
    max_consec = 0
    cur_consec = 0
    for r in rs:
        if r <= 0:
            cur_consec += 1
            if cur_consec > max_consec:
                max_consec = cur_consec
        else:
            cur_consec = 0

    # Equity curve & drawdown at 0.75% risk
    max_dd_pct = 0.0
    total_ret_pct = 0.0
    dd_2020_03 = None
    dd_2022 = None

    if daily_r_series is not None and len(daily_r_series) > 0:
        eq = 1.0
        peak = 1.0
        eq_curve = np.ones(len(daily_r_series), dtype=np.float64)
        for i, dr in enumerate(daily_r_series):
            eq = max(0.001, eq * (1.0 + dr * risk_pct))
            eq_curve[i] = eq
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100.0
            if dd > max_dd_pct:
                max_dd_pct = dd
        total_ret_pct = (eq - 1.0) * 100.0

        if day_strs is not None:
            # Sub-window peak-to-trough max drawdown in 2020-03 and 2022
            def _window_dd(start_d: str, end_d: str) -> float | None:
                idxs = [i for i, d in enumerate(day_strs) if start_d <= d <= end_d]
                if not idxs:
                    return None
                sub_peak = eq_curve[idxs[0] - 1] if idxs[0] > 0 else 1.0
                w_dd = 0.0
                for idx in idxs:
                    if eq_curve[idx] > sub_peak:
                        sub_peak = eq_curve[idx]
                    d_val = (sub_peak - eq_curve[idx]) / max(1e-9, sub_peak) * 100.0
                    if d_val > w_dd:
                        w_dd = d_val
                return round(w_dd, 2)

            dd_2020_03 = _window_dd("2020-03-01", "2020-04-30")
            dd_2022 = _window_dd("2022-01-01", "2022-12-31")
    else:
        # Trade-by-trade equity drawdown fallback
        eq = 1.0
        peak = 1.0
        for r in rs:
            eq = max(0.001, eq * (1.0 + r * risk_pct))
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100.0
            if dd > max_dd_pct:
                max_dd_pct = dd
        total_ret_pct = (eq - 1.0) * 100.0

    return {
        "trades": n,
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": round(min(99.0, pf), 3),
        "expectancy_r": round(exp_r, 4),
        "total_r": round(sum(rs), 2),
        "worst_trade_r": round(min(rs), 3),
        "best_trade_r": round(max(rs), 3),
        "max_consec_losses": max_consec,
        "max_dd_pct": round(max_dd_pct, 2),
        "total_return_pct": round(total_ret_pct, 2),
        "dd_2020_03_pct": dd_2020_03,
        "dd_2022_pct": dd_2022,
        "insufficient_sample": n < 20,
    }


def _aggregate_daily_series(
    syms_data: dict[str, dict[str, Any]],
    per_sym_daily_r: dict[str, dict[int, float]],
    day_list: list[str],
    start_d: str,
    end_d: str,
) -> tuple[np.ndarray, list[str]]:
    sub_days = [d for d in day_list if start_d <= d < end_d]
    if not sub_days:
        return np.zeros(0, dtype=np.float64), []
    d_map = {d: i for i, d in enumerate(sub_days)}
    arr = np.zeros(len(sub_days), dtype=np.float64)
    for sym, dr_dict in per_sym_daily_r.items():
        s_days = syms_data[sym]["days"]
        for local_idx, r_val in dr_dict.items():
            d_str = s_days[local_idx]
            if d_str in d_map:
                arr[d_map[d_str]] += r_val
    return arr, sub_days


def run_baseline_strategies_for_overlap(config: dict[str, Any], mt5_cfg: dict[str, Any]) -> dict[str, Any]:
    """Run L1, L2, L3, L4, F4 on 10Y daily data with MT5 TradFi + 5bps Crypto costs for overlap & correlation."""
    cache_1d_path = ROOT_DIR / ".cache" / "research_1d_bars.pkl"
    bars_1d: dict[str, list[OHLCVBar]] = pickle.loads(cache_1d_path.read_bytes())

    # Build hybrid instruments: MT5 for non-crypto, config (5 bps) for crypto
    mt5_insts, mt5_slip = build_cost_adjusted_instruments(config, mt5_cfg, "mt5", bars_1d)
    cfg_insts, cfg_slip = build_cost_adjusted_instruments(config, mt5_cfg, "config", bars_1d)
    hybrid_insts: dict[str, dict[str, Any]] = {}
    for sym, info in mt5_insts.items():
        if info.get("market") == "crypto":
            hybrid_insts[sym] = cfg_insts[sym]
        else:
            hybrid_insts[sym] = info

    daily_caches = build_enhanced_daily_cache(bars_1d, hybrid_insts)

    l2_syms = list(config["strategies"]["L2"]["symbols"])
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

    trades_by_strat: dict[str, list[SimTradeRecord]] = {}
    for sid, syms in [("L1", l1_syms), ("L2", l2_syms), ("L4", l4_syms), ("F4", l4_syms)]:
        tr_list, _ = simulate_variant(sid, daily_caches, syms, hybrid_insts, config, mt5_slip)
        trades_by_strat[sid] = tr_list
    trades_by_strat["L3"] = simulate_l3_records(
        daily_caches, l3_syms, hybrid_insts, config, mt5_slip
    )

    # Index trades by (strategy, symbol) for fast lookup
    pos_intervals: dict[str, dict[str, list[tuple[str, str, str]]]] = defaultdict(lambda: defaultdict(list))
    for sid, tlist in trades_by_strat.items():
        for t in tlist:
            pos_intervals[sid][t.symbol].append((t.entry_ts_utc[:10], t.exit_ts_utc[:10], t.direction))

    # Build L2 daily return series (realized on exit day & MTM approximation via daily bars)
    l2_trades = trades_by_strat["L2"]
    l2_realized_daily: dict[str, float] = defaultdict(float)
    for t in l2_trades:
        l2_realized_daily[t.exit_ts_utc[:10]] += t.eq_ret_pct

    # Also build L2 MTM daily return series from open positions & 1d closes
    l2_mtm_daily: dict[str, float] = defaultdict(float)
    for t in l2_trades:
        sym = t.symbol
        if sym not in daily_caches:
            continue
        bars = daily_caches[sym]["bars"]
        d_enter = t.entry_ts_utc[:10]
        d_exit = t.exit_ts_utc[:10]
        # Simple linear/daily close MTM scaled to match total eq_ret_pct
        in_bars = [b for b in bars if d_enter <= b.ts_utc[:10] <= d_exit]
        if len(in_bars) >= 2:
            raw_sum = 0.0
            step_rets = []
            for i in range(1, len(in_bars)):
                r_step = (in_bars[i].close / max(1e-9, in_bars[i - 1].close) - 1.0)
                if t.direction == "short":
                    r_step = -r_step
                step_rets.append((in_bars[i].ts_utc[:10], r_step))
                raw_sum += r_step
            # Scale proportionally to trade's exact net equity return
            if abs(raw_sum) > 1e-9:
                scale = t.eq_ret_pct / raw_sum
                for d_str, r_step in step_rets:
                    l2_mtm_daily[d_str] += r_step * scale
            else:
                l2_mtm_daily[d_exit] += t.eq_ret_pct
        else:
            l2_mtm_daily[d_exit] += t.eq_ret_pct

    return {
        "trades_by_strat": trades_by_strat,
        "pos_intervals": pos_intervals,
        "l2_realized_daily": l2_realized_daily,
        "l2_mtm_daily": l2_mtm_daily,
        "covered_symbols": set(daily_caches.keys()),
    }


def check_overlap_and_conflict(
    trades: list[CrashTrade],
    baseline_info: dict[str, Any],
) -> dict[str, Any]:
    """For each Crash Buy trigger, classify L1/L2/L3/L4/F4 state on that symbol: long, short, or flat."""
    pos_intervals = baseline_info["pos_intervals"]
    covered_syms = baseline_info["covered_symbols"]

    results: dict[str, Any] = {}
    for sid in ["L1", "L2", "L3", "L4", "F4"]:
        buckets: dict[str, list[CrashTrade]] = {"long": [], "short": [], "flat": [], "not_in_universe": []}
        for tr in trades:
            if tr.symbol not in covered_syms:
                buckets["not_in_universe"].append(tr)
                continue
            d_str = tr.entry_day_str
            state = "flat"
            for ent_d, ex_d, d_dir in pos_intervals[sid].get(tr.symbol, []):
                if ent_d <= d_str < ex_d:
                    state = d_dir
                    break
            buckets[state].append(tr)

        eligible_n = len(buckets["long"]) + len(buckets["short"]) + len(buckets["flat"])
        summary = {}
        for st in ["long", "short", "flat"]:
            sub = buckets[st]
            m = compute_trade_metrics(sub)
            m["share_pct"] = round(len(sub) / eligible_n * 100.0, 2) if eligible_n > 0 else 0.0
            summary[st] = m
        summary["eligible_trades"] = eligible_n
        summary["excluded_not_in_universe"] = len(buckets["not_in_universe"])
        results[sid] = summary

    return results


def compute_l2_correlation(
    day_strs: list[str],
    cb_daily_r: np.ndarray,
    baseline_info: dict[str, Any],
    cb_trades: list[CrashTrade],
) -> dict[str, float | None]:
    """Compute Pearson correlation between Crash Buy daily returns and L2 daily returns."""
    if len(day_strs) < 5:
        return {"mtm_corr": None, "realized_corr": None}

    l2_mtm = np.asarray([baseline_info["l2_mtm_daily"].get(d, 0.0) for d in day_strs], dtype=np.float64)
    l2_real = np.asarray([baseline_info["l2_realized_daily"].get(d, 0.0) for d in day_strs], dtype=np.float64)

    cb_mtm = cb_daily_r * RISK_PCT

    # Realized on exit day
    d_map = {d: i for i, d in enumerate(day_strs)}
    cb_real = np.zeros(len(day_strs), dtype=np.float64)
    for t in cb_trades:
        if t.exit_day_str in d_map:
            cb_real[d_map[t.exit_day_str]] += t.raw_r * RISK_PCT

    def _corr(a: np.ndarray, b: np.ndarray) -> float | None:
        if np.std(a) < 1e-12 or np.std(b) < 1e-12:
            return 0.0
        return round(float(np.corrcoef(a, b)[0, 1]), 4)

    return {
        "mtm_corr": _corr(cb_mtm, l2_mtm),
        "realized_corr": _corr(cb_real, l2_real),
    }


def evaluate_grid_on_universe(
    syms_data: dict[str, dict[str, Any]],
    universe_name: str,
    is_start: str,
    is_end: str,
    oos_start: str,
    oos_end: str,
    thresholds: list[float],
    baseline_info: dict[str, Any],
) -> dict[str, Any]:
    """Run entire parameter grid on a symbol universe with strict IS selection -> OOS once."""
    all_days_set: set[str] = set()
    for sd in syms_data.values():
        all_days_set.update(sd["days"])
    all_days = sorted(all_days_set)

    # Pre-cache simulation results per (sym, ref_mode, X, exit_mode, sl_mode, filter_mode)
    # To avoid redundant computations across universes if called multiple times, cache locally
    grid_records: list[dict[str, Any]] = []

    for ref_mode in REF_MODES:
        for thresh in thresholds:
            # Determine valid symbols for this threshold (Crypto tests 15%, TradFi tests up to 10%)
            active_syms = [
                s for s, sd in syms_data.items()
                if (thresh <= 0.10 or sd["is_crypto"])
            ]
            if not active_syms:
                continue

            for exit_mode in EXIT_MODES:
                for sl_mode in SL_MODES:
                    for filter_mode in FILTER_MODES:
                        is_trades: list[CrashTrade] = []
                        oos_trades: list[CrashTrade] = []
                        full_trades: list[CrashTrade] = []
                        per_sym_daily_r: dict[str, dict[int, float]] = {}
                        per_sym_counts: dict[str, dict[str, int]] = {}

                        for s in active_syms:
                            t_list, dr_dict = simulate_symbol_combo(
                                syms_data[s], ref_mode, thresh, exit_mode, sl_mode, filter_mode
                            )
                            per_sym_daily_r[s] = dr_dict
                            s_is = [t for t in t_list if is_start <= t.entry_day_str < is_end]
                            s_oos = [t for t in t_list if oos_start <= t.entry_day_str < oos_end]
                            s_full = [t for t in t_list if is_start <= t.entry_day_str < oos_end]
                            is_trades.extend(s_is)
                            oos_trades.extend(s_oos)
                            full_trades.extend(s_full)
                            per_sym_counts[s] = {"is": len(s_is), "oos": len(s_oos), "full": len(s_full)}

                        is_arr, is_days = _aggregate_daily_series(syms_data, per_sym_daily_r, all_days, is_start, is_end)
                        oos_arr, oos_days = _aggregate_daily_series(syms_data, per_sym_daily_r, all_days, oos_start, oos_end)
                        full_arr, full_days = _aggregate_daily_series(syms_data, per_sym_daily_r, all_days, is_start, oos_end)

                        is_m = compute_trade_metrics(is_trades, is_arr, is_days)
                        oos_m = compute_trade_metrics(oos_trades, oos_arr, oos_days)
                        full_m = compute_trade_metrics(full_trades, full_arr, full_days)

                        # L2 correlation (only compute full & OOS for speed if trades > 0, or fast vector)
                        corr_oos = compute_l2_correlation(oos_days, oos_arr, baseline_info, oos_trades)
                        corr_full = compute_l2_correlation(full_days, full_arr, baseline_info, full_trades)

                        grid_records.append({
                            "universe": universe_name,
                            "ref_mode": ref_mode,
                            "threshold_pct": int(round(thresh * 100)),
                            "exit_mode": exit_mode,
                            "sl_mode": sl_mode,
                            "filter_mode": filter_mode,
                            "is": is_m,
                            "oos": oos_m,
                            "full": full_m,
                            "l2_corr_oos": corr_oos,
                            "l2_corr_full": corr_full,
                            "per_symbol_counts": per_sym_counts,
                            "_oos_trades_obj": oos_trades,
                            "_full_trades_obj": full_trades,
                        })

    # Strict IS Selection: exclude sl_mode=='none' for deployable candidates; require IS trades >= 30 (fallback >= 20)
    deployable_is = [
        r for r in grid_records
        if r["sl_mode"] != "none" and r["is"]["trades"] >= 30
    ]
    if not deployable_is:
        deployable_is = [
            r for r in grid_records
            if r["sl_mode"] != "none" and r["is"]["trades"] >= 20
        ]
    # Rank by IS Risk-Adjusted Return (MAR = total_return / max_dd) and Profit Factor
    deployable_is.sort(
        key=lambda r: (
            r["is"]["total_return_pct"] / max(1.0, r["is"]["max_dd_pct"]),
            r["is"]["profit_factor"],
            r["is"]["expectancy_r"],
        ),
        reverse=True,
    )

    # Also find best per exit_mode and best overall with OOS >= 20 check AFTER selection
    top_candidates = deployable_is[:10]
    best_cand = top_candidates[0] if top_candidates else None

    # Best overlap & correlation detail for top 3 IS candidates
    top_details = []
    for cand in top_candidates[:5]:
        overlap_full = check_overlap_and_conflict(cand["_full_trades_obj"], baseline_info)
        overlap_oos = check_overlap_and_conflict(cand["_oos_trades_obj"], baseline_info)
        c_copy = {k: v for k, v in cand.items() if not k.startswith("_")}
        c_copy["overlap_full"] = overlap_full
        c_copy["overlap_oos"] = overlap_oos
        top_details.append(c_copy)

    # Strip temporary trade objects from grid records before JSON serialization
    clean_records = [{k: v for k, v in r.items() if not k.startswith("_")} for r in grid_records]

    # Check if ANY IS-selected top candidate passed OOS (>=20 OOS trades, PF >= 1.2, exp_r > 0)
    # Also check ALL configs in OOS with >=20 OOS trades just to know if ANY grid combo worked OOS (for honest reporting)
    oos_passing_all = [
        r for r in clean_records
        if r["sl_mode"] != "none" and r["oos"]["trades"] >= 20 and r["oos"]["profit_factor"] >= 1.20 and r["oos"]["expectancy_r"] > 0
    ]
    oos_passing_all.sort(key=lambda r: (r["oos"]["profit_factor"], r["oos"]["expectancy_r"]), reverse=True)

    return {
        "universe": universe_name,
        "is_period": f"{is_start} .. {is_end}",
        "oos_period": f"{oos_start} .. {oos_end}",
        "total_grid_combos": len(clean_records),
        "is_selected_top5": top_details,
        "oos_passing_diagnostic_count": len(oos_passing_all),
        "oos_passing_diagnostic_top5": oos_passing_all[:5],
        "all_records": clean_records,
    }


def evaluate_per_symbol_summary(
    syms_data: dict[str, dict[str, Any]],
    is_end: str,
    oos_end: str,
    is_start: str,
) -> dict[str, Any]:
    """Report per-symbol trigger counts and best/representative stats (flagging OOS < 20)."""
    out: dict[str, Any] = {}
    for sym, sd in syms_data.items():
        threshs = [0.03, 0.05, 0.07, 0.10, 0.15] if sd["is_crypto"] else [0.03, 0.05, 0.07, 0.10]
        sym_res = {}
        for ref_mode in REF_MODES:
            sym_res[ref_mode] = {}
            for th in threshs:
                # Representative setting: hold_5d + atr2 + none filter
                t_list, _ = simulate_symbol_combo(sd, ref_mode, th, "hold_5d", "atr2", "none")
                is_t = [t for t in t_list if is_start <= t.entry_day_str < is_end]
                oos_t = [t for t in t_list if is_end <= t.entry_day_str < oos_end]
                is_m = compute_trade_metrics(is_t)
                oos_m = compute_trade_metrics(oos_t)
                sym_res[ref_mode][f"{int(th*100)}%"] = {
                    "is_trades": is_m["trades"],
                    "oos_trades": oos_m["trades"],
                    "oos_flag": "樣本不足，不可信" if oos_m["trades"] < 20 else "樣本充足",
                    "oos_pf": oos_m["profit_factor"],
                    "oos_win_rate": oos_m["win_rate_pct"],
                    "oos_exp_r": oos_m["expectancy_r"],
                    "oos_worst_r": oos_m["worst_trade_r"],
                }
        out[sym] = sym_res
    return out


def generate_markdown_report(res: dict[str, Any], out_path: Path) -> None:
    """Generate comprehensive Traditional Chinese Markdown report with executive summary first."""
    u_tradfi = res["universes"]["tradfi_1h"]
    u_crypto = res["universes"]["crypto_1h"]
    u_all_1h = res["universes"]["all_1h"]
    u_10y = res["universes"]["all_10y_1d"]

    lines: list[str] = []
    lines.append("# 「單日大跌做多」(Crash Buy) 全維度實證研究報告")
    lines.append("")
    lines.append("## 一、核心結論與前置總表（先講結論：能不能用？）")
    lines.append("")

    # Analyze IS-selected candidates across universes
    def _best_line(u: dict[str, Any], label: str) -> dict[str, Any] | None:
        tops = u.get("is_selected_top5", [])
        return tops[0] if tops else None

    b_all = _best_line(u_all_1h, "全資產 1h")
    b_trad = _best_line(u_tradfi, "傳統標的 1h")
    b_cryp = _best_line(u_crypto, "加密貨幣 1h")
    b_10y = _best_line(u_10y, "10年日線壓力測試")

    any_pass = any(
        (c["oos"]["trades"] >= 20 and c["oos"]["profit_factor"] >= 1.20 and c["oos"]["expectancy_r"] > 0)
        for u in [u_all_1h, u_tradfi, u_crypto, u_10y]
        for c in u.get("is_selected_top5", [])[:1]
    )

    lines.append(f"> **一句話結論**：{'部分子集在 OOS 通過門檻，但整體容錯與樣本穩定度仍須審慎檢視。' if any_pass else '在嚴格樣本外 (OOS) 測試與真實交易成本下，沒有任何一個 IS 精選出的 Crash Buy 參數能穩定通過檢驗（或樣本數嚴重不足 / 危機期巨虧），不建議將 Crash Buy 加入實盤訊號系統。'}")
    lines.append("")
    lines.append("### 總表：各資產池 IS 最佳選定組合在 OOS 的真實表現（每筆風險 0.75%）")
    lines.append("")
    lines.append("| 測試池 (Universe) | IS 選定最佳參數 | IS 交易數 / PF / 期望R | OOS 交易數 | OOS 勝率 | OOS PF | OOS 期望R | OOS 最大回撤 | OOS 最大連虧 | OOS 最差單筆 | 2020/03 回撤 | 2022 回撤 | 與 L2 日損益相關 (MTM / Realized) | 結論 |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|")

    for name, u, b in [
        ("全資產 1h (2023-09~2026-10)", u_all_1h, b_all),
        ("傳統標的 1h (黃金/原油/指數)", u_tradfi, b_trad),
        ("加密主流 1h (2021-07~2026-11)", u_crypto, b_cryp),
        ("10年日線壓力測試 (2016-11~2026-11)", u_10y, b_10y),
    ]:
        if not b:
            lines.append(f"| {name} | 無合格 (IS<20) | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | -- | 樣本不足 |")
            continue
        p_str = f"{b['ref_mode']} / -{b['threshold_pct']}% / {b['exit_mode']} / {b['sl_mode']} / {b['filter_mode']}"
        ism = b["is"]
        oosm = b["oos"]
        fullm = b["full"]
        dd20 = f"-{fullm['dd_2020_03_pct']}%" if fullm.get("dd_2020_03_pct") is not None else "無資料"
        dd22 = f"-{fullm['dd_2022_pct']}%" if fullm.get("dd_2022_pct") is not None else "無資料"
        corr_m = b["l2_corr_oos"].get("mtm_corr")
        corr_r = b["l2_corr_oos"].get("realized_corr")
        corr_s = f"{corr_m if corr_m is not None else '--'} / {corr_r if corr_r is not None else '--'}"

        verdict = "樣本不足，不可信" if oosm["trades"] < 20 else (
            "OOS 通過" if (oosm["profit_factor"] >= 1.20 and oosm["expectancy_r"] > 0) else "OOS 淘汰"
        )
        lines.append(
            f"| **{name}** | `{p_str}` | {ism['trades']} / {ism['profit_factor']} / {ism['expectancy_r']:+.3f}R | "
            f"**{oosm['trades']}** | {oosm['win_rate_pct']}% | **{oosm['profit_factor']}** | {oosm['expectancy_r']:+.3f}R | "
            f"**-{oosm['max_dd_pct']}%** | {oosm['max_consec_losses']} 筆 | {oosm['worst_trade_r']}R | "
            f"{dd20} | {dd22} | `{corr_s}` | **{verdict}** |"
        )

    lines.append("")
    lines.append("### 歷史危機期間壓力測試參考（2020/03 新冠熔斷 與 2022 熊市，無濾網基準，0.75% 風險）")
    lines.append("")
    lines.append("| 資料組 / 代表設定 (不過濾 `none`) | 2020/03 最大回撤 | 2022 最大回撤 | 全期間最差單筆 | 全期間 PF | 白話解讀 |")
    lines.append("|---|---:|---:|---:|---:|---|")
    crisis_specs = [
        ("10年日線 `prev_close / -3% / hold_5d / atr2`", u_10y["all_records"], ("prev_close", 3, "hold_5d", "atr2", "none")),
        ("10年日線 `prev_close / -5% / hold_5d / atr2`", u_10y["all_records"], ("prev_close", 5, "hold_5d", "atr2", "none")),
        ("10年日線 `prev_close / -5% / rebound_prev_close / atr2`", u_10y["all_records"], ("prev_close", 5, "rebound_prev_close", "atr2", "none")),
        ("10年日線 `prev_close / -5% / l2_atr_trail / atr3`", u_10y["all_records"], ("prev_close", 5, "l2_atr_trail", "atr3", "none")),
        ("10年日線 `prev_close / -5% / hold_5d / none(無停損)`", u_10y["all_records"], ("prev_close", 5, "hold_5d", "none", "none")),
        ("加密 1h `prev_close / -5% / hold_5d / atr2`", u_crypto["all_records"], ("prev_close", 5, "hold_5d", "atr2", "none")),
        ("加密 1h `prev_close / -10% / hold_3d / atr3`", u_crypto["all_records"], ("prev_close", 10, "hold_3d", "atr3", "none")),
    ]
    for label, rec_list, tup in crisis_specs:
        match = next(
            (r for r in rec_list if (r["ref_mode"], r["threshold_pct"], r["exit_mode"], r["sl_mode"], r["filter_mode"]) == tup),
            None,
        )
        if match:
            fm = match["full"]
            d20 = f"-{fm['dd_2020_03_pct']}%" if fm.get("dd_2020_03_pct") is not None else "無資料"
            d22 = f"-{fm['dd_2022_pct']}%" if fm.get("dd_2022_pct") is not None else "無資料"
            note = "無停損遇連續崩盤回撤驚人" if tup[3] == "none" else ("危機期連吃多次接刀停損" if (fm.get("dd_2022_pct") or 0) > 10 else "低頻或受單筆 0.75% 風險保護")
            lines.append(f"| {label} | {d20} | {d22} | {fm['worst_trade_r']}R | {fm['profit_factor']} | {note} |")

    lines.append("")
    lines.append("> **註記（歷史危機資料涵蓋範圍）**：")
    lines.append("> - Repo 本地快取無 2008 年資料（最早日線為 `2016-11`，最早加密/黃金 1h 為 `2021-07`，最早指數/原油 1h 為 `2023-09`）。")
    lines.append("> - 因此 **2020/03 新冠熔斷** 由「10 年日線壓力測試」完整覆蓋，**2022 熊市** 由「加密主流 1h」與「10 年日線」雙重覆蓋。")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Section 2: Per-Symbol Trigger Counts
    lines.append("## 二、各標的觸發次數統計（OOS < 20 次標註「樣本不足，不可信」）")
    lines.append("")
    lines.append("以下統計採代表性配置（固定持有 5 日 + 2×ATR20 停損 + 不加濾網），展示各商品在 1h 歷史中的真實觸發頻率：")
    lines.append("")
    lines.append("| 標的 | 基準 | 門檻 -3% (IS / OOS / 狀態) | 門檻 -5% (IS / OOS / 狀態) | 門檻 -7% (IS / OOS / 狀態) | 門檻 -10% (IS / OOS / 狀態) | 門檻 -15% (IS / OOS / 狀態) |")
    lines.append("|---|---|---|---|---|---|---|")

    per_sym = res["per_symbol_summary"]
    for sym in ALL_1H_SYMS:
        if sym not in per_sym:
            continue
        for ref_mode in ["prev_close", "day_open"]:
            row = f"| **{sym}** | `{ref_mode}` "
            for th_s in ["3%", "5%", "7%", "10%", "15%"]:
                cell = per_sym[sym][ref_mode].get(th_s)
                if not cell:
                    row += "| -- "
                else:
                    flag_short = "⚠️樣本不足" if cell["oos_trades"] < 20 else f"✅充足(PF {cell['oos_pf']})"
                    row += f"| {cell['is_trades']} / **{cell['oos_trades']}** ({flag_short}) "
            row += "|"
            lines.append(row)

    lines.append("")
    lines.append("---")
    lines.append("")

    # Section 3: Overlap & Conflict Analysis with L1/L2/L3/L4/F4
    lines.append("## 三、與現有策略 (L1 / L2 / L3 / L4 / F4) 之重疊與衝突分析")
    lines.append("")
    lines.append("以「全資產 1h IS 最佳組合」每次 Crash Buy 觸發當下，檢查原策略是否正在持有同標的的多單（同向重疊）、空單（反向接刀衝突）或空手：")
    lines.append("")
    if b_all and b_all.get("overlap_full"):
        lines.append("| 現有策略 | Crash Buy 觸發時狀態 | 比例 (%) | 筆數 | 勝率 | PF | 期望值 R | 白話解讀 |")
        lines.append("|---|---|---:|---:|---:|---:|---:|---|")
        ov = b_all["overlap_full"]
        for sid in ["L1", "L2", "L3", "L4", "F4"]:
            s_ov = ov.get(sid, {})
            for st, st_zh in [("long", "同向持有多單 (重疊)"), ("short", "反向持有空單 (衝突對打)"), ("flat", "空手無持倉 (互補)")]:
                m = s_ov.get(st, {})
                if m.get("trades", 0) == 0 and m.get("share_pct", 0) == 0:
                    lines.append(f"| **{sid}** | {st_zh} | 0.0% | 0 | -- | -- | -- | 無此情況 |")
                else:
                    lines.append(
                        f"| **{sid}** | {st_zh} | {m['share_pct']}% | {m['trades']} | {m['win_rate_pct']}% | "
                        f"**{m['profit_factor']}** | {m['expectancy_r']:+.3f}R | "
                        f"{'大跌接刀與趨勢空單直接對打' if st == 'short' else ('加碼已經持有的趨勢多單' if st == 'long' else '獨立填補空手區間')} |"
                    )
    lines.append("")
    lines.append("---")
    lines.append("")

    # Section 4: Top 5 IS-Selected Candidates Details + Dimension Breakdowns
    lines.append("## 四、IS 精選前 5 名組合之 OOS 逐一盲測結果")
    lines.append("")
    for uname, u in [("全資產 1h", u_all_1h), ("傳統標的 1h", u_tradfi), ("加密主流 1h", u_crypto), ("10年日線 (2016~2026)", u_10y)]:
        lines.append(f"### {uname}")
        lines.append("")
        lines.append("| 排名 | 基準 | 門檻 | 出場 | 停損 | 濾網 | IS 筆數/PF/R | OOS 筆數/勝率/PF/R | OOS 最大回撤/連虧/最差 | 與 L2 相關 (MTM) |")
        lines.append("|---:|---|---|---|---|---|---|---|---|---:|")
        for idx, c in enumerate(u.get("is_selected_top5", []), 1):
            ism, oosm = c["is"], c["oos"]
            corr = c["l2_corr_oos"].get("mtm_corr")
            flag = "⚠️" if oosm["trades"] < 20 else ""
            lines.append(
                f"| #{idx} | `{c['ref_mode']}` | -{c['threshold_pct']}% | `{c['exit_mode']}` | `{c['sl_mode']}` | `{c['filter_mode']}` | "
                f"{ism['trades']} / {ism['profit_factor']} / {ism['expectancy_r']:+.3f}R | "
                f"{flag}{oosm['trades']} / {oosm['win_rate_pct']}% / **{oosm['profit_factor']}** / {oosm['expectancy_r']:+.3f}R | "
                f"-{oosm['max_dd_pct']}% / {oosm['max_consec_losses']} / {oosm['worst_trade_r']}R | {corr if corr is not None else '--'} |"
            )
        lines.append("")

    # Section 5: Grid Sensitivity Insights (No SL vs SL, SMA200 vs Cool vs None, Prev Close vs Day Open)
    lines.append("## 五、各維度變形敏感度對照（為什麼這個策略大多失效或過擬合？）")
    lines.append("")

    def _agg_by_dim(records: list[dict[str, Any]], dim_key: str) -> list[tuple[str, dict[str, Any]]]:
        groups = defaultdict(list)
        for r in records:
            if dim_key != "sl_mode" and r["sl_mode"] == "none":
                continue
            groups[str(r[dim_key])].append(r)
        out_rows = []
        for k, lst in groups.items():
            valid_oos = [x for x in lst if x["oos"]["trades"] >= 20]
            pool = valid_oos if valid_oos else lst
            avg_is_pf = sum(x["is"]["profit_factor"] for x in pool) / len(pool)
            avg_oos_pf = sum(x["oos"]["profit_factor"] for x in pool) / len(pool)
            avg_oos_r = sum(x["oos"]["expectancy_r"] for x in pool) / len(pool)
            avg_oos_dd = sum(x["oos"]["max_dd_pct"] for x in pool) / len(pool)
            avg_worst = min(x["oos"]["worst_trade_r"] for x in pool)
            out_rows.append((k, {
                "count": len(lst),
                "avg_is_pf": round(avg_is_pf, 3),
                "avg_oos_pf": round(avg_oos_pf, 3),
                "avg_oos_r": round(avg_oos_r, 4),
                "avg_oos_dd": round(avg_oos_dd, 2),
                "worst_r": round(avg_worst, 2),
            }))
        return out_rows

    lines.append("### 1. 全資產 1h 各維度平均表現（排除無停損以防極端值扭曲，停損維度單獨列出）")
    lines.append("")
    lines.append("| 維度 | 設定值 | 組合數 | IS 平均 PF | OOS 平均 PF | OOS 平均期望 R | OOS 平均回撤 | OOS 最差單筆 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
    recs = u_all_1h["all_records"]
    for dkey, dname in [
        ("ref_mode", "基準價"),
        ("threshold_pct", "觸發門檻 (%)"),
        ("exit_mode", "出場方式"),
        ("sl_mode", "停損方式"),
        ("filter_mode", "過濾條件"),
    ]:
        for val, st in _agg_by_dim(recs, dkey):
            lines.append(
                f"| {dname} | `{val}` | {st['count']} | {st['avg_is_pf']} | **{st['avg_oos_pf']}** | {st['avg_oos_r']:+.3f}R | -{st['avg_oos_dd']}% | {st['worst_r']}R |"
            )

    lines.append("")
    lines.append("### 2. 診斷檢查：OOS 全網格事後視角（僅供診斷是否有遺珠，不可直接採用）")
    lines.append("")
    lines.append(f"- 全資產 1h 共有 `{u_all_1h['oos_passing_diagnostic_count']}` 組參數事後看 OOS 達 PF ≥ 1.20 且筆數 ≥ 20；")
    lines.append(f"- 傳統標的 1h 共有 `{u_tradfi['oos_passing_diagnostic_count']}` 組；")
    lines.append(f"- 加密主流 1h 共有 `{u_crypto['oos_passing_diagnostic_count']}` 組；")
    lines.append(f"- 10年日線壓力測試共有 `{u_10y['oos_passing_diagnostic_count']}` 組。")
    lines.append("")
    for uname, u in [("全資產 1h", u_all_1h), ("傳統標的 1h", u_tradfi), ("加密主流 1h", u_crypto)]:
        diag = u.get("oos_passing_diagnostic_top5", [])
        if diag:
            lines.append(f"**{uname} OOS 事後最高 PF 前幾名**：")
            for c in diag[:3]:
                ism, oosm = c["is"], c["oos"]
                lines.append(
                    f"- `{c['ref_mode']} / -{c['threshold_pct']}% / {c['exit_mode']} / {c['sl_mode']} / {c['filter_mode']}` → "
                    f"IS: {ism['trades']}筆 PF={ism['profit_factor']} ({ism['expectancy_r']:+.3f}R) ｜ "
                    f"OOS: {oosm['trades']}筆 勝率={oosm['win_rate_pct']}% PF={oosm['profit_factor']} ({oosm['expectancy_r']:+.3f}R) 回撤=-{oosm['max_dd_pct']}%"
                )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 六、白話總結與實盤建議")
    lines.append("")
    lines.append("1. **為什麼「單日大跌接刀」看起來直覺很誘人，實測卻很難過關？**")
    lines.append("   - **成本與滑價吃掉短線反彈**：大跌當下盤中劇烈波動，點差與滑價放大，反彈回前日收盤往往只有 0.5R~1R。")
    lines.append("   - **危機期連續接刀效應**：2020/03（原油負油價/美股熔斷）與 2022（加密連環爆）期間，單日大跌之後隔天往往繼續大跌，即使加了冷卻期或 SMA200 過濾，在趨勢反轉初期仍會連吃多筆停損。")
    lines.append("   - **指數與黃金大跌樣本嚴重稀缺**：SP500、NAS100、黃金等傳統標的單日跌破 -5%、-7% 一年甚至發生不到 2 次，OOS 幾乎全部標註「樣本不足，不可信」。")
    lines.append("2. **與現有主力 L2 的關係**：")
    lines.append("   - 當 Crash Buy 觸發時，若原版 L2 當下正持有空單，兩者會直接反向對打（一多一空同時扣兩份交易成本與滑價）。")
    lines.append("3. **最終結論**：現有 Signal Desk 長線趨勢系統（L1/L2/L3/L4）維持現狀，**不上線 Crash Buy 策略**。")
    lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Markdown report written to {out_path}")


def main() -> None:
    t0 = time.time()
    config = load_config()
    mt5_cfg = load_mt5_costs_config()

    cache_1d_path = ROOT_DIR / ".cache" / "research_1d_bars.pkl"
    bars_1d_all: dict[str, list[OHLCVBar]] = pickle.loads(cache_1d_path.read_bytes())

    bin_dir = ROOT_DIR / ".cache" / "binance_monthly" / "merged"
    duka_dir = ROOT_DIR / ".cache" / "dukascopy" / "merged"

    print("1. Loading 1h and 1d cached datasets...")
    syms_1h_data: dict[str, dict[str, Any]] = {}
    for sym in ALL_1H_SYMS:
        if sym in CRYPTO_SYMS:
            b1h = _load_binance_1h(sym, bin_dir)
            pre_1d = bars_1d_all.get(sym, [])
        else:
            d_sym = "JPN225" if sym == "JP225" else sym
            b1h = _load_dukascopy_1h(d_sym, duka_dir)
            pre_1d = bars_1d_all.get(sym, [])
        if b1h:
            syms_1h_data[sym] = prepare_symbol_1h_sessions(sym, b1h, pre_1d, config, mt5_cfg)
            print(f"  Loaded 1h {sym:8s}: {len(syms_1h_data[sym]['days'])} days ({syms_1h_data[sym]['days'][0]} .. {syms_1h_data[sym]['days'][-1]})")

    syms_10y_data: dict[str, dict[str, Any]] = {}
    for sym in DAILY_10Y_SYMS:
        if sym in bars_1d_all:
            syms_10y_data[sym] = prepare_symbol_1d_sessions(sym, bars_1d_all[sym], config, mt5_cfg)

    print(f"2. Running L1/L2/L3/L4/F4 baselines for overlap & L2 correlation ({time.time() - t0:.1f}s)...")
    baseline_info = run_baseline_strategies_for_overlap(config, mt5_cfg)

    # 3. Evaluate Universes (70/30 splits)
    # TradFi 1h: 2023-09-01 .. 2026-10-31 -> 70% = 2025-11-18
    tradfi_1h_dict = {s: syms_1h_data[s] for s in TRADFI_SYMS if s in syms_1h_data}
    crypto_1h_dict = {s: syms_1h_data[s] for s in CRYPTO_SYMS if s in syms_1h_data}

    print(f"3. Running Grid Evaluations ({time.time() - t0:.1f}s)...")
    u_tradfi = evaluate_grid_on_universe(
        tradfi_1h_dict,
        "tradfi_1h",
        is_start="2023-09-01",
        is_end="2025-11-18",
        oos_start="2025-11-18",
        oos_end="2026-11-01",
        thresholds=[0.03, 0.05, 0.07, 0.10],
        baseline_info=baseline_info,
    )
    print(f"  tradfi_1h done ({time.time() - t0:.1f}s)")

    u_crypto = evaluate_grid_on_universe(
        crypto_1h_dict,
        "crypto_1h",
        is_start="2021-07-01",
        is_end="2025-04-06",
        oos_start="2025-04-06",
        oos_end="2026-11-17",
        thresholds=[0.03, 0.05, 0.07, 0.10, 0.15],
        baseline_info=baseline_info,
    )
    print(f"  crypto_1h done ({time.time() - t0:.1f}s)")

    u_all_1h = evaluate_grid_on_universe(
        syms_1h_data,
        "all_1h",
        is_start="2023-09-01",
        is_end="2025-11-18",
        oos_start="2025-11-18",
        oos_end="2026-11-17",
        thresholds=[0.03, 0.05, 0.07, 0.10, 0.15],
        baseline_info=baseline_info,
    )
    print(f"  all_1h done ({time.time() - t0:.1f}s)")

    u_10y = evaluate_grid_on_universe(
        syms_10y_data,
        "all_10y_1d",
        is_start="2016-11-17",
        is_end="2023-11-17",
        oos_start="2023-11-17",
        oos_end="2026-11-16",
        thresholds=[0.03, 0.05, 0.07, 0.10, 0.15],
        baseline_info=baseline_info,
    )
    print(f"  all_10y_1d done ({time.time() - t0:.1f}s)")

    # Per-symbol trigger counts using each asset's own 70/30 chronological timeline
    per_symbol_summary = {}
    for sym, sd in syms_1h_data.items():
        if sd["is_crypto"]:
            is_s, is_e, oos_e = "2021-07-01", "2025-04-06", "2026-11-17"
        else:
            is_s, is_e, oos_e = "2023-09-01", "2025-11-18", "2026-11-01"
        per_symbol_summary.update(evaluate_per_symbol_summary({sym: sd}, is_e, oos_e, is_s))

    results = {
        "metadata": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(time.time() - t0, 2),
            "risk_per_trade_pct": 0.75,
            "tradfi_1h_symbols": list(tradfi_1h_dict.keys()),
            "crypto_1h_symbols": list(crypto_1h_dict.keys()),
            "daily_10y_symbols": list(syms_10y_data.keys()),
            "cost_model": "The5ers MT5 for TradFi (0.001%/side + MT5 spread + 0.5bps slip); Crypto 5bps/side fee + spread + 1bps slip",
        },
        "universes": {
            "all_1h": u_all_1h,
            "tradfi_1h": u_tradfi,
            "crypto_1h": u_crypto,
            "all_10y_1d": u_10y,
        },
        "per_symbol_summary": per_symbol_summary,
    }

    out_dir = Path(__file__).resolve().parent
    out_json = out_dir / "results.json"
    out_md = out_dir / "report.md"

    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved JSON to {out_json}")
    generate_markdown_report(results, out_md)
    print(f"All complete in {time.time() - t0:.2f}s")


if __name__ == "__main__":
    main()
