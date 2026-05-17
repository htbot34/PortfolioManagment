"""Parse a GitHub Issue body and apply the result to the portfolio.

Two input modes:

  1. Structured (issue-form mode) - the body looks like:
       ### Action
       BUY
       ### Ticker
       NVDA
       ...
     Handled by parse() which extracts each ### section.

  2. Freeform (chat mode) - the body is plain English:
       "Bought 3 NVDA at 145.50"
       "Sold 4 META at 614"
       "Deposited 500"
       "Watching PLTR for a $35 entry"
     Handled by parse_freeform() with regex. If no trade pattern matches,
     the message is saved as a note in notes.yaml.

  apply()/append_note()/main() handle persistence and history.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from app.config import settings
from app.portfolio import store


HISTORY_PATH = Path(__file__).resolve().parent.parent.parent / "portfolio_history.yaml"
NOTES_PATH = Path(__file__).resolve().parent.parent.parent / "notes.yaml"

_FIELD_HEADERS = {
    "action": "Action",
    "ticker": "Ticker",
    "shares": "Shares",
    "price": "Fill price (per share)",
    "amount": "Cash amount",
    "date": "Date",
    "notes": "Notes",
}

# Freeform patterns (case-insensitive). Each pattern captures named groups.
_BUY_RE = re.compile(
    r"\b(?:bought|buy|added|picked up|grabbed|opened)\s+"
    r"(?P<shares>\d+(?:\.\d+)?)\s+"
    r"(?:shares?\s+of\s+)?(?P<ticker>[A-Za-z]{1,5})\s+"
    r"(?:at|@|for|around|near)\s*"
    r"\$?(?P<price>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_SELL_RE = re.compile(
    r"\b(?:sold|sell|trimmed|trim|dumped|exited|closed)\s+"
    r"(?P<shares>\d+(?:\.\d+)?)\s+"
    r"(?:shares?\s+of\s+)?(?P<ticker>[A-Za-z]{1,5})\s+"
    r"(?:at|@|for|around|near)\s*"
    r"\$?(?P<price>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_DEPOSIT_RE = re.compile(
    r"\b(?:deposit(?:ed)?|added\s+cash|put\s+in|funded|wired\s+in)\s*"
    r"\$?(?P<amount>\d+(?:[\.,]\d+)?)",
    re.IGNORECASE,
)
_WITHDRAW_RE = re.compile(
    r"\b(?:withdrew|withdraw|withdrawal\s+of|took\s+out|pulled\s+out)\s*"
    r"\$?(?P<amount>\d+(?:[\.,]\d+)?)",
    re.IGNORECASE,
)


@dataclass
class Trade:
    action: str            # BUY | SELL | DEPOSIT | WITHDRAW
    ticker: Optional[str]
    shares: Optional[float]
    price: Optional[float]
    amount: Optional[float]
    trade_date: str
    notes: str = ""


def _section(body: str, label: str) -> str:
    pattern = rf"###\s*{re.escape(label)}\s*\n+([\s\S]*?)(?=\n###|\Z)"
    m = re.search(pattern, body)
    if not m:
        return ""
    val = m.group(1).strip()
    if val.lower() in ("_no response_", "no response", "n/a", "-", "none"):
        return ""
    return val


def _is_structured(body: str) -> bool:
    return bool(re.search(r"###\s*Action\s*\n", body))


def parse(body: str) -> Trade:
    raw = {k: _section(body, hdr) for k, hdr in _FIELD_HEADERS.items()}
    action = (raw["action"] or "").strip().upper()
    if action not in ("BUY", "SELL", "DEPOSIT", "WITHDRAW"):
        raise ValueError(f"Action must be BUY / SELL / DEPOSIT / WITHDRAW (got '{action}')")

    def _f(v: str) -> Optional[float]:
        if not v:
            return None
        v = v.replace("$", "").replace(",", "").strip()
        try:
            return float(v)
        except ValueError:
            return None

    ticker = (raw["ticker"] or "").strip().upper() or None
    shares = _f(raw["shares"])
    price = _f(raw["price"])
    amount = _f(raw["amount"])
    trade_date = raw["date"] or date_cls.today().isoformat()
    try:
        datetime.strptime(trade_date, "%Y-%m-%d")
    except ValueError:
        trade_date = date_cls.today().isoformat()
    notes = raw["notes"]

    if action in ("BUY", "SELL"):
        if not ticker or not shares or not price:
            raise ValueError("BUY / SELL requires ticker, shares, and price")
    elif action in ("DEPOSIT", "WITHDRAW"):
        if not amount:
            raise ValueError("DEPOSIT / WITHDRAW requires amount")

    return Trade(action=action, ticker=ticker, shares=shares, price=price,
                 amount=amount, trade_date=trade_date, notes=notes)


def parse_freeform(text: str) -> Trade | None:
    """Try to extract a trade from natural English. Return None if not a trade."""
    today = date_cls.today().isoformat()
    m = _BUY_RE.search(text)
    if m:
        return Trade("BUY", m.group("ticker").upper(), float(m.group("shares")),
                     float(m.group("price")), None, today, text.strip())
    m = _SELL_RE.search(text)
    if m:
        return Trade("SELL", m.group("ticker").upper(), float(m.group("shares")),
                     float(m.group("price")), None, today, text.strip())
    m = _DEPOSIT_RE.search(text)
    if m:
        amt = float(m.group("amount").replace(",", ""))
        return Trade("DEPOSIT", None, None, None, amt, today, text.strip())
    m = _WITHDRAW_RE.search(text)
    if m:
        amt = float(m.group("amount").replace(",", ""))
        return Trade("WITHDRAW", None, None, None, amt, today, text.strip())
    return None


def apply(trade: Trade, account: store.Account) -> tuple[store.Account, str]:
    if trade.action == "BUY":
        cost_delta = trade.shares * trade.price
        if account.cash < cost_delta:
            raise ValueError(
                f"Not enough cash: trade costs ${cost_delta:,.2f}, account has ${account.cash:,.2f}. "
                "Log a DEPOSIT first, or adjust the trade size."
            )
        pos = account.position(trade.ticker)
        if pos:
            new_shares = pos.shares + trade.shares
            new_cost = ((pos.shares * pos.cost_basis) + cost_delta) / new_shares
            pos.shares = new_shares
            pos.cost_basis = round(new_cost, 4)
        else:
            account.positions.append(store.Position(
                ticker=trade.ticker, shares=trade.shares, cost_basis=trade.price,
            ))
        account.cash -= cost_delta
        summary = (
            f"BUY {trade.shares} {trade.ticker} @ ${trade.price:.2f} "
            f"(spent ${cost_delta:,.2f}, cash now ${account.cash:,.2f})"
        )
    elif trade.action == "SELL":
        pos = account.position(trade.ticker)
        if not pos:
            raise ValueError(f"No existing position in {trade.ticker} to sell")
        if trade.shares > pos.shares + 1e-9:
            raise ValueError(
                f"Trying to sell {trade.shares} of {trade.ticker} but only own {pos.shares}"
            )
        proceeds = trade.shares * trade.price
        pos.shares = round(pos.shares - trade.shares, 6)
        if pos.shares <= 1e-9:
            account.positions = [p for p in account.positions if p.ticker != trade.ticker]
        account.cash += proceeds
        summary = (
            f"SELL {trade.shares} {trade.ticker} @ ${trade.price:.2f} "
            f"(proceeds ${proceeds:,.2f}, cash now ${account.cash:,.2f})"
        )
    elif trade.action == "DEPOSIT":
        account.cash += trade.amount
        summary = f"DEPOSIT ${trade.amount:,.2f} (cash now ${account.cash:,.2f})"
    elif trade.action == "WITHDRAW":
        if account.cash < trade.amount:
            raise ValueError(
                f"Withdraw ${trade.amount:,.2f} > cash ${account.cash:,.2f}"
            )
        account.cash -= trade.amount
        summary = f"WITHDRAW ${trade.amount:,.2f} (cash now ${account.cash:,.2f})"
    else:
        raise ValueError(f"Unknown action: {trade.action}")
    return account, summary


def append_history(trade: Trade, summary: str) -> None:
    history: list = []
    if HISTORY_PATH.exists():
        try:
            history = yaml.safe_load(HISTORY_PATH.read_text()) or []
        except Exception:
            history = []
    history.append({
        "date": trade.trade_date,
        "action": trade.action,
        "ticker": trade.ticker,
        "shares": trade.shares,
        "price": trade.price,
        "amount": trade.amount,
        "notes": trade.notes,
        "summary": summary,
        "logged_at": datetime.utcnow().isoformat() + "Z",
    })
    HISTORY_PATH.write_text(yaml.safe_dump(history, sort_keys=False))


def append_note(text: str) -> str:
    """Save a freeform note (idea / observation) to notes.yaml."""
    notes: list = []
    if NOTES_PATH.exists():
        try:
            notes = yaml.safe_load(NOTES_PATH.read_text()) or []
        except Exception:
            notes = []
    if not isinstance(notes, list):
        notes = []
    entry = {
        "date": date_cls.today().isoformat(),
        "content": text.strip(),
        "logged_at": datetime.utcnow().isoformat() + "Z",
    }
    notes.append(entry)
    NOTES_PATH.write_text(yaml.safe_dump(notes, sort_keys=False))
    return f'Saved as note: "{text.strip()[:120]}"'


def main() -> int:
    """Workflow entrypoint. Accepts structured OR freeform bodies."""
    body = os.environ.get("ISSUE_BODY", "")
    if not body:
        print("ISSUE_BODY empty; nothing to do.")
        _write_output("error", "empty body")
        return 1

    # Decide which parser to use
    trade: Trade | None = None
    err: str | None = None
    if _is_structured(body):
        try:
            trade = parse(body)
        except ValueError as e:
            err = str(e)
    else:
        trade = parse_freeform(body)

    if trade is None:
        # Freeform with no trade pattern -> save as note
        summary = append_note(body)
        print(f"Applied: {summary}")
        _write_output("summary", summary)
        return 0

    if err:
        print(f"::error::Could not parse structured trade: {err}")
        _write_output("error", err)
        return 2

    account = store.load()
    try:
        account, summary = apply(trade, account)
    except ValueError as e:
        print(f"::error::Could not apply trade: {e}")
        _write_output("error", str(e))
        return 3
    store.save(account)
    append_history(trade, summary)
    print(f"Applied: {summary}")
    _write_output("summary", summary)
    return 0


def _write_output(key: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    delim = "EOF_TRADE_LOG"
    with open(out, "a") as f:
        f.write(f"{key}<<{delim}\n{value}\n{delim}\n")


if __name__ == "__main__":
    sys.exit(main())
