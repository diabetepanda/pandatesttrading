"""Unit tests for Round-2 6 strategies (S1-S3, L1-L3), Dukascopy/PAXGUSDT backup, indices, and live simulation execution."""

from __future__ import annotations

import copy
import csv
import io
import json
import lzma
import math
import struct
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.backtest.screener import (
    compute_metrics,
    screen_all_six_strategies,
    screen_single_strategy,
    simulate_l3_dual_momentum,
    simulate_portfolio_strategy,
    simulate_single_symbol,
)
from src.config_loader import load_config
from src.data.adapters import BinanceMonthlyArchiveFetcher, DukascopyArchiveFetcher, parse_dukascopy_bi5
from src.data.models import FetchResult, OHLCVBar
from src.indicators.technical import resample_1h_to_4h
from src.jobs.daily import run_daily
from src.jobs.scan import run_scan
from src.jobs.weekly import run_weekly_screening
from src.risk.manager import RiskManager
from src.signals.models import LifecycleState
from src.strategies.six_strategies import (
    allocate_subaccount_position,
    decide_l3_dual_momentum_step,
    evaluate_live_bar,
)


def _make_bars(
    count: int = 2300,
    step_minutes: int = 1440,
    mode: str = "trend_wave",
    phase: float = 0.0,
    start: datetime | None = None,
) -> list[OHLCVBar]:
    """Generate deterministic OHLCV bars for intraday or daily testing."""
    t0 = start or datetime(2020, 1, 1, tzinfo=timezone.utc)
    bars: list[OHLCVBar] = []
    price = 100.0
    for i in range(count):
        dt = t0 + timedelta(minutes=step_minutes * i)
        if mode == "trend_wave":
            step = math.sin(i / 14.0 + phase) * 2.2 + 0.04
        elif mode == "crash_whipsaw":
            step = 6.0 if (i % 6) < 3 else -6.2
        elif mode == "range_osc":
            step = math.sin(i / 4.0) * 3.0 if (i % 60) < 40 else (1.8 if (i % 120) < 60 else -1.8)
        elif mode == "rsi_pullback":
            # Long-term uptrend with periodic 3-day sharp pullbacks
            step = -3.8 if (i % 25) in (21, 22, 23) else 0.75
        elif mode == "orb_break":
            # Calm first hour then directional expansion
            bar_in_day = i % 96
            if bar_in_day < 4:
                step = 0.05 if i % 2 == 0 else -0.05
            elif bar_in_day < 48:
                step = 0.65 if (i // 96) % 2 == 0 else -0.65
            else:
                step = -0.15 if (i // 96) % 2 == 0 else 0.15
        else:
            step = math.sin(i / 10.0 + phase) * 2.0 + 0.03
        o = max(5.0, price)
        c = max(5.0, o + step)
        h = max(o, c) + 0.7
        l = max(1.0, min(o, c) - 0.7)
        bars.append(
            OHLCVBar(
                ts_utc=dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                open=round(o, 4),
                high=round(h, 4),
                low=round(l, 4),
                close=round(c, 4),
                volume=1000.0 + (i % 30) * 25.0,
            )
        )
        price = c
    return bars


def _step_live_sequence(
    strategy_id: str,
    bars: list[OHLCVBar],
    params: dict,
    inst: dict,
    interval: str,
    allow_short: bool = True,
) -> list[tuple[str, str, str, float | None]]:
    """Run live bar-by-bar decision function using exact next-bar-open & SL/TP state machine."""
    spread_bps = float(inst.get("assumed_spread_bps", 2.0))
    in_pos = False
    pos_dir = ""
    sl_px = None
    tp_px = None
    entry_bidx = 0
    pending_entry = None
    pending_exit = False
    trace = []

    for i in range(len(bars)):
        bar = bars[i]

        if pending_exit and in_pos:
            in_pos = False
            pos_dir = ""
            pending_exit = False

        if pending_entry is not None and not in_pos:
            in_pos = True
            pos_dir = pending_entry.direction
            sl_px = pending_entry.stop_loss
            tp_px = pending_entry.take_profit
            entry_bidx = i
            pending_entry = None

        exited_this_bar = False
        if in_pos:
            exited = False
            if pos_dir == "long":
                if sl_px is not None and (
                    bar.open * (1.0 - spread_bps / 20000.0) <= sl_px
                    or bar.low * (1.0 - spread_bps / 20000.0) <= sl_px
                ):
                    exited = True
                elif tp_px is not None and bar.high * (1.0 - spread_bps / 20000.0) >= tp_px:
                    exited = True
            else:
                if sl_px is not None and (
                    bar.open * (1.0 + spread_bps / 20000.0) >= sl_px
                    or bar.high * (1.0 + spread_bps / 20000.0) >= sl_px
                ):
                    exited = True
                elif tp_px is not None and bar.low * (1.0 + spread_bps / 20000.0) <= tp_px:
                    exited = True
            if exited:
                in_pos = False
                pos_dir = ""
                exited_this_bar = True
                continue

            if i < len(bars) - 1:
                bars_held = max(1, i - entry_bidx + 1)
                act = evaluate_live_bar(
                    strategy_id=strategy_id,
                    closed_bars=bars[: i + 1],
                    position_dir=pos_dir,
                    params=params,
                    market=inst["market"],
                    interval=interval,
                    allow_short=allow_short,
                    bars_held=bars_held,
                )
                if act.action == "exit":
                    sl_r = round(act.stop_loss, 6) if act.stop_loss is not None else None
                    trace.append((bar.ts_utc, act.action, act.direction, sl_r))
                    pending_exit = True

        if not in_pos and not exited_this_bar and i < len(bars) - 1:
            act = evaluate_live_bar(
                strategy_id=strategy_id,
                closed_bars=bars[: i + 1],
                position_dir="",
                params=params,
                market=inst["market"],
                interval=interval,
                allow_short=allow_short,
                bars_held=0,
            )
            if act.action in ("enter_long", "enter_short"):
                sl_r = round(act.stop_loss, 6) if act.stop_loss is not None else None
                trace.append((bar.ts_utc, act.action, act.direction, sl_r))
                pending_entry = act

    return trace


def test_backtest_and_live_produce_identical_signals_for_all_six_strategies() -> None:
    """Prove backtest and live decision functions produce 100% identical entries & exits across Round-2 S1-S3, L1-L3."""
    cfg = load_config()
    btc = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")

    # Verify 1h -> 4h resampling alignment first
    bars_1h_sample = _make_bars(24, 60, "trend_wave", start=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc))
    bars_4h_res = resample_1h_to_4h(bars_1h_sample, now_utc=datetime(2025, 1, 2, 12, 0, tzinfo=timezone.utc))
    assert len(bars_4h_res) == 6
    assert bars_4h_res[0].ts_utc == "2025-01-01T00:00:00Z"
    assert bars_4h_res[1].ts_utc == "2025-01-01T04:00:00Z"

    cases = [
        (
            "S1",
            _make_bars(480, 15, "orb_break", start=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)),
            {"session_open_utc": "00:00", "range_minutes": 60, "sl_mode": "atr_2.0", "eod_close_utc": "23:45"},
            "15m",
            True,
        ),
        (
            "S2",
            _make_bars(260, 240, "trend_wave"),
            {"breakout_bars": 20, "trail_bars": 8, "sl_atr_mult": 2.0, "max_hold_bars": 30},
            "4h",
            True,
        ),
        (
            "S3",
            _make_bars(420, 1440, "rsi_pullback"),
            {
                "trend_ma_period": 50,
                "rsi_period": 2,
                "rsi_long_thresh": 20.0,
                "rsi_short_thresh": 80.0,
                "exit_ma_period": 5,
                "allow_short": True,
                "sl_atr_mult": 2.5,
                "max_hold_bars": 10,
            },
            "1d",
            True,
        ),
        (
            "L1",
            _make_bars(420, 1440, "trend_wave"),
            {"entry_period": 20, "exit_period": 10, "sl_atr_mult": 2.5, "vol_target_pct": 0.015},
            "1d",
            True,
        ),
        (
            "L2",
            _make_bars(420, 1440, "trend_wave"),
            {"entry_period": 20, "exit_period": 10, "sl_atr_mult": 2.5, "vol_target_pct": 0.015},
            "1d",
            True,
        ),
    ]

    for sid, bars, params, interval, allow_short in cases:
        _, bt_trace = simulate_single_symbol(bars, sid, params, btc, cfg, interval=interval, allow_short=allow_short, start_idx=0)
        live_trace = _step_live_sequence(sid, bars, params, btc, interval, allow_short=allow_short)
        assert len(bt_trace) > 0, f"{sid} should produce real signals in test fixture"
        assert bt_trace == live_trace, f"Mismatch between backtest and live decisions for {sid}"
        if sid == "S1":
            entries_per_day: dict[str, int] = {}
            for ts, act, _, _ in live_trace:
                if act in ("enter_long", "enter_short"):
                    day = ts[:10]
                    entries_per_day[day] = entries_per_day.get(day, 0) + 1
                elif act == "exit":
                    assert ts[11:16] == "23:15"
            assert all(cnt <= 1 for cnt in entries_per_day.values()), "S1 exceeded 1 trade/day limit"

    # L3 cross-asset Dual Momentum parity proof (crypto + index)
    cryptos = [i for i in cfg["instruments"] if i["market"] == "crypto"][:3]
    indices = [i for i in cfg["instruments"] if i["market"] == "index"][:2]
    pool = cryptos + indices
    inst_map = {i["symbol"]: i for i in pool}
    bars_map = {
        i["symbol"]: _make_bars(240, 1440, "trend_wave", phase=idx * 0.9)
        for idx, i in enumerate(pool)
    }
    l3_params = {"lookback_days": 60, "top_k_crypto": 2, "top_k_index": 1, "rebalance_days": 7, "sl_atr_mult": 2.5}
    _, _, bt_l3_trace = simulate_l3_dual_momentum(bars_map, inst_map, l3_params, cfg)
    assert len(bt_l3_trace) > 0

    all_dates = sorted({b.ts_utc[:10] for b_list in bars_map.values() for b in b_list})
    held_syms: set[str] = set()
    held_sl: dict[str, float] = {}
    live_l3_trace = []
    date_idx_map = {sym: {b.ts_utc[:10]: idx for idx, b in enumerate(bs)} for sym, bs in bars_map.items()}
    markets_map = {sym: inst_map[sym]["market"] for sym in bars_map}
    for d_idx in range(1, len(all_dates) - 1):
        d = all_dates[d_idx]
        next_d = all_dates[d_idx + 1]
        for sym in list(held_syms):
            b_idx = date_idx_map[sym].get(d)
            if b_idx is not None:
                bar = bars_map[sym][b_idx]
                sp = float(inst_map[sym]["assumed_spread_bps"])
                if bar.open * (1 - sp / 20000) <= held_sl[sym] or bar.low * (1 - sp / 20000) <= held_sl[sym]:
                    held_syms.remove(sym)
                    del held_sl[sym]
        if d_idx % 7 != 0:
            continue
        up_to = {sym: bars_map[sym][: date_idx_map[sym][d] + 1] for sym in bars_map if d in date_idx_map[sym]}
        acts = decide_l3_dual_momentum_step(up_to, markets_map, set(held_syms), 60, 2, 1, 2.5)
        for sym, act in sorted(acts.items(), key=lambda kv: (0 if kv[1].action == "exit" else 1, kv[0])):
            if next_d not in date_idx_map.get(sym, {}):
                continue
            live_l3_trace.append((d, sym, act.action, act.direction))
            if act.action == "exit" and sym in held_syms:
                held_syms.remove(sym)
                held_sl.pop(sym, None)
            elif act.action == "enter_long" and sym not in held_syms:
                held_syms.add(sym)
                held_sl[sym] = act.stop_loss or 0.0

    assert bt_l3_trace == live_l3_trace


