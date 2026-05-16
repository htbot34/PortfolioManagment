from pathlib import Path

from app.portfolio.webull_import import parse_csv


def test_parse_csv(tmp_path: Path):
    csv = tmp_path / "positions.csv"
    csv.write_text(
        "Symbol,Quantity,Average Cost,Market Value\n"
        "AAPL,10,150.25,1700\n"
        "MSFT,5,300.00,1750\n"
    )
    positions = parse_csv(csv)
    assert len(positions) == 2
    assert positions[0].ticker == "AAPL"
    assert positions[0].shares == 10
    assert positions[0].cost_basis == 150.25
    assert positions[1].ticker == "MSFT"
