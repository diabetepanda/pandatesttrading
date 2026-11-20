"""Market data adapters (no API key required) and fallback fetcher."""

from __future__ import annotations

import csv
import io
import json
import lzma
import pickle
import struct
import threading
import time
import zipfile
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
import yfinance as yf

from src.data.models import AttemptRecord, FetchResult, OHLCVBar

INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
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
        now_sec = (now_utc or datetime.now(timezone.utc)).timestamp()
        bars_by_ts: dict[str, OHLCVBar] = {}
        end_time_ms: int | None = None
        remaining = max(1, limit)
        max_pages = max(1, min(10, (remaining + 999) // 1000))

        for _ in range(max_pages):
            batch_limit = min(remaining, 1000)
            params: dict[str, Any] = {"symbol": source_symbol, "interval": interval, "limit": batch_limit}
            if end_time_ms is not None:
                params["endTime"] = end_time_ms
            resp = requests.get(
                self.BASE_URL,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=self.timeout_sec,
            )
            resp.raise_for_status()
            raw = resp.json()
            if not isinstance(raw, list):
                raise ValueError(f"Unexpected Binance response: {raw}")
            if not raw:
                break

            earliest_ms: int | None = None
            for row in raw:
                open_ms = int(row[0])
                if earliest_ms is None or open_ms < earliest_ms:
                    earliest_ms = open_ms
                if not _is_closed_bar(open_ms / 1000.0, interval, now_sec):
                    continue
                ts_iso = _ms_to_iso_utc(open_ms)
                bars_by_ts[ts_iso] = OHLCVBar(
                    ts_utc=ts_iso,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )

            if len(raw) < batch_limit or len(bars_by_ts) >= limit or earliest_ms is None:
                break
            end_time_ms = earliest_ms - 1
            remaining = max(1, limit - len(bars_by_ts))

        bars = sorted(bars_by_ts.values(), key=lambda b: b.ts_utc)
        return bars[-limit:]


class OkxAdapter(DataAdapter):
    """OKX public market candles API (with history-candles pagination for > 300 bars)."""

    name = "okx"
    BASE_URL = "https://www.okx.com/api/v5/market/candles"
    HISTORY_URL = "https://www.okx.com/api/v5/market/history-candles"
    INTERVAL_MAP = {"1m": "1m", "15m": "15m", "1h": "1H", "1d": "1Dutc"}

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
        now_sec = (now_utc or datetime.now(timezone.utc)).timestamp()
        bars_by_ts: dict[str, OHLCVBar] = {}

        def _consume(raw_rows: list[Any]) -> int | None:
            min_ms: int | None = None
            for row in raw_rows:
                open_ms = int(row[0])
                if min_ms is None or open_ms < min_ms:
                    min_ms = open_ms
                confirm = str(row[8]) if len(row) > 8 else "1"
                if confirm != "1" and not _is_closed_bar(open_ms / 1000.0, interval, now_sec):
                    continue
                ts_iso = _ms_to_iso_utc(open_ms)
                bars_by_ts[ts_iso] = OHLCVBar(
                    ts_utc=ts_iso,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            return min_ms

        first_batch = min(limit, 300)
        resp = requests.get(
            self.BASE_URL,
            params={"instId": source_symbol, "bar": bar_param, "limit": first_batch},
            headers={"User-Agent": USER_AGENT},
            timeout=self.timeout_sec,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != "0":
            raise ValueError(f"OKX API error: {payload}")
        raw = payload.get("data", [])
        earliest_ms = _consume(raw)

        # Page backwards with history-candles (max 100/page) when caller requests > 300 bars
        if limit > 300 and len(raw) >= 200 and earliest_ms is not None and len(bars_by_ts) < limit:
            max_hist_pages = min(30, ((limit - len(bars_by_ts)) + 99) // 100 + 2)
            for _ in range(max_hist_pages):
                if len(bars_by_ts) >= limit or earliest_ms is None:
                    break
                h_resp = requests.get(
                    self.HISTORY_URL,
                    params={"instId": source_symbol, "bar": bar_param, "limit": 100, "after": str(earliest_ms)},
                    headers={"User-Agent": USER_AGENT},
                    timeout=self.timeout_sec,
                )
                h_resp.raise_for_status()
                h_payload = h_resp.json()
                if h_payload.get("code") != "0":
                    break
                h_raw = h_payload.get("data", [])
                if not h_raw:
                    break
                prev_ms = earliest_ms
                new_min_ms = _consume(h_raw)
                if len(h_raw) < 100 or new_min_ms is None or new_min_ms >= prev_ms:
                    break
                earliest_ms = new_min_ms

        bars = sorted(bars_by_ts.values(), key=lambda b: b.ts_utc)
        return bars[-limit:]


class BybitAdapter(DataAdapter):
    """Bybit v5 public spot kline API (with `end` pagination for > 1000 bars)."""

    name = "bybit"
    BASE_URL = "https://api.bybit.com/v5/market/kline"
    INTERVAL_MAP = {"1m": "1", "15m": "15", "1h": "60", "1d": "D"}

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
        now_sec = (now_utc or datetime.now(timezone.utc)).timestamp()
        bars_by_ts: dict[str, OHLCVBar] = {}
        end_ms: int | None = None
        remaining = max(1, limit)
        max_pages = 1 if limit <= 1000 else min(6, (limit + 999) // 1000)

        for _ in range(max_pages):
            batch_limit = min(remaining, 1000)
            params: dict[str, Any] = {
                "category": "spot",
                "symbol": source_symbol,
                "interval": iv,
                "limit": batch_limit,
            }
            if end_ms is not None:
                params["end"] = end_ms
            resp = requests.get(
                self.BASE_URL,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=self.timeout_sec,
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("retCode") != 0:
                raise ValueError(f"Bybit API error: {payload}")
            raw = payload.get("result", {}).get("list", [])
            if not raw:
                break

            earliest_ms: int | None = None
            for row in raw:
                open_ms = int(row[0])
                if earliest_ms is None or open_ms < earliest_ms:
                    earliest_ms = open_ms
                if not _is_closed_bar(open_ms / 1000.0, interval, now_sec):
                    continue
                ts_iso = _ms_to_iso_utc(open_ms)
                bars_by_ts[ts_iso] = OHLCVBar(
                    ts_utc=ts_iso,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )

            if len(raw) < batch_limit or len(bars_by_ts) >= limit or earliest_ms is None:
                break
            end_ms = earliest_ms - 1
            remaining = max(1, limit - len(bars_by_ts))

        bars = sorted(bars_by_ts.values(), key=lambda b: b.ts_utc)
        return bars[-limit:]


class KrakenAdapter(DataAdapter):
    """Kraken public OHLC API."""

    name = "kraken"
    BASE_URL = "https://api.kraken.com/0/public/OHLC"
    INTERVAL_MAP = {"1m": 1, "15m": 15, "1h": 60, "1d": 1440}

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
    PERIOD_MAP = {"1m": "5d", "15m": "1mo", "1h": "2y", "1d": "10y"}
    _last_request_mono: float = 0.0
    _yf_lock = threading.Lock()

    def __init__(self, timeout_sec: int = 15, min_interval_sec: float = 0.15) -> None:
        super().__init__(timeout_sec=timeout_sec)
        self.min_interval_sec = max(0.0, float(min_interval_sec))

    @classmethod
    def _choose_period(cls, interval: str, limit: int) -> str:
        if interval == "1m":
            return "1d" if limit <= 360 else "5d"
        if interval == "15m":
            return "5d" if limit <= 450 else "1mo"
        if interval == "1h":
            return "1mo" if limit <= 500 else "2y"
        if interval == "1d":
            if limit <= 65:
                return "3mo"
            if limit <= 125:
                return "6mo"
            if limit <= 500:
                return "2y"
            return "10y"
        return cls.PERIOD_MAP[interval]

    def fetch_bars(
        self,
        source_symbol: str,
        interval: str,
        limit: int = 200,
        now_utc: datetime | None = None,
    ) -> list[OHLCVBar]:
        if interval not in self.PERIOD_MAP:
            raise ValueError(f"Unsupported interval: {interval}")
        period = self._choose_period(interval, limit)

        with YFinanceAdapter._yf_lock:
            if self.min_interval_sec > 0:
                elapsed = time.monotonic() - YFinanceAdapter._last_request_mono
                if elapsed < self.min_interval_sec:
                    time.sleep(self.min_interval_sec - elapsed)
            YFinanceAdapter._last_request_mono = time.monotonic()

            df = yf.download(
                tickers=source_symbol,
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=False,
                threads=False,
                timeout=self.timeout_sec,
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


def fetch_crypto_derivatives_meta(
    instrument: dict[str, Any],
    timeout_sec: int = 10,
) -> dict[str, Any]:
    """Fetch crypto funding rate and 1h open-interest change % (OKX primary, Binance Futures fallback).

    Returns a structured dict with keys: source, funding_rate, oi_change, oi_change_1h_pct, status.
    status is one of: "ok", "資料不足", "抓取失敗".
    """
    sources = instrument.get("sources", {})
    had_http_ok = False
    last_source: str | None = None

    okx_spot = sources.get("okx")
    if okx_spot:
        okx_swap = f"{okx_spot}-SWAP"
        try:
            fr_resp = requests.get(
                "https://www.okx.com/api/v5/public/funding-rate",
                params={"instId": okx_swap},
                headers={"User-Agent": USER_AGENT},
                timeout=timeout_sec,
            )
            fr_resp.raise_for_status()
            fr_data = fr_resp.json().get("data", [])

            oi_resp = requests.get(
                "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history",
                params={"instId": okx_swap, "period": "1H", "limit": 2},
                headers={"User-Agent": USER_AGENT},
                timeout=timeout_sec,
            )
            oi_resp.raise_for_status()
            oi_data = oi_resp.json().get("data", [])
            had_http_ok = True
            last_source = "okx"

            if fr_data and len(oi_data) >= 2:
                funding_rate = float(fr_data[0]["fundingRate"])
                oi_now = float(oi_data[0][1])
                oi_prev = float(oi_data[1][1])
                if oi_prev > 0:
                    oi_chg_pct = round(((oi_now - oi_prev) / oi_prev) * 100.0, 4)
                    return {
                        "source": "okx",
                        "funding_rate": funding_rate,
                        "oi_change": oi_chg_pct,
                        "oi_change_1h_pct": oi_chg_pct,
                        "status": "ok",
                    }
        except Exception:
            pass

    bin_sym = sources.get("binance_vision") or instrument.get("symbol")
    if bin_sym:
        try:
            fr_resp = requests.get(
                "https://fapi.binance.com/fapi/v1/premiumIndex",
                params={"symbol": bin_sym},
                headers={"User-Agent": USER_AGENT},
                timeout=timeout_sec,
            )
            fr_resp.raise_for_status()
            fr_json = fr_resp.json()

            oi_resp = requests.get(
                "https://fapi.binance.com/futures/data/openInterestHist",
                params={"symbol": bin_sym, "period": "1h", "limit": 2},
                headers={"User-Agent": USER_AGENT},
                timeout=timeout_sec,
            )
            oi_resp.raise_for_status()
            oi_list = oi_resp.json()
            had_http_ok = True
            last_source = "binance_futures"

            if "lastFundingRate" in fr_json and isinstance(oi_list, list) and len(oi_list) >= 2:
                funding_rate = float(fr_json["lastFundingRate"])
                oi_prev = float(oi_list[0]["sumOpenInterest"])
                oi_now = float(oi_list[1]["sumOpenInterest"])
                if oi_prev > 0:
                    oi_chg_pct = round(((oi_now - oi_prev) / oi_prev) * 100.0, 4)
                    return {
                        "source": "binance_futures",
                        "funding_rate": funding_rate,
                        "oi_change": oi_chg_pct,
                        "oi_change_1h_pct": oi_chg_pct,
                        "status": "ok",
                    }
        except Exception:
            pass

    return {
        "source": last_source,
        "funding_rate": None,
        "oi_change": None,
        "oi_change_1h_pct": None,
        "status": "資料不足" if had_http_ok else "抓取失敗",
    }


def normalize_derivatives_info(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize derivatives metadata for watch_today.json logging."""
    if raw is None:
        return {
            "source": None,
            "funding_rate": None,
            "oi_change": None,
            "status": "資料不足",
        }
    fr = raw.get("funding_rate")
    oi_chg = raw.get("oi_change", raw.get("oi_change_1h_pct"))
    src = raw.get("source")
    if isinstance(src, (int, float)):
        src = "okx" if float(src) == 1.0 else ("binance_futures" if float(src) == 2.0 else str(src))
    status = raw.get("status")
    if not status:
        status = "ok" if (fr is not None and oi_chg is not None) else "資料不足"
    return {
        "source": src,
        "funding_rate": fr,
        "oi_change": oi_chg,
        "status": status,
    }


def parse_binance_kline_zip(
    zip_bytes: bytes,
    interval: str,
    now_ts_sec: float | None = None,
) -> list[OHLCVBar]:
    """Parse one Binance Spot monthly kline ZIP archive supporting ms (13-digit) & us (16-digit) timestamps."""
    now_sec = now_ts_sec if now_ts_sec is not None else datetime.now(timezone.utc).timestamp()
    out: list[OHLCVBar] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        csv_names = [n for n in zf.namelist() if n.endswith(".csv")] or zf.namelist()
        if not csv_names:
            return out
        with zf.open(csv_names[0]) as fp:
            reader = csv.reader(io.TextIOWrapper(fp, encoding="utf-8", errors="replace"))
            for row in reader:
                if not row or len(row) < 6:
                    continue
                first = row[0].strip()
                if not first.isdigit():
                    continue
                open_ms = int(first)
                # Binance spot CSV archives switched from ms (13 digits) to us (16 digits) in 2025-01
                if open_ms >= 10**14:
                    open_ms //= 1000
                if not _is_closed_bar(open_ms / 1000.0, interval, now_sec):
                    continue
                out.append(
                    OHLCVBar(
                        ts_utc=_ms_to_iso_utc(open_ms),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                    )
                )
    return out


class BinanceMonthlyArchiveFetcher:
    """Downloads and caches >=5y historical OHLCV bars from data.binance.vision monthly ZIPs."""

    ARCHIVE_URL = "https://data.binance.vision/data/spot/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{ym}.zip"
    KLINES_API_URL = "https://data-api.binance.vision/api/v3/klines"

    def __init__(
        self,
        cache_dir: Path | str = ".cache/binance_monthly",
        timeout_sec: int = 20,
        max_workers: int = 20,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.timeout_sec = timeout_sec
        self.max_workers = max(1, max_workers)
        self.bounds_path = self.cache_dir / "symbol_bounds.json"
        self._lock = threading.Lock()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)
        self._api_adapter = BinanceVisionAdapter(timeout_sec=timeout_sec)

    @staticmethod
    def _month_range(start_ym: tuple[int, int], end_ym: tuple[int, int]) -> list[str]:
        y, m = start_ym
        ey, em = end_ym
        out: list[str] = []
        while (y, m) <= (ey, em):
            out.append(f"{y:04d}-{m:02d}")
            m += 1
            if m > 12:
                m = 1
                y += 1
        return out

    @staticmethod
    def _subtract_months(year: int, month: int, months: int) -> tuple[int, int]:
        total = year * 12 + (month - 1) - months
        return total // 12, (total % 12) + 1

    def _get_symbol_bounds(self, source_symbol: str, today_key: str) -> tuple[int | None, int | None]:
        with self._lock:
            bounds: dict[str, Any] = {}
            if self.bounds_path.exists():
                try:
                    bounds = json.loads(self.bounds_path.read_text(encoding="utf-8"))
                except Exception:
                    bounds = {}
            entry = dict(bounds.get(source_symbol, {}))
            first_ms = entry.get("first_open_ms") if isinstance(entry.get("first_open_ms"), int) else None
            latest_ms = (
                entry.get("latest_open_ms")
                if entry.get("checked_date") == today_key and isinstance(entry.get("latest_open_ms"), int)
                else None
            )
            if first_ms and latest_ms:
                return first_ms, latest_ms

        if not first_ms:
            try:
                resp = self._session.get(
                    self.KLINES_API_URL,
                    params={"symbol": source_symbol, "interval": "1d", "startTime": 0, "limit": 1},
                    headers={"User-Agent": USER_AGENT},
                    timeout=self.timeout_sec,
                )
                resp.raise_for_status()
                raw = resp.json()
                if isinstance(raw, list) and raw:
                    val = int(raw[0][0])
                    first_ms = val // 1000 if val >= 10**14 else val
            except Exception:
                pass

        if not latest_ms:
            try:
                resp = self._session.get(
                    self.KLINES_API_URL,
                    params={"symbol": source_symbol, "interval": "1d", "limit": 1},
                    headers={"User-Agent": USER_AGENT},
                    timeout=self.timeout_sec,
                )
                resp.raise_for_status()
                raw = resp.json()
                if isinstance(raw, list) and raw:
                    val = int(raw[0][0])
                    latest_ms = val // 1000 if val >= 10**14 else val
            except Exception:
                pass

        if first_ms or latest_ms:
            with self._lock:
                bounds = {}
                if self.bounds_path.exists():
                    try:
                        bounds = json.loads(self.bounds_path.read_text(encoding="utf-8"))
                    except Exception:
                        bounds = {}
                cur = dict(bounds.get(source_symbol, {}))
                if first_ms:
                    cur["first_open_ms"] = first_ms
                if latest_ms:
                    cur["latest_open_ms"] = latest_ms
                    cur["checked_date"] = today_key
                bounds[source_symbol] = cur
                self.bounds_path.write_text(json.dumps(bounds, indent=2) + "\n", encoding="utf-8")
        return first_ms, latest_ms

    def _fetch_month_zip(self, source_symbol: str, interval: str, ym: str) -> bytes | None:
        zip_dir = self.cache_dir / source_symbol / interval
        zip_dir.mkdir(parents=True, exist_ok=True)
        zip_path = zip_dir / f"{source_symbol}-{interval}-{ym}.zip"
        if zip_path.exists() and zip_path.stat().st_size > 0:
            return zip_path.read_bytes()

        url = self.ARCHIVE_URL.format(symbol=source_symbol, interval=interval, ym=ym)
        resp = self._session.get(url, headers={"User-Agent": USER_AGENT}, timeout=self.timeout_sec)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        zip_bytes = resp.content
        # Validate archive before caching to disk
        with zipfile.ZipFile(io.BytesIO(zip_bytes)):
            pass
        zip_path.write_bytes(zip_bytes)
        return zip_bytes

    def fetch_history(
        self,
        source_symbol: str,
        interval: str,
        years: float = 5.1,
        now_utc: datetime | None = None,
        include_current_month: bool = True,
    ) -> list[OHLCVBar]:
        """Return closed OHLCV bars covering `years` (or full listing history if newer), cached on disk."""
        if interval not in INTERVAL_SECONDS:
            raise ValueError(f"Unsupported interval: {interval}")
        now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
        now_sec = now.timestamp()
        today_key = now.strftime("%Y-%m-%d")

        first_ms, latest_ms = self._get_symbol_bounds(source_symbol, today_key)
        effective_anchor = now
        if latest_ms is not None:
            latest_dt = datetime.fromtimestamp(latest_ms / 1000.0, tz=timezone.utc) + timedelta(days=1)
            if latest_dt < effective_anchor:
                effective_anchor = latest_dt

        last_complete_dt = effective_anchor.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        end_ym = (last_complete_dt.year, last_complete_dt.month)
        months_back = max(1, int(round(years * 12)) + 1)
        target_start_ym = self._subtract_months(end_ym[0], end_ym[1], months_back)

        if first_ms is not None:
            first_dt = datetime.fromtimestamp(first_ms / 1000.0, tz=timezone.utc)
            first_ym = (first_dt.year, first_dt.month)
            if first_ym > target_start_ym:
                target_start_ym = first_ym

        month_strs = self._month_range(target_start_ym, end_ym)
        merged_dir = self.cache_dir / "merged"
        merged_dir.mkdir(parents=True, exist_ok=True)
        merged_key = f"{source_symbol}_{interval}_{month_strs[0]}_{month_strs[-1]}" if month_strs else f"{source_symbol}_{interval}_empty"
        merged_path = merged_dir / f"{merged_key}.pkl"

        archive_bars: list[OHLCVBar] | None = None
        if merged_path.exists():
            try:
                loaded = pickle.loads(merged_path.read_bytes())
                if isinstance(loaded, list):
                    archive_bars = loaded
            except Exception:
                archive_bars = None

        if archive_bars is None:
            bars_by_ts: dict[str, OHLCVBar] = {}

            def _load_one(ym: str) -> list[OHLCVBar]:
                zbytes = self._fetch_month_zip(source_symbol, interval, ym)
                if not zbytes:
                    return []
                return parse_binance_kline_zip(zbytes, interval, now_ts_sec=now_sec)

            if self.max_workers > 1 and len(month_strs) > 1:
                with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                    futures = {pool.submit(_load_one, ym): ym for ym in month_strs}
                    for fut in as_completed(futures):
                        try:
                            for b in fut.result():
                                bars_by_ts[b.ts_utc] = b
                        except Exception:
                            continue
            else:
                for ym in month_strs:
                    try:
                        for b in _load_one(ym):
                            bars_by_ts[b.ts_utc] = b
                    except Exception:
                        continue

            archive_bars = sorted(bars_by_ts.values(), key=lambda b: b.ts_utc)
            if archive_bars:
                try:
                    merged_path.write_bytes(pickle.dumps(archive_bars))
                except Exception:
                    pass

        merged_map: dict[str, OHLCVBar] = {b.ts_utc: b for b in archive_bars if b.ts_utc}

        # Supplement current incomplete month from live Binance Spot klines API (optional graceful fallback)
        if include_current_month:
            tail_limit = 3200 if interval == "15m" else (800 if interval == "1h" else 60)
            tail_day = now.strftime("%Y-%m-%d")
            tail_path = merged_dir / f"tail_{source_symbol}_{interval}_{tail_day}.pkl"
            tail_bars: list[OHLCVBar] | None = None
            if tail_path.exists():
                try:
                    tail_bars = pickle.loads(tail_path.read_bytes())
                except Exception:
                    tail_bars = None
            if tail_bars is None:
                try:
                    tail_bars = self._api_adapter.fetch_bars(source_symbol, interval, limit=tail_limit, now_utc=now)
                    if tail_bars:
                        tail_path.write_bytes(pickle.dumps(tail_bars))
                except Exception:
                    tail_bars = []
            for b in tail_bars or []:
                merged_map[b.ts_utc] = b

        return sorted(merged_map.values(), key=lambda b: b.ts_utc)


DUKASCOPY_SYMBOL_MAP: dict[str, tuple[str, float]] = {
    "XAUUSD": ("XAUUSD", 1000.0),
    "XAGUSD": ("XAGUSD", 1000.0),
    "COPPER": ("COPPERCMDUSD", 1000.0),
    "WTI": ("LIGHTCMDUSD", 1000.0),
    "NATGAS": ("GASCMDUSD", 1000.0),
    "EURUSD": ("EURUSD", 100000.0),
    "GBPUSD": ("GBPUSD", 100000.0),
    "USDJPY": ("USDJPY", 1000.0),
    "AUDUSD": ("AUDUSD", 100000.0),
    "USDCAD": ("USDCAD", 100000.0),
    "USDCHF": ("USDCHF", 100000.0),
    "NZDUSD": ("NZDUSD", 100000.0),
    "EURJPY": ("EURJPY", 1000.0),
    "EURGBP": ("EURGBP", 100000.0),
    "GBPJPY": ("GBPJPY", 1000.0),
}


def parse_dukascopy_bi5(
    bi5_bytes: bytes,
    base_dt_utc: datetime,
    divisor: float,
    interval: str = "1h",
    now_ts_sec: float | None = None,
) -> list[OHLCVBar]:
    """Decompress Dukascopy LZMA .bi5 candle buffer into closed OHLCVBar entries."""
    if not bi5_bytes:
        return []
    raw = lzma.decompress(bi5_bytes)
    rec_size = 24
    now_sec = now_ts_sec if now_ts_sec is not None else datetime.now(timezone.utc).timestamp()
    base_sec = base_dt_utc.astimezone(timezone.utc).timestamp()
    out: list[OHLCVBar] = []
    for offset in range(0, len(raw) - (len(raw) % rec_size), rec_size):
        dt_off, o_int, c_int, l_int, h_int, vol = struct.unpack(">5If", raw[offset : offset + rec_size])
        open_sec = base_sec + float(dt_off)
        if not _is_closed_bar(open_sec, interval, now_sec):
            continue
        out.append(
            OHLCVBar(
                ts_utc=_sec_to_iso_utc(open_sec),
                open=round(o_int / divisor, 6),
                high=round(h_int / divisor, 6),
                low=round(l_int / divisor, 6),
                close=round(c_int / divisor, 6),
                volume=float(vol),
            )
        )
    return out


class DukascopyArchiveFetcher:
    """Fetches and caches >=5y FX/commodity 15m & 1h bars from Dukascopy free public datafeed."""

    HOUR_URL = "https://datafeed.dukascopy.com/datafeed/{pair}/{year:04d}/{month0:02d}/BID_candles_hour_1.bi5"
    DAY_MIN_URL = "https://datafeed.dukascopy.com/datafeed/{pair}/{year:04d}/{month0:02d}/{day:02d}/BID_candles_min_1.bi5"

    def __init__(
        self,
        cache_dir: Path | str = ".cache/dukascopy",
        timeout_sec: int = 5,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.timeout_sec = timeout_sec
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._session = requests.Session()
        self._unreachable = False

    def _fetch_hour_bi5(self, pair: str, year: int, month1: int) -> bytes | None:
        month0 = month1 - 1
        cpath = self.cache_dir / pair / f"{year:04d}" / f"{month0:02d}_BID_candles_hour_1.bi5"
        if cpath.exists() and cpath.stat().st_size > 0:
            return cpath.read_bytes()
        if self._unreachable:
            return None
        url = self.HOUR_URL.format(pair=pair, year=year, month0=month0)
        try:
            resp = self._session.get(url, headers={"User-Agent": USER_AGENT}, timeout=self.timeout_sec)
            if resp.status_code in (403, 429) or resp.status_code >= 500:
                self._unreachable = True
                return None
            if resp.status_code == 404 or not resp.content:
                return None
            lzma.decompress(resp.content)
            cpath.parent.mkdir(parents=True, exist_ok=True)
            cpath.write_bytes(resp.content)
            return resp.content
        except Exception:
            self._unreachable = True
            return None

    def _fetch_day_min_bi5(self, pair: str, year: int, month1: int, day: int) -> bytes | None:
        month0 = month1 - 1
        cpath = self.cache_dir / pair / f"{year:04d}" / f"{month0:02d}" / f"{day:02d}_BID_candles_min_1.bi5"
        if cpath.exists() and cpath.stat().st_size > 0:
            return cpath.read_bytes()
        if self._unreachable:
            return None
        url = self.DAY_MIN_URL.format(pair=pair, year=year, month0=month0, day=day)
        try:
            resp = self._session.get(url, headers={"User-Agent": USER_AGENT}, timeout=self.timeout_sec)
            if resp.status_code in (403, 429) or resp.status_code >= 500:
                self._unreachable = True
                return None
            if resp.status_code == 404 or not resp.content:
                return None
            lzma.decompress(resp.content)
            cpath.parent.mkdir(parents=True, exist_ok=True)
            cpath.write_bytes(resp.content)
            return resp.content
        except Exception:
            self._unreachable = True
            return None

    def fetch_history(
        self,
        symbol: str,
        interval: str,
        years: float = 5.1,
        now_utc: datetime | None = None,
    ) -> list[OHLCVBar]:
        """Return closed 1h or 15m (resampled from 1m) bars from Dukascopy, cached on disk."""
        mapping = DUKASCOPY_SYMBOL_MAP.get(symbol)
        if not mapping or interval not in ("15m", "1h"):
            return []
        pair, divisor = mapping
        now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
        now_sec = now.timestamp()
        start_dt = now - timedelta(days=int(round(years * 365.25)))

        merged_dir = self.cache_dir / "merged"
        merged_dir.mkdir(parents=True, exist_ok=True)
        cache_key = f"{symbol}_{interval}_{start_dt.strftime('%Y%m')}_{now.strftime('%Y%m')}.pkl"
        merged_path = merged_dir / cache_key
        if merged_path.exists():
            try:
                cached = pickle.loads(merged_path.read_bytes())
                if isinstance(cached, list) and cached:
                    return cached
            except Exception:
                pass

        if self._unreachable:
            return []

        bars_by_ts: dict[str, OHLCVBar] = {}
        if interval == "1h":
            ym_cur = (start_dt.year, start_dt.month)
            ym_end = (now.year, now.month)
            # Probe most recent completed month first so unreachable hosts fail fast
            prev_m = (now.replace(day=1) - timedelta(days=1))
            probe_bytes = self._fetch_hour_bi5(pair, prev_m.year, prev_m.month)
            if not probe_bytes and self._unreachable:
                return []
            while ym_cur <= ym_end:
                y, m = ym_cur
                raw_bytes = self._fetch_hour_bi5(pair, y, m)
                if raw_bytes:
                    base_dt = datetime(y, m, 1, 0, 0, tzinfo=timezone.utc)
                    for b in parse_dukascopy_bi5(raw_bytes, base_dt, divisor, "1h", now_ts_sec=now_sec):
                        bars_by_ts[b.ts_utc] = b
                if self._unreachable:
                    return []
                m += 1
                if m > 12:
                    m = 1
                    y += 1
                ym_cur = (y, m)
        else:
            from src.data.resample import resample_1m_to_15m

            # Probe one recent weekday first so unreachable hosts fail fast
            probe_dt = (now - timedelta(days=3)).replace(hour=0, minute=0, second=0, microsecond=0)
            while probe_dt.weekday() >= 5:
                probe_dt -= timedelta(days=1)
            self._fetch_day_min_bi5(pair, probe_dt.year, probe_dt.month, probe_dt.day)
            if self._unreachable:
                return []

            cursor = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            bars_1m: list[OHLCVBar] = []
            while cursor <= now:
                if cursor.weekday() < 5:
                    raw_bytes = self._fetch_day_min_bi5(pair, cursor.year, cursor.month, cursor.day)
                    if raw_bytes:
                        bars_1m.extend(parse_dukascopy_bi5(raw_bytes, cursor, divisor, "1m", now_ts_sec=now_sec))
                    if self._unreachable:
                        return []
                cursor += timedelta(days=1)
            for b in resample_1m_to_15m(bars_1m, now_utc=now):
                bars_by_ts[b.ts_utc] = b

        out = sorted(bars_by_ts.values(), key=lambda b: b.ts_utc)
        if out:
            try:
                merged_path.write_bytes(pickle.dumps(out))
            except Exception:
                pass
        return out
