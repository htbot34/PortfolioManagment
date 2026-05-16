from app.research import rules

RISK = {
    "investor": {"risk_tolerance": "aggressive"},
    "constraints": {"max_single_position_pct": 25, "min_cash_buffer_pct": 5, "max_sector_pct": 45},
}


def test_uptrend_aggressive_recommends_add_or_hold():
    out = rules.recommend(
        "X",
        quote={"price": 100, "pe_ratio": 30},
        tech={"sma50": 90, "sma200": 80, "rsi14": 55, "pct_off_52w_high": -8},
        position_ctx={"weight_pct": 10, "unrealized_pl_pct": 20},
        risk=RISK,
        news_count=5,
    )
    assert out["action"] in {"add", "hold"}
    assert out["horizon"] == "long_term"
    assert "Stacked uptrend" in out["thesis"]


def test_overweight_triggers_trim():
    out = rules.recommend(
        "X",
        quote={"price": 100, "pe_ratio": 20},
        tech={"sma50": 90, "sma200": 80, "rsi14": 50, "pct_off_52w_high": -5},
        position_ctx={"weight_pct": 40, "unrealized_pl_pct": 60},
        risk=RISK,
        news_count=2,
    )
    assert out["action"] == "trim"
    assert out["conviction"] >= 3


def test_downtrend_and_deep_drawdown_can_sell():
    out = rules.recommend(
        "X",
        quote={"price": 50, "pe_ratio": None},
        tech={"sma50": 60, "sma200": 80, "rsi14": 35, "pct_off_52w_high": -40},
        position_ctx={"weight_pct": 5, "unrealized_pl_pct": -30},
        risk=RISK,
        news_count=1,
    )
    assert out["action"] in {"sell", "hold"}
