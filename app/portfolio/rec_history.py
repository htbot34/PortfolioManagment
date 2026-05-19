"""Recommendation history store.

Persists every rec the brief produces so the user can later Accept / Reject /
Counter it, and so future builds can learn from what the user has already
turned down. The file is committed (not gitignored) because the static-site
workflow runs in a fresh checkout each time and there is no other server-side
state.

Schema (a list of dicts, newest-last):

    - rec_id: ab12cd34
      date: 2026-05-18
      ticker: META
      action: trim
      size:
        display: "Trim 30% -> 3 shares -> $1,800 freed"
        shares: 3
        dollars: 1800
      status: pending | accepted | rejected | counter
      user_reason: null            # set on reject / counter
      counter_proposal: null       # { action, size, reason } on counter
      executed_price: null         # set on accept
      executed_shares: null        # set on accept
      resolved_at: null            # ISO8601 UTC; set on any non-pending status
      created_at: 2026-05-18T11:00:00Z
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml


HISTORY_PATH = Path(__file__).resolve().parent.parent.parent / "rec_history.yaml"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path: Path | None = None) -> list[dict]:
    """Return the full history list (newest-last). Empty list when missing."""
    p = path or HISTORY_PATH
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text()) or []
    except Exception:
        return []
    return data if isinstance(data, list) else []


def save(history: list[dict], path: Path | None = None) -> None:
    p = path or HISTORY_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(history, sort_keys=False))


def _extract_recs_from_brief(brief: dict) -> list[dict]:
    out: list[dict] = []
    pa = (brief or {}).get("primary_action")
    if pa:
        out.append(pa)
    out.extend((brief or {}).get("secondary_actions") or [])
    return [r for r in out if r and r.get("rec_id")]


def _new_entry(rec: dict, today: str | None = None) -> dict:
    size = rec.get("size") or {}
    return {
        "rec_id": rec.get("rec_id"),
        "date": today or date.today().isoformat(),
        "ticker": rec.get("ticker"),
        "action": rec.get("action"),
        "size": {
            "display": size.get("display") or "",
            "shares": size.get("shares") or rec.get("shares"),
            "dollars": size.get("dollars") or rec.get("dollars"),
        },
        "status": "pending",
        "user_reason": None,
        "counter_proposal": None,
        "executed_price": None,
        "executed_shares": None,
        "resolved_at": None,
        "created_at": _now_iso(),
    }


def record_pending(brief: dict, path: Path | None = None) -> list[dict]:
    """Append today's brief recs to history with ``status: pending``.

    Idempotent: an entry with the same ``rec_id`` is left untouched (whether
    it's still pending or already resolved). Returns the newly added entries.
    """
    history = load(path)
    seen = {e.get("rec_id") for e in history if e.get("rec_id")}
    added: list[dict] = []
    for rec in _extract_recs_from_brief(brief):
        if rec.get("rec_id") in seen:
            continue
        entry = _new_entry(rec)
        history.append(entry)
        added.append(entry)
    if added:
        save(history, path)
    return added


def find(rec_id: str, history: list[dict] | None = None,
         path: Path | None = None) -> dict | None:
    h = history if history is not None else load(path)
    for e in h:
        if e.get("rec_id") == rec_id:
            return e
    return None


def update_status(
    rec_id: str,
    status: str,
    *,
    user_reason: str | None = None,
    counter_proposal: dict | None = None,
    executed_price: float | None = None,
    executed_shares: float | None = None,
    path: Path | None = None,
) -> dict | None:
    """Mark an existing rec with a new status. Returns the updated entry."""
    if status not in ("accepted", "rejected", "counter", "pending"):
        raise ValueError(f"unknown status: {status!r}")
    history = load(path)
    entry = find(rec_id, history)
    if entry is None:
        return None
    entry["status"] = status
    entry["resolved_at"] = _now_iso() if status != "pending" else None
    if user_reason is not None:
        entry["user_reason"] = user_reason
    if counter_proposal is not None:
        entry["counter_proposal"] = counter_proposal
    if executed_price is not None:
        entry["executed_price"] = float(executed_price)
    if executed_shares is not None:
        entry["executed_shares"] = float(executed_shares)
    save(history, path)
    return entry


def recent(days: int = 30, history: list[dict] | None = None,
           path: Path | None = None) -> list[dict]:
    h = history if history is not None else load(path)
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    return [e for e in h if (e.get("date") or "") >= cutoff]


def pending(history: list[dict] | None = None,
            path: Path | None = None) -> list[dict]:
    h = history if history is not None else load(path)
    return [e for e in h if e.get("status") == "pending"]
