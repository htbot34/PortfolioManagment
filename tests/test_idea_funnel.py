"""Tests for the unified idea funnel merge + scoring."""
from app.research import idea_funnel


UNIVERSE = {"NVDA", "AMD", "PLTR", "CRWD", "SMR", "OKLO", "COIN", "FTNT"}


def _merge(**over):
    kwargs = dict(
        screen_results=[],
        scan_buckets={},
        top_movers_up=[],
        headlines=[],
        insider_scores={},
        held=set(),
        universe_tickers=UNIVERSE,
    )
    kwargs.update(over)
    return idea_funnel.merge_sources(**kwargs)


# --- single-source ----------------------------------------------------------

def test_momentum_only_idea_surfaces():
    out = _merge(scan_buckets={"breakouts": [{"ticker": "NVDA", "price": 100.0,
                                              "vol_ratio_20d": 1.8}]})
    assert len(out) == 1
    idea = out[0]
    assert idea["ticker"] == "NVDA"
    assert idea["source_count"] == 1
    assert idea["sources"][0]["source"] == "momentum"
    assert "1.8x volume" in idea["sources"][0]["detail"]
    assert idea["rank"] == 1


def test_theme_negative_score_is_dropped():
    out = _merge(screen_results=[{"ticker": "AMD", "score": -2.0, "price": 50.0}])
    assert out == []


def test_mover_below_threshold_is_not_a_news_signal():
    out = _merge(top_movers_up=[{"ticker": "AMD", "day_change_pct": 1.0, "price": 50}])
    assert out == []


def test_mover_above_threshold_surfaces():
    out = _merge(top_movers_up=[{"ticker": "AMD", "day_change_pct": 8.0, "price": 50}])
    assert len(out) == 1
    assert out[0]["sources"][0]["source"] == "news"


# --- held exclusion ----------------------------------------------------------

def test_held_names_are_excluded_from_every_source():
    out = _merge(
        held={"NVDA"},
        scan_buckets={"breakouts": [{"ticker": "NVDA", "price": 100}]},
        screen_results=[{"ticker": "NVDA", "score": 4.0}],
        insider_scores={"NVDA": {"score": 3, "summary": "x"}},
    )
    assert out == []


# --- confluence --------------------------------------------------------------

def test_confluence_multi_signal_outranks_single_signal():
    out = _merge(
        scan_buckets={"breakouts": [{"ticker": "AMD", "price": 50, "vol_ratio_20d": 2.0}],
                      "momentum_continuation": [{"ticker": "NVDA", "price": 100}]},
        screen_results=[{"ticker": "NVDA", "score": 5.0, "price": 100}],
        insider_scores={"NVDA": {"score": 2, "summary": "2 buyers"}},
    )
    ranked = {i["ticker"]: i for i in out}
    # NVDA has 3 sources, AMD has 1 -> NVDA must rank first.
    assert out[0]["ticker"] == "NVDA"
    assert ranked["NVDA"]["source_count"] == 3
    assert ranked["NVDA"]["score"] > ranked["AMD"]["score"]


def test_confluence_multiplier_applied():
    # NVDA: momentum 2.5 + theme min(3, 5*0.6)=3.0 = 5.5 base, x1.3 confluence.
    out = _merge(
        scan_buckets={"momentum_continuation": [{"ticker": "NVDA", "price": 100}]},
        screen_results=[{"ticker": "NVDA", "score": 5.0}],
    )
    assert out[0]["source_count"] == 2
    assert out[0]["score"] == round(5.5 * 1.3, 2)


def test_multiple_momentum_buckets_get_capped_bonus():
    out = _merge(scan_buckets={
        "breakouts": [{"ticker": "NVDA", "price": 100, "vol_ratio_20d": 1.5}],
        "new_52w_highs": [{"ticker": "NVDA", "price": 100}],
    })
    # one momentum source, strongest bucket (3.0) + 0.5 bonus for the 2nd setup.
    assert out[0]["source_count"] == 1
    assert out[0]["sources"][0]["points"] == 3.5


# --- insider -----------------------------------------------------------------

def test_insider_score_zero_does_not_surface():
    out = _merge(insider_scores={"COIN": {"score": 0, "summary": "no cluster"}})
    assert out == []


def test_insider_points_scale_with_score():
    out = _merge(insider_scores={"COIN": {"score": 3, "summary": "strong cluster"}})
    assert out[0]["sources"][0]["points"] == 4.5


# --- headline matching -------------------------------------------------------

def test_headline_matches_long_ticker():
    hits = idea_funnel._match_headlines(
        [{"title": "CRWD jumps after earnings beat"}], UNIVERSE, set())
    assert hits == {"CRWD": "CRWD jumps after earnings beat"}


def test_headline_ignores_short_tickers_and_held():
    # 'AI' style short tickers excluded; held names excluded.
    hits = idea_funnel._match_headlines(
        [{"title": "NVDA and AMD lead the rally"}], UNIVERSE, {"AMD"})
    assert hits == {"NVDA": "NVDA and AMD lead the rally"}


def test_why_summary_phrasing():
    assert idea_funnel._why([{"label": "Momentum"}], 1) == "Momentum signal"
    three = [{"label": "Momentum"}, {"label": "Theme fit"}, {"label": "Insider buying"}]
    assert idea_funnel._why(three, 3) == \
        "Momentum, Theme fit & Insider buying -- 3 signals aligned"
