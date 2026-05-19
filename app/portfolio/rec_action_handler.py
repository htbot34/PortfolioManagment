"""Handle rec-action GitHub issues (accept / reject / counter).

Triggered by ``.github/workflows/process_trade.yml`` when an issue is opened
with the ``rec-action`` label. Reads the issue body for the structured fields
(rec_id, executed_price, executed_shares, reason, counter_action,
counter_shares).

- Accept   -> updates ``rec_history.yaml`` AND mutates ``portfolio.yaml`` via
              ``trade_log.apply`` (weighted-average cost basis on buys,
              full-sell removes the position; cash adjusted by
              executed_price * executed_shares). Trade also appended to
              ``portfolio_history.yaml`` for the activity feed.
- Reject   -> rec_history only (status + user_reason).
- Counter  -> rec_history only (status + counter_proposal).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from app.portfolio import rec_history, store, trade_log


# Field labels from the issue forms; case-insensitive heading match.
_FIELDS = {
    "rec_id": ("Rec ID",),
    "executed_price": ("Executed price (per share)", "Executed price"),
    "executed_shares": ("Executed shares",),
    "reason": ("Reason",),
    "counter_action": ("Counter action",),
    "counter_shares": ("Counter shares",),
    "notes": ("Notes",),
}


def _section(body: str, label: str) -> str:
    pattern = rf"###\s*{re.escape(label)}\s*\n+([\s\S]*?)(?=\n###|\Z)"
    m = re.search(pattern, body)
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


def _f(v: str) -> float | None:
    if not v:
        return None
    try:
        return float(v.replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def classify(title: str, body: str) -> str:
    """Infer action (accept | reject | counter) from issue title or body shape."""
    t = (title or "").lower()
    if "accept" in t:
        return "accept"
    if "reject" in t:
        return "reject"
    if "counter" in t:
        return "counter"
    # Fallback: presence of executed fields => accept; counter_action => counter.
    if _read_field(body, "counter_action"):
        return "counter"
    if _read_field(body, "executed_price") or _read_field(body, "executed_shares"):
        return "accept"
    return "reject"


def apply_from_issue(title: str, body: str) -> dict:
    """Return ``{kind, rec_id, summary, entry}`` after applying to rec_history."""
    rec_id = _read_field(body, "rec_id")
    if not rec_id:
        raise ValueError("missing rec_id")
    kind = classify(title, body)
    if kind == "accept":
        price = _f(_read_field(body, "executed_price"))
        shares = _f(_read_field(body, "executed_shares"))
        if price is None or shares is None:
            raise ValueError("accept requires executed_price and executed_shares")
        # Update rec_history first so we can read the rec's ticker + action.
        entry = rec_history.update_status(
            rec_id, "accepted",
            executed_price=price, executed_shares=shares,
            user_reason=_read_field(body, "notes") or None,
        )
        if entry is None:
            raise ValueError(f"rec_id {rec_id} not found")
        # Translate the rec action into a Trade and mutate portfolio.yaml.
        rec_action = (entry.get("action") or "").lower()
        if rec_action in ("buy", "add", "new_buy"):
            trade_action = "BUY"
        elif rec_action in ("trim", "sell"):
            trade_action = "SELL"
        else:
            # hold / watch / unknown -> log only, no portfolio mutation.
            summary = (
                f"Accepted {entry.get('ticker')} {entry.get('action')} "
                f"(no portfolio mutation for action '{rec_action}')"
            )
            return {"kind": kind, "rec_id": rec_id, "summary": summary, "entry": entry}
        trade = trade_log.Trade(
            action=trade_action, ticker=entry.get("ticker"),
            shares=shares, price=price, amount=None,
            trade_date=entry.get("date") or "",
            notes=f"Accepted rec {rec_id}",
        )
        account = store.load()
        account, mutate_summary = trade_log.apply(trade, account)
        store.save(account)
        trade_log.append_history(trade, mutate_summary)
        summary = (
            f"Accepted {entry.get('ticker')} {entry.get('action')} -> "
            f"{shares:g} shares @ ${price:.2f}. {mutate_summary}"
        )
    elif kind == "reject":
        reason = _read_field(body, "reason")
        if not reason:
            raise ValueError("reject requires a reason")
        entry = rec_history.update_status(rec_id, "rejected", user_reason=reason)
        if entry is None:
            raise ValueError(f"rec_id {rec_id} not found")
        summary = f"Rejected {entry.get('ticker')} {entry.get('action')}: {reason[:80]}"
    elif kind == "counter":
        cp = {
            "action": _read_field(body, "counter_action") or None,
            "shares": _f(_read_field(body, "counter_shares")),
            "reason": _read_field(body, "reason") or None,
        }
        if not (cp["action"] or cp["shares"]):
            raise ValueError("counter requires action and/or shares")
        entry = rec_history.update_status(
            rec_id, "counter",
            counter_proposal=cp,
            user_reason=cp["reason"],
        )
        if entry is None:
            raise ValueError(f"rec_id {rec_id} not found")
        summary = f"Counter on {entry.get('ticker')}: {cp.get('action') or entry.get('action')}"
    else:
        raise ValueError(f"unknown rec-action kind: {kind}")
    return {"kind": kind, "rec_id": rec_id, "summary": summary, "entry": entry}


def _write_output(key: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    delim = "EOF_REC_ACTION"
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
    _write_output("kind", result["kind"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
