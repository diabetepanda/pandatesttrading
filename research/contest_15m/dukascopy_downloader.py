#!/usr/bin/env python3
"""Resumable, rate-limited Dukascopy .bi5 downloader with exponential backoff.

Uses curl_cffi HTTP/2 persistent session (with requests fallback) so TLS
handshakes happen once and rate limits are respected via adaptive pacing.
"""

from __future__ import annotations

import argparse
import lzma
import pickle
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    cffi_requests = None
    HAS_CFFI = False

import requests as std_requests

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.data.adapters import parse_dukascopy_bi5
from src.data.models import OHLCVBar
from src.data.resample import resample_1m_to_15m

HOUR_URL = "https://datafeed.dukascopy.com/datafeed/{pair}/{year:04d}/{month0:02d}/BID_candles_hour_1.bi5"
DAY_MIN_URL = "https://datafeed.dukascopy.com/datafeed/{pair}/{year:04d}/{month0:02d}/{day:02d}/BID_candles_min_1.bi5"


class TokenRateLimiter:
    """Thread-safe adaptive rate limiter with backoff on 429/503."""

    def __init__(self, req_per_sec: float = 1.5) -> None:
        self._lock = threading.Lock()
        self._base_interval = 1.0 / max(0.2, req_per_sec)
        self._cur_interval = self._base_interval
        self._next_allowed = 0.0

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                if now >= self._next_allowed:
                    self._next_allowed = now + self._cur_interval
                    return
                wait = self._next_allowed - now
            time.sleep(min(wait, 1.0))

    def penalize(self, cooldown_sec: float = 15.0) -> None:
        with self._lock:
            self._cur_interval = min(2.0, self._cur_interval * 1.25)
            self._next_allowed = max(self._next_allowed, time.monotonic() + cooldown_sec)

    def reward(self) -> None:
        with self._lock:
            if self._cur_interval > self._base_interval:
                self._cur_interval = max(self._base_interval, self._cur_interval * 0.97)


class ResumableDukascopyDownloader:
    """Resumable disk-cached Dukascopy archive downloader."""

    def __init__(
        self,
        cache_dir: Path | str = ".cache/dukascopy",
        req_per_sec: float = 1.5,
        max_workers: int = 1,
        timeout_sec: int = 35,
        max_retries: int = 6,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "merged").mkdir(parents=True, exist_ok=True)
        self.limiter = TokenRateLimiter(req_per_sec=req_per_sec)
        self.max_workers = max_workers
        self.timeout_sec = timeout_sec
        self.max_retries = max_retries
        self._local = threading.local()

    def _session(self) -> Any:
        sess = getattr(self._local, "session", None)
        if sess is None:
            if HAS_CFFI:
                sess = cffi_requests.Session(impersonate="chrome")
            else:
                sess = std_requests.Session()
                sess.headers.update({"User-Agent": "curl/8.7.1", "Accept": "*/*"})
            self._local.session = sess
        return sess

    def _fetch_bi5_cached(self, url: str, cache_path: Path) -> bytes | None:
        empty_marker = cache_path.with_suffix(".empty")
        if cache_path.exists() and cache_path.stat().st_size > 0:
            return cache_path.read_bytes()
        if empty_marker.exists():
            return None

        for attempt in range(self.max_retries):
            self.limiter.acquire()
            try:
                resp = self._session().get(url, timeout=self.timeout_sec)
                if resp.status_code == 200:
                    if not resp.content:
                        cache_path.parent.mkdir(parents=True, exist_ok=True)
                        empty_marker.touch()
                        return None
                    try:
                        lzma.decompress(resp.content)
                    except Exception:
                        time.sleep(1.0 * (2 ** attempt))
                        continue
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_bytes(resp.content)
                    self.limiter.reward()
                    return resp.content
                if resp.status_code == 404:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    empty_marker.touch()
                    return None
                if resp.status_code in (403, 429, 500, 502, 503, 504):
                    retry_after = float(resp.headers.get("Retry-After", 0) or 0)
                    backoff = max(retry_after, min(60.0, (8.0 * (2 ** attempt)) + random.uniform(0.5, 2.0)))
                    print(f"    HTTP {resp.status_code} on {cache_path.name}, sleeping {backoff:.1f}s (attempt {attempt+1})", flush=True)
                    self.limiter.penalize(cooldown_sec=backoff)
                    time.sleep(backoff)
                    continue
                return None
            except Exception as exc:
                backoff = min(45.0, (5.0 * (2 ** attempt)) + random.uniform(0.5, 2.0))
                print(f"    {type(exc).__name__} on {cache_path.name}, sleeping {backoff:.1f}s (attempt {attempt+1})", flush=True)
                self.limiter.penalize(cooldown_sec=backoff * 0.5)
                time.sleep(backoff)
        return None

    def fetch_symbol_1h(
        self,
        symbol: str,
        pair: str,
        divisor: float,
        years: float = 3.2,
        now_utc: datetime | None = None,
    ) -> list[OHLCVBar]:
        now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
        now_sec = now.timestamp()
        start_dt = now - timedelta(days=int(round(years * 365.25)))

        ym_list: list[tuple[int, int]] = []
        y, m = start_dt.year, start_dt.month
        while (y, m) <= (now.year, now.month):
            ym_list.append((y, m))
            m += 1
            if m > 12:
                m = 1
                y += 1

        bars_by_ts: dict[str, OHLCVBar] = {}
        done = 0
        for yy, mm in ym_list:
            m0 = mm - 1
            cpath = self.cache_dir / pair / f"{yy:04d}" / f"{m0:02d}_BID_candles_hour_1.bi5"
            url = HOUR_URL.format(pair=pair, year=yy, month0=m0)
            raw = self._fetch_bi5_cached(url, cpath)
            if raw:
                base_dt = datetime(yy, mm, 1, 0, 0, tzinfo=timezone.utc)
                for b in parse_dukascopy_bi5(raw, base_dt, divisor, "1h", now_ts_sec=now_sec):
                    bars_by_ts[b.ts_utc] = b
            done += 1
            if done % 15 == 0 or done == len(ym_list):
                print(f"    [1h] {symbol}: {done}/{len(ym_list)} months", flush=True)

        out = sorted(bars_by_ts.values(), key=lambda b: b.ts_utc)
        if out:
            merged_path = self.cache_dir / "merged" / f"{symbol}_1h_3y.pkl"
            merged_path.write_bytes(pickle.dumps(out))
        return out

    def fetch_symbol_15m(
        self,
        symbol: str,
        pair: str,
        divisor: float,
        years: float = 3.2,
        now_utc: datetime | None = None,
    ) -> list[OHLCVBar]:
        now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
        now_sec = now.timestamp()
        start_dt = (now - timedelta(days=int(round(years * 365.25)))).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        days: list[datetime] = []
        cur = start_dt
        while cur <= now:
            if cur.weekday() < 5:
                days.append(cur)
            cur += timedelta(days=1)

        bars_1m: list[OHLCVBar] = []
        done = 0
        for d in days:
            m0 = d.month - 1
            cpath = self.cache_dir / pair / f"{d.year:04d}" / f"{m0:02d}" / f"{d.day:02d}_BID_candles_min_1.bi5"
            url = DAY_MIN_URL.format(pair=pair, year=d.year, month0=m0, day=d.day)
            raw = self._fetch_bi5_cached(url, cpath)
            if raw:
                bars_1m.extend(parse_dukascopy_bi5(raw, d, divisor, "1m", now_ts_sec=now_sec))
            done += 1
            if done % 100 == 0 or done == len(days):
                print(f"    [15m] {symbol}: {done}/{len(days)} days", flush=True)

        bars_15m = resample_1m_to_15m(bars_1m, now_utc=now)
        if bars_15m:
            merged_path = self.cache_dir / "merged" / f"{symbol}_15m_3y.pkl"
            merged_path.write_bytes(pickle.dumps(bars_15m))
        return bars_15m


