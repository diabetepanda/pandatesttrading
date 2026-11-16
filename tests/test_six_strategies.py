"""Unit tests for the 6 unified strategies (S1-S3, L1-L3), sub-account risk limits, archive caching, and bounded drawdowns."""

from __future__ import annotations

import copy
import csv
import io
import json
import math
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.backtest.screener import (
    compute_metrics,
    screen_all_six_strategies,
    screen_single_strategy,
    simulate_l2_portfolio,
    simulate_portfolio_strategy,
    simulate_single_symbol,
)
from src.config_loader import load_config
from src.data.adapters import BinanceMonthlyArchiveFetcher
from src.data.models import FetchResult, OHLCVBar
from src.jobs.weekly import run_weekly_screening
from src.risk.manager import RiskManager
from src.strategies.six_strategies import (
    allocate_subaccount_position,
    decide_l2_rotation_step,
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
            # Violent reversals triggering frequent losing trades to stress-test drawdown floor
            step = 6.0 if (i % 6) < 3 else -6.2
        elif mode == "range_osc":
            step = math.sin(i / 4.0) * 3.0 if (i % 60) < 40 else (1.8 if (i % 120) < 60 else -1.8)
        else:
            if (i % 80) < 60:
                step = 0.05 if i % 2 == 0 else -0.05
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
        (
            "S1",
            _make_bars(420, 60, "range_osc"),
            {
                "trend_ma_period": 50,
                "exit_ma_period": 5,
                "rsi_period": 2,
                "rsi_long_thresh": 25.0,
                "rsi_short_thresh": 75.0,
                "sl_atr_mult": 2.0,
            },
            "1h",
        ),
        (
            "S2",
            _make_bars(420, 15, "squeeze"),
            {
                "bb_period": 20,
                "bb_std": 2.0,
                "squeeze_lookback": 48,
                "squeeze_percentile": 35.0,
                "sl_atr_mult": 1.5,
            },
            "15m",
        ),
        (
            "S3",
            _make_bars(420, 15, "range_osc"),
            {
                "range_bars": 24,
                "min_edge_touches": 2,
                "edge_zone_pct": 25.0,
                "rsi_period": 14,
                "rsi_oversold": 42.0,
                "rsi_overbought": 58.0,
                "sl_atr_buffer": 0.5,
            },
            "15m",
        ),
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
    _, _, bt_l2_trace = simulate_l2_portfolio(bars_map, inst_map, l2_params, cfg)
    assert len(bt_l2_trace) > 0

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

    params = {"entry_period": 20, "exit_period": 10, "sl_atr_mult": 2.5}
    tr_zero, _ = simulate_single_symbol(bars, "L1", params, inst_zero, cfg_zero, interval="1d")
    tr_cost, _ = simulate_single_symbol(bars, "L1", params, inst_cost, cfg, interval="1d")
    assert len(tr_zero) > 0
    assert sum(t.net_pnl_usd for t in tr_cost) < sum(t.net_pnl_usd for t in tr_zero)


def test_equity_curve_never_below_zero_and_drawdown_under_100_pct() -> None:
    """Equity curve must remain strictly > 0 and max drawdown < 100% across all 6 strategies including stress whipsaw."""
    cfg = load_config()
    small_cfg = copy.deepcopy(cfg)
    syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT"]
    small_cfg["instruments"] = [i for i in cfg["instruments"] if i["symbol"] in syms]

    bars_1d = {s: _make_bars(2200, 1440, "crash_whipsaw", phase=idx * 0.4) for idx, s in enumerate(syms)}
    bars_1h = {s: _make_bars(1500, 60, "crash_whipsaw", phase=idx * 0.4) for idx, s in enumerate(syms)}
    bars_15m = {s: _make_bars(1500, 15, "crash_whipsaw", phase=idx * 0.4) for idx, s in enumerate(syms)}

    results = screen_all_six_strategies({"1d": bars_1d, "1h": bars_1h, "15m": bars_15m}, small_cfg)
    assert len(results) == 6
    for r in results:
        for split_key in ("in_sample", "out_of_sample"):
            m = r[split_key]
            assert m["min_equity_usd"] > 0.0, f"{r['strategy_id']} {split_key} min_equity went <= 0"
            assert m["ending_equity_usd"] > 0.0
            assert 0.0 <= m["max_drawdown_pct"] < 100.0, f"{r['strategy_id']} {split_key} DD >= 100%: {m['max_drawdown_pct']}"

    # Direct portfolio curve check for L3 under extreme whipsaw
    inst_map = {i["symbol"]: i for i in small_cfg["instruments"]}
    tr_l3, eq_l3, _ = simulate_portfolio_strategy(
        bars_by_symbol=bars_1d,
        strategy_id="L3",
        params={"ma_period": 150, "sl_atr_mult": 2.5},
        instruments_by_sym=inst_map,
        config=small_cfg,
        interval="1d",
    )
    assert all(eq > 0.0 for eq in eq_l3)
    m_l3 = compute_metrics(tr_l3, small_cfg["strategies"]["L3"]["capital_usd"], equity_curve_points=eq_l3)
    assert m_l3["max_drawdown_pct"] < 100.0


def test_shared_subaccount_sizing_and_concurrent_limits() -> None:
    """Verify backtest and RiskManager live sizing use the same allocate_subaccount_position limits."""
    cfg = load_config()
    rm = RiskManager(cfg)
    btc = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")

    # Identical output between direct allocate_subaccount_position and RiskManager live wrapper
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
    )
    assert direct == via_rm
    assert direct.approved is True
    # 0.75% of $20,000 = $150 target risk
    assert abs(direct.risk_usd - 150.0) < 1e-4

    # Concurrent position cap enforced (max_concurrent_positions = 5)
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

    # Total open risk cap enforced (5% <= 6% of $20,000 = $1,000)
    risk_capped = rm.check_new_signal(
        instrument=btc,
        strategy="L3",
        direction="long",
        entry_price=100.0,
        stop_loss=95.0,
        tp1=110.0,
        tp2=115.0,
        desired_notional_usd=5000.0,
        leverage=5.0,
        account_state={
            "equity_usd": 100000.0,
            "peak_equity_usd": 100000.0,
            "subaccount_equity_usd": 20000.0,
            "subaccount_open_positions": 3,
            "subaccount_open_risk_usd": 1000.0,
            "subaccount_open_notional_usd": 9000.0,
        },
        control={"paused": False, "risk_review_done": False},
        now_utc="2026-11-16T02:00:00Z",
    )
    assert risk_capped.approved is False
    assert "總風險已達子帳戶" in risk_capped.reason


