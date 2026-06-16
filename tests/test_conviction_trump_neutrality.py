"""Neutrality proof for the Trump-signal gate refactor.

Invariant: when a ticker has NO qualifying Trump mention, the
conviction gate's `qualifies` decision must be identical to the
pre-Trump 3-of-3 rule.

This test enumerates every (technical_pass, sector_pass, news_pass)
combination AND every insider rescue scenario, computes the
*pre-change* expected qualification from the old truth table, and
asserts the current evaluate() matches. If anyone ever lets the Trump
substitution path leak into neutral tickers, this test fails loudly.
"""
from __future__ import annotations

import itertools

import pytest

from app.research import conviction

from datetime import date, timedelta

# Fixtures must stay inside the 14-day news window and 30-day trump TTL or they rot and break CI.
_RECENT = (date.today() - timedelta(days=3)).isoformat()


# ---------------------------------------------------------------------------
# Synthetic payload builders. Each one toggles a single primary signal
# pass/fail by selecting indicator values that the live signal modules
# score deterministically -- no mocking of the signal layer, so the test
# actually exercises the gate end-to-end.
# ---------------------------------------------------------------------------

_BULL_NEWS = [
    # Two long-durability bullish items, magnitude 5 each: net = 10 > 3.
    {"direction": "bullish", "magnitude": 5, "durability": "long",
     "one_line_summary": "ACME secures $1B contract",
     "published": _RECENT,
     # Crucially: no Trump mention.
     "trump_mention": False, "trump_valence": "none",
     "trump_confidence": 0.0},
    {"direction": "bullish", "magnitude": 5, "durability": "long",
     "one_line_summary": "ACME raises full-year guidance",
     "published": _RECENT,
     "trump_mention": False, "trump_valence": "none",
     "trump_confidence": 0.0},
]

_BEAR_NEWS = [
    {"direction": "bearish", "magnitude": 5, "durability": "long",
     "one_line_summary": "ACME warns on guidance",
     "published": _RECENT,
     "trump_mention": False, "trump_valence": "none",
     "trump_confidence": 0.0},
    {"direction": "bearish", "magnitude": 5, "durability": "long",
     "one_line_summary": "ACME under investigation",
     "published": _RECENT,
     "trump_mention": False, "trump_valence": "none",
     "trump_confidence": 0.0},
]


def _macro_sector_pass(direction: str = "long") -> dict:
    """Macro where XLK has both 5d and 20d positive (long-aligned)."""
    sign = 1 if direction == "long" else -1
    return {"sectors": {"Tech": {"ticker": "XLK",
                                  "ret_5d": sign * 2.5,
                                  "ret_20d": sign * 6.0}}}


def _macro_sector_fail() -> dict:
    """Macro where XLK 5d is negative -> sector fails the 'long' direction."""
    return {"sectors": {"Tech": {"ticker": "XLK",
                                  "ret_5d": -2.5, "ret_20d": -6.0}}}


def _payload(*, tech_pass: bool, news_pass: bool, sector_payload_sector: str,
             with_insider_cluster: bool = False) -> dict:
    """Build a synthetic candidate payload.

    Toggles:
      tech_pass: True -> stacked uptrend + RSI in band + MACD up (score 3).
                 False -> RSI overbought, no trend, no MACD (score 0/1).
      news_pass: True -> two strongly bullish items, False -> bearish.
      sector_payload_sector: passed through to ``conviction._extract_sector``
                              via the `sector` field. The macro decides whether
                              that sector's ETF data is long-aligned.
      with_insider_cluster: True -> attach 4 distinct buyers @ $600k each so
                              the insider score reaches tier 2 (>=2).
    """
    if tech_pass:
        tech_fields = {"rsi14": 55, "macd_hist": 0.5,
                        "stacked_uptrend": True, "above_sma200": True,
                        "breakout_20d": True}
    else:
        tech_fields = {"rsi14": 78, "macd_hist": -0.1,
                        "stacked_uptrend": False, "above_sma200": False,
                        "breakout_20d": False}

    news_classifications = _BULL_NEWS if news_pass else _BEAR_NEWS

    insider_txns: list[dict] = []
    if with_insider_cluster:
        from datetime import date, timedelta
        d = (date.today() - timedelta(days=5)).isoformat()
        for filer in ("Alice", "Bob", "Carol", "Dave"):
            insider_txns.append({
                "filer_name": filer, "role": "Director",
                "transaction_date": d, "transaction_code": "P",
                "acquired_disposed": "A",
                "shares": 6000.0, "price": 100.0, "total_value": 600_000.0,
                "is_planned_10b5_1": False,
            })

    return {
        "ticker": "ACME",
        "sector": sector_payload_sector,
        **tech_fields,
        "news_classifications": news_classifications,
        "insider_transactions": insider_txns,
    }


