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
    # Trump joined the primary signal set; trump_block tracks
    # attack-veto entry blocks distinctly from the trump-signal block.
    assert set(rec["blocked_by"]) == {
        "technical", "sector_momentum", "news", "trump",
        "earnings_window", "correlation_block", "valuation_block",
        "regime", "soft_veto", "trump_block"}
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


def _overlay_block_ev(ticker, *, correlation_block=None, valuation_block=None):
    """A candidate that cleared all 3 fundamental primaries (so it qualified)
    but was then knocked out by a post-qualification overlay. trump is the
    always-neutral 4th -> a naive attribution would blame it."""
    return {"ticker": ticker, "pre_block": None, "qualifies": False,
            "promoted_by_insider": False,
            "reasons": {"technical": True, "sector_momentum": True,
                         "news": True, "trump": False},
            "insider_score": 0,
            "correlation_block": correlation_block,
            "valuation_block": valuation_block}


def test_correlation_block_not_attributed_to_trump():
    ev = _overlay_block_ev("CORR", correlation_block="high correlation to top holdings")
    rec = gate_telemetry.record([ev], [ev], "2026-06-08")
    assert rec["blocked_by"]["correlation_block"] == 1
    assert rec["blocked_by"]["trump"] == 0       # the bug: was 1
    assert rec["near_miss"] == []                # 3 primaries passed -> not a near-miss


def test_valuation_block_not_attributed_to_trump():
    ev = _overlay_block_ev("VAL", valuation_block="valuation extreme (99th pct in sector)")
    rec = gate_telemetry.record([ev], [ev], "2026-06-08")
    assert rec["blocked_by"]["valuation_block"] == 1
    assert rec["blocked_by"]["trump"] == 0       # the bug: was 1


def test_genuine_trump_neutral_fail_still_attributed_when_a_primary_failed():
    """A real primary failure (sector) is still attributed to that primary,
    not to an overlay - the overlay fields are None here."""
    ev = {"ticker": "SEC", "pre_block": None, "qualifies": False,
          "promoted_by_insider": False,
          "reasons": {"technical": True, "sector_momentum": False,
                       "news": True, "trump": False},
          "insider_score": 0}
    rec = gate_telemetry.record([ev], [ev], "2026-06-08")
    assert rec["blocked_by"]["sector_momentum"] == 1
    assert rec["blocked_by"]["correlation_block"] == 0
    assert rec["blocked_by"]["valuation_block"] == 0


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


# ---------------------------------------------------------------------------
# Phase A: data-availability status fields (finding #2).
# ---------------------------------------------------------------------------

def _near_miss_ev(ticker, *, insider_status, news_status, insider_score=0):
    """A confluence-1 near-miss (tech + sector pass, news fail)."""
    return {"ticker": ticker, "pre_block": None, "qualifies": False,
            "promoted_by_insider": False, "earnings_block": None,
            "reasons": {"technical": True, "sector_momentum": True, "news": False},
            "insider_score": insider_score,
            "insider_status": insider_status, "news_status": news_status}


def test_insider_unavailable_serializes_distinctly_from_zero():
    """An UNREACHABLE insider near-miss must serialize differently from a
    genuine score-0 - both read insider_score 0, only the status disambiguates."""
    zero = _near_miss_ev("ZRO", insider_status="zero", news_status="ok")
    unavail = _near_miss_ev("UNA", insider_status="unavailable", news_status="ok")
    rec = gate_telemetry.record([zero, unavail], [zero, unavail], "2026-06-03")
    nm = {n["ticker"]: n for n in rec["near_miss"]}
    assert nm["ZRO"]["insider_score"] == nm["UNA"]["insider_score"] == 0
    assert nm["ZRO"]["insider_status"] == "zero"
    assert nm["UNA"]["insider_status"] == "unavailable"
    assert nm["ZRO"] != nm["UNA"]                      # serialize differently
    assert rec["insider"]["zero"] == 1
    assert rec["insider"]["unavailable"] == 1


def test_news_outage_serializes_distinctly_from_empty():
    """A both-feeds-down news outage must be distinguishable from genuine
    no-news, in both the per-near-miss record and the per-day rollup."""
    empty = _near_miss_ev("EMP", insider_status="zero", news_status="empty")
    outage = _near_miss_ev("OUT", insider_status="zero", news_status="outage")
    rec = gate_telemetry.record([empty, outage], [empty, outage], "2026-06-03")
    nm = {n["ticker"]: n for n in rec["near_miss"]}
    assert nm["EMP"]["news_status"] == "empty"
    assert nm["OUT"]["news_status"] == "outage"
    assert rec["news"]["empty"] == 1
    assert rec["news"]["outage"] == 1


def test_record_status_buckets_default_for_legacy_evals():
    """Evals lacking the new fields fold into the safe defaults (the old
    test helper _ev never sets them)."""
    evals = [_ev("AAA"), _ev("BBB", news=False)]
    rec = gate_telemetry.record(evals, evals, "2026-06-03")
    assert rec["insider"]["not_evaluated"] == 2
    assert rec["news"]["unknown"] == 2
    assert sum(rec["insider"].values()) == 2
    assert sum(rec["news"].values()) == 2


def test_rollup_counts_new_status_buckets():
    hist = [
        gate_telemetry.record(
            [_near_miss_ev("A", insider_status="unavailable", news_status="ok")],
            [_near_miss_ev("A", insider_status="unavailable", news_status="ok")],
            "2026-06-01"),
        gate_telemetry.record(
            [_near_miss_ev("B", insider_status="zero", news_status="outage")],
            [_near_miss_ev("B", insider_status="zero", news_status="outage")],
            "2026-06-02"),
    ]
    r = gate_telemetry.rollup(hist)
    assert r["insider"]["unavailable"] == 1
    assert r["insider"]["zero"] == 1
    assert r["news"]["ok"] == 1
    assert r["news"]["outage"] == 1
    assert r["insider_unavailable_days"] == 1
    assert r["news_outage_days"] == 1


def test_old_format_records_still_load_and_rollup(tmp_path):
    """Pre-Phase-A records (no insider/news blocks) load and roll up without
    error and contribute nothing to the new buckets."""
    import yaml
    p = tmp_path / "gt.yaml"
    old = {"date": "2026-05-01", "candidates_evaluated": 2,
           "cleared_primary": 0, "cleared_insider_promotion": 0,
           "blocked_by": {"technical": 1, "sector_momentum": 0, "news": 1,
                          "earnings_window": 0, "regime": 0, "soft_veto": 0},
           "near_miss": [{"ticker": "OLD", "passed": ["technical"],
                          "failed": "news", "insider_score": 0}]}
    p.write_text(yaml.safe_dump([old]))
    hist = gate_telemetry.load(p)
    assert len(hist) == 1                       # old format still loads
    r = gate_telemetry.rollup(hist)
    assert r["days"] == 1
    assert r["insider"]["unavailable"] == 0     # absent block -> no spurious count
    assert r["news"]["outage"] == 0
    assert r["insider_unavailable_days"] == 0
    assert r["news_outage_days"] == 0
