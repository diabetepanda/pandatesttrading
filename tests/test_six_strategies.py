"""Unit tests for the 6 unified strategies (S1-S3, L1-L3): backtest-live parity, IS/OOS isolation, costs, and portfolio screening."""

from __future__ import annotations

import copy
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.backtest.screener import (
    screen_all_six_strategies,
    screen_single_strategy,
    simulate_l2_portfolio,
    simulate_single_symbol,
)
from src.config_loader import load_config
from src.data.models import FetchResult, OHLCVBar
from src.jobs.weekly import run_weekly_screening
from src.strategies.six_strategies import decide_l2_rotation_step, evaluate_live_bar


def _make_bars(
    count: int = 2300,
    step_minutes: int = 1440,
    mode: str = "trend_wave",
    phase: float = 0.0,
) -> list[OHLCVBar]:
    """Generate deterministic OHLCV bars for intraday or daily testing."""
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    bars: list[OHLCVBar] = []
    price = 100.0
    for i in range(count):
        dt = start + timedelta(minutes=step_minutes * i)
        if mode == "trend_wave":
            step = math.sin(i / 14.0 + phase) * 2.2 + 0.04
        elif mode == "range_osc":
            # Oscillates in a clean box with periodic extremes to trigger S3 and squeeze breakouts
            step = math.sin(i / 4.0) * 3.0 if (i % 60) < 40 else (1.8 if (i % 120) < 60 else -1.8)
        else:
            # Flat tight squeeze followed by sharp breakout spikes for S2
            if (i % 80) < 60:
                step = (0.05 if i % 2 == 0 else -0.05)
            else:
                step = 2.5 if (i % 160) < 80 else -2.5
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
    cfg: dict,
    allow_short: bool = True,
) -> list[tuple[str, str, str, float | None]]:
    """Run live bar-by-bar decision function through history using the exact backtest SL/TP state machine."""
    spread_bps = float(inst.get("assumed_spread_bps", 2.0))
    slip = float(cfg.get("risk", {}).get("default_slippage_bps", 1.0)) / 10000.0
    in_pos = False
    pos_dir = ""
    sl_px = None
    tp_px = None
    trace = []

    for i in range(1, len(bars) - 1):
        bar = bars[i]
        if in_pos:
            exited = False
            if pos_dir == "long":
                if sl_px is not None and (bar.open * (1.0 - spread_bps / 20000.0) <= sl_px or bar.low * (1.0 - spread_bps / 20000.0) <= sl_px):
                    exited = True
                elif tp_px is not None and bar.high * (1.0 - spread_bps / 20000.0) >= tp_px:
                    exited = True
            else:
                if sl_px is not None and (bar.open * (1.0 + spread_bps / 20000.0) >= sl_px or bar.high * (1.0 + spread_bps / 20000.0) >= sl_px):
                    exited = True
                elif tp_px is not None and bar.low * (1.0 + spread_bps / 20000.0) <= tp_px:
                    exited = True
            if exited:
                in_pos = False
                pos_dir = ""
                continue

        act = evaluate_live_bar(
            strategy_id=strategy_id,
            closed_bars=bars[: i + 1],
            position_dir=pos_dir,
            params=params,
            market=inst["market"],
            interval=interval,
            allow_short=allow_short,
        )
        if act.action != "hold":
            sl_r = round(act.stop_loss, 6) if act.stop_loss is not None else None
            trace.append((bar.ts_utc, act.action, act.direction, sl_r))
        if in_pos and act.action == "exit":
            in_pos = False
            pos_dir = ""
        elif not in_pos and act.action in ("enter_long", "enter_short"):
            in_pos = True
            pos_dir = act.direction
            sl_px = act.stop_loss
            tp_px = act.take_profit

    return trace


def test_backtest_and_live_produce_identical_signals_for_all_six_strategies() -> None:
    """Prove backtest and live decision functions produce 100% identical entries & exits across S1-S3, L1-L3."""
    cfg = load_config()
    btc = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")

    cases = [
        ("S1", _make_bars(420, 60, "range_osc"), {"trend_ma_period": 50, "exit_ma_period": 5, "rsi_period": 2, "rsi_long_thresh": 25.0, "rsi_short_thresh": 75.0, "sl_atr_mult": 2.0}, "1h"),
        ("S2", _make_bars(420, 15, "squeeze"), {"bb_period": 20, "bb_std": 2.0, "squeeze_lookback": 48, "squeeze_percentile": 35.0, "sl_atr_mult": 1.5}, "15m"),
        ("S3", _make_bars(420, 15, "range_osc"), {"range_bars": 24, "min_edge_touches": 2, "edge_zone_pct": 25.0, "rsi_period": 14, "rsi_oversold": 42.0, "rsi_overbought": 58.0, "sl_atr_buffer": 0.5}, "15m"),
        ("L1", _make_bars(420, 1440, "trend_wave"), {"entry_period": 20, "exit_period": 10, "sl_atr_mult": 2.5}, "1d"),
        ("L3", _make_bars(420, 1440, "trend_wave"), {"ma_period": 100, "sl_atr_mult": 2.5}, "1d"),
    ]

    for sid, bars, params, interval in cases:
        _, bt_trace = simulate_single_symbol(bars, sid, params, btc, cfg, interval=interval, allow_short=True, start_idx=0)
        live_trace = _step_live_sequence(sid, bars, params, btc, interval, cfg, allow_short=True)
        assert len(bt_trace) > 0, f"{sid} should produce real signals in test fixture"
        assert bt_trace == live_trace, f"Mismatch between backtest and live decisions for {sid}"

    # L2 cross-sectional rotation parity proof
    cryptos = [i for i in cfg["instruments"] if i["market"] == "crypto"][:4]
    inst_map = {i["symbol"]: i for i in cryptos}
    bars_map = {
        i["symbol"]: _make_bars(240, 1440, "trend_wave", phase=idx * 0.9)
        for idx, i in enumerate(cryptos)
    }
    l2_params = {"lookback_days": 14, "top_k": 2, "rebalance_days": 7, "sl_atr_mult": 3.0}
    _, bt_l2_trace = simulate_l2_portfolio(bars_map, inst_map, l2_params, cfg)
    assert len(bt_l2_trace) > 0

    # Re-run step-by-step live decide_l2_rotation_step at rebalance dates
    all_dates = sorted({b.ts_utc[:10] for b_list in bars_map.values() for b in b_list})
    held_syms: set[str] = set()
    held_sl: dict[str, float] = {}
    live_l2_trace = []
    date_idx_map = {sym: {b.ts_utc[:10]: idx for idx, b in enumerate(bs)} for sym, bs in bars_map.items()}
    for d_idx in range(1, len(all_dates) - 1):
        d = all_dates[d_idx]
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
        acts = decide_l2_rotation_step(up_to, set(held_syms), 14, 2, 3.0)
        for sym, act in acts.items():
            live_l2_trace.append((d, sym, act.action, act.direction))
            if act.action == "exit" and sym in held_syms:
                held_syms.remove(sym)
                held_sl.pop(sym, None)
            elif act.action == "enter_long" and sym not in held_syms:
                held_syms.add(sym)
                held_sl[sym] = act.stop_loss or 0.0

    assert bt_l2_trace == live_l2_trace


