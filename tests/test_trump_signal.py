"""Tests for the Trump-mention signal."""
from datetime import date, timedelta

import yaml

from app.research import news_classifier, trump_signal


def _classification(*, mention=True, valence="endorse", confidence=0.8,
                    published=None, headline="headline", summary=""):
    return {
        "trump_mention": mention,
        "trump_valence": valence,
        "trump_confidence": confidence,
        "published": (published or date.today()).isoformat()
            if isinstance(published, date)
            else published,
        "headline": headline,
        "one_line_summary": summary or headline,
        "direction": "bullish" if valence == "endorse" else "bearish",
        "magnitude": 4,
        "durability": "medium",
        "source": "llm",
    }


# ---------------------------------------------------------------------------
# Core valences
# ---------------------------------------------------------------------------

def test_endorsement_marks_valence_endorse():
    cls = [_classification(valence="endorse", confidence=0.85,
                            published=date.today())]
    out = trump_signal.evaluate("ACME", cls)
    assert out["mention"] is True
    assert out["valence"] == "endorse"
    assert out["confidence"] == 0.85
    assert out["manual"] is False


def test_attack_marks_valence_attack():
    cls = [_classification(valence="attack", confidence=0.9,
                            published=date.today())]
    out = trump_signal.evaluate("ACME", cls)
    assert out["mention"] is True
    assert out["valence"] == "attack"


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def test_stale_mention_does_not_count():
    """A mention outside the TTL window must not surface."""
    long_ago = date.today() - timedelta(days=120)
    cls = [_classification(valence="endorse", confidence=0.95,
                            published=long_ago)]
    out = trump_signal.evaluate("ACME", cls, ttl_days=30)
    assert out["mention"] is False
    assert out["valence"] == "none"
    assert out["low_confidence_seen"] == []  # stale, not low-confidence


def test_low_confidence_does_not_pass_but_is_logged():
    """Mention exists, in TTL, but below the confidence floor."""
    cls = [_classification(valence="endorse", confidence=0.4,
                            published=date.today())]
    out = trump_signal.evaluate("ACME", cls, min_confidence=0.6)
    assert out["mention"] is False
    assert out["valence"] == "none"
    # The under-threshold mention IS recorded for review.
    assert len(out["low_confidence_seen"]) == 1
    assert out["low_confidence_seen"][0]["valence"] == "endorse"
    assert out["low_confidence_seen"][0]["confidence"] == 0.4


def test_neutral_news_returns_no_mention():
    cls = [{"direction": "bullish", "magnitude": 3, "durability": "medium",
            "trump_mention": False, "trump_valence": "none",
            "trump_confidence": 0.0,
            "published": date.today().isoformat(),
            "headline": "Earnings beat", "one_line_summary": "Earnings beat"}]
    out = trump_signal.evaluate("ACME", cls)
    assert out["mention"] is False
    assert out["confidence"] == 0.0


# ---------------------------------------------------------------------------
# Manual override
# ---------------------------------------------------------------------------

def test_manual_override_respected(tmp_path):
    """A manual watchlist entry within TTL takes precedence."""
    wl = tmp_path / "trump_watchlist.yaml"
    wl.write_text(yaml.safe_dump([
        {"ticker": "ACME", "valence": "endorse",
         "as_of": date.today().isoformat(),
         "note": "Direct manual override"},
    ]))
    # Even with a contradicting news classification, the manual entry wins.
    cls = [_classification(valence="attack", confidence=0.95,
                            published=date.today())]
    out = trump_signal.evaluate("ACME", cls, manual_overrides_path=wl)
    assert out["mention"] is True
    assert out["valence"] == "endorse"
    assert out["confidence"] == 1.0
    assert out["manual"] is True
    assert "manual" in out["summary"].lower()


def test_manual_override_outside_ttl_does_not_count(tmp_path):
    wl = tmp_path / "trump_watchlist.yaml"
    wl.write_text(yaml.safe_dump([
        {"ticker": "ACME", "valence": "endorse",
         "as_of": (date.today() - timedelta(days=200)).isoformat(),
         "note": "Stale"},
    ]))
    out = trump_signal.evaluate("ACME", [], manual_overrides_path=wl,
                                  ttl_days=30)
    assert out["mention"] is False


