"""Tests for app.research.gate_telemetry."""
from datetime import date, timedelta

from app.research import gate_telemetry


def _ev(ticker, technical=True, sector=True, news=True, qualifies=None,
        promoted=False, insider_score=0, earnings_block=None, pre_block=None):
    """Build one gate-eval entry as daily_brief would.

    A failing technical signal short-circuits the conviction gate, so the
    sector/news signals never run - they read False, matching reality.
    """
    if pre_block:
        return {"ticker": ticker, "pre_block": pre_block}
    if not technical:
        sector = news = False
    reasons = {"technical": technical, "sector_momentum": sector, "news": news}
    if qualifies is None:
        qualifies = technical and sector and news
    return {"ticker": ticker, "pre_block": None, "qualifies": qualifies,
            "promoted_by_insider": promoted, "earnings_block": earnings_block,
            "reasons": reasons, "insider_score": insider_score}


def test_record_shape():
    evals = [_ev("AAA"), _ev("BBB", news=False)]
    rec = gate_telemetry.record(evals, evals, "2026-05-22")
    assert rec["date"] == "2026-05-22"
    assert rec["candidates_evaluated"] == 2
    assert rec["cleared_primary"] == 1
    assert rec["cleared_insider_promotion"] == 0
    assert set(rec["blocked_by"]) == {
        "technical", "sector_momentum", "news",
        "earnings_window", "regime", "soft_veto"}
    assert rec["blocked_by"]["news"] == 1
    assert isinstance(rec["near_miss"], list)
    assert rec["near_miss"][0]["ticker"] == "BBB"
    assert rec["near_miss"][0]["failed"] == "news"
    assert rec["near_miss"][0]["passed"] == ["technical", "sector_momentum"]


def test_insider_promotion_counted_separately():
    evals = [_ev("PRM", news=False, qualifies=True, promoted=True, insider_score=2)]
    rec = gate_telemetry.record(evals, evals, "2026-05-22")
    assert rec["cleared_primary"] == 0
    assert rec["cleared_insider_promotion"] == 1
    # A promoted candidate is not a near-miss.
    assert rec["near_miss"] == []


def test_pre_block_and_earnings():
    evals = [
        _ev("RG", pre_block="regime"),
        _ev("SV", pre_block="soft_veto"),
        _ev("ER", qualifies=False, earnings_block="earnings within 3 days"),
    ]
    rec = gate_telemetry.record(evals, evals, "2026-05-22")
    assert rec["blocked_by"]["regime"] == 1
    assert rec["blocked_by"]["soft_veto"] == 1
    assert rec["blocked_by"]["earnings_window"] == 1
    # The earnings candidate passed all 3 signals -> not a near-miss.
    assert rec["near_miss"] == []


def test_technical_fail_blocks_on_technical():
    evals = [_ev("TKO", technical=False, qualifies=False)]
    rec = gate_telemetry.record(evals, evals, "2026-05-22")
    assert rec["blocked_by"]["technical"] == 1
    # technical fail -> 0 of 3 passed -> not a near-miss
    assert rec["near_miss"] == []


def test_near_miss_sorted_by_insider_then_ticker():
    evals = [
        _ev("ZZZ", news=False, insider_score=1),
        _ev("AAA", news=False, insider_score=3),
        _ev("MMM", news=False, insider_score=3),
    ]
    rec = gate_telemetry.record(evals, evals, "2026-05-22")
    assert [n["ticker"] for n in rec["near_miss"]] == ["AAA", "MMM", "ZZZ"]


def test_near_miss_capped_at_five():
    evals = [_ev(f"T{i}", news=False) for i in range(8)]
    rec = gate_telemetry.record(evals, evals, "2026-05-22")
    assert len(rec["near_miss"]) == 5


def test_rolling_window_keeps_last_30(tmp_path):
    p = tmp_path / "gt.yaml"
    start = date(2026, 1, 1)
    for i in range(40):
        d = (start + timedelta(days=i)).isoformat()
        gate_telemetry.persist(gate_telemetry.record([], [], d), path=p)
    hist = gate_telemetry.load(p)
    assert len(hist) == 30
    assert hist[0]["date"] == (start + timedelta(days=10)).isoformat()
    assert hist[-1]["date"] == (start + timedelta(days=39)).isoformat()


def test_same_day_rewrite_replaces(tmp_path):
    p = tmp_path / "gt.yaml"
    gate_telemetry.persist(gate_telemetry.record([1, 2], [], "2026-05-22"), path=p)
    gate_telemetry.persist(gate_telemetry.record([1], [], "2026-05-22"), path=p)
    hist = gate_telemetry.load(p)
    assert len(hist) == 1
    assert hist[0]["candidates_evaluated"] == 1


def test_load_missing_and_malformed(tmp_path):
    assert gate_telemetry.load(tmp_path / "nope.yaml") == []
    bad = tmp_path / "bad.yaml"
    bad.write_text("{not: valid: yaml: [")
    assert gate_telemetry.load(bad) == []


def test_rollup_math():
    hist = [
        {"date": "2026-05-20", "cleared_primary": 1,
         "cleared_insider_promotion": 0,
         "blocked_by": {"technical": 0, "sector_momentum": 1, "news": 2,
                        "earnings_window": 0, "regime": 0, "soft_veto": 0},
         "near_miss": [{"ticker": "A"}, {"ticker": "B"}]},
        {"date": "2026-05-21", "cleared_primary": 0,
         "cleared_insider_promotion": 1,
         "blocked_by": {"technical": 0, "sector_momentum": 0, "news": 3,
                        "earnings_window": 0, "regime": 0, "soft_veto": 0},
         "near_miss": [{"ticker": "C"}]},
    ]
    r = gate_telemetry.rollup(hist)
    assert r["days"] == 2
    assert r["cleared"] == 2
    assert r["reached_2of3"] == 4          # 3 near-miss + 1 insider promotion
    assert r["mean_near_miss_per_day"] == 1.5
    assert r["top_blocker_signal"] == "news"   # 2 + 3 = 5 of 6 total blocks
    assert r["top_blocker_pct"] == round(5 / 6 * 100, 1)


def test_rollup_empty():
    r = gate_telemetry.rollup([])
    assert r["days"] == 0
    assert r["cleared"] == 0
    assert r["reached_2of3"] == 0
    assert r["top_blocker_signal"] is None