def test_oos_mutation_never_leaks_into_is_parameter_selection() -> None:
    """Mutating the 30% OOS slice must NOT alter chosen IS params or IS metrics."""
    cfg = load_config()
    small_cfg = copy.deepcopy(cfg)
    small_cfg["instruments"] = [i for i in cfg["instruments"] if i["symbol"] in ("BTCUSDT", "ETHUSDT", "EURUSD")]

    bars_orig_1d = {sym: _make_bars(2300, 1440, "trend_wave") for sym in ("BTCUSDT", "ETHUSDT", "EURUSD")}
    bars_mut_1d = {}
    for sym, bars in bars_orig_1d.items():
        sp = int(len(bars) * 0.7)
        mutated = list(bars[:sp])
        for j, b in enumerate(bars[sp:]):
            f = 4.0 if j % 2 == 0 else 0.25
            mutated.append(OHLCVBar(b.ts_utc, b.open * f, b.high * f * 1.2, b.low * f * 0.8, b.close * f, b.volume * 5))
        bars_mut_1d[sym] = mutated

    for sid in ("L1", "L2", "L3"):
        r1 = screen_single_strategy(sid, {"1d": bars_orig_1d, "15m": {}, "1h": {}}, small_cfg)
        r2 = screen_single_strategy(sid, {"1d": bars_mut_1d, "15m": {}, "1h": {}}, small_cfg)
        assert r1["chosen_params"] == r2["chosen_params"]
        assert r1["in_sample"] == r2["in_sample"]
        assert r1["out_of_sample"] != r2["out_of_sample"]


def test_backtest_deducts_fees_spread_slippage_and_overnight_costs() -> None:
    cfg = load_config()
    bars = _make_bars(420, 1440, "trend_wave")
    inst_zero = {"symbol": "TEST", "market": "crypto", "assumed_spread_bps": 0.0, "fee_rate": 0.0, "overnight_cost_daily_bps": 0.0}
    inst_cost = {"symbol": "TEST", "market": "crypto", "assumed_spread_bps": 10.0, "fee_rate": 0.002, "overnight_cost_daily_bps": 5.0}
    cfg_zero = copy.deepcopy(cfg)
    cfg_zero["risk"]["default_slippage_bps"] = 0.0

    params = {"entry_period": 20, "exit_period": 10, "sl_atr_mult": 2.5}
    tr_zero, _ = simulate_single_symbol(bars, "L1", params, inst_zero, cfg_zero, interval="1d")
    tr_cost, _ = simulate_single_symbol(bars, "L1", params, inst_cost, cfg, interval="1d")
    assert len(tr_zero) > 0
    assert sum(t.net_pnl_usd for t in tr_cost) < sum(t.net_pnl_usd for t in tr_zero)


def test_short_term_under_5_years_flagged_honestly_without_faking() -> None:
    """Short-term intraday data < 5 years reports real available years & OOS trades, strictly failing passed."""
    cfg = load_config()
    small_cfg = copy.deepcopy(cfg)
    small_cfg["instruments"] = [i for i in cfg["instruments"] if i["symbol"] in ("BTCUSDT", "ETHUSDT")]

    bars_15m = {s: _make_bars(960, 15, "range_osc") for s in ("BTCUSDT", "ETHUSDT")}
    bars_1h = {s: _make_bars(400, 60, "range_osc") for s in ("BTCUSDT", "ETHUSDT")}
    bars_1d = {s: _make_bars(2300, 1440, "trend_wave") for s in ("BTCUSDT", "ETHUSDT")}

    results = screen_all_six_strategies({"15m": bars_15m, "1h": bars_1h, "1d": bars_1d}, small_cfg)
    assert [r["strategy_id"] for r in results] == ["S1", "S2", "S3", "L1", "L2", "L3"]
    for s_row in results[:3]:
        assert s_row["status"] == "insufficient_history"
        assert s_row["passed"] is False
        assert s_row["history_years"] < 5.0
        assert "不足 5 年" in s_row["reason"]
        assert isinstance(s_row["out_of_sample"]["trades"], int)


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
