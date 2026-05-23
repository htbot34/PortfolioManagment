"""Persistent ranked-idea queue -- the spine of the idea conversation loop.

The idea funnel produces a ranked list every build. This store persists those
ideas across builds keyed by ticker, carries the user's verdict on each
(interested / watching / pass), and feeds the verdicts back into ranking so the
funnel learns from what the user engages with.

The file is committed (like ``rec_history.yaml``) because the static-site
workflow runs in a fresh checkout with no other server-side state.

It never touches ``portfolio.yaml`` -- a verdict is queue metadata only.

Schema (a list of dicts, newest-last):

    - ticker: PLTR
      idea_id: ab12cd34          # sha1(ticker)[:8] -- stable display id
      first_seen: 2026-05-21
      last_seen: 2026-05-21
      last_rank: 3
      last_score: 8.4
      last_why: "Momentum & insider buying -- 2 signals aligned"
      verdict: open              # open | interested | watching | pass
      verdict_at: null           # ISO8601 UTC; set when a verdict is given
      user_note: null
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

QUEUE_PATH = Path(__file__).resolve().parent.parent.parent / "idea_queue.yaml"

VALID_VERDICTS = ("open", "interested", "watching", "pass")
# verdicts that change ranking / are surfaced as a decision
_DECIDED = ("interested", "watching", "pass")


def idea_id(ticker: str) -> str:
    return hashlib.sha1((ticker or "").upper().encode()).hexdigest()[:8]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path: Path | None = None) -> list[dict]:
    p = path or QUEUE_PATH
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text()) or []
    except Exception:
        return []
    return data if isinstance(data, list) else []


def save(queue: list[dict], path: Path | None = None) -> None:
    p = path or QUEUE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(queue, sort_keys=False))


def find(ticker: str, queue: list[dict]) -> dict | None:
    t = (ticker or "").upper()
    for e in queue:
        if (e.get("ticker") or "").upper() == t:
            return e
    return None


def _bare_entry(ticker: str) -> dict:
    today = date.today().isoformat()
    return {
        "ticker": ticker.upper(),
        "idea_id": idea_id(ticker),
        "first_seen": today,
        "last_seen": today,
        "last_rank": None,
        "last_score": None,
        "last_why": None,
        "swing_plan": None,
        "verdict": "open",
        "verdict_at": None,
        "user_note": None,
    }


def verdict_map(queue: list[dict]) -> dict[str, str]:
    """Ticker -> verdict, for decided verdicts only (drives funnel ranking)."""
    return {(e.get("ticker") or "").upper(): e["verdict"]
            for e in queue
            if e.get("verdict") in _DECIDED}


def sync_from_funnel(ideas: list[dict], queue: list[dict] | None = None,
                     path: Path | None = None) -> list[dict]:
    """Upsert today's funnel ideas into the queue, then persist.

    Refreshes rank / score / why and ``last_seen`` for each idea. Verdict
    fields are never touched here -- a ``pass`` stays a ``pass``. Queue entries
    for tickers not in today's funnel are kept untouched (so a verdict on a
    name that has dropped off the funnel survives).
    """
    queue = list(queue if queue is not None else load(path))
    today = date.today().isoformat()
    for idea in ideas or []:
        ticker = (idea.get("ticker") or "").upper()
        if not ticker:
            continue
        entry = find(ticker, queue)
        if entry is None:
            entry = _bare_entry(ticker)
            queue.append(entry)
        entry["last_seen"] = today
        entry["last_rank"] = idea.get("rank")
        entry["last_score"] = idea.get("score")
        entry["last_why"] = idea.get("why")
        # Carry the latest swing plan so the intraday check can reconcile it
        # against today's price without re-running the funnel.
        entry["swing_plan"] = idea.get("swing_plan")
    save(queue, path)
    return queue


def _days_ago(date_str: str | None, today: date) -> int | None:
    """Calendar-day distance from ``date_str`` (date or ISO timestamp) to
    ``today``. Returns None when the string is missing or unparseable."""
    if not date_str:
        return None
    try:
        d = date.fromisoformat(str(date_str)[:10])
    except Exception:
        return None
    return (today - d).days


def prune(queue: list[dict], todays_funnel_tickers: set[str],
          today: date) -> tuple[list[dict], dict]:
    """Age the queue so it doesn't accumulate forever.

    Rules:
      - ``open`` with ``last_seen`` older than 14 days AND not in today's
        funnel  -> dropped.
      - ``pass`` with ``verdict_at`` older than 90 days -> reset to ``open``
        with ``verdict_at=None`` (so the funnel can re-promote it). The
        ``user_note`` is left untouched.
      - ``interested`` / ``watching`` -> never auto-expire.

    Returns the pruned queue and stats: ``{"dropped_open", "expired_pass"}``.
    """
    funnel = {(t or "").upper() for t in (todays_funnel_tickers or set())}
    out: list[dict] = []
    dropped_open = 0
    expired_pass = 0
    for entry in queue or []:
        ticker = (entry.get("ticker") or "").upper()
        verdict = entry.get("verdict") or "open"
        if verdict == "open":
            age = _days_ago(entry.get("last_seen"), today)
            if age is not None and age > 14 and ticker not in funnel:
                dropped_open += 1
                continue
            out.append(entry)
        elif verdict == "pass":
            age = _days_ago(entry.get("verdict_at"), today)
            if age is not None and age > 90:
                e = dict(entry)
                e["verdict"] = "open"
                e["verdict_at"] = None
                out.append(e)
                expired_pass += 1
            else:
                out.append(entry)
        else:
            # interested / watching - never auto-expire
            out.append(entry)
    return out, {"dropped_open": dropped_open, "expired_pass": expired_pass}


def set_verdict(ticker: str, verdict: str, note: str | None = None,
                path: Path | None = None) -> dict:
    """Record a user verdict on an idea. Creates the entry if the ticker is not
    already in the queue (the user may rule on a name off today's funnel)."""
    verdict = (verdict or "").strip().lower()
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}; expected one of {VALID_VERDICTS}")
    queue = load(path)
    entry = find(ticker, queue)
    if entry is None:
        entry = _bare_entry(ticker)
        queue.append(entry)
    entry["verdict"] = verdict
    entry["verdict_at"] = _now_iso()
    if note:
        entry["user_note"] = note.strip()
    save(queue, path)
    return entry
