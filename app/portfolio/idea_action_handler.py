"""Handle idea-action GitHub issues -- the idea conversation loop.

Triggered by ``.github/workflows/process_trade.yml`` when an issue is opened
with the ``idea-action`` label. The user gives a verdict on a funnel idea
(interested / watching / pass) and an optional note; this records it to
``idea_queue.yaml``. The next build feeds the verdict back into funnel ranking.

This handler never mutates ``portfolio.yaml`` -- a verdict is advisory metadata.
"""
from __future__ import annotations

import os
import re
import sys

from app.portfolio import idea_queue

_FIELDS = {
    "ticker": ("Ticker",),
    "verdict": ("Verdict",),
    "note": ("Note", "Notes"),
}


def _section(body: str, label: str) -> str:
    pattern = rf"###\s*{re.escape(label)}\s*\n+([\s\S]*?)(?=\n###|\Z)"
    m = re.search(pattern, body or "")
    if not m:
        return ""
    val = m.group(1).strip()
    if val.lower() in ("_no response_", "no response", "n/a", "-", "none"):
        return ""
    return val


def _read_field(body: str, key: str) -> str:
    for label in _FIELDS[key]:
        v = _section(body, label)
        if v:
            return v
    return ""


def _normalize_verdict(raw: str) -> str:
    v = (raw or "").strip().lower()
    # tolerate phrasings the dropdown / free text might produce
    if v.startswith("interest"):
        return "interested"
    if v.startswith("watch"):
        return "watching"
    if v.startswith("pass") or v in ("skip", "no", "reject"):
        return "pass"
    return v


def apply_from_issue(title: str, body: str) -> dict:
    """Return ``{ticker, verdict, summary, entry}`` after recording the verdict."""
    ticker = _read_field(body, "ticker").upper()
    if not ticker:
        raise ValueError("missing ticker")
    if not re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", ticker):
        raise ValueError(f"invalid ticker {ticker!r}")
    verdict = _normalize_verdict(_read_field(body, "verdict"))
    if verdict not in idea_queue.VALID_VERDICTS or verdict == "open":
        raise ValueError(
            "verdict must be one of: interested, watching, pass")
    note = _read_field(body, "note") or None
    entry = idea_queue.set_verdict(ticker, verdict, note)
    summary = f"Idea {ticker}: verdict '{verdict}'"
    if note:
        summary += f" -- {note[:80]}"
    return {"ticker": ticker, "verdict": verdict, "summary": summary, "entry": entry}


def _write_output(key: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    delim = "EOF_IDEA_ACTION"
    with open(out, "a") as f:
        f.write(f"{key}<<{delim}\n{value}\n{delim}\n")


def main() -> int:
    title = os.environ.get("ISSUE_TITLE", "")
    body = os.environ.get("ISSUE_BODY", "")
    if not body:
        print("::error::empty ISSUE_BODY")
        _write_output("error", "empty body")
        return 1
    try:
        result = apply_from_issue(title, body)
    except ValueError as e:
        print(f"::error::{e}")
        _write_output("error", str(e))
        return 2
    print(f"Applied: {result['summary']}")
    _write_output("summary", result["summary"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
