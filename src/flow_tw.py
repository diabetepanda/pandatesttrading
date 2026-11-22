"""Daily Taiwan institutional flow risk-alert generator (`data/flow_tw.json`).

Uses keyless public sources from Taiwan Stock Exchange (BFI82U), Taiwan Futures
Exchange (TAIFEX TXF institutional OI CSV), and FinMind public API mirror:
  - A1: Foreign Spot Net Buy/Sell 60-day rolling z-score (^TWII, trigger z > +1.5)
  - A2: Foreign TXF Net OI 1-day change 60-day rolling z-score (^TWII, trigger z < -1.5,
        excluding monthly TXF settlement days on the 3rd Wednesday of each month)

Note: These indicators failed strict global BH FDR (q ~ 0.42 and 0.20) and are
surfaced strictly as next-day risk hints — never wired into L2/L3 scoring or
simulated account trades.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import time as time_mod
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd
import requests

TPE_TZ = ZoneInfo("Asia/Taipei")

TWSE_BFI82U_URL = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"
TAIFEX_FUT_CSV_URL = "https://www.taifex.com.tw/cht/3/futContractsDateDown"
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

A1_Z_THRESHOLD = 1.5
A2_Z_THRESHOLD = -1.5
ZSCORE_WINDOW = 60
ZSCORE_MIN_PERIODS = 30
HISTORY_DAYS = 60
MAX_STALE_CALENDAR_DAYS = 14
REQUEST_TIMEOUT_SEC = 8

DISCLAIMER_TEXT = "只看隔天、證據偏弱，僅供參考，不是買賣訊號"


def is_txf_settlement_day(d: date | str) -> bool:
    """Return True if `d` is the 3rd Wednesday of its month (TXF monthly settlement)."""
    dt = date.fromisoformat(d) if isinstance(d, str) else d
    return dt.weekday() == 2 and 15 <= dt.day <= 21


def compute_rolling_zscore_series(
    values: list[float | None],
    window: int = ZSCORE_WINDOW,
    min_periods: int = ZSCORE_MIN_PERIODS,
) -> list[float | None]:
    """Compute strictly backward-looking rolling z-score matching pandas rolling std(ddof=1)."""
    s = pd.Series(
        [float(v) if v is not None and math.isfinite(float(v)) else float("nan") for v in values],
        dtype=float,
    )
    mean_s = s.rolling(window=window, min_periods=min_periods).mean()
    std_s = s.rolling(window=window, min_periods=min_periods).std()
    z_s = (s - mean_s) / std_s.replace(0.0, float("nan"))
    out: list[float | None] = []
    for val in z_s.tolist():
        if val is None or not math.isfinite(val):
            out.append(None)
        else:
            out.append(round(float(val), 4))
    return out


def _default_http_get(url: str, params: dict[str, Any] | None = None, timeout: int = REQUEST_TIMEOUT_SEC) -> Any:
    return requests.get(
        url,
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=min(10, int(timeout)),
    )


def _default_http_post(url: str, data: dict[str, Any] | None = None, timeout: int = REQUEST_TIMEOUT_SEC) -> Any:
    return requests.post(
        url,
        data=data,
        headers={"User-Agent": USER_AGENT},
        timeout=min(10, int(timeout)),
    )


def _parse_num_str(raw: Any) -> float:
    s = str(raw or "").strip().replace(",", "")
    if not s or s in ("-", "--", "—"):
        raise ValueError(f"Empty numeric string: {raw!r}")
    return float(s)


def fetch_a1_finmind(
    start_date: date,
    end_date: date,
    http_get: Callable[..., Any] = _default_http_get,
    timeout_sec: int = REQUEST_TIMEOUT_SEC,
) -> dict[str, dict[str, Any]]:
    """Fetch A1 foreign net buy/sell (in 億元) from FinMind public dataset."""
    resp = http_get(
        FINMIND_URL,
        params={
            "dataset": "TaiwanStockTotalInstitutionalInvestors",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
        timeout=min(10, timeout_sec),
    )
    if getattr(resp, "status_code", 200) != 200:
        raise RuntimeError(f"FinMind A1 HTTP {getattr(resp, 'status_code', '?')}")
    payload = resp.json()
    rows = payload.get("data") or []
    by_date: dict[str, float] = {}
    for r in rows:
        d = str(r.get("date") or "")[:10]
        name = str(r.get("name") or "")
        if len(d) != 10 or name not in ("Foreign_Investor", "Foreign_Dealer_Self"):
            continue
        net = float(r.get("buy", 0) or 0) - float(r.get("sell", 0) or 0)
        by_date[d] = by_date.get(d, 0.0) + net

    return {
        d: {
            "date": d,
            "foreign_ntd": val,
            "foreign_100m": val / 1e8,
            "source": "FinMind_BFI82U",
        }
        for d, val in sorted(by_date.items())
    }


def fetch_a1_twse_recent(
    end_date: date,
    lookback_weekdays: int = 4,
    http_get: Callable[..., Any] = _default_http_get,
    timeout_sec: int = REQUEST_TIMEOUT_SEC,
    sleep_sec: float = 1.05,
) -> dict[str, dict[str, Any]]:
    """Fetch recent trading days of A1 foreign net buy/sell directly from TWSE BFI82U."""
    out: dict[str, dict[str, Any]] = {}
    checked = 0
    cur = end_date
    attempts = 0
    while checked < lookback_weekdays and attempts < lookback_weekdays * 2 + 4:
        attempts += 1
        if cur.weekday() < 5:
            checked += 1
            if checked > 1 and sleep_sec > 0:
                time_mod.sleep(sleep_sec)
            day_str = cur.strftime("%Y%m%d")
            try:
                resp = http_get(
                    TWSE_BFI82U_URL,
                    params={"type": "day", "dayDate": day_str, "response": "json"},
                    timeout=min(10, timeout_sec),
                )
                if getattr(resp, "status_code", 200) == 200:
                    j = resp.json()
                    data_rows = j.get("data") or []
                    foreign_ntd = 0.0
                    found_foreign = False
                    for row in data_rows:
                        if isinstance(row, (list, tuple)) and len(row) >= 4 and "外資" in str(row[0]):
                            foreign_ntd += _parse_num_str(row[3])
                            found_foreign = True
                    if found_foreign:
                        d_iso = cur.isoformat()
                        out[d_iso] = {
                            "date": d_iso,
                            "foreign_ntd": foreign_ntd,
                            "foreign_100m": foreign_ntd / 1e8,
                            "source": "TWSE_BFI82U",
                        }
            except Exception:
                pass
        cur -= timedelta(days=1)
    return out


def fetch_a2_taifex(
    start_date: date,
    end_date: date,
    http_post: Callable[..., Any] = _default_http_post,
    timeout_sec: int = REQUEST_TIMEOUT_SEC,
) -> dict[str, dict[str, Any]]:
    """Fetch A2 foreign TXF net open interest directly from TAIFEX official CSV."""
    resp = http_post(
        TAIFEX_FUT_CSV_URL,
        data={
            "queryStartDate": start_date.strftime("%Y/%m/%d"),
            "queryEndDate": end_date.strftime("%Y/%m/%d"),
            "commodityId": "TXF",
        },
        timeout=min(10, timeout_sec),
    )
    if getattr(resp, "status_code", 200) != 200:
        raise RuntimeError(f"TAIFEX A2 HTTP {getattr(resp, 'status_code', '?')}")
    raw_bytes = getattr(resp, "content", None)
    if isinstance(raw_bytes, bytes):
        txt = raw_bytes.decode("cp950", errors="replace")
    else:
        txt = str(getattr(resp, "text", "") or "")

    out: dict[str, dict[str, Any]] = {}
    reader = csv.reader(io.StringIO(txt))
    for row in reader:
        if len(row) < 15:
            continue
        if row[1].strip() == "臺股期貨" and "外資" in row[2].strip():
            raw_d = row[0].strip().replace("/", "-")
            parts = raw_d.split("-")
            if len(parts) == 3 and len(parts[0]) == 4:
                d_iso = f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
                long_oi = int(_parse_num_str(row[9]))
                short_oi = int(_parse_num_str(row[11]))
                net_oi = int(_parse_num_str(row[13]))
                out[d_iso] = {
                    "date": d_iso,
                    "long_oi": long_oi,
                    "short_oi": short_oi,
                    "net_oi": net_oi,
                    "source": "TAIFEX_Direct",
                }
    if not out:
        raise RuntimeError("TAIFEX A2 returned 0 TXF foreign rows")
    return out


def fetch_a2_finmind(
    start_date: date,
    end_date: date,
    http_get: Callable[..., Any] = _default_http_get,
    timeout_sec: int = REQUEST_TIMEOUT_SEC,
) -> dict[str, dict[str, Any]]:
    """Fallback A2 fetcher using FinMind public dataset (TaiwanFuturesInstitutionalInvestors)."""
    resp = http_get(
        FINMIND_URL,
        params={
            "dataset": "TaiwanFuturesInstitutionalInvestors",
            "data_id": "TX",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
        timeout=min(10, timeout_sec),
    )
    if getattr(resp, "status_code", 200) != 200:
        raise RuntimeError(f"FinMind A2 HTTP {getattr(resp, 'status_code', '?')}")
    payload = resp.json()
    rows = payload.get("data") or []
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        inv = str(r.get("institutional_investors") or "")
        if "外資" not in inv:
            continue
        d = str(r.get("date") or "")[:10]
        if len(d) != 10:
            continue
        long_oi = int(r.get("long_open_interest_balance_volume", 0) or 0)
        short_oi = int(r.get("short_open_interest_balance_volume", 0) or 0)
        out[d] = {
            "date": d,
            "long_oi": long_oi,
            "short_oi": short_oi,
            "net_oi": long_oi - short_oi,
            "source": "FinMind_TAIFEX",
        }
    if not out:
        raise RuntimeError("FinMind A2 returned 0 TX foreign rows")
    return out


def _extract_prev_rows(prev_doc: dict[str, Any] | None) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Recover prior A1 and A2 series rows from an existing flow_tw.json payload."""
    a1_prev: dict[str, dict[str, Any]] = {}
    a2_prev: dict[str, dict[str, Any]] = {}
    if not isinstance(prev_doc, dict):
        return a1_prev, a2_prev

    prev_a1_src = str((prev_doc.get("a1") or {}).get("source_used") or "cached")
    prev_a2_src = str((prev_doc.get("a2") or {}).get("source_used") or "cached")
    for row in prev_doc.get("history_60d") or []:
        if not isinstance(row, dict):
            continue
        d = str(row.get("date") or "")[:10]
        if len(d) != 10:
            continue
        if row.get("a1_foreign_100m") is not None:
            f100m = float(row["a1_foreign_100m"])
            a1_prev[d] = {
                "date": d,
                "foreign_ntd": f100m * 1e8,
                "foreign_100m": f100m,
                "source": prev_a1_src,
            }
        if row.get("a2_net_oi") is not None:
            a2_prev[d] = {
                "date": d,
                "net_oi": int(row["a2_net_oi"]),
                "prev_chg_1d": int(row["a2_chg_1d"]) if row.get("a2_chg_1d") is not None else None,
                "source": prev_a2_src,
            }
    return a1_prev, a2_prev