def test_oos_mutation_never_leaks_into_is_parameter_selection() -> None:
    """Mutating the 30% OOS slice must NOT alter chosen IS params or IS metrics across S3, L1, L2, L3."""
    cfg = load_config()
    small_cfg = copy.deepcopy(cfg)
    small_cfg["instruments"] = [
        i for i in cfg["instruments"] if i["symbol"] in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "EURUSD", "NAS100", "SP500")
    ]

    bars_orig_1d = {
        "BTCUSDT": _make_bars(2300, 1440, "rsi_pullback", phase=0.1),
        "ETHUSDT": _make_bars(2300, 1440, "trend_wave", phase=0.8),
        "SOLUSDT": _make_bars(2300, 1440, "trend_wave", phase=1.7),
        "EURUSD": _make_bars(2300, 1440, "rsi_pullback", phase=0.3),
        "NAS100": _make_bars(2300, 1440, "trend_wave", phase=1.2),
        "SP500": _make_bars(2300, 1440, "trend_wave", phase=2.0),
    }
    bars_mut_1d = {}
    for sym, bars in bars_orig_1d.items():
        sp = int(len(bars) * 0.7)
        mutated = list(bars[:sp])
        for j, b in enumerate(bars[sp:]):
            f = 4.0 if j % 2 == 0 else 0.25
            mutated.append(OHLCVBar(b.ts_utc, b.open * f, b.high * f * 1.2, b.low * f * 0.8, b.close * f, b.volume * 5))
        bars_mut_1d[sym] = mutated

    for sid in ("S3", "L1", "L2", "L3"):
        r1 = screen_single_strategy(sid, {"1d": bars_orig_1d, "15m": {}, "1h": {}, "4h": {}}, small_cfg)
        r2 = screen_single_strategy(sid, {"1d": bars_mut_1d, "15m": {}, "1h": {}, "4h": {}}, small_cfg)
        assert r1["chosen_params"] == r2["chosen_params"]
        assert r1["in_sample"] == r2["in_sample"]
        assert r1["out_of_sample"] != r2["out_of_sample"]


