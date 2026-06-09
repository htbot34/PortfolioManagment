"""Render tests for the index.html action_card macro.

Regression for the daily "Refresh site" crash: optional numeric fields
guarded with a bare ``X is not none`` blow up when the key is MISSING from
the action dict, because Jinja resolves a missing key to Undefined and
``Undefined is not none`` is True - the numeric comparison/format on the
next line then raises UndefinedError and aborts the whole build.

The macro must skip (not crash on) optional keys that are absent entirely,
while rendering present-and-numeric values exactly as before.
"""
from app import build_site


def _brief(primary_action: dict) -> dict:
    return {
        "verdict": "trade",
        "headline": f"BUY {primary_action['ticker']} - test.",
        "generated_for": "2026-06-09",
        "primary_action": primary_action,
        "secondary_actions": [],
        "market_snapshot": "",
        "watching": [],
    }


def _render_index(brief: dict) -> str:
    env = build_site._env()
    return env.get_template("index.html").render(
        brief=brief, macro={}, exposures={}, scan={}, recs_by_ticker={},
        activity=[], regime=None, intraday=None, base="",
        generated_at="2026-06-09 12:00 UTC", risk={},
        flags={"has_llm": False}, diagnostics={}, insider_diagnostics={},
        repo="htbot34/PortfolioManagment",
    )


def test_action_card_tolerates_missing_optional_numeric_fields():
    """A primary_action that OMITS the optional numeric keys must render.

    Omitted here: unrealized_pnl_on_action on the action itself,
    avg_corr_to_top5 under conviction_gate.correlation.candidate_to_book,
    and percentile_in_sector under conviction_gate.valuation (with a tier
    set so the valuation block is entered and the inner guard exercised).
    """
    pa = {
        "rec_id": "20260609-TESTX-buy",
        "ticker": "TESTX",
        "action": "buy",
        "entry": "~$100.00",
        "stop": "$95.00",
        "target": "$115.00",
        "size_pct": 5,
        "thesis": "Test thesis.",
        "invalidation": "Daily close below $95.00",
        "conviction": 5,
        "evidence": [],
        "conviction_gate": {
            "qualifies": True,
            "signals": {},
            "correlation": {"candidate_to_book": {}},  # no avg_corr_to_top5
            "valuation": {"tier": "fair"},  # no percentile_in_sector
        },
    }
    html = _render_index(_brief(pa))
    assert isinstance(html, str)
    assert "TESTX" in html
    # The guarded optional blocks were skipped, not crashed:
    assert "Unrealized P" not in html
    assert "Correlation to your top 5" not in html
    assert "th pct in sector" not in html
    # The valuation block itself still rendered (tier was present).
    assert "fair" in html


def test_action_card_still_renders_present_numeric_fields():
    """Hardened guards must not change behavior for present values."""
    pa = {
        "rec_id": "20260609-TESTX-buy",
        "ticker": "TESTX",
        "action": "buy",
        "entry": "~$100.00",
        "stop": "$95.00",
        "target": "$115.00",
        "size_pct": 5,
        "unrealized_pnl_on_action": -120.5,
        "thesis": "Test thesis.",
        "invalidation": "Daily close below $95.00",
        "conviction": 5,
        "evidence": [],
        "conviction_gate": {
            "qualifies": True,
            "signals": {},
            "correlation": {"candidate_to_book": {"avg_corr_to_top5": 0.42}},
            "valuation": {"tier": "fair", "percentile_in_sector": 85},
        },
    }
    html = _render_index(_brief(pa))
    assert "-$120.50" in html
    assert "0.42" in html
    assert "85th pct in sector" in html
