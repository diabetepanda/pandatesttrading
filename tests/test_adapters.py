"""Unit tests for data adapters and fallback switching."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd

from src.data.adapters import (
    BinanceVisionAdapter,
    BybitAdapter,
    DataAdapter,
    FallbackDataFetcher,
    OkxAdapter,
    StooqAdapter,
    YFinanceAdapter,
)
from src.data.models import OHLCVBar


def test_binance_vision_adapter_parses_closed_bars() -> None:
    now = datetime(2026, 11, 16, 12, 30, tzinfo=timezone.utc)
    t_closed = int(datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    t_open = int(datetime(2026, 11, 16, 12, 20, tzinfo=timezone.utc).timestamp() * 1000)

    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        [t_closed, "90000", "90500", "89900", "90200", "12.5", t_closed + 899999],
        [t_open, "90200", "90300", "90100", "90250", "1.0", t_open + 899999],
    ]
    with patch("src.data.adapters.requests.get", return_value=mock_resp):
        bars = BinanceVisionAdapter().fetch_bars("BTCUSDT", "15m", now_utc=now)

    assert len(bars) == 1
    assert bars[0].close == 90200.0
    assert bars[0].ts_utc == "2026-11-16T12:00:00Z"


def test_okx_adapter_parses_and_sorts_ascending() -> None:
    now = datetime(2026, 11, 16, 12, 30, tzinfo=timezone.utc)
    t1 = int(datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    t2 = int(datetime(2026, 11, 16, 12, 15, tzinfo=timezone.utc).timestamp() * 1000)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "code": "0",
        "data": [
            [str(t2), "3100", "3120", "3090", "3110", "50", "0", "0", "1"],
            [str(t1), "3080", "3105", "3075", "3100", "40", "0", "0", "1"],
        ],
    }
    with patch("src.data.adapters.requests.get", return_value=mock_resp):
        bars = OkxAdapter().fetch_bars("ETH-USDT", "15m", now_utc=now)

    assert len(bars) == 2
    assert bars[0].ts_utc < bars[1].ts_utc
    assert bars[1].close == 3110.0


def test_bybit_adapter_parses_bars() -> None:
    now = datetime(2026, 11, 16, 12, 30, tzinfo=timezone.utc)
    t1 = int(datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "retCode": 0,
        "result": {"list": [[str(t1), "100", "105", "99", "103", "200", "20600"]]},
    }
    with patch("src.data.adapters.requests.get", return_value=mock_resp):
        bars = BybitAdapter().fetch_bars("SOLUSDT", "15m", now_utc=now)

    assert len(bars) == 1
    assert bars[0].close == 103.0


def test_yfinance_adapter_handles_multiindex_columns() -> None:
    now = datetime(2026, 11, 16, 12, 30, tzinfo=timezone.utc)
    idx = pd.to_datetime(["2026-11-16T12:00:00Z"])
    cols = pd.MultiIndex.from_tuples(
        [("Open", "EURUSD=X"), ("High", "EURUSD=X"), ("Low", "EURUSD=X"), ("Close", "EURUSD=X"), ("Volume", "EURUSD=X")]
    )
    df = pd.DataFrame([[1.08, 1.085, 1.079, 1.082, 0]], index=idx, columns=cols)

    with patch("src.data.adapters.yf.download", return_value=df):
        bars = YFinanceAdapter().fetch_bars("EURUSD=X", "15m", now_utc=now)

    assert len(bars) == 1
    assert bars[0].close == 1.082


def test_stooq_adapter_daily_csv() -> None:
    now = datetime(2026, 11, 16, 12, 30, tzinfo=timezone.utc)
    csv_text = "Date,Open,High,Low,Close\n2026-11-13,1.080,1.085,1.078,1.083\n"
    mock_resp = MagicMock()
    mock_resp.text = csv_text
    with patch("src.data.adapters.requests.get", return_value=mock_resp):
        bars = StooqAdapter().fetch_bars("eurusd", "1d", now_utc=now)

    assert len(bars) == 1
    assert bars[0].close == 1.083


def test_fallback_fetcher_switches_when_primary_fails() -> None:
    class FailingAdapter(DataAdapter):
        name = "binance_vision"

        def fetch_bars(self, source_symbol: str, interval: str, limit: int = 200, now_utc=None):
            raise ConnectionError("451 blocked")

    class WorkingAdapter(DataAdapter):
        name = "okx"

        def fetch_bars(self, source_symbol: str, interval: str, limit: int = 200, now_utc=None):
            return [OHLCVBar("2026-11-16T12:00:00Z", 100.0, 101.0, 99.0, 100.5, 10.0)]

    fetcher = FallbackDataFetcher(adapters={"binance_vision": FailingAdapter(), "okx": WorkingAdapter()})
    inst = {"symbol": "BTCUSDT", "sources": {"binance_vision": "BTCUSDT", "okx": "BTC-USDT"}}
    res = fetcher.fetch(inst, "15m")

    assert res.ok is True
    assert res.source_used == "okx"
    assert len(res.attempts) == 2
    assert res.attempts[0].ok is False
    assert res.attempts[1].ok is True


def test_fetch_crypto_derivatives_meta_okx_and_fallback() -> None:
    from src.data.adapters import fetch_crypto_derivatives_meta

    inst = {"symbol": "BTCUSDT", "sources": {"okx": "BTC-USDT", "binance_vision": "BTCUSDT"}}

    fr_okx = MagicMock()
    fr_okx.json.return_value = {"code": "0", "data": [{"fundingRate": "0.0001"}]}
    oi_okx = MagicMock()
    oi_okx.json.return_value = {"code": "0", "data": [["2", "105.0"], ["1", "100.0"]]}

    with patch("src.data.adapters.requests.get", side_effect=[fr_okx, oi_okx]):
        meta = fetch_crypto_derivatives_meta(inst)
    assert meta["status"] == "ok"
    assert meta["source"] == "okx"
    assert meta["funding_rate"] == 0.0001
    assert meta["oi_change"] == 5.0

    # HTTP 200 but incomplete fields -> status == "資料不足"
    empty_ok = MagicMock()
    empty_ok.json.return_value = {"code": "0", "data": []}
    with patch("src.data.adapters.requests.get", side_effect=[empty_ok, empty_ok, ConnectionError("bin")]):
        meta_insuff = fetch_crypto_derivatives_meta(inst)
    assert meta_insuff["status"] == "資料不足"
    assert meta_insuff["funding_rate"] is None

    # When OKX & Binance both fail with network errors -> status == "抓取失敗"
    with patch("src.data.adapters.requests.get", side_effect=ConnectionError("blocked")):
        meta_fail = fetch_crypto_derivatives_meta(inst)
    assert meta_fail["status"] == "抓取失敗"
    assert meta_fail["funding_rate"] is None


def test_okx_and_bybit_paginate_past_single_page_cap() -> None:
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    base_ms = int(datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    day_ms = 86400 * 1000

    # OKX: first page returns 300 rows from /candles, second page returns 50 older rows from /history-candles
    okx_p1 = [[str(base_ms - i * day_ms), "100", "105", "95", "101", "10", "0", "0", "1"] for i in range(300)]
    okx_p2 = [[str(base_ms - (300 + i) * day_ms), "90", "95", "85", "91", "10", "0", "0", "1"] for i in range(50)]
    resp_okx_1 = MagicMock()
    resp_okx_1.json.return_value = {"code": "0", "data": okx_p1}
    resp_okx_2 = MagicMock()
    resp_okx_2.json.return_value = {"code": "0", "data": okx_p2}
    with patch("src.data.adapters.requests.get", side_effect=[resp_okx_1, resp_okx_2]) as mock_get:
        bars_okx = OkxAdapter().fetch_bars("BTC-USDT", "1d", limit=400, now_utc=now)
    assert len(bars_okx) == 350
    assert bars_okx[0].ts_utc < bars_okx[-1].ts_utc
    assert "history-candles" in mock_get.call_args_list[1].args[0]

    # Bybit: first page 1000 rows, second page 100 rows
    by_p1 = [[str(base_ms - i * day_ms), "100", "105", "95", "101", "10", "1000"] for i in range(1000)]
    by_p2 = [[str(base_ms - (1000 + i) * day_ms), "90", "95", "85", "91", "10", "900"] for i in range(100)]
    resp_by_1 = MagicMock()
    resp_by_1.json.return_value = {"retCode": 0, "result": {"list": by_p1}}
    resp_by_2 = MagicMock()
    resp_by_2.json.return_value = {"retCode": 0, "result": {"list": by_p2}}
    with patch("src.data.adapters.requests.get", side_effect=[resp_by_1, resp_by_2]):
        bars_by = BybitAdapter().fetch_bars("BTCUSDT", "1d", limit=1150, now_utc=now)
    assert len(bars_by) == 1100
    assert bars_by[0].ts_utc < bars_by[-1].ts_utc