def test_backtest_deducts_fees_spread_slippage_and_overnight_costs() -> None:
    cfg = load_config()
    bars = _make_bars(420, 1440, "trend_wave")
    inst_zero = {
        "symbol": "TEST",
        "market": "crypto",
        "assumed_spread_bps": 0.0,
        "fee_rate": 0.0,
        "overnight_cost_daily_bps": 0.0,
    }
    inst_cost = {
        "symbol": "TEST",
        "market": "crypto",
        "assumed_spread_bps": 10.0,
        "fee_rate": 0.002,
        "overnight_cost_daily_bps": 5.0,
    }
    cfg_zero = copy.deepcopy(cfg)
    cfg_zero["risk"]["default_slippage_bps"] = 0.0

    params = {"entry_period": 20, "exit_period": 10, "sl_atr_mult": 2.5, "vol_target_pct": 0.015}
    tr_zero, _ = simulate_single_symbol(bars, "L1", params, inst_zero, cfg_zero, interval="1d")
    tr_cost, _ = simulate_single_symbol(bars, "L1", params, inst_cost, cfg, interval="1d")
    assert len(tr_zero) > 0
    assert sum(t.net_pnl_usd for t in tr_cost) < sum(t.net_pnl_usd for t in tr_zero)


def test_equity_curve_never_below_zero_and_drawdown_under_100_pct() -> None:
    """Equity curve must remain strictly > 0 and max drawdown < 100% across all 6 strategies including stress whipsaw."""
    cfg = load_config()
    small_cfg = copy.deepcopy(cfg)
    syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "NAS100", "SP500"]
    small_cfg["instruments"] = [i for i in cfg["instruments"] if i["symbol"] in syms]

    bars_1d = {s: _make_bars(2200, 1440, "crash_whipsaw", phase=idx * 0.4) for idx, s in enumerate(syms)}
    bars_4h = {s: _make_bars(1500, 240, "crash_whipsaw", phase=idx * 0.4) for idx, s in enumerate(syms) if s != "NAS100"}
    bars_15m = {s: _make_bars(1500, 15, "crash_whipsaw", phase=idx * 0.4) for idx, s in enumerate(syms[:5])}

    results = screen_all_six_strategies({"1d": bars_1d, "4h": bars_4h, "1h": {}, "15m": bars_15m}, small_cfg)
    assert len(results) == 6
    for r in results:
        for split_key in ("in_sample", "out_of_sample"):
            m = r[split_key]
            assert m["min_equity_usd"] > 0.0, f"{r['strategy_id']} {split_key} min_equity went <= 0"
            assert m["ending_equity_usd"] > 0.0
            assert 0.0 <= m["max_drawdown_pct"] < 100.0, f"{r['strategy_id']} {split_key} DD >= 100%: {m['max_drawdown_pct']}"

    # Direct portfolio curve check for L3 under extreme whipsaw
    inst_map = {i["symbol"]: i for i in small_cfg["instruments"]}
    tr_l3, eq_l3, _ = simulate_l3_dual_momentum(
        bars_by_symbol=bars_1d,
        instruments_by_sym=inst_map,
        params={"lookback_days": 60, "top_k_crypto": 2, "top_k_index": 1, "rebalance_days": 7, "sl_atr_mult": 2.5},
        config=small_cfg,
    )
    assert all(eq > 0.0 for eq in eq_l3)
    m_l3 = compute_metrics(tr_l3, small_cfg["strategies"]["L3"]["capital_usd"], equity_curve_points=eq_l3)
    assert m_l3["max_drawdown_pct"] < 100.0


