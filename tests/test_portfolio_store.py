from pathlib import Path

from app.portfolio import store


def test_load_seed_portfolio():
    acct = store.load()
    tickers = [p.ticker for p in acct.positions]
    assert "META" in tickers
    assert "NBIS" in tickers
    meta = acct.position("meta")
    assert meta is not None
    assert meta.shares == 11
    assert meta.cost_basis == 677.22


def test_book_value():
    acct = store.load()
    nflx = acct.position("NFLX")
    assert nflx.book_value == 9 * 90.11


def test_save_roundtrip(tmp_path: Path):
    acct = store.load()
    out = tmp_path / "p.yaml"
    store.save(acct, out)
    reloaded = store.load(out)
    assert len(reloaded.positions) == len(acct.positions)
    assert reloaded.positions[0].ticker == acct.positions[0].ticker
