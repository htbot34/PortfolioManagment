"""Parse a Webull positions CSV export into portfolio.yaml.

Webull's Account > Positions > Export produces a CSV with columns that have
varied slightly over time. This parser is forgiving: it looks for columns by
fuzzy name match.
"""
import csv
from pathlib import Path

from app.portfolio.store import Account, Position, save


_TICKER_KEYS = ("symbol", "ticker")
_SHARES_KEYS = ("quantity", "shares", "qty")
_COST_KEYS = ("cost basis", "avg cost", "average cost", "cost")


def _find(row: dict, candidates: tuple[str, ...]) -> str | None:
    norm = {k.strip().lower(): v for k, v in row.items() if k}
    for cand in candidates:
        if cand in norm:
            return norm[cand]
    for key, val in norm.items():
        for cand in candidates:
            if cand in key:
                return val
    return None


def parse_csv(path: Path) -> list[Position]:
    positions: list[Position] = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticker = _find(row, _TICKER_KEYS)
            shares_raw = _find(row, _SHARES_KEYS)
            cost_raw = _find(row, _COST_KEYS)
            if not (ticker and shares_raw and cost_raw):
                continue
            try:
                shares = float(str(shares_raw).replace(",", ""))
                cost = float(str(cost_raw).replace("$", "").replace(",", ""))
            except ValueError:
                continue
            if shares <= 0:
                continue
            positions.append(Position(ticker=ticker.upper().strip(), shares=shares, cost_basis=cost))
    return positions


def import_csv(csv_path: Path, account: Account) -> Account:
    account.positions = parse_csv(csv_path)
    save(account)
    return account
