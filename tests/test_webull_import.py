"""Tests for the Webull CSV importer (parsing + dry-run + diff)."""
from pathlib import Path
from unittest.mock import patch

from app.portfolio import store
from app.portfolio.webull_import import diff_positions, import_csv, parse_csv


def _write_csv(tmp_path: Path, rows: str) -> Path:
    csv = tmp_path / "positions.csv"
    csv.write_text("Symbol,Quantity,Average Cost,Market Value\n" + rows)
    return csv


def test_parse_csv(tmp_path: Path):
    csv = _write_csv(tmp_path, "AAPL,10,150.25,1700\nMSFT,5,300.00,1750\n")
    positions = parse_csv(csv)
    assert len(positions) == 2
    assert positions[0].ticker == "AAPL"
    assert positions[0].shares == 10
    assert positions[0].cost_basis == 150.25
    assert positions[1].ticker == "MSFT"


def test_diff_positions_classifies_changes():
    cur = [store.Position("AAPL", 10, 150.0), store.Position("MSFT", 5, 300.0)]
    inc = [store.Position("AAPL", 10, 150.0), store.Position("NVDA", 2, 140.0)]
    diff = diff_positions(cur, inc)
    by_ticker = {d.ticker: d for d in diff}
    assert by_ticker["AAPL"].kind == "unchanged"
    assert by_ticker["MSFT"].kind == "removed"
    assert by_ticker["NVDA"].kind == "added"


def test_diff_positions_detects_share_or_cost_change():
    cur = [store.Position("AAPL", 10, 150.0)]
    inc = [store.Position("AAPL", 12, 150.0)]
    diff = diff_positions(cur, inc)
    assert diff[0].kind == "changed"
    assert diff[0].before == {"shares": 10, "cost_basis": 150.0}
    assert diff[0].after == {"shares": 12, "cost_basis": 150.0}


def test_import_csv_dry_run_does_not_save(tmp_path: Path):
    csv = _write_csv(tmp_path, "AAPL,10,150.25,1700\n")
    acct = store.Account(cash=1000.0, total_value=0, currency="USD",
                          positions=[store.Position("META", 11, 677.22)])
    with patch("app.portfolio.webull_import.save") as mock_save:
        result = import_csv(csv, acct)
    assert result.saved is False
    mock_save.assert_not_called()
    # Account is still unmodified: original META position untouched.
    assert acct.positions[0].ticker == "META"
    # Diff should show META removed and AAPL added.
    kinds = {d.kind for d in result.diff}
    assert "removed" in kinds and "added" in kinds


def test_import_csv_confirm_saves(tmp_path: Path):
    csv = _write_csv(tmp_path, "AAPL,10,150.25,1700\n")
    acct = store.Account(cash=1000.0, total_value=0, currency="USD",
                          positions=[store.Position("META", 11, 677.22)])
    with patch("app.portfolio.webull_import.save") as mock_save:
        result = import_csv(csv, acct, confirm=True)
    assert result.saved is True
    mock_save.assert_called_once_with(acct)
    assert acct.positions[0].ticker == "AAPL"


def test_import_csv_confirm_with_no_changes_does_not_save(tmp_path: Path):
    csv = _write_csv(tmp_path, "META,11,677.22,1\n")
    acct = store.Account(cash=1000.0, total_value=0, currency="USD",
                          positions=[store.Position("META", 11, 677.22)])
    with patch("app.portfolio.webull_import.save") as mock_save:
        result = import_csv(csv, acct, confirm=True)
    assert result.saved is False
    mock_save.assert_not_called()


def test_import_result_helpers(tmp_path: Path):
    csv = _write_csv(tmp_path, "AAPL,10,150.25,1700\nMSFT,5,300.00,1750\n")
    acct = store.Account(cash=1000.0, total_value=0, currency="USD",
                          positions=[store.Position("AAPL", 8, 145.0)])
    with patch("app.portfolio.webull_import.save"):
        result = import_csv(csv, acct)
    assert len(result.added) == 1 and result.added[0].ticker == "MSFT"
    assert len(result.changed) == 1 and result.changed[0].ticker == "AAPL"
    assert len(result.removed) == 0
    assert "added" in result.summary_line()