# ---------------------------------------------------------------------------
# The old truth table -- THE invariant this test enforces.
# ---------------------------------------------------------------------------

def _expected_qualifies_old_rule(*, tech: bool, sector: bool, news: bool,
                                   insider_rescues: bool) -> bool:
    """The pre-Trump qualification logic.

    - Technical is a hard prerequisite.
    - 3-of-3 primaries qualifies.
    - 2-of-3 + insider cluster (score>=2) qualifies, provided technical
      is one of the two passers.
    """
    if not tech:
        return False
    primary_pass = sum((tech, sector, news))
    if primary_pass == 3:
        return True
    if primary_pass == 2 and insider_rescues:
        return True
    return False


# ---------------------------------------------------------------------------
# THE neutrality proof.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tech,sector,news,with_insider",
    list(itertools.product([True, False], repeat=4)),
)
def test_no_trump_mention_matrix_matches_pre_change_logic(
        tech, sector, news, with_insider):
    """For every (tech, sector, news, insider) combination with NO Trump
    mention, evaluate() must return the same qualifies value as the
    pre-Trump 3-of-3 rule.

    16 cases. Any divergence means the Trump path leaked into neutral
    tickers, which would silently lower the bar for the entire universe.
    """
    payload = _payload(
        tech_pass=tech, news_pass=news,
        sector_payload_sector="Technology",
        with_insider_cluster=with_insider,
    )
    # Force trump_signal_result to "no mention" without going through
    # the detector (so we are not at the mercy of LLM stubs).
    payload["trump_signal_result"] = {
        "mention": False, "valence": "none", "confidence": 0.0,
        "as_of": None, "source": "", "summary": "", "manual": False,
        "low_confidence_seen": [],
    }
    macro = _macro_sector_pass() if sector else _macro_sector_fail()
    out = conviction.evaluate(payload, direction="long", macro=macro)

    # The old rule's insider rescue only fires when primary_pass == 2 AND
    # the cluster exists. Construct the same expectation.
    primary_pass = sum((tech, sector, news))
    insider_rescues = with_insider and primary_pass == 2
    expected = _expected_qualifies_old_rule(
        tech=tech, sector=sector, news=news,
        insider_rescues=insider_rescues,
    )
    assert out["qualifies"] is expected, (
        f"NEUTRALITY VIOLATED: tech={tech} sector={sector} news={news} "
        f"insider={with_insider} -> got qualifies={out['qualifies']!r}, "
        f"expected {expected!r}.\n"
        f"signals: { {k: v.get('pass') for k, v in out['signals'].items()} }"
    )


def test_no_trump_mention_trump_signal_is_neutral_fail():
    """Sanity: when no mention exists, the trump signal is a neutral
    fail -- not a confirmation, not a veto."""
    payload = _payload(tech_pass=True, news_pass=True,
                        sector_payload_sector="Technology")
    payload["trump_signal_result"] = {
        "mention": False, "valence": "none", "confidence": 0.0,
        "as_of": None, "source": "", "summary": "", "manual": False,
        "low_confidence_seen": [],
    }
    out = conviction.evaluate(payload, direction="long",
                                macro=_macro_sector_pass())
    assert out["signals"]["trump"]["pass"] is False
    assert out["signals"]["trump"]["valence"] == "none"
    assert out["signals"]["trump"].get("avoid") is False
    assert "trump_block" not in out
    assert "trump_exit_flag" not in out


