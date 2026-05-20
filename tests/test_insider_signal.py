"""Tests for insider cluster scoring + the conviction promotion path."""
from datetime import date, timedelta

from app.research import conviction, insider_signal


def _txn(filer, code="P", value=100_000, role="", planned=False, days_ago=5):
    d = (date.today() - timedelta(days=days_ago)).isoformat()
    shares = 1000.0
    price = value / shares if shares else 0.0
    return {
        "filer_name": filer, "role": role, "transaction_date": d,
        "transaction_code": code, "acquired_disposed": "A" if code == "P" else "D",
        "shares": shares, "price": price, "total_value": float(value),
        "is_planned_10b5_1": planned,
    }


# ---------------------------------------------------------------------------
# Score tiers (bull)
# ---------------------------------------------------------------------------

def test_score_0_single_buyer():
    txns = [_txn("Alice", value=2_000_000)]
    out = insider_signal.insider_cluster_score("CO", txns)
    assert out["score"] == 0
    assert out["distinct_buyers"] == 1


def test_score_0_below_100k():
    txns = [_txn("Alice", value=30_000), _txn("Bob", value=40_000)]
    out = insider_signal.insider_cluster_score("CO", txns)
    assert out["score"] == 0


def test_score_1_two_buyers_modest_dollars():
    txns = [_txn("Alice", value=120_000), _txn("Bob", value=150_000)]
    out = insider_signal.insider_cluster_score("CO", txns)
    assert out["score"] == 1
    assert out["distinct_buyers"] == 2


def test_score_2_two_buyers_mid_dollars():
    txns = [_txn("Alice", value=400_000), _txn("Bob", value=500_000)]
    out = insider_signal.insider_cluster_score("CO", txns)
    assert out["score"] == 2  # 2 buyers, $900k in 500k-2M band


def test_score_2_four_buyers_any_amount():
    txns = [_txn(n, value=30_000) for n in ("A", "B", "C", "D")]
    # total $120k -> over the $100k floor, 4 distinct buyers -> tier 2
    out = insider_signal.insider_cluster_score("CO", txns)
    assert out["score"] == 2
    assert out["distinct_buyers"] == 4


def test_score_3_four_buyers_over_1m():
    txns = [_txn(n, value=400_000) for n in ("A", "B", "C", "D")]
    out = insider_signal.insider_cluster_score("CO", txns)
    assert out["score"] == 3


def test_score_3_csuite_million_dollar_buy():
    txns = [
        _txn("Jane CEO", value=1_500_000, role="Chief Executive Officer"),
        _txn("Bob Director", value=120_000, role="Director"),
    ]
    out = insider_signal.insider_cluster_score("CO", txns)
    assert out["score"] == 3
    assert "C-suite" in out["summary"]


def test_csuite_buy_alone_without_other_buyer_is_not_score_3():
    txns = [_txn("Jane CEO", value=1_500_000, role="Chief Executive Officer")]
    out = insider_signal.insider_cluster_score("CO", txns)
    assert out["score"] == 0  # only 1 distinct buyer


def test_award_codes_ignored():
    # "A" award grants are not open-market purchases - they don't count.
    txns = [_txn("Alice", code="A", value=2_000_000),
            _txn("Bob", code="A", value=2_000_000)]
    out = insider_signal.insider_cluster_score("CO", txns)
    assert out["score"] == 0


# ---------------------------------------------------------------------------
# Bear-case score + planned-sale exclusion
# ---------------------------------------------------------------------------

def test_short_score_counts_open_market_sales():
    txns = [_txn("Alice", code="S", value=600_000),
            _txn("Bob", code="S", value=700_000)]
    out = insider_signal.insider_cluster_score_short("CO", txns)
    assert out["score"] == 2
    assert out["distinct_sellers"] == 2


def test_short_score_excludes_planned_10b5_1_sales():
    txns = [
        _txn("Alice", code="S", value=600_000, planned=True),
        _txn("Bob", code="S", value=700_000, planned=True),
    ]
    out = insider_signal.insider_cluster_score_short("CO", txns)
    assert out["score"] == 0  # both scheduled -> no signal


def test_short_score_mixed_planned_and_discretionary():
    txns = [
        _txn("Alice", code="S", value=600_000, planned=True),   # excluded
        _txn("Bob", code="S", value=200_000, planned=False),    # counted
        _txn("Carol", code="S", value=200_000, planned=False),  # counted
    ]
    out = insider_signal.insider_cluster_score_short("CO", txns)
    assert out["distinct_sellers"] == 2
    assert out["score"] == 1  # 2 sellers, $400k


# ---------------------------------------------------------------------------
# Promotion path in conviction.evaluate
# ---------------------------------------------------------------------------

def _macro(r5=1.0, r20=3.0):
    return {"sectors": {"Tech": {"ticker": "XLK", "ret_5d": r5, "ret_20d": r20}}}


def _tech_news_pass_sector_fail_payload(insider_txns):
    """Technical + news pass; sector will fail via a bad macro."""
    return {
        "ticker": "ACME",
        "sector": "Technology",
        "rsi14": 55, "macd_hist": 0.5,
        "stacked_uptrend": True, "above_sma200": True, "breakout_20d": True,
        "news_classifications": [
            {"direction": "bullish", "magnitude": 4, "durability": "long",
             "one_line_summary": "Strong quarter", "published": None},
            {"direction": "bullish", "magnitude": 3, "durability": "medium",
             "one_line_summary": "Deal", "published": None},
        ],
        "insider_transactions": insider_txns,
    }


def test_promotion_fires_when_two_of_three_and_insider_score_2():
    txns = [_txn(n, value=400_000) for n in ("A", "B", "C", "D")]  # score 2
    payload = _tech_news_pass_sector_fail_payload(txns)
    out = conviction.evaluate(payload, direction="long",
                              macro=_macro(r5=-1, r20=-2))  # sector fails
    assert out["qualifies"] is True
    assert out["promoted_by_insider"] is True
    assert out["annotation"] == "promoted on insider cluster"
    assert out["signals"]["insider"]["score"] >= 2


def test_promotion_does_not_fire_with_weak_insider():
    txns = [_txn("Alice", value=120_000), _txn("Bob", value=130_000)]  # score 1
    payload = _tech_news_pass_sector_fail_payload(txns)
    out = conviction.evaluate(payload, direction="long",
                              macro=_macro(r5=-1, r20=-2))
    assert out["qualifies"] is False
    assert out["promoted_by_insider"] is False


def test_promotion_never_overrides_a_failing_technical():
    # Technical fails (overbought, no trend); even a perfect insider cluster
    # must not promote.
    txns = [_txn(n, value=1_000_000) for n in ("A", "B", "C", "D")]  # score 3
    payload = {
        "ticker": "ACME", "sector": "Technology",
        "rsi14": 82, "macd_hist": -0.2,
        "stacked_uptrend": False, "above_sma200": False, "breakout_20d": False,
        "news_classifications": [
            {"direction": "bullish", "magnitude": 5, "durability": "long",
             "one_line_summary": "x", "published": None},
        ],
        "insider_transactions": txns,
    }
    out = conviction.evaluate(payload, direction="long", macro=_macro())
    assert out["qualifies"] is False
    assert out["promoted_by_insider"] is False
    assert "insider" not in out["signals"]  # not even evaluated


def test_full_three_of_three_does_not_need_insider():
    payload = _tech_news_pass_sector_fail_payload([])
    out = conviction.evaluate(payload, direction="long", macro=_macro())  # sector OK
    assert out["qualifies"] is True
    assert out["promoted_by_insider"] is False
    assert "insider" not in out["signals"]
