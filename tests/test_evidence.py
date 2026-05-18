"""Tests for daily_brief evidence trail generation."""
from app.research import daily_brief


def test_entry_evidence_overheated_rsi_warns():
    s = {"ticker": "X", "rsi14": 78, "vol_ratio_20d": 2.4,
         "macd_hist": 0.5, "pct_off_52w_high": -1, "theme": "Mega cap tech"}
    t = {"golden_cross_recent": True, "death_cross_recent": False}
    ev = daily_brief._evidence_for_entry(s, t)
    refs = {e["ref"]: e for e in ev}
    assert "RSI 78" in refs
    assert "extended" in refs["RSI 78"]["supports"]
    assert "MACD histogram +0.50" in refs
    assert "golden cross within 20 days" in refs


def test_entry_evidence_oversold_signals_mean_reversion():
    s = {"ticker": "X", "rsi14": 22, "vol_ratio_20d": 1.4,
         "macd_hist": 0.1, "pct_off_52w_high": -35, "theme": "Semiconductors"}
    ev = daily_brief._evidence_for_entry(s, {})
    refs = {e["ref"]: e["supports"] for e in ev}
    assert "deep oversold" in refs["RSI 22"]


def test_defense_evidence_uses_constraint_and_technicals():
    r = {"ticker": "META", "position": {"weight_pct": 64}}
    t = {"stacked_downtrend": True, "rsi14": 25,
         "death_cross_recent": True, "pct_off_52w_high": -30}
    ev = daily_brief._evidence_for_defense(r, t, weight_pct=64)
    sources = {e["source"] for e in ev}
    assert "constraint" in sources
    assert "technical" in sources
    refs = [e["ref"] for e in ev]
    supports = [e.get("supports", "") for e in ev]
    assert any("64%" in x for x in refs)
    assert any("downtrend" in x for x in supports)
    assert any("death cross" in x for x in refs)


def test_defense_evidence_skips_missing_weight():
    ev = daily_brief._evidence_for_defense({"ticker": "X"}, {}, weight_pct=None)
    assert all(e["source"] != "constraint" for e in ev)


def test_entry_evidence_includes_volume_quality():
    s_strong = {"ticker": "X", "vol_ratio_20d": 2.5}
    s_weak = {"ticker": "X", "vol_ratio_20d": 1.1}
    strong = daily_brief._evidence_for_entry(s_strong, {})
    weak = daily_brief._evidence_for_entry(s_weak, {})
    s_supp = next(e["supports"] for e in strong if "volume" in e["ref"])
    w_supp = next(e["supports"] for e in weak if "volume" in e["ref"])
    assert "confirmation" in s_supp and "weak" not in s_supp
    assert "weak" in w_supp