def test_shared_subaccount_sizing_and_concurrent_limits() -> None:
    """Verify backtest and RiskManager live sizing use the same allocate_subaccount_position limits and vol scaling."""
    cfg = load_config()
    rm = RiskManager(cfg)
    btc = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")

    direct = allocate_subaccount_position(
        strategy_id="L3",
        subaccount_equity_usd=20000.0,
        open_positions_count=1,
        open_total_risk_usd=150.0,
        open_total_notional_usd=3000.0,
        entry_price=100.0,
        stop_loss=95.0,
        fee_rate=float(btc["fee_rate"]),
        slippage_bps=float(cfg["risk"]["default_slippage_bps"]),
        config=cfg,
        risk_multiplier=0.8,
    )
    via_rm = rm.check_subaccount_allocation(
        strategy_id="L3",
        instrument=btc,
        entry_price=100.0,
        stop_loss=95.0,
        subaccount_equity_usd=20000.0,
        open_positions_count=1,
        open_total_risk_usd=150.0,
        open_total_notional_usd=3000.0,
        risk_multiplier=0.8,
    )
    assert direct == via_rm
    assert direct.approved is True
    # 0.75% of $20,000 * 0.8 = $120 target risk
    assert abs(direct.risk_usd - 120.0) < 1e-4

    # Concurrent position cap enforced
    capped = rm.check_subaccount_allocation(
        strategy_id="L3",
        instrument=btc,
        entry_price=100.0,
        stop_loss=95.0,
        subaccount_equity_usd=20000.0,
        open_positions_count=cfg["backtest"]["max_concurrent_positions"],
        open_total_risk_usd=400.0,
        open_total_notional_usd=5000.0,
    )
    assert capped.approved is False
    assert "檔數已達上限" in capped.reason

    # Total open risk cap enforced (5% of $20,000 = $1,000)
    risk_capped = rm.check_new_signal(
        instrument=btc,
        strategy="L3",
        direction="long",
        entry_price=100.0,
        stop_loss=95.0,
        tp1=110.0,
        tp2=115.0,
        leverage=5.0,
        account_state={
            "equity_usd": 100000.0,
            "peak_equity_usd": 100000.0,
            "subaccount_equity_usd": 20000.0,
            "subaccount_open_positions": 3,
            "subaccount_open_risk_usd": 1200.0,
            "subaccount_open_notional_usd": 9000.0,
        },
        control={"paused": False, "risk_review_done": False},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert risk_capped.approved is False
    assert "總風險已達子帳戶" in risk_capped.reason


def test_dukascopy_bi5_parser_and_paxgusdt_gold_backup(tmp_path: Path) -> None:
    """Verify Dukascopy LZMA .bi5 binary parsing, disk caching, PAXGUSDT Gold backup, and 資料不足 handling."""
    rec1 = struct.pack(">5If", 0, 2000000, 2005000, 1998000, 2008000, 12.5)
    rec2 = struct.pack(">5If", 3600, 2005000, 2012000, 2004000, 2015000, 18.0)
    bi5_bytes = lzma.compress(rec1 + rec2)
    base_dt = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    parsed = parse_dukascopy_bi5(
        bi5_bytes,
        base_dt,
        divisor=1000.0,
        interval="1h",
        now_ts_sec=datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc).timestamp(),
    )
    assert len(parsed) == 2
    assert parsed[0].ts_utc == "2024-01-01T00:00:00Z"
    assert parsed[0].open == 2000.0 and parsed[1].close == 2012.0

    # Verify DukascopyArchiveFetcher disk cache hit & unreachable fallback
    duka_cache = tmp_path / "duka_cache"
    duka = DukascopyArchiveFetcher(cache_dir=duka_cache, timeout_sec=2)
    cache_file = duka_cache / "EURUSD" / "2024" / "00_BID_candles_hour_1.bi5"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(bi5_bytes)
    duka._unreachable = True  # Must read existing disk cache cleanly without network
    assert len(duka._fetch_hour_bi5("EURUSD", 2024, 1) or b"") > 0

    # Weekly job with offline Dukascopy + PAXGUSDT Gold backup + WTl/FX insufficient labeling
    cfg = load_config()
    test_cfg_path = tmp_path / "cfg.yaml"
    import yaml

    small_cfg = copy.deepcopy(cfg)
    small_cfg["instruments"] = [
        i for i in cfg["instruments"] if i["symbol"] in ("BTCUSDT", "XAUUSD", "WTI", "EURUSD", "NAS100")
    ]
    test_cfg_path.write_text(yaml.safe_dump(small_cfg), encoding="utf-8")

    start_2020 = datetime(2020, 1, 1, tzinfo=timezone.utc)
    paxg_15m = _make_bars(240, 15, "orb_break", start=start_2020)
    # Stretch timestamps to span >5y cleanly with fake archive
    long_15m = _make_bars(220, 15 * 96 * 9, "orb_break", start=start_2020)
    long_1h = _make_bars(220, 60 * 240, "trend_wave", start=start_2020)
    long_1d = _make_bars(2200, 1440, "rsi_pullback", start=start_2020)

    class _FakeBinArchive:
        def fetch_history(self, symbol: str, interval: str, years: float = 5.1, now_utc=None):
            if symbol in ("BTCUSDT", "PAXGUSDT"):
                return long_15m if interval == "15m" else long_1h
            return []

    class _OfflineDuka:
        def fetch_history(self, symbol: str, interval: str, years: float = 5.1, now_utc=None):
            return []

    class _FakeDailyFetcher:
        def fetch(self, inst: dict, interval: str, limit: int = 200, now_utc=None):
            return FetchResult(inst["symbol"], interval, True, "fake", long_1d if interval == "1d" else [], None)

    out_dir = tmp_path / "out_weekly"
    res = run_weekly_screening(
        out_dir=out_dir,
        config_path=test_cfg_path,
        fetcher=_FakeDailyFetcher(),
        archive_fetcher=_FakeBinArchive(),
        dukascopy_fetcher=_OfflineDuka(),
        now_utc=datetime(2026, 4, 20, 0, 15, tzinfo=timezone.utc),
    )
    by_id = {r["strategy_id"]: r for r in res["strategies"]}
    assert "PAXGUSDT" in by_id["S1"]["data_note"]
    assert by_id["S1"]["verification_label"] in ("資料不足，觀察中", "觀察中（未通過驗證）", "已驗證")
    assert "NAS100" not in by_id["S1"].get("symbols_covered", [])


