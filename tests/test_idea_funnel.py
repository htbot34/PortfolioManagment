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


# --- queue verdicts ----------------------------------------------------------

def test_pass_verdict_drops_idea():
    out = _merge(
        scan_buckets={"breakouts": [{"ticker": "NVDA", "price": 100}]},
        queue_verdicts={"NVDA": "pass"})
    assert out == []


def test_interested_verdict_boosts_score():
    base = _merge(scan_buckets={"momentum_continuation": [{"ticker": "NVDA", "price": 100}]})
    boosted = _merge(
        scan_buckets={"momentum_continuation": [{"ticker": "NVDA", "price": 100}]},
        queue_verdicts={"NVDA": "interested"})
    assert boosted[0]["score"] > base[0]["score"]
    assert boosted[0]["verdict"] == "interested"


def test_watching_verdict_carried_without_score_change():
    base = _merge(scan_buckets={"momentum_continuation": [{"ticker": "NVDA", "price": 100}]})
    watch = _merge(
        scan_buckets={"momentum_continuation": [{"ticker": "NVDA", "price": 100}]},
        queue_verdicts={"NVDA": "watching"})
    assert watch[0]["score"] == base[0]["score"]
    assert watch[0]["verdict"] == "watching"


def test_no_verdict_defaults_to_open():
    out = _merge(scan_buckets={"breakouts": [{"ticker": "NVDA", "price": 100}]})
    assert out[0]["verdict"] == "open"


# --- swing plan attachment --------------------------------------------------

def test_breakout_idea_gets_a_swing_plan():
    out = _merge(scan_buckets={"breakouts": [
        {"ticker": "NVDA", "price": 100.0, "vol_ratio_20d": 1.8, "atr14": 3.0}]})
    plan = out[0]["swing_plan"]
    assert plan is not None
    assert plan["stop"] < plan["target"]


def test_idea_without_atr_has_no_swing_plan():
    out = _merge(insider_scores={"COIN": {"score": 2, "summary": "cluster"}})
    assert out[0]["swing_plan"] is None


def test_why_summary_phrasing():
    assert idea_funnel._why([{"label": "Momentum"}], 1) == "Momentum signal"
    three = [{"label": "Momentum"}, {"label": "Theme fit"}, {"label": "Insider buying"}]
    assert idea_funnel._why(three, 3) == \
        "Momentum, Theme fit & Insider buying -- 3 signals aligned"


# --- independence-weighted confluence ---------------------------------------

def _ci(ticker, sources, score, swing_plan=None):
    """A funnel idea: sources is a list of (source, detail) pairs."""
    srcs = [{"source": s, "label": s, "detail": d, "points": 1.0}
            for s, d in sources]
    return {"ticker": ticker, "score": score, "source_count": len(srcs),
            "sources": srcs, "swing_plan": swing_plan}


def _cfunnel(*ideas):
    return {"ideas": list(ideas)}


def test_confluence_momentum_theme_ranks_below_momentum_insider():
    mt = _ci("MT", [("momentum", "20-day breakout"), ("theme", "theme fit")], 10.0)
    mi = _ci("MI", [("momentum", "20-day breakout"), ("insider", "cluster")], 10.0)
    top = idea_funnel.top_independent_confluence(_cfunnel(mt, mi), n=2)
    by = {i["ticker"]: i for i in top}
    assert by["MT"]["confluence_multiplier"] == 0.7
    assert by["MI"]["confluence_multiplier"] == 1.2
    assert by["MT"]["confluence_score"] < by["MI"]["confluence_score"]


def test_confluence_three_sources_use_max_pairwise_multiplier():
    idea = _ci("X", [("momentum", "breakout"), ("news", "up 8%"),
                     ("insider", "cluster")], 10.0)
    top = idea_funnel.top_independent_confluence(_cfunnel(idea), n=3)
    # pairs: momentum+news 1.0, momentum+insider 1.2, news+insider 1.3 -> 1.3
    assert top[0]["confluence_multiplier"] == 1.3


def test_confluence_pullback_classified_from_momentum_detail():
    pn = _ci("PN", [("momentum", "pullback to SMA50 support"),
                    ("news", "up 8%")], 10.0)
    mn = _ci("MN", [("momentum", "20-day breakout"), ("news", "up 8%")], 10.0)
    top = idea_funnel.top_independent_confluence(_cfunnel(pn, mn), n=2)
    by = {i["ticker"]: i for i in top}
    assert by["PN"]["confluence_multiplier"] == 1.15
    assert by["MN"]["confluence_multiplier"] == 1.0
    assert "pullback" in by["PN"]["confluence_label"].lower()


def test_confluence_respects_n_and_skips_single_signal():
    single = _ci("S", [("momentum", "breakout")], 99.0)
    multi = [_ci(f"M{i}", [("news", "x"), ("insider", "y")], 10.0 + i)
             for i in range(5)]
    top = idea_funnel.top_independent_confluence(_cfunnel(single, *multi), n=3)
    assert len(top) == 3
    assert all(i["ticker"] != "S" for i in top)
    assert top[0]["ticker"] == "M4"  # highest raw score, equal multiplier


def test_confluence_empty_when_no_multi_signal_ideas():
    f = _cfunnel(_ci("A", [("momentum", "breakout")], 5.0),
                 _ci("B", [("theme", "fit")], 4.0))
    assert idea_funnel.top_independent_confluence(f, n=3) == []


def test_confluence_label_format():
    idea = _ci("X", [("momentum", "breakout"), ("insider", "cluster")], 10.0)
    top = idea_funnel.top_independent_confluence(_cfunnel(idea), n=1)
    assert top[0]["confluence_label"] == \
        "Momentum + insider buying -- 2 independent signals"
