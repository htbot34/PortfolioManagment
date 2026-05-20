"""Tests for the valuation overlay + the conviction valuation gate."""
from app.research import conviction, valuation


def _f(ticker, trailing_pe=None, forward_pe=None, price_to_sales=None,
       sector="Technology"):
    return {
        "ticker": ticker, "trailing_pe": trailing_pe, "forward_pe": forward_pe,
        "peg_ratio": None, "ev_to_ebitda": None, "price_to_sales": price_to_sales,
        "profit_margin": None, "return_on_equity": None, "sector": sector,
    }


def _peers(*pes):
    """Build a sector-comparables list with the given trailing P/E values."""
    return [_f(f"PEER{i}", trailing_pe=pe) for i, pe in enumerate(pes)]


# ---------------------------------------------------------------------------
# valuation_score - percentile + tier math
# ---------------------------------------------------------------------------

def test_cheap_tier_low_percentile():
    # Candidate PE 10 vs peers mostly 30-60 -> low percentile -> cheap.
    out = valuation.valuation_score(
        "CO", _f("CO", trailing_pe=10),
        _peers(30, 35, 40, 45, 50, 55, 60, 25, 33, 48))
    assert out["tier"] == "cheap"
    assert out["percentile_in_sector"] < 25


def test_fair_tier_mid_percentile():
    out = valuation.valuation_score(
        "CO", _f("CO", trailing_pe=40),
        _peers(20, 25, 30, 35, 45, 50, 55, 60, 38, 42))
    assert out["tier"] == "fair"
    assert 25 <= out["percentile_in_sector"] < 75


def test_expensive_tier():
    out = valuation.valuation_score(
        "CO", _f("CO", trailing_pe=58),
        _peers(20, 25, 30, 35, 40, 45, 50, 55, 60, 62))
    assert out["tier"] == "expensive"


def test_extreme_tier_top_percentile():
    # Candidate PE above every peer -> 100th percentile -> extreme.
    out = valuation.valuation_score(
        "CO", _f("CO", trailing_pe=120),
        _peers(20, 25, 30, 35, 40, 45, 50, 55, 60, 65))
    assert out["tier"] == "extreme"
    assert out["percentile_in_sector"] >= 90


def test_unknown_when_no_metric():
    out = valuation.valuation_score("CO", _f("CO"), _peers(20, 30, 40))
    assert out["tier"] == "unknown"


def test_unknown_when_too_few_peers():
    out = valuation.valuation_score("CO", _f("CO", trailing_pe=30), _peers(25))
    assert out["tier"] == "unknown"


def test_falls_back_to_price_to_sales():
    # No P/E anywhere; both candidate and peers only have price_to_sales.
    cand = _f("CO", price_to_sales=2.0)
    peers = [_f(f"P{i}", price_to_sales=ps) for i, ps in enumerate([8, 9, 10, 11, 12])]
    out = valuation.valuation_score("CO", cand, peers)
    assert out["metric"] == "price_to_sales"
    assert out["tier"] == "cheap"


def test_candidate_excluded_from_its_own_peer_set():
    # If the candidate appears in the comparables, it must not skew its own
    # percentile.
    cand = _f("CO", trailing_pe=30)
    peers = [_f("CO", trailing_pe=30)] + _peers(50, 55, 60, 65)
    out = valuation.valuation_score("CO", cand, peers)
    assert out["n_peers"] == 4  # the duplicate CO row dropped


# ---------------------------------------------------------------------------
# conviction valuation gate
# ---------------------------------------------------------------------------

def _macro(r5=1.0, r20=3.0):
    return {"sectors": {"Tech": {"ticker": "XLK", "ret_5d": r5, "ret_20d": r20}}}


def _qualifying_payload():
    return {
        "ticker": "ACME", "sector": "Technology",
        "rsi14": 55, "macd_hist": 0.5,
        "stacked_uptrend": True, "above_sma200": True, "breakout_20d": True,
        "news_classifications": [
            {"direction": "bullish", "magnitude": 4, "durability": "long",
             "one_line_summary": "x", "published": None},
            {"direction": "bullish", "magnitude": 3, "durability": "medium",
             "one_line_summary": "y", "published": None},
        ],
        "insider_transactions": [],
    }


def test_extreme_valuation_blocks_a_new_buy():
    out = conviction.evaluate(
        _qualifying_payload(), direction="long", macro=_macro(), action="new_buy",
        fundamentals=_f("ACME", trailing_pe=200),
        sector_comparables=_peers(20, 25, 30, 35, 40, 45),
    )
    assert out["qualifies"] is False
    assert "extreme" in out["valuation_block"]


def test_mid_valuation_does_not_block():
    out = conviction.evaluate(
        _qualifying_payload(), direction="long", macro=_macro(), action="new_buy",
        fundamentals=_f("ACME", trailing_pe=35),
        sector_comparables=_peers(20, 25, 30, 40, 45, 50),
    )
    assert out["qualifies"] is True
    assert "valuation_block" not in out
    assert out["valuation"]["tier"] == "fair"


def test_cheap_valuation_sets_tailwind():
    out = conviction.evaluate(
        _qualifying_payload(), direction="long", macro=_macro(), action="new_buy",
        fundamentals=_f("ACME", trailing_pe=8),
        sector_comparables=_peers(30, 35, 40, 45, 50, 55),
    )
    assert out["qualifies"] is True
    assert out.get("valuation_tailwind") is True


def test_extreme_valuation_overridden_by_score3_insider():
    # Sector fails -> 2-of-3; a score-3 insider cluster promotes; an extreme
    # valuation is then overridden because the promotion was score-3.
    txns = [{"filer_name": n, "role": "Chief Executive Officer"
             if n == "A" else "Director",
             "transaction_date": "2026-05-18", "transaction_code": "P",
             "acquired_disposed": "A", "shares": 1000.0,
             "price": 1500.0 if n == "A" else 200.0,
             "total_value": 1_500_000.0 if n == "A" else 200_000.0,
             "is_planned_10b5_1": False} for n in ("A", "B")]
    payload = _qualifying_payload()
    payload["insider_transactions"] = txns
    out = conviction.evaluate(
        payload, direction="long", macro=_macro(r5=-1, r20=-2),  # sector fails
        action="new_buy",
        fundamentals=_f("ACME", trailing_pe=300),
        sector_comparables=_peers(20, 25, 30, 35, 40, 45),
    )
    assert out["promoted_by_insider"] is True
    assert out["signals"]["insider"]["score"] == 3
    assert out["qualifies"] is True            # extreme valuation overridden
    assert "valuation_override" in out


def test_short_cheap_valuation_annotates_not_blocks():
    payload = {
        "ticker": "ACME", "sector": "Technology",
        "stacked_downtrend": True, "rsi14": 72, "macd_hist": -0.3,
        "death_cross_recent": True, "above_sma200": False,
        "news_classifications": [
            {"direction": "bearish", "magnitude": 4, "durability": "long",
             "one_line_summary": "x", "published": None},
            {"direction": "bearish", "magnitude": 3, "durability": "medium",
             "one_line_summary": "y", "published": None},
        ],
        "insider_transactions": [],
    }
    out = conviction.evaluate(
        payload, direction="short", macro=_macro(r5=-1, r20=-2), action="sell",
        fundamentals=_f("ACME", trailing_pe=8),
        sector_comparables=_peers(30, 35, 40, 45, 50, 55),
    )
    assert out["qualifies"] is True   # not blocked
    assert "valuation_annotation" in out
