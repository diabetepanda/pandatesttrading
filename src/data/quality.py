"""Data continuity and quality checks for OHLCV series."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from src.data.adapters import INTERVAL_SECONDS
from src.data.models import FetchResult, OHLCVBar, QualityReport


def _parse_iso_utc(ts_str: str) -> datetime:
    return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def is_weekend_closure(dt: datetime) -> bool:
    """Return True if dt falls in typical FX/commodity weekend closure (Fri 22:00 - Sun 22:00 UTC)."""
    wd = dt.weekday()  # Mon=0 .. Sun=6
    if wd == 5:  # Saturday
        return True
    if wd == 4 and dt.hour >= 22:  # Friday >= 22:00 UTC
        return True
    if wd == 6 and dt.hour < 22:  # Sunday < 22:00 UTC
        return True
    return False


def is_market_open(market: str, dt: datetime, trading_hours: str | None = None) -> bool:
    """Approximate whether a market is expected to be trading at dt (UTC)."""
    if market == "crypto":
        return True
    if trading_hours in ("TW_STOCK", "TW_INDEX"):
        if dt.weekday() >= 5:
            return False
        minutes = dt.hour * 60 + dt.minute
        # TWSE 09:00-13:30 Asia/Taipei == 01:00-05:30 UTC
        return (1 * 60) <= minutes <= (5 * 60 + 30)
    if trading_hours in ("KR_STOCK", "JP_INDEX"):
        if dt.weekday() >= 5:
            return False
        minutes = dt.hour * 60 + dt.minute
        # KRX 09:00-15:30 KST / TSE 09:00-15:30 JST == 00:00-06:30 UTC
        return 0 <= minutes <= (6 * 60 + 30)
    if market in ("forex", "commodity", "index"):
        return not is_weekend_closure(dt)
    if market == "stock":
        # US equities: Mon-Fri 13:30 - 21:00 UTC (covers EDT & EST)
        if dt.weekday() >= 5:
            return False
        minutes = dt.hour * 60 + dt.minute
        return (13 * 60 + 30) <= minutes <= (21 * 60)
    return True


def _is_expected_break(prev_dt: datetime, curr_dt: datetime, market: str, interval: str) -> bool:
    """Check whether a gap between two bars is a normal market closure."""
    if market == "crypto":
        return False

    if interval == "1d":
        # Check if all intermediate days are weekend days (Sat/Sun)
        cursor = prev_dt + timedelta(days=1)
        while cursor < curr_dt:
            if cursor.weekday() < 5:
                return False
            cursor += timedelta(days=1)
        return True

    # Intraday (1m, 15m) for forex/commodity/stock
    # Weekend gap: Friday evening to Sunday/Monday
    midpoint = prev_dt + (curr_dt - prev_dt) / 2
    if (
        is_weekend_closure(prev_dt + timedelta(minutes=1))
        or is_weekend_closure(curr_dt - timedelta(minutes=1))
        or is_weekend_closure(midpoint)
    ):
        return True

    # Daily settlement break for commodities/FX (typically 21:00-23:00 UTC, <= 90 min)
    gap_minutes = (curr_dt - prev_dt).total_seconds() / 60.0
    if market == "commodity" and gap_minutes <= 90 and prev_dt.hour in (20, 21, 22):
        return True

    # Overnight closure for stocks
    if market == "stock" and curr_dt.date() > prev_dt.date():
        return True

    return False


def _effective_delay_minutes(latest_close_dt: datetime, now: datetime, market: str) -> float:
    """Compute delay in minutes from latest_close_dt to now, excluding weekend closures."""
    if now <= latest_close_dt:
        return 0.0
    total_sec = (now - latest_close_dt).total_seconds()
    if market == "crypto":
        return round(total_sec / 60.0, 1)

    # Subtract any overlap with weekend closure windows [Fri 22:00 UTC, Sun 22:00 UTC]
    weekend_sec = 0.0
    start_day = (latest_close_dt - timedelta(days=3)).replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cursor = start_day
    while cursor <= end_day:
        if cursor.weekday() == 4:  # Friday
            win_start = cursor.replace(hour=22, minute=0, second=0, microsecond=0)
            win_end = win_start + timedelta(hours=48)
            overlap_start = max(latest_close_dt, win_start)
            overlap_end = min(now, win_end)
            if overlap_end > overlap_start:
                weekend_sec += (overlap_end - overlap_start).total_seconds()
        cursor += timedelta(days=1)

    effective_sec = max(0.0, total_sec - weekend_sec)
    return round(effective_sec / 60.0, 1)


def check_continuity(
    fetch_result: FetchResult,
    market: str,
    quality_config: dict[str, Any],
    now_utc: datetime | None = None,
    trading_hours: str | None = None,
) -> QualityReport:
    """Evaluate bar continuity, delay, and abnormal gaps."""
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    interval = fetch_result.interval
    symbol = fetch_result.symbol

    if not fetch_result.ok or not fetch_result.bars:
        return QualityReport(
            symbol=symbol,
            interval=interval,
            status="failed",
            source_used=fetch_result.source_used,
            bar_count=0,
            latest_ts_utc=None,
            latest_close=None,
            delay_minutes=None,
            missing_ratio=1.0,
            abnormal_gap_count=0,
            reason=f"資料來源失敗: {fetch_result.error or '無回傳資料'}",
        )

    bars: list[OHLCVBar] = fetch_result.bars
    interval_sec = INTERVAL_SECONDS[interval]
    timestamps = [_parse_iso_utc(b.ts_utc) for b in bars]

    expected_bars = 1
    missing_bars = 0
    for i in range(1, len(timestamps)):
        prev_dt = timestamps[i - 1]
        curr_dt = timestamps[i]
        diff_sec = (curr_dt - prev_dt).total_seconds()
        if diff_sec <= interval_sec:
            expected_bars += 1
            continue
        if _is_expected_break(prev_dt, curr_dt, market, interval):
            expected_bars += 1
        elif interval == "1d" and market != "crypto":
            # Exclude Saturdays/Sundays when counting missed daily bars around weekday market holidays
            missed_weekdays = 0
            cursor = prev_dt + timedelta(days=1)
            while cursor.date() < curr_dt.date():
                if cursor.weekday() < 5:
                    missed_weekdays += 1
                cursor += timedelta(days=1)
            expected_bars += 1 + missed_weekdays
            missing_bars += missed_weekdays
        else:
            steps = max(1, round(diff_sec / interval_sec))
            expected_bars += steps
            missing_bars += max(0, steps - 1)

    missing_ratio = round(missing_bars / expected_bars, 4) if expected_bars > 0 else 0.0

    # Delay from the close of the latest bar to now (excluding weekend closures for non-crypto)
    latest_close_dt = timestamps[-1] + timedelta(seconds=interval_sec)
    delay_minutes = _effective_delay_minutes(latest_close_dt, now, market)

    # Abnormal price gaps (excluding expected closures)
    ranges = [max(1e-12, b.high - b.low) for b in bars]
    med_range = median(ranges) if ranges else 1e-12
    gap_mult = float(quality_config.get("abnormal_gap_atr_mult", 5.0))
    abnormal_gap_count = 0
    for i in range(1, len(bars)):
        if _is_expected_break(timestamps[i - 1], timestamps[i], market, interval):
            continue
        open_gap = abs(bars[i].open - bars[i - 1].close)
        if med_range > 0 and open_gap > med_range * gap_mult:
            abnormal_gap_count += 1

    max_missing = float(quality_config.get("max_missing_ratio", 0.10))
    max_delay_map = quality_config.get("max_delay_minutes", {})
    max_delay = float(max_delay_map.get(interval, 45))

    reasons: list[str] = []
    status = "ok"

    if missing_ratio > max_missing:
        status = "unstable"
        reasons.append(f"缺 K 線比例過高 ({missing_ratio:.1%} > {max_missing:.1%})")

    market_open_now = is_market_open(market, now, trading_hours=trading_hours)
    if market_open_now and delay_minutes > max_delay:
        if status == "ok":
            status = "stale"
        reasons.append(f"資料延遲 ({delay_minutes:.1f} 分鐘 > {max_delay:.0f} 分鐘)")

    if not reasons:
        reasons.append("正常" if market_open_now else "休市中（使用最後收盤價）")

    return QualityReport(
        symbol=symbol,
        interval=interval,
        status=status,
        source_used=fetch_result.source_used,
        bar_count=len(bars),
        latest_ts_utc=bars[-1].ts_utc,
        latest_close=bars[-1].close,
        delay_minutes=delay_minutes,
        missing_ratio=missing_ratio,
        abnormal_gap_count=abnormal_gap_count,
        reason="；".join(reasons),
    )
