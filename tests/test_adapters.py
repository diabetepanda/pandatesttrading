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
