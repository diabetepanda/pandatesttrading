"""Market data adapters (no API key required) and fallback fetcher."""

from __future__ import annotations

import csv
import io
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import requests
import yfinance as yf

from src.data.models import AttemptRecord, FetchResult, OHLCVBar

INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60,
    "15m": 900,
    "1d": 86400,
}

USER_AGENT = "SignalDesk/1.0 (+https://github.com/diabetepanda/pandatesttrading)"


def _to_iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms_to_iso_utc(ms: int | float) -> str:
    dt = datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)
    return _to_iso_utc(dt)


def _sec_to_iso_utc(sec: int | float) -> str:
    dt = datetime.fromtimestamp(float(sec), tz=timezone.utc)
    return _to_iso_utc(dt)


def _is_closed_bar(open_ts_sec: float, interval: str, now_ts_sec: float | None = None) -> bool:
    now = now_ts_sec if now_ts_sec is not None else datetime.now(timezone.utc).timestamp()
    dur = INTERVAL_SECONDS[interval]
    return (open_ts_sec + dur) <= now


class DataAdapter(ABC):
    """Base class for free, no-key market data adapters."""

    name: str = "base"

    def __init__(self, timeout_sec: int = 15) -> None:
        self.timeout_sec = timeout_sec

    @abstractmethod
    def fetch_bars(
        self,
        source_symbol: str,
        interval: str,
        limit: int = 200,
        now_utc: datetime | None = None,
    ) -> list[OHLCVBar]:
        """Fetch closed OHLCV bars sorted ascending by timestamp."""