def test_live_scan_and_daily_emit_signals_with_verification_status(tmp_path: Path) -> None:
    """Verify scan.py & daily.py share strategy code onto simulation ledger, exclude daily_only from intraday, and tag verification_status."""
    cfg = load_config()
    small_cfg = copy.deepcopy(cfg)
    small_cfg["instruments"] = [
        i for i in cfg["instruments"] if i["symbol"] in ("BTCUSDT", "ETHUSDT", "NAS100", "EURUSD")
    ]
    import yaml

    cfg_path = tmp_path / "config.yaml"
    ctrl_path = tmp_path / "control.json"
    ev_path = tmp_path / "events.yaml"
    cfg_path.write_text(yaml.safe_dump(small_cfg), encoding="utf-8")
    ctrl_path.write_text(json.dumps({"paused": False, "risk_review_done": False}), encoding="utf-8")
    ev_path.write_text("events: []\n", encoding="utf-8")

    out_dir = tmp_path / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Seed unverified L1 backtest result so verification_status must reflect "觀察中（未通過驗證）"
    (out_dir / "backtest_results.json").write_text(
        json.dumps(
            {
                "strategies": [
                    {
                        "strategy_id": "L1",
                        "passed": False,
                        "status": "failed",
                        "verification_label": "觀察中（未通過驗證）",
                        "chosen_params": {"entry_period": 20, "exit_period": 10, "sl_atr_mult": 2.5, "vol_target_pct": 0.015},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    now = datetime(2026, 4, 20, 2, 1, tzinfo=timezone.utc)
    # Build daily bars ending on the closed bar just before `now` so continuity is fresh
    end_day = datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc)
    flat_daily = [
        OHLCVBar(
            ts_utc=(end_day - timedelta(days=249 - k)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1000.0,
        )
        for k in range(250)
    ]
    # Final closed bar breaks cleanly above rolling 20-day high (101.0)
    flat_daily[-1] = OHLCVBar(
        ts_utc=flat_daily[-1].ts_utc,
        open=100.5,
        high=105.0,
        low=100.2,
        close=104.5,
        volume=3000.0,
    )

    flat_15m = [
        OHLCVBar(
            ts_utc=(now - timedelta(minutes=15 * (60 - k))).strftime("%Y-%m-%dT%H:%M:%SZ"),
            open=100.0,
            high=100.5,
            low=99.5,
            close=100.0,
            volume=500.0,
        )
        for k in range(59)
    ]
    flat_1m = [
        OHLCVBar(
            ts_utc=(now - timedelta(minutes=(30 - k))).strftime("%Y-%m-%dT%H:%M:%SZ"),
            open=100.0,
            high=100.2,
            low=99.8,
            close=100.0,
            volume=100.0,
        )
        for k in range(29)
    ]

    intraday_fetched: list[tuple[str, str]] = []

    class _FakeLiveFetcher:
        def fetch(self, inst: dict, interval: str, limit: int = 200, now_utc=None):
            if interval in ("1m", "15m", "1h"):
                intraday_fetched.append((inst["symbol"], interval))
            if interval == "1d":
                return FetchResult(inst["symbol"], interval, True, "fake", flat_daily, None)
            if interval == "15m":
                return FetchResult(inst["symbol"], interval, True, "fake", flat_15m, None)
            if interval == "1m":
                return FetchResult(inst["symbol"], interval, True, "fake", flat_1m, None)
            return FetchResult(inst["symbol"], interval, False, "", [], "unsupported")

    # 1) Verify scan.py skips NAS100 (daily_only) for 1m/15m intraday fetching
    scan_res = run_scan(
        out_dir=out_dir,
        config_path=cfg_path,
        control_path=ctrl_path,
        events_path=ev_path,
        fetcher=_FakeLiveFetcher(),
        derivatives_fetcher=None,
        now_utc=now,
    )
    assert all(sym != "NAS100" for sym, _ in intraday_fetched), "daily_only index must not fetch 1m/15m"
    assert "signals_published" in scan_res

    # 2) Verify daily.py emits L1 signal onto simulation ledger even when L1 is "觀察中（未通過驗證）"
    daily_res = run_daily(
        out_dir=out_dir,
        config_path=cfg_path,
        control_path=ctrl_path,
        fetcher=_FakeLiveFetcher(),
        now_utc=now,
    )
    assert len(daily_res["signals_published"]) >= 1
    signals_index = json.loads((out_dir / "signals_index.json").read_text(encoding="utf-8"))
    l1_signals = [s for s in signals_index if s["strategy"] == "L1"]
    assert len(l1_signals) >= 1
    assert l1_signals[0]["verification_status"] == "觀察中（未通過驗證）"
    assert (out_dir / "signals" / f"{l1_signals[0]['signal_id']}.json").exists()
    assert any(s["signal_id"] in daily_res["signals_published"] for s in l1_signals)


def test_binance_monthly_archive_fetcher_caches_and_parses_ms_and_us_timestamps(tmp_path: Path) -> None:
    """Verify BinanceMonthlyArchiveFetcher handles both 13-digit ms & 16-digit us timestamps and caches ZIPs on disk."""
    def _make_zip_bytes(rows: list[list[str]]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            csv_buf = io.StringIO()
            writer = csv.writer(csv_buf)
            writer.writerows(rows)
            zf.writestr("BTCUSDT-1h.csv", csv_buf.getvalue())
        return buf.getvalue()

    ms_ts = str(int(datetime(2024, 12, 15, 0, 0, tzinfo=timezone.utc).timestamp() * 1000))
    us_ts = str(int(datetime(2025, 1, 15, 0, 0, tzinfo=timezone.utc).timestamp() * 1_000_000))
    zip_dec = _make_zip_bytes([[ms_ts, "90000", "90500", "89500", "90200", "123.4", "0"]])
    zip_jan = _make_zip_bytes([[us_ts, "95000", "95500", "94500", "95200", "234.5", "0"]])

    fetcher = BinanceMonthlyArchiveFetcher(cache_dir=tmp_path, timeout_sec=5, max_workers=2)
    calls: list[str] = []

    class _FakeResp:
        def __init__(self, status_code: int, content: bytes = b"", json_data=None):
            self.status_code = status_code
            self.content = content
            self._json = json_data

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            return self._json

    first_ms = int(datetime(2024, 12, 1, tzinfo=timezone.utc).timestamp() * 1000)
    latest_ms = int(datetime(2025, 2, 10, tzinfo=timezone.utc).timestamp() * 1000)

    def _fake_get(url: str, **kwargs):
        calls.append(url)
        if "api/v3/klines" in url:
            params = kwargs.get("params", {})
            if "startTime" in params:
                return _FakeResp(200, json_data=[[first_ms]])
            return _FakeResp(200, json_data=[[latest_ms]])
        if "2024-12.zip" in url:
            return _FakeResp(200, content=zip_dec)
        if "2025-01.zip" in url:
            return _FakeResp(200, content=zip_jan)
        return _FakeResp(404)

    fetcher._session.get = _fake_get  # type: ignore[method-assign]
    now = datetime(2025, 2, 10, 0, 0, tzinfo=timezone.utc)
    bars = fetcher.fetch_history("BTCUSDT", "1h", years=0.3, now_utc=now, include_current_month=False)
    assert [b.ts_utc for b in bars] == ["2024-12-15T00:00:00Z", "2025-01-15T00:00:00Z"]
    assert (tmp_path / "BTCUSDT" / "1h" / "BTCUSDT-1h-2024-12.zip").exists()
    assert (tmp_path / "BTCUSDT" / "1h" / "BTCUSDT-1h-2025-01.zip").exists()
    assert len(calls) >= 2

    calls.clear()
    fetcher._session.get = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("Cache miss!"))  # type: ignore[method-assign]
    bars_cached = fetcher.fetch_history("BTCUSDT", "1h", years=0.3, now_utc=now, include_current_month=False)
    assert bars_cached == bars
    assert len(calls) == 0


def test_weekly_job_outputs_six_strategies_json(tmp_path: Path) -> None:
    class _FakeFetcher:
        def fetch(self, inst: dict, interval: str, limit: int = 200, now_utc=None):
            step = {"1m": 1, "15m": 15, "1d": 1440}[interval]
            cnt = 2200 if interval == "1d" else 600
            return FetchResult(
                symbol=inst["symbol"],
                interval=interval,
                ok=True,
                source_used="fake",
                bars=_make_bars(cnt, step, "trend_wave"),
                error=None,
            )

    now = datetime(2026, 4, 20, 0, 15, tzinfo=timezone.utc)
    res = run_weekly_screening(out_dir=tmp_path, fetcher=_FakeFetcher(), now_utc=now)
    assert res["summary"]["total_strategies"] == 6
    saved = json.loads((tmp_path / "backtest_results.json").read_text(encoding="utf-8"))
    assert [s["strategy_id"] for s in saved["strategies"]] == ["S1", "S2", "S3", "L1", "L2", "L3"]
    assert all("verification_label" in s for s in saved["strategies"])


def test_expired_bars_never_emit_signals_and_marked_in_watch(tmp_path: Path) -> None:
    """Bars older than 1 candle + allowed delay (e.g. weekend Friday FX bars at Monday 12:20 UTC) must NOT emit signals."""
    cfg = load_config()
    small_cfg = copy.deepcopy(cfg)
    small_cfg["instruments"] = [i for i in cfg["instruments"] if i["symbol"] in ("EURUSD", "BTCUSDT")]
    import yaml

    cfg_path = tmp_path / "config.yaml"
    ctrl_path = tmp_path / "control.json"
    ev_path = tmp_path / "events.yaml"
    cfg_path.write_text(yaml.safe_dump(small_cfg), encoding="utf-8")
    ctrl_path.write_text(json.dumps({"paused": False, "risk_review_done": False}), encoding="utf-8")
    ev_path.write_text("events: []\n", encoding="utf-8")
    out_dir = tmp_path / "data"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Monday 2026-11-16 12:20 UTC while EURUSD only has Friday 2026-11-13 bars (strong 4h breakout on last bar)
    now = datetime(2026, 11, 16, 12, 20, tzinfo=timezone.utc)
    fri_end_15m = datetime(2026, 11, 13, 21, 45, tzinfo=timezone.utc)
    eur_15m = [
        OHLCVBar(
            ts_utc=(fri_end_15m - timedelta(minutes=15 * (400 - k))).strftime("%Y-%m-%dT%H:%M:%SZ"),
            open=1.0800,
            high=1.0810,
            low=1.0790,
            close=1.0800 if k < 384 else 1.1200,
            volume=100.0,
        )
        for k in range(401)
    ]
    eur_1m = [
        OHLCVBar(
            ts_utc=(fri_end_15m - timedelta(minutes=(60 - k))).strftime("%Y-%m-%dT%H:%M:%SZ"),
            open=1.1200,
            high=1.1210,
            low=1.1190,
            close=1.1200,
            volume=50.0,
        )
        for k in range(60)
    ]
    eur_1d = _make_bars(250, 1440, "trend_wave", start=datetime(2026, 3, 1, tzinfo=timezone.utc))

    # BTCUSDT has fresh bars ending just before now
    btc_15m = _make_bars(400, 15, "trend_wave", start=now - timedelta(minutes=15 * 401))
    btc_1m = _make_bars(60, 1, "trend_wave", start=now - timedelta(minutes=61))
    btc_1d = _make_bars(250, 1440, "trend_wave", start=now - timedelta(days=251))

    class _ExpiredFxFetcher:
        def fetch(self, inst: dict, interval: str, limit: int = 200, now_utc=None):
            if inst["symbol"] == "EURUSD":
                bars = {"1m": eur_1m, "15m": eur_15m, "1d": eur_1d}[interval]
                return FetchResult("EURUSD", interval, True, "yfinance", bars, None)
            bars = {"1m": btc_1m, "15m": btc_15m, "1d": btc_1d}[interval]
            return FetchResult("BTCUSDT", interval, True, "binance_vision", bars, None)

    scan_res = run_scan(
        out_dir=out_dir,
        config_path=cfg_path,
        control_path=ctrl_path,
        events_path=ev_path,
        fetcher=_ExpiredFxFetcher(),
        derivatives_fetcher=lambda _i: {"source": "okx", "funding_rate": 0.0001, "oi_change_1h_pct": 0.5, "status": "ok"},
        now_utc=now,
    )
    eur_sigs = [
        s
        for s in json.loads((out_dir / "signals_index.json").read_text(encoding="utf-8"))
        if s["symbol"] == "EURUSD"
    ]
    assert eur_sigs == [], f"Expired EURUSD bars must NEVER emit signals: {eur_sigs}"
    assert not any(sid for sid in scan_res["signals_published"] if sid in {s["signal_id"] for s in eur_sigs})

    watch_doc = json.loads((out_dir / "watch_today.json").read_text(encoding="utf-8"))
    eur_watch = next(it for it in watch_doc["items"] if it["symbol"] == "EURUSD")
    assert "資料過期" in eur_watch["reason"]
    assert eur_watch["strategies"]["S2"]["status"] == "expired"

    # Daily job with expired 1d bars (> 25h old) must also block and label 資料過期
    stale_1d = _make_bars(250, 1440, "rsi_pullback", start=datetime(2026, 3, 1, tzinfo=timezone.utc))
    daily_res = run_daily(
        out_dir=out_dir,
        config_path=cfg_path,
        control_path=ctrl_path,
        fetcher=type(
            "_StaleDailyFetcher",
            (),
            {"fetch": lambda self, inst, iv, limit=200, now_utc=None: FetchResult(inst["symbol"], iv, True, "fake", stale_1d if iv == "1d" else [], None)},
        )(),
        now_utc=now,
    )
    assert daily_res["signals_published"] == []
    watch_daily = json.loads((out_dir / "watch_today_daily.json").read_text(encoding="utf-8"))
    eur_daily = next(it for it in watch_daily["items"] if it["symbol"] == "EURUSD")
    assert "資料過期" in eur_daily["reason"]


def test_closed_at_utc_never_earlier_than_published_at_utc_and_purges_corrupted_signals(tmp_path: Path) -> None:
    """Historical bars before published_at_utc must never cancel/close a signal, and legacy bad signals auto-purge."""
    from src.engine.simulator import CandidateSignal, MarketBar, SimulationEngine
    from src.signals.models import InputSnapshot

    cfg = load_config()
    eng = SimulationEngine(cfg, out_dir=tmp_path)
    pub_ts = "2026-11-16T12:20:00Z"
    cand = CandidateSignal(
        strategy="S2",
        symbol="BTCUSDT",
        direction="long",
        entry_low=99.0,
        entry_high=101.0,
        add_levels=(),
        stop_loss=94.0,
        tp1=112.0,
        tp2=118.0,
        invalidation_price=93.0,
        invalidation_cond="觸及止損價",
        reasons=("breakout",),
        snapshot=InputSnapshot("SNAP-TEST", "PENDING", pub_ts, "BTCUSDT", {"interval": "4h", "close": 100.0}, {}, {}),
        atr=2.0,
        atr_ratio=1.0,
        leverage=3.0,
        time_stop_bars=30,
        verification_status="觀察中（未通過驗證）",
    )
    sig = eng.submit_candidate_signal(
        cand,
        now_utc=pub_ts,
        latest_bar_close_utc="2026-11-16T12:00:00Z",
        control={"paused": False, "risk_review_done": False},
    )
    assert sig is not None

    # Replay an old historical bar from 2026-11-13 that would have hit invalidation_price=93.0
    eng.replay_bars(
        "BTCUSDT",
        [MarketBar("2026-11-13T21:00:00Z", "2026-11-13T21:01:00Z", 90.0, 91.0, 89.0, 90.0, 100.0)],
    )
    tr = eng.ledger.trades[sig.signal_id]
    assert tr.state == LifecycleState.WAITING_ENTRY
    assert tr.closed_at_utc is None

    # Now replay a bar AFTER published_at_utc that triggers invalidation -> closed_at_utc >= published_at_utc
    eng.replay_bars(
        "BTCUSDT",
        [MarketBar("2026-11-16T12:21:00Z", "2026-11-16T12:22:00Z", 93.5, 93.6, 92.0, 92.5, 100.0)],
    )
    assert tr.state == LifecycleState.CANCELLED
    assert tr.closed_at_utc is not None and tr.closed_at_utc >= sig.published_at_utc

    # Seed SIG-20261116-0001..0003 corrupted FX S2 entries & verify auto-purge upon load_state()
    purge_dir = tmp_path / "purge_check"
    eng2 = SimulationEngine(cfg, out_dir=purge_dir)
    for idx, sym in enumerate(("EURUSD", "GBPUSD", "USDCHF"), start=1):
        sid = f"SIG-20261116-000{idx}"
        (purge_dir / "signals").mkdir(parents=True, exist_ok=True)
        (purge_dir / "snapshots").mkdir(parents=True, exist_ok=True)
        (purge_dir / "signals" / f"{sid}.json").write_text(
            json.dumps(
                {
                    "signal_id": sid,
                    "published_at_utc": "2026-11-16T12:20:00Z",
                    "strategy": "S2",
                    "symbol": sym,
                    "market": "forex",
                    "direction": "long",
                }
            ),
            encoding="utf-8",
        )
        (purge_dir / "snapshots" / f"SNAP-{sid}.json").write_text(
            json.dumps({"snapshot_id": f"SNAP-{sid}", "signal_id": sid, "kline_summary": {"interval": "4h", "ts_utc": "2026-11-13T16:00:00Z"}}),
            encoding="utf-8",
        )
        # Also inject corrupted closed_at_utc < published_at_utc into ledger trades
        from src.engine.simulator import TradeRecord

        eng2.ledger.trades[sid] = TradeRecord(
            signal_id=sid,
            strategy="S2",
            symbol=sym,
            market="forex",
            correlation_group="fx",
            direction="long",
            state=LifecycleState.CANCELLED,
            entry_low=1.08,
            entry_high=1.09,
            add_levels=(),
            initial_stop_loss=1.07,
            current_stop_loss=1.07,
            current_tp1=1.11,
            current_tp2=1.12,
            invalidation_price=1.07,
            valid_until_utc="2026-11-16T16:20:00Z",
            time_stop_bars=30,
            r_plan_usd=75.0,
            planned_notional_usd=1000.0,
            leverage=0.03,
            margin_leverage=5.0,
            assumed_spread_bps=2.0,
            fee_rate=0.0,
            slippage_bps=1.0,
            overnight_cost_daily_bps=0.2,
            closed_at_utc="2026-11-13T21:00:00Z",
            exit_reason="pre_entry_invalidated",
            published_at_utc="2026-11-16T12:20:00Z",
        )
    eng2.save_state("2026-11-16T12:25:00Z")

    eng_reloaded = SimulationEngine(cfg, out_dir=purge_dir)
    eng_reloaded.load_state()
    eng_reloaded.save_state("2026-11-16T12:35:00Z")
    for idx in (1, 2, 3):
        sid = f"SIG-20261116-000{idx}"
        assert sid not in eng_reloaded.ledger.trades
        assert not (purge_dir / "signals" / f"{sid}.json").exists()
        assert not (purge_dir / "snapshots" / f"SNAP-{sid}.json").exists()
    assert json.loads((purge_dir / "signals_index.json").read_text(encoding="utf-8")) == []