def test_kill_switch_makes_trump_signal_inert():
    """trump_signal_enabled=False must make the trump signal neutral
    even if a payload-attached finding says 'endorse'."""
    payload = _payload(tech_pass=True, news_pass=True,
                        sector_payload_sector="Technology")
    payload["trump_signal_result"] = {
        "mention": True, "valence": "endorse", "confidence": 1.0,
        "as_of": _RECENT, "source": "Source", "summary": "x",
        "manual": True, "low_confidence_seen": [],
    }
    cfg = {
        "trump_signal_enabled": False,
        "trump_ttl_days": 30, "trump_min_confidence": 0.6,
        "trump_confluence_min": 2, "trump_solo_with_technical": False,
        "trump_attack_vetoes_longs": True,
    }
    out = conviction.evaluate(payload, direction="long",
                                macro=_macro_sector_pass(),
                                gate_config=cfg)
    # Kill switch overwrites the payload finding -> neutral.
    assert out["signals"]["trump"]["pass"] is False
    assert out["signals"]["trump"]["valence"] == "none"


# ---------------------------------------------------------------------------
# Trump SUBSTITUTION and VETO -- non-neutral cases (sanity, not invariant)
# ---------------------------------------------------------------------------

def test_trump_endorse_substitutes_for_one_primary():
    """With tech + sector pass + news FAIL, an endorsement substitutes
    for news so qualifications=2 holds."""
    payload = _payload(tech_pass=True, news_pass=False,
                        sector_payload_sector="Technology")
    payload["trump_signal_result"] = {
        "mention": True, "valence": "endorse", "confidence": 0.9,
        "as_of": _RECENT, "source": "WH statement",
        "summary": "Praised", "manual": False, "low_confidence_seen": [],
    }
    out = conviction.evaluate(payload, direction="long",
                                macro=_macro_sector_pass())
    assert out["qualifies"] is True
    assert out["signals"]["trump"]["pass"] is True
    assert out["signals"]["trump"]["valence"] == "endorse"


def test_trump_attack_vetoes_new_long_entry():
    """An attack on an otherwise-qualified candidate blocks a new buy."""
    payload = _payload(tech_pass=True, news_pass=True,
                        sector_payload_sector="Technology")
    payload["trump_signal_result"] = {
        "mention": True, "valence": "attack", "confidence": 0.9,
        "as_of": _RECENT, "source": "Truth Social",
        "summary": "Slammed", "manual": False, "low_confidence_seen": [],
    }
    out = conviction.evaluate(payload, direction="long",
                                macro=_macro_sector_pass(),
                                action="new_buy")
    assert out["qualifies"] is False
    assert "trump_block" in out
    assert "attack" in out["trump_block"].lower()


def test_trump_attack_on_existing_holding_attaches_exit_flag():
    """An attack on a HELD position annotates instead of auto-blocking."""
    payload = _payload(tech_pass=True, news_pass=True,
                        sector_payload_sector="Technology")
    payload["position"] = {"weight_pct": 8.0}  # presence => held
    payload["trump_signal_result"] = {
        "mention": True, "valence": "attack", "confidence": 0.9,
        "as_of": _RECENT, "source": "Truth Social",
        "summary": "Slammed", "manual": False, "low_confidence_seen": [],
    }
    out = conviction.evaluate(payload, direction="long",
                                macro=_macro_sector_pass(),
                                action="add")
    # The holding's "add" is not blocked; the exit flag is attached.
    assert "trump_block" not in out
    assert "trump_exit_flag" in out


def test_trump_solo_with_technical_off_by_default():
    """Tech + Trump endorsement alone (no sector, no news) does NOT
    qualify under the default config -- the solo flag is OFF."""
    payload = _payload(tech_pass=True, news_pass=False,
                        sector_payload_sector="Technology")
    payload["trump_signal_result"] = {
        "mention": True, "valence": "endorse", "confidence": 0.9,
        "as_of": _RECENT, "source": "WH",
        "summary": "Praised", "manual": False, "low_confidence_seen": [],
    }
    out = conviction.evaluate(payload, direction="long",
                                macro=_macro_sector_fail())  # sector fail
    # 1 confirmation only (trump) -> below the default confluence_min=2.
    # The insider rescue does not fire either (no cluster on this payload).
    assert out["qualifies"] is False


