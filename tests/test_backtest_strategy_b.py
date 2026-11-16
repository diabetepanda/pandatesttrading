"""Unit tests for Strategy B backtest, strict IS/OOS isolation, cost deduction, and screening."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from src.backtest.screener import BacktestTrade, compute_metrics, screen_instrument
from src.config_loader import load_config
from src.data.models import OHLCVBar
from src.strategies.strategy_b import StrategyB


def _make_daily_bars(days: int = 2300, seed_mode: str = "trending_cycles") -> list[OHLCVBar]:
    """Generate deterministic multi-year daily OHLCV bars."""
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    bars: list[OHLCVBar] = []
    price = 100.0
    for i in range(days):
        dt = start + timedelta(days=i)
        if seed_mode == "trending_cycles":
            # Periodic 40-day bull/bear waves with slight upward drift
            wave = math.sin(i / 18.0) * 1.4 + 0.03
        else:
            wave = -math.sin(i / 10.0) * 2.0
        o = max(5.0, price)
        c = max(5.0, o + wave)
        h = max(o, c) + 0.8
        l = max(1.0, min(o, c) - 0.8)
        bars.append(
            OHLCVBar(
                ts_utc=dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                open=round(o, 4),
                high=round(h, 4),
                low=round(l, 4),
                close=round(c, 4),
                volume=1000.0 + (i % 50) * 20.0,
            )
        )
        price = c
    return bars


def test_parameter_selection_uses_only_in_sample_data_never_oos() -> None:
    """Dedicated anti-leakage proof: mutating OOS bars MUST NOT change chosen IS parameters."""
    cfg = load_config()
    inst = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")
    bars = _make_daily_bars(days=2300)  # ~6.3 years

    res_orig = screen_instrument(inst, bars, cfg)
    assert res_orig["status"] == "ok"
    assert res_orig["chosen_strategy"] is not None
    assert res_orig["chosen_params"] is not None

    # Severely mutate ONLY the out-of-sample (last 30%) slice
    split_idx = int(len(bars) * float(cfg["strategy_b"]["in_sample_ratio"]))
    mutated_bars = list(bars[:split_idx])
    for j, b in enumerate(bars[split_idx:]):
        factor = 5.0 if j % 2 == 0 else 0.2
        mutated_bars.append(
            OHLCVBar(
                ts_utc=b.ts_utc,
                open=b.open * factor,
                high=b.high * factor * 1.2,
                low=b.low * factor * 0.8,
                close=b.close * factor,
                volume=b.volume * 10.0,
            )
        )

    res_mutated = screen_instrument(inst, mutated_bars, cfg)
    assert res_mutated["chosen_strategy"] == res_orig["chosen_strategy"]
    assert res_mutated["chosen_params"] == res_orig["chosen_params"]
    assert res_mutated["in_sample"] == res_orig["in_sample"]
    assert res_mutated["out_of_sample"] != res_orig["out_of_sample"]


def test_backtest_deducts_fees_spread_slippage_and_overnight_costs() -> None:
    cfg = load_config()
    inst_zero = {
        "symbol": "TESTUSD",
        "market": "forex",
        "assumed_spread_bps": 0.0,
        "fee_rate": 0.0,
        "overnight_cost_daily_bps": 0.0,
    }
    inst_cost = {
        "symbol": "TESTUSD",
        "market": "forex",
        "assumed_spread_bps": 10.0,
        "fee_rate": 0.002,
        "overnight_cost_daily_bps": 5.0,
    }
    cfg_zero = dict(cfg)
    cfg_zero["risk"] = dict(cfg["risk"], default_slippage_bps=0.0)

    bars = _make_daily_bars(days=2300)
    res_zero = screen_instrument(inst_zero, bars, cfg_zero)
    res_cost = screen_instrument(inst_cost, bars, cfg)

    assert res_zero["in_sample"]["trades"] > 0
    assert res_cost["in_sample"]["total_return_pct"] < res_zero["in_sample"]["total_return_pct"]


def test_insufficient_history_under_5_years_is_flagged_and_not_forced() -> None:
    cfg = load_config()
    inst = next(i for i in cfg["instruments"] if i["symbol"] == "BTCUSDT")
    bars_short = _make_daily_bars(days=1200)  # ~3.28 years < 5 years

    res = screen_instrument(inst, bars_short, cfg)
    assert res["status"] == "insufficient_history"
    assert res["passed"] is False
    assert res["history_years"] < 5.0
    assert res["in_sample"] is None
    assert res["out_of_sample"] is None
    assert "不足" in res["reason"]


def test_strict_oos_pass_criteria_never_relaxed() -> None:
    cfg = load_config()
    sb_cfg = cfg["strategy_b"]
    # 1) Too few trades (< 30) -> fail even with high PF
    few_trades = [
        BacktestTrade("2025-01-01T00:00:00Z", "2025-01-10T00:00:00Z", "long", 100.0, 110.0, 1.5, 750.0, 10.0)
        for _ in range(15)
    ]
    m_few = compute_metrics(few_trades, pool_capital=80000.0)
    assert m_few["trades"] < sb_cfg["min_oos_trades"]

    # 2) Run screening on choppy market -> fails strict criteria (never relaxed)
    bars_chop = _make_daily_bars(days=2300, seed_mode="chop")
    inst = next(i for i in cfg["instruments"] if i["symbol"] == "EURUSD")
    res_chop = screen_instrument(inst, bars_chop, cfg)
    if res_chop["out_of_sample"]["trades"] < 30 or res_chop["out_of_sample"]["profit_factor"] <= 1.3:
        assert res_chop["passed"] is False


def test_strategy_b_daily_scan_only_evaluates_passed_instruments() -> None:
    cfg = load_config()
    inst = next(i for i in cfg["instruments"] if i["symbol"] == "EURUSD")
    bars = _make_daily_bars(days=300)
    now = datetime(2026, 11, 16, 0, 15, tzinfo=timezone.utc)

    strat_b = StrategyB(cfg)
    ev_fail = strat_b.evaluate_instrument(
        instrument=inst,
        bars_1d=bars,
        backtest_entry={"symbol": "EURUSD", "passed": False, "reason": "OOS 獲利因子不足"},
        report_1d={"status": "ok"},
        now_utc=now,
    )
    assert ev_fail.decision == "watch"
    assert "未通過" in ev_fail.watch_reason

    ev_pass = strat_b.evaluate_instrument(
        instrument=inst,
        bars_1d=bars,
        backtest_entry={
            "symbol": "EURUSD",
            "passed": True,
            "chosen_strategy": "donchian",
            "chosen_params": {"period": 20, "sl_atr_mult": 2.5},
        },
        report_1d={"status": "ok"},
        now_utc=now,
    )
    assert ev_pass.filter_checks["passed_screening"] is True
    assert "regime" in ev_pass.indicators


class _FakeFetcher:
    def __init__(self, bars_1d: list[OHLCVBar], bars_15m: list[OHLCVBar] | None = None) -> None:
        self._bars_1d = bars_1d
        self._bars_15m = bars_15m or []

    def fetch(self, inst: dict, interval: str, limit: int = 200, now_utc: datetime | None = None):
        from src.data.models import FetchResult

        if interval == "1d":
            return FetchResult(
                symbol=inst["symbol"],
                interval="1d",
                source_used="fake_daily",
                bars=self._bars_1d[-limit:],
                ok=True,
                error=None,
            )
        return FetchResult(
            symbol=inst["symbol"],
            interval=interval,
            source_used="fake_15m",
            bars=self._bars_15m[-limit:],
            ok=bool(self._bars_15m),
            error=None if self._bars_15m else "no 15m",
        )


def test_weekly_and_daily_jobs_end_to_end(tmp_path) -> None:
    import json
    from src.jobs.daily import run_daily
    from src.jobs.weekly import run_weekly_screening

    bars = _make_daily_bars(days=2300)
    now = datetime(2026, 4, 20, 0, 15, tzinfo=timezone.utc)
    fake = _FakeFetcher(bars_1d=bars)

    res_weekly = run_weekly_screening(out_dir=tmp_path, fetcher=fake, now_utc=now)
    assert res_weekly["summary"]["total"] >= 18
    assert (tmp_path / "backtest_results.json").exists()
    assert (tmp_path / "backtests" / "latest.json").exists()

    saved_bt = json.loads((tmp_path / "backtest_results.json").read_text(encoding="utf-8"))
    assert len(saved_bt["results"]) == res_weekly["summary"]["total"]

    res_daily = run_daily(out_dir=tmp_path, fetcher=fake, now_utc=now)
    assert (tmp_path / "watch_today_b.json").exists()
    assert (tmp_path / "new_signals_to_commit.json").exists()
    assert "signals_published" in res_daily
    assert isinstance(res_daily["watch_count"], int)
