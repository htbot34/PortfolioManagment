from dataclasses import dataclass
from pathlib import Path

import yaml

from app.config import settings


@dataclass
class Position:
    ticker: str
    shares: float
    cost_basis: float

    @property
    def book_value(self) -> float:
        return self.shares * self.cost_basis


@dataclass
class Account:
    cash: float
    total_value: float
    currency: str
    positions: list[Position]

    def position(self, ticker: str) -> Position | None:
        ticker = ticker.upper()
        for p in self.positions:
            if p.ticker == ticker:
                return p
        return None


def load(path: Path | None = None) -> Account:
    path = path or settings.portfolio_path
    with open(path) as f:
        data = yaml.safe_load(f)
    acct = data.get("account", {})
    positions = [
        Position(ticker=p["ticker"].upper(), shares=float(p["shares"]), cost_basis=float(p["cost_basis"]))
        for p in data.get("positions", [])
    ]
    return Account(
        cash=float(acct.get("cash", 0.0)),
        total_value=float(acct.get("total_value", 0.0)),
        currency=acct.get("currency", "USD"),
        positions=positions,
    )


def save(account: Account, path: Path | None = None) -> None:
    path = path or settings.portfolio_path
    data = {
        "account": {
            "cash": round(account.cash, 2),
            "total_value": round(account.total_value, 2),
            "currency": account.currency,
        },
        "positions": [
            {"ticker": p.ticker, "shares": p.shares, "cost_basis": p.cost_basis} for p in account.positions
        ],
    }
    with open(path, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
