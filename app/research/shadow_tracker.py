"""Near-miss shadow tracker - measurement-only.

Records the gate's near-misses (candidates that passed 2-of-3 primary signals
but were rejected) and tracks how those tickers actually performed over fixed
forward horizons (5, 10, 20 trading days). The purpose is purely to gather
evidence about whether the conviction gate - and specifically the news signal
- is well-calibrated.

This module is READ-ONLY with respect to the conviction gate:
  * It only ``load()``s ``gate_telemetry.yaml``, never writes to it.
  * It does not import or call anything that mutates gate thresholds or
    signal logic.
  * Its outputs are separate data files (``shadow_ledger.yaml`` and
    ``shadow_calibration.yaml``) that nothing else in the build consumes.

Near-misses on the candidates path are long ("buy") candidates, so a rejection
looks WRONG when the ticker delivered positive *excess* return over SPY across
the horizon, and RIGHT when it did not.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd
import yaml

from app.data import prices
from app.logging import get_logger
from app.research import gate_telemetry

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
LEDGER_PATH = ROOT / "shadow_ledger.yaml"
CALIBRATION_PATH = ROOT / "shadow_calibration.yaml"

HORIZONS = (5, 10, 20)
BENCHMARK = "SPY"
DIRECTION = "long"

# How far past the miss date the first available trading close is allowed to
# be before we refuse to anchor (treat the row as unrecoverable). 7 calendar
# days covers a Fri/holiday miss rolling to the next trading day; anything
# further out almost certainly means the price window has rolled past the
# miss date and the first row is months too late.
ENTRY_TOLERANCE_DAYS = 7

HistoryFn = Callable[[str], "pd.DataFrame | None"]


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _to_date(s) -> date | None:
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    if isinstance(s, datetime):
        return s.date()
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _normalize_index(df: "pd.DataFrame") -> "pd.DataFrame":
    """Return a copy with a tz-naive DatetimeIndex sorted ascending.

    ``tz_localize(None)`` raises on an already-tz-aware index in some pandas
    builds, so we branch on the existing tz and use ``tz_convert(None)`` to
    strip it. A tz-naive index passes through untouched.
    """
    if df is None or df.empty:
        return df
    idx = pd.to_datetime(df.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert(None)
    out = df.copy()
    out.index = idx
    return out.sort_index()


def _close_on_or_after(
    df: "pd.DataFrame", target: date, max_calendar_days: int | None = None,
) -> tuple[date | None, float | None]:
    """First trading-day close at or after ``target``. ``(None, None)`` if none.

    When ``max_calendar_days`` is set, the located trading day must be within
    that many calendar days of ``target`` -- otherwise ``(None, None)`` is
    returned. This guards against silently anchoring to the start of a rolled-
    forward price window when the original miss date is no longer in it.
    """
    if df is None or df.empty or "Close" not in df.columns:
        return None, None
    ts = pd.Timestamp(target)
    sub = df[df.index >= ts]
    if sub.empty:
        return None, None
    row = sub.iloc[0]
    d = row.name.date() if hasattr(row.name, "date") else None
    if max_calendar_days is not None and d is not None:
        if (d - target).days > max_calendar_days:
            return None, None
    try:
        return d, float(row["Close"])
    except Exception:
        return d, None


def _close_n_trading_days_after(
    df: "pd.DataFrame", anchor: date, n: int
) -> tuple[date | None, float | None]:
    """Close ``n`` trading days after the trading row at or after ``anchor``.

    Returns ``(None, None)`` if the series does not extend that far yet -
    that's how a "pending" horizon is signalled.
    """
    if df is None or df.empty or "Close" not in df.columns:
        return None, None
    ts = pd.Timestamp(anchor)
    pos = df.index.searchsorted(ts, side="left")
    if pos >= len(df):
        return None, None
    target_pos = pos + n
    if target_pos >= len(df):
        return None, None
    row = df.iloc[target_pos]
    d = row.name.date() if hasattr(row.name, "date") else None
    try:
        return d, float(row["Close"])
    except Exception:
        return d, None


def _empty_horizon() -> dict:
    return {
        "status": "pending",
        "exit_date": None,
        "exit_price": None,
        "benchmark_exit_price": None,
        "forward_return": None,
        "benchmark_forward_return": None,
        "excess_return": None,
        "hit": None,
    }


def _hit_for_direction(direction: str, excess_return: float | None) -> bool | None:
    """Direction-aware "would acting anyway have paid off?" check.

    For a long near-miss, the gate's REJECT looks WRONG (hit=True) when
    excess return over the benchmark is strictly positive. For a short
    near-miss, the relationship inverts. Unrealized horizons return None.
    """
    if excess_return is None:
        return None
    if direction == "short":
        return excess_return < 0
    return excess_return > 0


def _shadow_key(ticker: str, miss_date: str) -> tuple[str, str]:
    return (ticker.upper(), str(miss_date)[:10])


def _iter_near_misses(history: list[dict]) -> Iterable[dict]:
    """Yield ``(ticker, miss_date, failed, passed, insider_score)`` for each
    near-miss across the telemetry history. Skips malformed rows."""
    for row in history or []:
        if not isinstance(row, dict):
            continue
        miss_date = _to_date(row.get("date"))
        if miss_date is None:
            continue
        for nm in row.get("near_miss") or []:
            if not isinstance(nm, dict):
                continue
            ticker = (nm.get("ticker") or "").upper()
            failed = nm.get("failed")
            if not ticker or not failed:
                continue
            yield {
                "ticker": ticker,
                "miss_date": miss_date.isoformat(),
                "failed_signal": failed,
                "passed_signals": list(nm.get("passed") or []),
                "insider_score": int(nm.get("insider_score") or 0),
            }


def _new_record(near_miss: dict) -> dict:
    return {
        "ticker": near_miss["ticker"],
        "miss_date": near_miss["miss_date"],
        "direction": DIRECTION,
        "failed_signal": near_miss["failed_signal"],
        "passed_signals": near_miss["passed_signals"],
        "insider_score": near_miss["insider_score"],
        "entry_price": None,
        "benchmark_entry_price": None,
        "horizons": {str(h): _empty_horizon() for h in HORIZONS},
        "last_updated": None,
    }


def _update_record(
    record: dict,
    ticker_df: "pd.DataFrame | None",
    bench_df: "pd.DataFrame | None",
    today: date,
) -> dict:
    """Fill in entry + horizon prices/returns for one record, in place.

    Idempotency rules (this is what protects realized values from being
    silently rewritten as the price window rolls forward):
      * Entry prices are FROZEN once set: a non-null ``entry_price`` /
        ``benchmark_entry_price`` is never overwritten on a later run.
      * Realized horizons are FROZEN: once a horizon has ``status="realized"``
        it is skipped on every subsequent run.
      * The entry trading day is only accepted if it lies within
        ``ENTRY_TOLERANCE_DAYS`` of the miss date. Outside the tolerance the
        record stays unanchored (entry stays null, all horizons stay pending)
        instead of guessing.
      * If price data is missing, the record stays pending without crashing.
    """
    miss_date = _to_date(record.get("miss_date"))
    if miss_date is None:
        return record

    ticker_df = _normalize_index(ticker_df) if ticker_df is not None else None
    bench_df = _normalize_index(bench_df) if bench_df is not None else None

    entry_date, entry_price_now = _close_on_or_after(
        ticker_df, miss_date, max_calendar_days=ENTRY_TOLERANCE_DAYS)
    bench_entry_date, bench_entry_now = _close_on_or_after(
        bench_df, miss_date, max_calendar_days=ENTRY_TOLERANCE_DAYS)

    # Freeze entry prices: only fill in nulls.
    if record.get("entry_price") is None and entry_price_now is not None:
        record["entry_price"] = entry_price_now
    if record.get("benchmark_entry_price") is None and bench_entry_now is not None:
        record["benchmark_entry_price"] = bench_entry_now

    entry_price = record.get("entry_price")
    bench_entry_price = record.get("benchmark_entry_price")
    # A horizon can only be realized when the current window actually contains
    # the entry row; otherwise we can't safely step forward N trading days.
    has_anchor = entry_date is not None and bench_entry_date is not None

    horizons = record.setdefault("horizons", {})
    for h in HORIZONS:
        slot = horizons.setdefault(str(h), _empty_horizon())
        # Freeze realized horizons.
        if slot.get("status") == "realized":
            continue
        if not has_anchor or entry_price is None or bench_entry_price is None:
            slot["status"] = "pending"
            continue
        exit_date, exit_price = _close_n_trading_days_after(ticker_df, entry_date, h)
        _, bench_exit_price = _close_n_trading_days_after(bench_df, bench_entry_date, h)
        if exit_price is None or bench_exit_price is None:
            slot["status"] = "pending"
            slot["exit_date"] = None
            slot["exit_price"] = None
            slot["benchmark_exit_price"] = None
            slot["forward_return"] = None
            slot["benchmark_forward_return"] = None
            slot["excess_return"] = None
            slot["hit"] = None
            continue
        fwd = (exit_price - entry_price) / entry_price if entry_price else None
        bench_fwd = (
            (bench_exit_price - bench_entry_price) / bench_entry_price
            if bench_entry_price
            else None
        )
        excess = (fwd - bench_fwd) if (fwd is not None and bench_fwd is not None) else None
        slot["status"] = "realized"
        slot["exit_date"] = exit_date.isoformat() if exit_date else None
        slot["exit_price"] = exit_price
        slot["benchmark_exit_price"] = bench_exit_price
        slot["forward_return"] = fwd
        slot["benchmark_forward_return"] = bench_fwd
        slot["excess_return"] = excess
        slot["hit"] = _hit_for_direction(record.get("direction") or DIRECTION, excess)

    record["last_updated"] = today.isoformat()
    return record


def load_ledger(path: Path | None = None) -> list[dict]:
    p = path or LEDGER_PATH
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text()) or []
    except Exception:
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def save_ledger(records: list[dict], path: Path | None = None) -> None:
    p = path or LEDGER_PATH
    try:
        p.write_text(yaml.safe_dump(records, sort_keys=False))
    except Exception:
        log.warning("shadow_tracker: failed to save ledger to %s", p)


def save_calibration(rollup: dict, path: Path | None = None) -> None:
    p = path or CALIBRATION_PATH
    try:
        p.write_text(yaml.safe_dump(rollup, sort_keys=False))
    except Exception:
        log.warning("shadow_tracker: failed to save calibration to %s", p)


def _merge_records(existing: list[dict], near_misses: Iterable[dict]) -> list[dict]:
    """Idempotently fold telemetry near-misses into the existing ledger.

    Rows are keyed by ``(ticker, miss_date)``. New near-misses become new
    rows; already-tracked rows are left untouched here (their horizons are
    refreshed in ``_update_record``). Static metadata on existing rows
    (failed_signal, passed_signals, insider_score) is updated if telemetry
    has been edited.
    """
    by_key: dict[tuple[str, str], dict] = {
        _shadow_key(r.get("ticker", ""), r.get("miss_date", "")): r
        for r in existing
        if r.get("ticker") and r.get("miss_date")
    }
    for nm in near_misses:
        key = _shadow_key(nm["ticker"], nm["miss_date"])
        if key in by_key:
            rec = by_key[key]
            rec["failed_signal"] = nm["failed_signal"]
            rec["passed_signals"] = nm["passed_signals"]
            rec["insider_score"] = nm["insider_score"]
        else:
            by_key[key] = _new_record(nm)
    out = list(by_key.values())
    out.sort(key=lambda r: (r.get("miss_date") or "", r.get("ticker") or ""))
    return out


def _aggregate(records: list[dict]) -> dict:
    """Group realized horizons by failing signal; report per-signal hit rate
    and average excess return at each horizon. ``all`` is the union row."""
    groups: dict[str, list[dict]] = {}
    for r in records:
        sig = r.get("failed_signal") or "unknown"
        groups.setdefault(sig, []).append(r)
        groups.setdefault("all", []).append(r)

    def _slot(rs: list[dict], h: int) -> dict:
        realized = []
        pending = 0
        for r in rs:
            slot = (r.get("horizons") or {}).get(str(h)) or {}
            if slot.get("status") == "realized" and slot.get("excess_return") is not None:
                realized.append(slot)
            else:
                pending += 1
        if not realized:
            return {
                "n_realized": 0,
                "n_pending": pending,
                "hit_rate": None,
                "avg_excess_return": None,
                "avg_forward_return": None,
            }
        hits = sum(1 for s in realized if s.get("hit"))
        avg_ex = sum(float(s["excess_return"]) for s in realized) / len(realized)
        fwds = [float(s["forward_return"]) for s in realized
                if s.get("forward_return") is not None]
        avg_fwd = sum(fwds) / len(fwds) if fwds else None
        return {
            "n_realized": len(realized),
            "n_pending": pending,
            "hit_rate": hits / len(realized),
            "avg_excess_return": avg_ex,
            "avg_forward_return": avg_fwd,
        }

    by_failed_signal: dict[str, dict] = {}
    for sig, rs in groups.items():
        if sig == "all":
            continue
        by_failed_signal[sig] = {
            "count": len(rs),
            "horizons": {str(h): _slot(rs, h) for h in HORIZONS},
        }
    overall = {
        "count": len(records),
        "horizons": {str(h): _slot(groups.get("all", []), h) for h in HORIZONS},
    }
    return {
        "generated_at": _today().isoformat(),
        "horizons": list(HORIZONS),
        "benchmark": BENCHMARK,
        "direction": DIRECTION,
        "total_records": len(records),
        "by_failed_signal": by_failed_signal,
        "overall": overall,
    }


def update(
    telemetry_path: Path | None = None,
    ledger_path: Path | None = None,
    calibration_path: Path | None = None,
    today: date | None = None,
    history_fn: HistoryFn | None = None,
    benchmark: str | None = None,
) -> dict:
    """Refresh the shadow ledger from telemetry and recompute calibration.

    Read-only with respect to the gate: this only loads
    ``gate_telemetry.yaml`` and never writes to it.

    Returns the calibration rollup dict (also written to disk).
    """
    today = today or _today()
    history_fn = history_fn or prices.history
    bench = benchmark or BENCHMARK

    history = gate_telemetry.load(telemetry_path)
    near_misses = list(_iter_near_misses(history))

    existing = load_ledger(ledger_path)
    records = _merge_records(existing, near_misses)

    history_cache: dict[str, "pd.DataFrame | None"] = {}

    def _hist(t: str) -> "pd.DataFrame | None":
        key = t.upper()
        if key not in history_cache:
            try:
                history_cache[key] = history_fn(key)
            except Exception as e:
                log.warning("shadow_tracker: history fetch failed for %s: %s", key, e)
                history_cache[key] = None
        return history_cache[key]

    bench_df = _hist(bench)
    for rec in records:
        ticker_df = _hist(rec.get("ticker", ""))
        _update_record(rec, ticker_df, bench_df, today)

    save_ledger(records, ledger_path)
    rollup = _aggregate(records)
    save_calibration(rollup, calibration_path)
    return rollup


def safe_update(*args, **kwargs) -> dict | None:
    """``update()`` wrapped so a price-data outage or any other failure can
    never break the daily refresh. Returns None on failure."""
    try:
        return update(*args, **kwargs)
    except Exception as e:
        log.warning("shadow_tracker: update failed: %s", e)
        return None
