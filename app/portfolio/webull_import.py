"""Parse a Webull positions CSV export into portfolio.yaml.

Webull's Account > Positions > Export produces a CSV with columns that have
varied slightly over time. This parser is forgiving: it looks for columns by
fuzzy name match.

Safety: ``import_csv`` defaults to a **dry run** that returns a diff
(added / removed / changed positions) WITHOUT writing portfolio.yaml. Pass
``confirm=True`` to actually save. This prevents accidental overwrites of
the live portfolio from a partial / stale CSV.
"""
import csv
from dataclasses import dataclass, field
from pathlib import Path

from app.portfolio.store import Account, Position, save


_TICKER_KEYS = ("symbol", "ticker")
_SHARES_KEYS = ("quantity", "shares", "qty")
_COST_KEYS = ("cost basis", "avg cost", "average cost", "cost")


@dataclass
class PositionDiff:
    """Per-ticker change between current portfolio and the imported CSV."""
    ticker: str
    kind: str   # "added" | "removed" | "changed" | "unchanged"
    before: dict | None = None
    after: dict | None = None


@dataclass
class ImportResult:
    """Outcome of an ``import_csv`` call.

    ``saved`` is True only when the caller passed ``confirm=True`` AND the
    diff produced at least one change. Inspect ``diff`` to see what would
    happen before committing.
    """
    positions: list[Position]
    diff: list[PositionDiff] = field(default_factory=list)
    saved: bool = False

    @property
    def added(self) -> list[PositionDiff]:
        return [d for d in self.diff if d.kind == "added"]

    @property
    def removed(self) -> list[PositionDiff]:
        return [d for d in self.diff if d.kind == "removed"]

    @property
    def changed(self) -> list[PositionDiff]:
        return [d for d in self.diff if d.kind == "changed"]

    def summary_line(self) -> str:
        a, r, c = len(self.added), len(self.removed), len(self.changed)
        return f"{a} added, {r} removed, {c} changed (saved={self.saved})"


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
    """Parse the CSV file at ``path`` into a list of Position objects.

    Rows missing any of ticker / shares / cost are skipped. Shares strings
    with commas and cost strings with ``$`` / commas are normalized.
    """
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


def diff_positions(current: list[Position], incoming: list[Position]) -> list[PositionDiff]:
    """Compute per-ticker diff. Returns one record per ticker in either list."""
    cur_map = {p.ticker.upper(): p for p in current}
    inc_map = {p.ticker.upper(): p for p in incoming}
    out: list[PositionDiff] = []
    for ticker in sorted(set(cur_map) | set(inc_map)):
        cur = cur_map.get(ticker)
        inc = inc_map.get(ticker)
        if cur and not inc:
            out.append(PositionDiff(ticker, "removed",
                                     before={"shares": cur.shares, "cost_basis": cur.cost_basis}))
        elif inc and not cur:
            out.append(PositionDiff(ticker, "added",
                                     after={"shares": inc.shares, "cost_basis": inc.cost_basis}))
        elif cur and inc:
            if abs(cur.shares - inc.shares) > 1e-9 or abs(cur.cost_basis - inc.cost_basis) > 1e-4:
                out.append(PositionDiff(
                    ticker, "changed",
                    before={"shares": cur.shares, "cost_basis": cur.cost_basis},
                    after={"shares": inc.shares, "cost_basis": inc.cost_basis},
                ))
            else:
                out.append(PositionDiff(ticker, "unchanged",
                                         before={"shares": cur.shares, "cost_basis": cur.cost_basis}))
    return out


def import_csv(csv_path: Path, account: Account, confirm: bool = False) -> ImportResult:
    """Import positions from a Webull CSV. **Dry-run by default.**

    Pass ``confirm=True`` to actually overwrite the account's positions and
    save to ``portfolio.yaml``. The returned :class:`ImportResult` always
    contains the parsed positions and the diff against the current account,
    whether or not the change was committed.
    """
    incoming = parse_csv(csv_path)
    diff = diff_positions(account.positions, incoming)
    has_changes = any(d.kind != "unchanged" for d in diff)
    if confirm and has_changes:
        account.positions = incoming
        save(account)
        return ImportResult(positions=incoming, diff=diff, saved=True)
    return ImportResult(positions=incoming, diff=diff, saved=False)