def load_costs_config() -> dict[str, Any]:
    cfg_path = Path(__file__).resolve().parent / "costs_config.yaml"
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Dukascopy 1h/15m history with backoff")
    parser.add_argument("--interval", choices=["1h", "15m", "both"], default="1h")
    parser.add_argument("--years", type=float, default=3.2)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--rate", type=float, default=1.5)
    parser.add_argument("--symbols", nargs="*", default=None)
    args = parser.parse_args()

    costs = load_costs_config()
    insts = costs.get("instruments", {})
    dl = ResumableDukascopyDownloader(req_per_sec=args.rate, max_workers=args.workers)
    print(f"Using {'curl_cffi HTTP/2' if HAS_CFFI else 'requests HTTP/1.1'} | rate={args.rate}/s", flush=True)

    now_dt = datetime.now(timezone.utc)
    targets = args.symbols or [s for s, v in insts.items() if v.get("market") != "crypto"]

    for sym in targets:
        info = insts.get(sym)
        if not info or not info.get("dukascopy_pair"):
            continue
        pair = info["dukascopy_pair"]
        div = float(info["dukascopy_divisor"])
        if args.interval in ("1h", "both"):
            t0 = time.monotonic()
            b1h = dl.fetch_symbol_1h(sym, pair, div, years=args.years, now_utc=now_dt)
            span = f"{b1h[0].ts_utc} -> {b1h[-1].ts_utc}" if b1h else "EMPTY"
            print(f"[1h]  {sym:8s}: {len(b1h):6d} bars ({span}) in {time.monotonic()-t0:.1f}s", flush=True)
        if args.interval in ("15m", "both"):
            t1 = time.monotonic()
            b15 = dl.fetch_symbol_15m(sym, pair, div, years=args.years, now_utc=now_dt)
            span = f"{b15[0].ts_utc} -> {b15[-1].ts_utc}" if b15 else "EMPTY"
            print(f"[15m] {sym:8s}: {len(b15):6d} bars ({span}) in {time.monotonic()-t1:.1f}s", flush=True)


if __name__ == "__main__":
    main()