def test_short_term_excludes_fx_and_commodity_and_handles_short_history(tmp_path: Path) -> None:
    """Short-term backtest must exclude FX/commodities lacking 5y data and never mix in 1-month data."""
    cfg = load_config()
    small_cfg = copy.deepcopy(cfg)
    small_cfg["instruments"] = [
        i for i in cfg["instruments"] if i["symbol"] in ("BTCUSDT", "SUIUSDT", "EURUSD", "XAUUSD")
    ]

    # Case 1: Only short-term crypto < 5 years -> flagged insufficient_history
    bars_15m_short = {
        "BTCUSDT": _make_bars(960, 15, "range_osc"),
        "SUIUSDT": _make_bars(960, 15, "range_osc"),
        "EURUSD": _make_bars(960, 15, "range_osc"),
        "XAUUSD": _make_bars(960, 15, "range_osc"),
    }
    bars_1h_short = {k: _make_bars(400, 60, "range_osc") for k in bars_15m_short}
    bars_1d_long = {k: _make_bars(2300, 1440, "trend_wave") for k in bars_15m_short}

    res_short = screen_all_six_strategies(
        {"15m": bars_15m_short, "1h": bars_1h_short, "1d": bars_1d_long}, small_cfg
    )
    assert [r["strategy_id"] for r in res_short] == ["S1", "S2", "S3", "L1", "L2", "L3"]
    for s_row in res_short[:3]:
        assert s_row["status"] == "insufficient_history"
        assert s_row["passed"] is False
        assert s_row["excluded_no_free_history_count"] == 2
        assert "無足夠免費歷史資料已排除" in s_row["data_note"]

    # Case 2: BTC has >5y while SUI has ~1y -> FX excluded, SUI included & marked, strategy history >= 5y
    start_2020 = datetime(2020, 1, 1, tzinfo=timezone.utc)
    start_2024 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # 5.1 years on 1h: ~44700 bars -> fast fixture uses 2 points spanning 5.2 years? Need >= 30 bars spanning 5.1y
    long_btc_1h = _make_bars(200, 60 * 240, "range_osc", start=start_2020)  # 200 * 10d = 5.47y
    short_sui_1h = _make_bars(80, 60 * 48, "range_osc", start=start_2024)
    res_s1 = screen_single_strategy(
        "S1",
        {"1h": {"BTCUSDT": long_btc_1h, "SUIUSDT": short_sui_1h, "EURUSD": _make_bars(100, 60)}, "15m": {}, "1d": {}},
        small_cfg,
    )
    assert res_s1["eligible_instruments"] == 2  # BTC + SUI; EURUSD & XAUUSD excluded!
    assert res_s1["excluded_no_free_history_count"] == 2
    assert res_s1["short_history_crypto_count"] == 1
    assert "SUIUSDT" in res_s1["data_note"]
    assert res_s1["history_years"] >= 5.0


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

    ms_ts = str(int(datetime(2024, 12, 15, 0, 0, tzinfo=timezone.utc).timestamp() * 1000))  # 13 digits
    us_ts = str(int(datetime(2025, 1, 15, 0, 0, tzinfo=timezone.utc).timestamp() * 1_000_000))  # 16 digits
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

    # Second run must hit local disk cache with ZERO network calls
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
