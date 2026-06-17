"""Phase 4: the per-position analyze loop runs on a small thread pool but must
produce output identical to the old sequential loop (same recs order, same
ticker_payloads), and preserve each step's per-ticker fail-soft.
"""
import pytest

from app import build_site
from app.data import fundamentals as fundamentals_mod
from app.research import analyst, correlation, valuation


class _Pos:
    def __init__(self, ticker):
        self.ticker = ticker


class _Account:
    def __init__(self, tickers):
        self.positions = [_Pos(t) for t in tickers]


def _raise(exc):
    def _f(*a, **k):
        raise exc
    return _f


@pytest.fixture
def stub_collaborators(monkeypatch):
    # Deterministic, per-ticker stand-ins so each rec depends only on its
    # ticker - lets us assert ordering and value-identity precisely.
    monkeypatch.setattr(analyst, "analyze_ticker",
        lambda ticker, position_context=None: {
            "ticker": ticker, "action": "hold",
            "ctx": position_context, "thesis": f"ok {ticker}"})
    monkeypatch.setattr(correlation, "candidate_correlation_to_book",
        lambda ticker, account: {"available": True, "rho": len(ticker)})
    monkeypatch.setattr(fundamentals_mod, "get_fundamentals",
        lambda ticker: {"sector": f"sec-{ticker}"})
    monkeypatch.setattr(valuation, "build_sector_comparables",
        lambda sector: {"sector": sector})
    monkeypatch.setattr(valuation, "valuation_score",
        lambda ticker, f, comps: {"tier": "fair", "for": ticker})


def _sequential(account, wbt):
    """A faithful re-implementation of the original sequential loop body,
    using whatever the collaborators are currently patched to."""
    recs, payloads = [], {}
    for p in account.positions:
        try:
            rec = analyst.analyze_ticker(p.ticker, position_context=wbt.get(p.ticker, {}))
        except Exception as e:
            rec = {"ticker": p.ticker, "error": str(e), "action": "hold", "horizon": "long_term",
                   "conviction": 1, "thesis": f"Failed to analyze: {e}",
                   "key_catalysts": [], "key_risks": [], "suggested_action_detail": "",
                   "quote": {}, "technicals": {}, "news": [], "earnings": None,
                   "consensus": None, "analyst_recs": [], "position": {}}
        try:
            rec["correlation_to_book"] = correlation.candidate_correlation_to_book(p.ticker, account)
        except Exception:
            rec["correlation_to_book"] = {"available": False}
        try:
            f = fundamentals_mod.get_fundamentals(p.ticker)
            comps = valuation.build_sector_comparables(f.get("sector"))
            rec["valuation"] = valuation.valuation_score(p.ticker, f, comps)
        except Exception:
            rec["valuation"] = {"tier": "unknown"}
        recs.append(rec)
        payloads[p.ticker] = rec
    return recs, payloads


def test_parallel_output_identical_to_sequential(stub_collaborators):
    tickers = ["AAPL", "MSFT", "NVDA", "AMD", "TSLA", "GOOGL", "META", "AVGO"]
    account = _Account(tickers)
    wbt = {"AAPL": {"weight": 0.1}, "MSFT": {"weight": 0.2}}

    par_recs, par_payloads = build_site._analyze_positions(account, wbt)
    seq_recs, seq_payloads = _sequential(account, wbt)

    assert par_recs == seq_recs
    assert par_payloads == seq_payloads
    # Order is preserved despite the thread pool.
    assert [r["ticker"] for r in par_recs] == tickers
    assert list(par_payloads.keys()) == tickers
    # position_context is threaded through unchanged.
    assert par_recs[0]["ctx"] == {"weight": 0.1}
    assert par_recs[2]["ctx"] == {}  # NVDA absent from wbt -> default {}


def test_one_position_failure_does_not_abort_others(monkeypatch, stub_collaborators):
    def flaky(ticker, position_context=None):
        if ticker == "MSFT":
            raise RuntimeError("boom")
        return {"ticker": ticker, "action": "hold", "thesis": f"ok {ticker}"}

    monkeypatch.setattr(analyst, "analyze_ticker", flaky)
    account = _Account(["AAPL", "MSFT", "NVDA"])

    recs, payloads = build_site._analyze_positions(account, {})
    assert [r["ticker"] for r in recs] == ["AAPL", "MSFT", "NVDA"]  # order kept
    assert recs[1]["error"] == "boom"
    assert recs[1]["thesis"] == "Failed to analyze: boom"
    # Neighbors unaffected, and the failed rec still gets the later steps.
    assert recs[0]["thesis"] == "ok AAPL"
    assert recs[2]["thesis"] == "ok NVDA"
    assert recs[1]["correlation_to_book"] == {"available": True, "rho": 4}
    assert payloads["MSFT"] is recs[1]


def test_correlation_step_failsoft(monkeypatch, stub_collaborators):
    monkeypatch.setattr(correlation, "candidate_correlation_to_book",
                        _raise(RuntimeError("corr down")))
    recs, _ = build_site._analyze_positions(_Account(["AAPL"]), {})
    assert recs[0]["correlation_to_book"] == {"available": False}
    assert recs[0]["thesis"] == "ok AAPL"  # analysis itself still succeeded


def test_valuation_step_failsoft(monkeypatch, stub_collaborators):
    monkeypatch.setattr(fundamentals_mod, "get_fundamentals",
                        _raise(RuntimeError("fund down")))
    recs, _ = build_site._analyze_positions(_Account(["AAPL"]), {})
    assert recs[0]["valuation"] == {"tier": "unknown"}


def test_empty_positions():
    recs, payloads = build_site._analyze_positions(_Account([]), {})
    assert recs == []
    assert payloads == {}