def evaluate_flow_tw_signals(
    a1_rows: list[dict[str, Any]],
    a2_rows: list[dict[str, Any]],
    *,
    now_utc: datetime | None = None,
    a1_stale: bool = False,
    a2_stale: bool = False,
    stale_reason: str | None = None,
    max_stale_days: int = MAX_STALE_CALENDAR_DAYS,
    history_days: int = HISTORY_DAYS,
) -> dict[str, Any]:
    """Compute A1/A2 60-day rolling z-scores, settlement exclusions, and risk-alert payload."""
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_tpe = now.astimezone(TPE_TZ)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Sort & deduplicate A1
    a1_sorted = sorted({r["date"]: dict(r) for r in a1_rows if r.get("date")}.values(), key=lambda x: x["date"])
    a1_vals = [float(r["foreign_100m"]) for r in a1_sorted]
    a1_zs = compute_rolling_zscore_series(a1_vals)
    a1_by_date: dict[str, dict[str, Any]] = {}
    for row, z in zip(a1_sorted, a1_zs):
        trig = bool(z is not None and z > A1_Z_THRESHOLD)
        a1_by_date[row["date"]] = {
            "date": row["date"],
            "foreign_100m": round(float(row["foreign_100m"]), 2),
            "foreign_ntd": float(row.get("foreign_ntd", float(row["foreign_100m"]) * 1e8)),
            "z60": round(float(z), 2) if z is not None else None,
            "z60_exact": z,
            "triggered": trig,
            "source": row.get("source") or "TWSE_BFI82U",
        }

    # Sort & deduplicate A2, compute 1-day net_oi change
    a2_sorted = sorted({r["date"]: dict(r) for r in a2_rows if r.get("date")}.values(), key=lambda x: x["date"])
    a2_chgs: list[float | None] = []
    for idx, row in enumerate(a2_sorted):
        if idx == 0:
            prev_chg = row.get("prev_chg_1d")
            a2_chgs.append(float(prev_chg) if prev_chg is not None else None)
        else:
            a2_chgs.append(float(int(row["net_oi"]) - int(a2_sorted[idx - 1]["net_oi"])))
    a2_zs = compute_rolling_zscore_series(a2_chgs)
    a2_by_date: dict[str, dict[str, Any]] = {}
    for row, chg, z in zip(a2_sorted, a2_chgs, a2_zs):
        d = row["date"]
        settle = is_txf_settlement_day(d)
        raw_hit = bool(z is not None and z < A2_Z_THRESHOLD)
        trig = bool(raw_hit and not settle)
        a2_by_date[d] = {
            "date": d,
            "net_oi": int(row["net_oi"]),
            "chg_1d": int(round(chg)) if chg is not None else None,
            "z60": round(float(z), 2) if z is not None else None,
            "z60_exact": z,
            "is_settlement_day": settle,
            "excluded_by_settlement": bool(raw_hit and settle),
            "triggered": trig,
            "source": row.get("source") or "TAIFEX_Direct",
        }

    all_dates = sorted(set(a1_by_date.keys()) | set(a2_by_date.keys()))
    latest_date = all_dates[-1] if all_dates else None
    latest_a1 = a1_by_date[a1_sorted[-1]["date"]] if a1_sorted else None
    latest_a2 = a2_by_date[a2_sorted[-1]["date"]] if a2_sorted else None

    # Check calendar staleness if latest_date is too far in the past
    date_stale = False
    date_stale_msg: str | None = None
    if latest_date is None:
        date_stale = True
        date_stale_msg = "尚無可用台股籌碼資料"
    else:
        age_days = (now_tpe.date() - date.fromisoformat(latest_date)).days
        if age_days > max_stale_days:
            date_stale = True
            date_stale_msg = f"最新資料日期 {latest_date} 已超過 {max_stale_days} 天未更新"

    is_a1_stale = bool(a1_stale or latest_a1 is None or date_stale)
    is_a2_stale = bool(a2_stale or latest_a2 is None or date_stale)
    overall_stale = bool(is_a1_stale or is_a2_stale)
    reasons = [r for r in (stale_reason, date_stale_msg) if r]
    combined_stale_reason = "；".join(dict.fromkeys(reasons)) if reasons else None

    latest_settle = is_txf_settlement_day(latest_date) if latest_date else False

    a1_trig = bool(latest_a1 and latest_a1["triggered"])
    a2_trig = bool(latest_a2 and latest_a2["triggered"])

    if a1_trig:
        a1_msg = "外資異常大買，隔天偏多（歷史上漲機率約 73%，平常約 57%）"
    elif latest_a1 and latest_a1["z60"] is not None:
        a1_msg = f"今日無異常（60日 z = {latest_a1['z60']:+.2f}）"
    else:
        a1_msg = "樣本不足或資料過期"

    if a2_trig:
        a2_msg = "外資期貨空單暴增，隔天下跌風險升高（歷史下跌機率約 68%）"
    elif latest_a2 and latest_a2["excluded_by_settlement"]:
        a2_msg = f"今日為台指期結算日（第三個星期三），已排除 A2 訊號（z = {latest_a2['z60']:+.2f}）"
    elif latest_a2 and latest_a2["z60"] is not None:
        a2_msg = f"今日無異常（60日 z = {latest_a2['z60']:+.2f}）"
    else:
        a2_msg = "樣本不足或資料過期"

    recent_dates = all_dates[-history_days:] if history_days > 0 else all_dates
    history_60d: list[dict[str, Any]] = []
    for d in recent_dates:
        r1 = a1_by_date.get(d)
        r2 = a2_by_date.get(d)
        history_60d.append(
            {
                "date": d,
                "is_settlement_day": is_txf_settlement_day(d),
                "a1_foreign_100m": r1["foreign_100m"] if r1 else None,
                "a1_z60": r1["z60"] if r1 else None,
                "a1_triggered": r1["triggered"] if r1 else False,
                "a2_net_oi": r2["net_oi"] if r2 else None,
                "a2_chg_1d": r2["chg_1d"] if r2 else None,
                "a2_z60": r2["z60"] if r2 else None,
                "a2_triggered": r2["triggered"] if r2 else False,
            }
        )

    return {
        "version": 1,
        "symbol": "TWII",
        "market": "^TWII",
        "updated_at_utc": now_iso,
        "updated_at_taipei": now_tpe.strftime("%Y-%m-%d %H:%M"),
        "latest_date": latest_date,
        "data_status": "資料過期" if overall_stale else "ok",
        "stale": overall_stale,
        "stale_reason": combined_stale_reason,
        "is_settlement_day": latest_settle,
        "any_triggered": bool(a1_trig or a2_trig),
        "a1": {
            "id": "A1_foreign_z60",
            "name": "外資現貨買賣超",
            "unit": "億元",
            "date": latest_a1["date"] if latest_a1 else None,
            "value": latest_a1["foreign_100m"] if latest_a1 else None,
            "z60": latest_a1["z60"] if latest_a1 else None,
            "threshold": A1_Z_THRESHOLD,
            "direction": "gt",
            "triggered": a1_trig,
            "window_days": ZSCORE_WINDOW,
            "source_used": latest_a1["source"] if latest_a1 else None,
            "stale": is_a1_stale,
            "data_status": "資料過期" if is_a1_stale else "ok",
            "message": a1_msg,
        },
        "a2": {
            "id": "A2_txf_1d_chg",
            "name": "外資台指期淨未平倉單日變化",
            "unit": "口",
            "date": latest_a2["date"] if latest_a2 else None,
            "net_oi": latest_a2["net_oi"] if latest_a2 else None,
            "value": latest_a2["chg_1d"] if latest_a2 else None,
            "z60": latest_a2["z60"] if latest_a2 else None,
            "threshold": A2_Z_THRESHOLD,
            "direction": "lt",
            "is_settlement_day": latest_a2["is_settlement_day"] if latest_a2 else latest_settle,
            "excluded_by_settlement": latest_a2["excluded_by_settlement"] if latest_a2 else False,
            "triggered": a2_trig,
            "window_days": ZSCORE_WINDOW,
            "source_used": latest_a2["source"] if latest_a2 else None,
            "stale": is_a2_stale,
            "data_status": "資料過期" if is_a2_stale else "ok",
            "message": a2_msg,
        },
        "history_60d": history_60d,
        "disclaimer": DISCLAIMER_TEXT,
    }