def test_trump_solo_with_technical_on_allows_solo():
    """With the solo flag enabled, tech + trump endorsement alone passes."""
    payload = _payload(tech_pass=True, news_pass=False,
                        sector_payload_sector="Technology")
    payload["trump_signal_result"] = {
        "mention": True, "valence": "endorse", "confidence": 0.9,
        "as_of": _RECENT, "source": "WH",
        "summary": "Praised", "manual": False, "low_confidence_seen": [],
    }
    cfg = {
        "trump_signal_enabled": True,
        "trump_ttl_days": 30, "trump_min_confidence": 0.6,
        "trump_confluence_min": 2,
        "trump_solo_with_technical": True,
        "trump_attack_vetoes_longs": True,
    }
    out = conviction.evaluate(payload, direction="long",
                                macro=_macro_sector_fail(),
                                gate_config=cfg)
    assert out["qualifies"] is True


# ---------------------------------------------------------------------------
# Phase C: insider promotion requires a FUNDAMENTAL surviving primary
# (sector or news). Trump alone must NOT open the 1-confirmation promotion
# tier -- closing the latent below-bar trapdoor (audit finding #1).
# ---------------------------------------------------------------------------

def _endorse_finding():
    return {"mention": True, "valence": "endorse", "confidence": 0.9,
            "as_of": _RECENT, "source": "WH", "summary": "Praised",
            "manual": False, "low_confidence_seen": []}


def _no_trump_finding():
    return {"mention": False, "valence": "none", "confidence": 0.0,
            "as_of": None, "source": "", "summary": "", "manual": False,
            "low_confidence_seen": []}


def test_trump_plus_insider_cannot_promote_when_sector_and_news_fail():
    """tech + trump + insider, with BOTH sector and news failing, must NOT
    qualify -- a Trump mention alone cannot unlock insider promotion."""
    payload = _payload(tech_pass=True, news_pass=False,           # news fails
                       sector_payload_sector="Technology",
                       with_insider_cluster=True)                 # insider score 3
    payload["trump_signal_result"] = _endorse_finding()
    out = conviction.evaluate(payload, direction="long",
                                macro=_macro_sector_fail())       # sector fails
    assert out["signals"]["sector_momentum"]["pass"] is False
    assert out["signals"]["news"]["pass"] is False
    assert out["signals"]["trump"]["pass"] is True
    assert out["qualifies"] is False
    assert out["promoted_by_insider"] is False
    # Insider is not even consulted: there is no fundamental primary to rescue.
    assert out["insider_status"] == "not_evaluated"
    assert "insider" not in out["signals"]


def test_insider_promotion_still_fires_on_sector_plus_insider():
    """tech + sector (news fail, no trump) + insider>=2 still promotes."""
    payload = _payload(tech_pass=True, news_pass=False,
                       sector_payload_sector="Technology",
                       with_insider_cluster=True)
    payload["trump_signal_result"] = _no_trump_finding()
    out = conviction.evaluate(payload, direction="long",
                                macro=_macro_sector_pass())       # sector passes
    assert out["qualifies"] is True
    assert out["promoted_by_insider"] is True
    assert out["insider_status"] == "scored"


def test_insider_promotion_still_fires_on_news_plus_insider():
    """tech + news (sector fail, no trump) + insider>=2 still promotes."""
    payload = _payload(tech_pass=True, news_pass=True,
                       sector_payload_sector="Technology",
                       with_insider_cluster=True)
    payload["trump_signal_result"] = _no_trump_finding()
    out = conviction.evaluate(payload, direction="long",
                                macro=_macro_sector_fail())       # sector fails
    assert out["qualifies"] is True
    assert out["promoted_by_insider"] is True
    assert out["insider_status"] == "scored"


def test_trump_plus_sector_plus_insider_qualifies_via_confluence_not_promotion():
    """tech + sector + trump (news fail) is a >=2 confluence qualification --
    NOT an insider promotion. The insider tier is never consulted here."""
    payload = _payload(tech_pass=True, news_pass=False,
                       sector_payload_sector="Technology",
                       with_insider_cluster=True)
    payload["trump_signal_result"] = _endorse_finding()
    out = conviction.evaluate(payload, direction="long",
                                macro=_macro_sector_pass())       # sector passes
    assert out["qualifies"] is True
    assert out["promoted_by_insider"] is False     # confluence, not promotion
    assert out["insider_status"] == "not_evaluated"