class BinanceVisionAdapter(DataAdapter):
    """Binance public market data endpoint (data-api.binance.vision)."""

    name = "binance_vision"
    BASE_URL = "https://data-api.binance.vision/api/v3/klines"

    def fetch_bars(
        self,
        source_symbol: str,
        interval: str,
        limit: int = 200,
        now_utc: datetime | None = None,
    ) -> list[OHLCVBar]:
        if interval not in INTERVAL_SECONDS:
            raise ValueError(f"Unsupported interval: {interval}")
        resp = requests.get(
            self.BASE_URL,
            params={"symbol": source_symbol, "interval": interval, "limit": min(limit, 1000)},
            headers={"User-Agent": USER_AGENT},
            timeout=self.timeout_sec,
        )
        resp.raise_for_status()
        raw = resp.json()
        if not isinstance(raw, list):
            raise ValueError(f"Unexpected Binance response: {raw}")

        now_sec = (now_utc or datetime.now(timezone.utc)).timestamp()
        bars: list[OHLCVBar] = []
        for row in raw:
            open_ms = int(row[0])
            if not _is_closed_bar(open_ms / 1000.0, interval, now_sec):
                continue
            bars.append(
                OHLCVBar(
                    ts_utc=_ms_to_iso_utc(open_ms),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            )
        bars.sort(key=lambda b: b.ts_utc)
        return bars


class OkxAdapter(DataAdapter):
    """OKX public market candles API."""

    name = "okx"
    BASE_URL = "https://www.okx.com/api/v5/market/candles"
    INTERVAL_MAP = {"1m": "1m", "15m": "15m", "1d": "1Dutc"}

    def fetch_bars(
        self,
        source_symbol: str,
        interval: str,
        limit: int = 200,
        now_utc: datetime | None = None,
    ) -> list[OHLCVBar]:
        bar_param = self.INTERVAL_MAP.get(interval)
        if not bar_param:
            raise ValueError(f"Unsupported interval: {interval}")
        resp = requests.get(
            self.BASE_URL,
            params={"instId": source_symbol, "bar": bar_param, "limit": min(limit, 300)},
            headers={"User-Agent": USER_AGENT},
            timeout=self.timeout_sec,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != "0":
            raise ValueError(f"OKX API error: {payload}")
        raw = payload.get("data", [])

        now_sec = (now_utc or datetime.now(timezone.utc)).timestamp()
        bars: list[OHLCVBar] = []
        for row in raw:
            open_ms = int(row[0])
            confirm = str(row[8]) if len(row) > 8 else "1"
            if confirm != "1" and not _is_closed_bar(open_ms / 1000.0, interval, now_sec):
                continue
            bars.append(
                OHLCVBar(
                    ts_utc=_ms_to_iso_utc(open_ms),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            )
        bars.sort(key=lambda b: b.ts_utc)
        return bars


class BybitAdapter(DataAdapter):
    """Bybit v5 public spot kline API."""

    name = "bybit"
    BASE_URL = "https://api.bybit.com/v5/market/kline"
    INTERVAL_MAP = {"1m": "1", "15m": "15", "1d": "D"}

    def fetch_bars(
        self,
        source_symbol: str,
        interval: str,
        limit: int = 200,
        now_utc: datetime | None = None,
    ) -> list[OHLCVBar]:
        iv = self.INTERVAL_MAP.get(interval)
        if not iv:
            raise ValueError(f"Unsupported interval: {interval}")
        resp = requests.get(
            self.BASE_URL,
            params={"category": "spot", "symbol": source_symbol, "interval": iv, "limit": min(limit, 1000)},
            headers={"User-Agent": USER_AGENT},
            timeout=self.timeout_sec,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("retCode") != 0:
            raise ValueError(f"Bybit API error: {payload}")
        raw = payload.get("result", {}).get("list", [])

        now_sec = (now_utc or datetime.now(timezone.utc)).timestamp()
        bars: list[OHLCVBar] = []
        for row in raw:
            open_ms = int(row[0])
            if not _is_closed_bar(open_ms / 1000.0, interval, now_sec):
                continue
            bars.append(
                OHLCVBar(
                    ts_utc=_ms_to_iso_utc(open_ms),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            )
        bars.sort(key=lambda b: b.ts_utc)
        return bars


class KrakenAdapter(DataAdapter):
    """Kraken public OHLC API."""

    name = "kraken"
    BASE_URL = "https://api.kraken.com/0/public/OHLC"
    INTERVAL_MAP = {"1m": 1, "15m": 15, "1d": 1440}

    def fetch_bars(
        self,
        source_symbol: str,
        interval: str,
        limit: int = 200,
        now_utc: datetime | None = None,
    ) -> list[OHLCVBar]:
        iv = self.INTERVAL_MAP.get(interval)
        if not iv:
            raise ValueError(f"Unsupported interval: {interval}")
        resp = requests.get(
            self.BASE_URL,
            params={"pair": source_symbol, "interval": iv},
            headers={"User-Agent": USER_AGENT},
            timeout=self.timeout_sec,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("error"):
            raise ValueError(f"Kraken API error: {payload['error']}")
        result = payload.get("result", {})
        pair_rows = next((v for k, v in result.items() if k != "last" and isinstance(v, list)), [])

        now_sec = (now_utc or datetime.now(timezone.utc)).timestamp()
        bars: list[OHLCVBar] = []
        for row in pair_rows[-limit:]:
            open_sec = int(row[0])
            if not _is_closed_bar(open_sec, interval, now_sec):
                continue
            bars.append(
                OHLCVBar(
                    ts_utc=_sec_to_iso_utc(open_sec),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[6]),
                )
            )
        bars.sort(key=lambda b: b.ts_utc)
        return bars


class CoinbaseAdapter(DataAdapter):
    """Coinbase Exchange public candles API."""

    name = "coinbase"
    BASE_URL_TEMPLATE = "https://api.exchange.coinbase.com/products/{symbol}/candles"

    def fetch_bars(
        self,
        source_symbol: str,
        interval: str,
        limit: int = 200,
        now_utc: datetime | None = None,
    ) -> list[OHLCVBar]:
        granularity = INTERVAL_SECONDS.get(interval)
        if not granularity:
            raise ValueError(f"Unsupported interval: {interval}")
        url = self.BASE_URL_TEMPLATE.format(symbol=source_symbol)
        resp = requests.get(
            url,
            params={"granularity": granularity},
            headers={"User-Agent": USER_AGENT},
            timeout=self.timeout_sec,
        )
        resp.raise_for_status()
        raw = resp.json()
        if not isinstance(raw, list):
            raise ValueError(f"Unexpected Coinbase response: {raw}")

        now_sec = (now_utc or datetime.now(timezone.utc)).timestamp()
        bars: list[OHLCVBar] = []
        for row in raw[:limit]:
            # Coinbase format: [time, low, high, open, close, volume]
            open_sec = int(row[0])
            if not _is_closed_bar(open_sec, interval, now_sec):
                continue
            bars.append(
                OHLCVBar(
                    ts_utc=_sec_to_iso_utc(open_sec),
                    open=float(row[3]),
                    high=float(row[2]),
                    low=float(row[1]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            )
        bars.sort(key=lambda b: b.ts_utc)
        return bars


class YFinanceAdapter(DataAdapter):
    """Yahoo Finance adapter via yfinance (no API key)."""

    name = "yfinance"
    PERIOD_MAP = {"1m": "5d", "15m": "1mo", "1d": "1y"}

    def fetch_bars(
        self,
        source_symbol: str,
        interval: str,
        limit: int = 200,
        now_utc: datetime | None = None,
    ) -> list[OHLCVBar]:
        period = self.PERIOD_MAP.get(interval)
        if not period:
            raise ValueError(f"Unsupported interval: {interval}")

        df = yf.download(
            tickers=source_symbol,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=False,
            threads=False,
        )
        if df is None or df.empty:
            return []

        if isinstance(df.columns, pd.MultiIndex):
            df = df.copy()
            df.columns = df.columns.get_level_values(0)

        idx = pd.to_datetime(df.index)
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        else:
            idx = idx.tz_convert("UTC")
        df = df.copy()
        df.index = idx

        now_sec = (now_utc or datetime.now(timezone.utc)).timestamp()
        bars: list[OHLCVBar] = []
        for ts, row in df.tail(limit + 5).iterrows():
            open_sec = ts.timestamp()
            if not _is_closed_bar(open_sec, interval, now_sec):
                continue
            o, h, l, c = row.get("Open"), row.get("High"), row.get("Low"), row.get("Close")
            if pd.isna(o) or pd.isna(h) or pd.isna(l) or pd.isna(c):
                continue
            vol = row.get("Volume", 0.0)
            vol_val = 0.0 if pd.isna(vol) else float(vol)
            bars.append(
                OHLCVBar(
                    ts_utc=_to_iso_utc(ts.to_pydatetime()),
                    open=float(o),
                    high=float(h),
                    low=float(l),
                    close=float(c),
                    volume=vol_val,
                )
            )
        bars.sort(key=lambda b: b.ts_utc)
        return bars[-limit:]


class StooqAdapter(DataAdapter):
    """Stooq free CSV adapter (daily interval only)."""

    name = "stooq"
    BASE_URL = "https://stooq.com/q/d/l/"

    def fetch_bars(
        self,
        source_symbol: str,
        interval: str,
        limit: int = 200,
        now_utc: datetime | None = None,
    ) -> list[OHLCVBar]:
        if interval != "1d":
            raise ValueError("Stooq free CSV only supports 1d interval")

        resp = requests.get(
            self.BASE_URL,
            params={"s": source_symbol, "i": "d"},
            headers={"User-Agent": USER_AGENT},
            timeout=self.timeout_sec,
        )
        resp.raise_for_status()
        text = resp.text.strip()
        if not text or "No data" in text:
            return []

        reader = csv.DictReader(io.StringIO(text))
        now_sec = (now_utc or datetime.now(timezone.utc)).timestamp()
        bars: list[OHLCVBar] = []
        for row in reader:
            date_str = row.get("Date")
            if not date_str:
                continue
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if not _is_closed_bar(dt.timestamp(), "1d", now_sec):
                continue
            bars.append(
                OHLCVBar(
                    ts_utc=_to_iso_utc(dt),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row.get("Volume") or 0.0),
                )
            )
        bars.sort(key=lambda b: b.ts_utc)
        return bars[-limit:]


ADAPTER_REGISTRY: dict[str, type[DataAdapter]] = {
    "binance_vision": BinanceVisionAdapter,
    "okx": OkxAdapter,
    "bybit": BybitAdapter,
    "kraken": KrakenAdapter,
    "coinbase": CoinbaseAdapter,
    "yfinance": YFinanceAdapter,
    "stooq": StooqAdapter,
}


class FallbackDataFetcher:
    """Fetches bars by trying configured adapters in order until one succeeds."""

    def __init__(
        self,
        timeout_sec: int = 15,
        adapters: dict[str, DataAdapter] | None = None,
    ) -> None:
        self.timeout_sec = timeout_sec
        self.adapters: dict[str, DataAdapter] = adapters or {
            name: cls(timeout_sec=timeout_sec) for name, cls in ADAPTER_REGISTRY.items()
        }

    def fetch(
        self,
        instrument: dict[str, Any],
        interval: str,
        limit: int = 200,
        now_utc: datetime | None = None,
    ) -> FetchResult:
        """Fetch bars using the first working adapter in instrument['sources']."""
        symbol = instrument["symbol"]
        sources: dict[str, str] = instrument.get("sources", {})
        attempts: list[AttemptRecord] = []

        for adapter_name, source_code in sources.items():
            adapter = self.adapters.get(adapter_name)
            if adapter is None:
                continue
            t0 = time.monotonic()
            try:
                bars = adapter.fetch_bars(source_code, interval, limit=limit, now_utc=now_utc)
                latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
                if bars:
                    attempts.append(
                        AttemptRecord(
                            adapter=adapter_name,
                            ok=True,
                            bar_count=len(bars),
                            latency_ms=latency_ms,
                            latest_ts_utc=bars[-1].ts_utc,
                            latest_close=bars[-1].close,
                        )
                    )
                    return FetchResult(
                        symbol=symbol,
                        interval=interval,
                        ok=True,
                        source_used=adapter_name,
                        bars=bars,
                        attempts=attempts,
                    )
                attempts.append(
                    AttemptRecord(
                        adapter=adapter_name,
                        ok=False,
                        bar_count=0,
                        latency_ms=latency_ms,
                        error="Empty bars returned",
                    )
                )
            except Exception as exc:
                latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
                attempts.append(
                    AttemptRecord(
                        adapter=adapter_name,
                        ok=False,
                        bar_count=0,
                        latency_ms=latency_ms,
                        error=str(exc)[:300],
                    )
                )

        err_summary = "; ".join(f"{a.adapter}: {a.error}" for a in attempts) or "No adapters configured"
        return FetchResult(
            symbol=symbol,
            interval=interval,
            ok=False,
            source_used=None,
            bars=[],
            error=err_summary,
            attempts=attempts,
        )

    def probe_all(
        self,
        instrument: dict[str, Any],
        interval: str,
        limit: int = 60,
        now_utc: datetime | None = None,
    ) -> list[AttemptRecord]:
        """Probe every configured adapter for an instrument and interval."""
        sources: dict[str, str] = instrument.get("sources", {})
        attempts: list[AttemptRecord] = []
        for adapter_name, source_code in sources.items():
            adapter = self.adapters.get(adapter_name)
            if adapter is None:
                continue
            t0 = time.monotonic()
            try:
                bars = adapter.fetch_bars(source_code, interval, limit=limit, now_utc=now_utc)
                latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
                attempts.append(
                    AttemptRecord(
                        adapter=adapter_name,
                        ok=len(bars) > 0,
                        bar_count=len(bars),
                        latency_ms=latency_ms,
                        error="" if bars else "Empty bars returned",
                        latest_ts_utc=bars[-1].ts_utc if bars else None,
                        latest_close=bars[-1].close if bars else None,
                    )
                )
            except Exception as exc:
                latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
                attempts.append(
                    AttemptRecord(
                        adapter=adapter_name,
                        ok=False,
                        bar_count=0,
                        latency_ms=latency_ms,
                        error=str(exc)[:300],
                    )
                )
        return attempts