def generate_flow_tw_file(
    out_dir: Path,
    *,
    now_utc: datetime | None = None,
    http_get: Callable[..., Any] = _default_http_get,
    http_post: Callable[..., Any] = _default_http_post,
    timeout_sec: int = REQUEST_TIMEOUT_SEC,
    sleep_sec: float = 1.05,
    max_stale_days: int = MAX_STALE_CALENDAR_DAYS,
    use_local_seed: bool = True,
) -> dict[str, Any]:
    """Fetch public TWSE/TAIFEX/FinMind flow data, compute A1/A2, and write `flow_tw.json`.

    Never raises on network errors: preserves prior `flow_tw.json` data and marks `資料過期`.
    """
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_tpe = now.astimezone(TPE_TZ)
    end_d = now_tpe.date()
    start_d = end_d - timedelta(days=150)

    out_path = out_dir / "flow_tw.json"
    seed_path = Path(__file__).resolve().parent.parent / "data" / "flow_tw.json"
    prev_doc: dict[str, Any] | None = None
    for candidate in (out_path, seed_path) if use_local_seed else (out_path,):
        if candidate.exists():
            try:
                prev_doc = json.loads(candidate.read_text(encoding="utf-8"))
                break
            except Exception:
                prev_doc = None

    a1_prev, a2_prev = _extract_prev_rows(prev_doc)
    stale_errors: list[str] = []

    # ---- Fetch A1 (FinMind range + TWSE BFI82U recent official override) ----
    a1_merged = dict(a1_prev)
    a1_fetched_any = False
    a1_errs: list[str] = []
    try:
        fm_a1 = fetch_a1_finmind(start_d, end_d, http_get=http_get, timeout_sec=timeout_sec)
        if fm_a1:
            a1_merged.update(fm_a1)
            a1_fetched_any = True
    except Exception as exc:
        a1_errs.append(f"FinMind A1: {exc}")

    try:
        twse_a1 = fetch_a1_twse_recent(
            end_d,
            lookback_weekdays=4,
            http_get=http_get,
            timeout_sec=timeout_sec,
            sleep_sec=sleep_sec,
        )
        if twse_a1:
            a1_merged.update(twse_a1)
            a1_fetched_any = True
    except Exception as exc:
        a1_errs.append(f"TWSE BFI82U: {exc}")

    a1_stale = not a1_fetched_any
    if a1_stale:
        stale_errors.append("A1 外資買賣超抓取失敗" + (f"（{'; '.join(a1_errs)}）" if a1_errs else ""))

    # ---- Fetch A2 (TAIFEX Direct primary -> FinMind fallback) ----
    a2_merged = dict(a2_prev)
    a2_fetched_any = False
    a2_errs: list[str] = []
    try:
        taifex_a2 = fetch_a2_taifex(start_d, end_d, http_post=http_post, timeout_sec=timeout_sec)
        if taifex_a2:
            a2_merged.update(taifex_a2)
            a2_fetched_any = True
    except Exception as exc:
        a2_errs.append(f"TAIFEX A2: {exc}")

    if not a2_fetched_any:
        try:
            fm_a2 = fetch_a2_finmind(start_d, end_d, http_get=http_get, timeout_sec=timeout_sec)
            if fm_a2:
                a2_merged.update(fm_a2)
                a2_fetched_any = True
        except Exception as exc:
            a2_errs.append(f"FinMind A2: {exc}")

    a2_stale = not a2_fetched_any
    if a2_stale:
        stale_errors.append("A2 外資台指期未平倉抓取失敗" + (f"（{'; '.join(a2_errs)}）" if a2_errs else ""))

    payload = evaluate_flow_tw_signals(
        list(a1_merged.values()),
        list(a2_merged.values()),
        now_utc=now,
        a1_stale=a1_stale,
        a2_stale=a2_stale,
        stale_reason="；".join(stale_errors) if stale_errors else None,
        max_stale_days=max_stale_days,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Taiwan institutional flow risk-alert flow_tw.json")
    parser.add_argument("--out-dir", type=Path, default=Path("data"))
    parser.add_argument("--timeout", type=int, default=REQUEST_TIMEOUT_SEC)
    args = parser.parse_args()

    res = generate_flow_tw_file(out_dir=args.out_dir, timeout_sec=args.timeout)
    print(
        f"Generated {args.out_dir / 'flow_tw.json'}: "
        f"latest_date={res['latest_date']}, status={res['data_status']}, "
        f"A1_z={res['a1']['z60']} (trig={res['a1']['triggered']}), "
        f"A2_z={res['a2']['z60']} (trig={res['a2']['triggered']}, settle={res['is_settlement_day']})"
    )


if __name__ == "__main__":
    main()
