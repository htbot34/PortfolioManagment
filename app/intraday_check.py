"""Intraday awareness checks. NOT a recommendation engine.

Runs every 30 minutes during US market hours via ``.github/workflows/intraday.yml``,
pulls a tiny data subset (no scanner, no LLM), and writes
``intraday_alerts.json`` at the repo root. The daily build reads that file
and renders a rose banner on ``index.html`` for every alert checked within
the last 90 minutes.

Alert kinds:
- ``macro_shock``       VIX > 25 OR VIX intraday change > +15%, OR SPX
                         intraday change < -2%.
- ``position_break``    a held position gapped down > -5% OR closed below
                         SMA50 on volume > 1.5x its 20-day average.
- ``watchlist_entry``   a top-5 candidate from yesterday's scanner buckets
                         is in its computed buy zone today.

The output schema::

    {
      "checked_at": "2026-05-18T15:30:00Z",
      "alerts": [
        {"severity": "high|med|low",
         "kind":     "macro_shock|position_break|watchlist_entry",
         "text":     "human-readable description"}
      ]
    }

Severity is informational only.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.data import prices
from app.logging import get_logger
from app.portfolio import idea_queue, store

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parent.parent
ALERTS_PATH = ROOT / "intraday_alerts.json"
DATA_JSON_PATH = ROOT / "data.json"


# ---------------------------------------------------------------------------
# Individual checks - each returns a list of alert dicts
# ---------------------------------------------------------------------------

def check_macro() -> list[dict]:
    """SPX + VIX shock detection."""
    out: list[dict] = []
    vix = prices.quote("^VIX", fast=True)
    if vix.price is not None and vix.price > 25:
        out.append({
            "severity": "high", "kind": "macro_shock",
            "text": f"VIX at {vix.price:.1f} (above 25 - elevated fear)",
        })
    if vix.day_change_pct is not None and vix.day_change_pct > 15:
        out.append({
            "severity": "high", "kind": "macro_shock",
            "text": f"VIX spiked {vix.day_change_pct:+.1f}% today",
        })
    spx = prices.quote("^GSPC", fast=True)
    if spx.day_change_pct is not None and spx.day_change_pct < -2:
        out.append({
            "severity": "high", "kind": "macro_shock",
            "text": f"SPX down {spx.day_change_pct:.2f}% today",
        })
    return out


def check_positions(account: store.Account) -> list[dict]:
    """Held positions: gap-down > -5% or SMA50 break on heavy volume."""
    out: list[dict] = []
    for pos in (account.positions or []):
        q = prices.quote(pos.ticker, fast=True)
        if q.day_change_pct is not None and q.day_change_pct < -5:
            out.append({
                "severity": "high", "kind": "position_break",
                "text": f"{pos.ticker} gapped down {q.day_change_pct:.1f}% today",
            })
            continue  # avoid double-firing
        t = prices.technicals(pos.ticker)
        sma50 = t.get("sma50")
        vol_ratio = t.get("vol_ratio_20d")
        price = q.price
        if (sma50 and price is not None and price < sma50
                and vol_ratio is not None and vol_ratio > 1.5):
            out.append({
                "severity": "med", "kind": "position_break",
                "text": (
                    f"{pos.ticker} lost SMA50 (${sma50:.2f}) on "
                    f"{vol_ratio:.1f}x volume"
                ),
            })
    return out


def check_watchlist(data_json_path: Path | None = None) -> list[dict]:
    """Read yesterday's data.json scanner buckets; alert if a top-5 candidate
    enters its computed buy zone today.

    For breakout candidates, the "buy zone" is within 0.5% of the breakout
    level (today's close >= yesterday's price). For oversold candidates, the
    "buy zone" is RSI rising back through 30 today.
    """
    path = data_json_path or DATA_JSON_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        log.debug("intraday: cannot read %s: %s", path, e)
        return []
    scanner = (data or {}).get("scanner") or {}
    buckets = scanner.get("buckets") or {}
    candidates: list[tuple[str, str, float | None]] = []
    for bucket_name in ("breakouts", "oversold_bounces"):
        for row in (buckets.get(bucket_name) or [])[:5]:
            t = row.get("ticker")
            p = row.get("price")
            if t:
                candidates.append((bucket_name, t, p))
    seen: set[str] = set()
    out: list[dict] = []
    for bucket, ticker, yest_price in candidates:
        if ticker in seen:
            continue
        seen.add(ticker)
        q = prices.quote(ticker, fast=True)
        if q.price is None or yest_price is None:
            continue
        if bucket == "breakouts":
            # Breaking out today on top of yesterday's setup.
            if q.price >= yest_price * 0.995:
                out.append({
                    "severity": "low", "kind": "watchlist_entry",
                    "text": (
                        f"{ticker} at ${q.price:.2f} - within buy zone of "
                        f"yesterday's breakout setup (${yest_price:.2f})"
                    ),
                })
        elif bucket == "oversold_bounces":
            t = prices.technicals(ticker)
            rsi = t.get("rsi14")
            if rsi is not None and 30 < rsi <= 35:
                out.append({
                    "severity": "low", "kind": "watchlist_entry",
                    "text": (
                        f"{ticker} RSI {rsi:.0f} - oversold reversal in "
                        f"progress (from yesterday's bucket)"
                    ),
                })
    return out


def check_plan_reconciliation(fired_plans: dict, today_utc: str,
                              queue: list[dict] | None = None) -> list[dict]:
    """Alert when a queued idea's swing-plan entry zone is hit today.

    Considers idea_queue entries with verdict in (open, interested, watching)
    that carry a swing_plan with both entry_low and entry_high. Dedup: a
    (ticker, target) key fires at most once per UTC day. ``fired_plans`` is
    mutated to record new fires.
    """
    queue = queue if queue is not None else idea_queue.load()
    out: list[dict] = []
    for entry in queue or []:
        if entry.get("verdict") not in ("open", "interested", "watching"):
            continue
        plan = entry.get("swing_plan") or {}
        entry_low = plan.get("entry_low")
        entry_high = plan.get("entry_high")
        target = plan.get("target")
        if entry_low is None or entry_high is None or target is None:
            continue
        ticker = (entry.get("ticker") or "").upper()
        if not ticker:
            continue
        key = f"{ticker}|{round(float(target), 2)}"
        if fired_plans.get(key) == today_utc:
            continue
        try:
            q = prices.quote(ticker, fast=True)
        except Exception:
            continue
        if q.price is None:
            continue
        if not (float(entry_low) <= q.price <= float(entry_high)):
            continue
        stop = plan.get("stop")
        hold = plan.get("hold_window") or ""
        stop_txt = f"${float(stop):.2f}" if stop is not None else "n/a"
        out.append({
            "severity": "med", "kind": "watchlist_entry",
            "text": (
                f"{ticker} in entry zone ${float(entry_low):.2f}-"
                f"${float(entry_high):.2f} (plan: stop {stop_txt}, "
                f"target ${float(target):.2f}, hold {hold})"
            ),
        })
        fired_plans[key] = today_utc
    return out


def _load_fired_plans(today_utc: str) -> dict:
    """Read fired_plans from the existing alerts file, filtered to today."""
    if not ALERTS_PATH.exists():
        return {}
    try:
        data = json.loads(ALERTS_PATH.read_text())
    except Exception:
        return {}
    fp = data.get("fired_plans") or {}
    return {k: v for k, v in fp.items() if v == today_utc}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run() -> dict:
    """Run all checks and return the payload that will be written to disk."""
    alerts: list[dict] = []
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fired_plans = _load_fired_plans(today_utc)
    try:
        alerts.extend(check_macro())
    except Exception:
        log.exception("intraday macro check failed")
    try:
        alerts.extend(check_positions(store.load()))
    except Exception:
        log.exception("intraday positions check failed")
    try:
        alerts.extend(check_watchlist())
    except Exception:
        log.exception("intraday watchlist check failed")
    try:
        alerts.extend(check_plan_reconciliation(fired_plans, today_utc))
    except Exception:
        log.exception("intraday plan reconciliation failed")
    return {
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "alerts": alerts,
        "fired_plans": fired_plans,
    }


def main() -> int:
    payload = run()
    ALERTS_PATH.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {len(payload['alerts'])} alert(s) to {ALERTS_PATH.name}")
    for a in payload["alerts"]:
        print(f"  [{a['severity']}] {a['kind']}: {a['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