def test_manual_override_picks_most_recent_for_same_ticker(tmp_path):
    wl = tmp_path / "trump_watchlist.yaml"
    wl.write_text(yaml.safe_dump([
        {"ticker": "ACME", "valence": "attack",
         "as_of": (date.today() - timedelta(days=10)).isoformat()},
        {"ticker": "ACME", "valence": "endorse",
         "as_of": (date.today() - timedelta(days=2)).isoformat()},
    ]))
    out = trump_signal.evaluate("ACME", [], manual_overrides_path=wl)
    assert out["valence"] == "endorse"


def test_malformed_watchlist_is_ignored(tmp_path):
    wl = tmp_path / "trump_watchlist.yaml"
    wl.write_text("{garbage not yaml")
    out = trump_signal.evaluate("ACME", [], manual_overrides_path=wl)
    assert out["mention"] is False


# ---------------------------------------------------------------------------
# Keyword fallback in news_classifier
# ---------------------------------------------------------------------------

def test_keyword_fallback_detects_endorsement():
    """When the LLM is stubbed out, the deterministic fallback fires."""
    item = {
        "headline": "President Trump praises ACME, calls it the best.",
        "summary": "",
        "published": date.today().isoformat(),
    }
    # Stub the LLM client to None-like behavior (returns no classifications)
    def _dead_client(system, user):
        return None
    out = news_classifier.classify_news_items(
        "ACME", [item], llm_client=_dead_client)
    assert len(out) == 1
    r = out[0]
    assert r["source"] == "keyword_fallback"
    assert r["trump_mention"] is True
    assert r["trump_valence"] == "endorse"
    assert r["trump_confidence"] >= 0.6


def test_keyword_fallback_detects_attack():
    item = {
        "headline": "Trump blasts ACME, threatens tariffs",
        "summary": "",
        "published": date.today().isoformat(),
    }
    out = news_classifier.classify_news_items(
        "ACME", [item], llm_client=lambda s, u: None)
    r = out[0]
    assert r["trump_mention"] is True
    assert r["trump_valence"] == "attack"


def test_keyword_fallback_ignores_unrelated_news():
    item = {
        "headline": "ACME reports Q3 earnings beat",
        "summary": "",
        "published": date.today().isoformat(),
    }
    out = news_classifier.classify_news_items(
        "ACME", [item], llm_client=lambda s, u: None)
    r = out[0]
    assert r["trump_mention"] is False
    assert r["trump_valence"] == "none"


def test_keyword_fallback_ambiguous_marks_low_confidence():
    """Trump mention present but no clear valence -> low-confidence flag."""
    item = {
        "headline": "Trump comments on the ACME deal",
        "summary": "",
        "published": date.today().isoformat(),
    }
    out = news_classifier.classify_news_items(
        "ACME", [item], llm_client=lambda s, u: None)
    r = out[0]
    assert r["trump_mention"] is True
    # 'deal' is a positive term, so this actually picks endorse. Verify a
    # cleanly-ambiguous one:
    item2 = {
        "headline": "Trump mentioned ACME in remarks today",
        "summary": "",
        "published": date.today().isoformat(),
    }
    out2 = news_classifier.classify_news_items(
        "ACME", [item2], llm_client=lambda s, u: None)
    r2 = out2[0]
    assert r2["trump_mention"] is True
    assert r2["trump_valence"] == "none"
    assert r2["trump_confidence"] < 0.6


# ---------------------------------------------------------------------------
# End-to-end through the news_classifier -> trump_signal pipeline
# ---------------------------------------------------------------------------

def test_end_to_end_keyword_endorsement_passes_signal(tmp_path):
    item = {
        "headline": "President Trump praises ACME, calls it best",
        "summary": "",
        "published": date.today().isoformat(),
    }
    classes = news_classifier.classify_news_items(
        "ACME", [item], llm_client=lambda s, u: None)
    out = trump_signal.evaluate("ACME", classes,
                                  manual_overrides_path=tmp_path / "no.yaml")
    assert out["mention"] is True
    assert out["valence"] == "endorse"
