"""Tests for market regime detection + regime-gated daily-brief behavior.

All inputs are synthetic dicts - detect_regime is a pure function.
"""
from app.research import conviction, daily_brief, regime, sizing
from app.portfolio.store import Account, Position


# ---------------------------------------------------------------------------
# Synthetic macro / breadth payloads
# ---------------------------------------------------------------------------

def _macro(spy_price, sma50, sma200, vix, vix_5d=0.0, hyg_trend="flat",
           range_bound=False, ret_5d=0.0, ret_20d=0.0):
    return {
        "spy": {"price": spy_price, "sma50": sma50, "sma200": sma200,
                "ret_5d": ret_5d, "ret_20d": ret_20d,
                "range_bound_10d": range_bound},
        "vix": {"level": vix, "change_5d_pct": vix_5d, "avg_20d": vix},
        "hyg_ief": {"trend": hyg_trend, "ratio_change_20d_pct": 0.0},
    }


def _breadth(pct):
    return {"pct_above_sma50": pct, "n_basket": 50}


# ---------------------------------------------------------------------------
# detect_regime - the four regimes
# ---------------------------------------------------------------------------

def test_detects_risk_on():
    out = regime.detect_regime(
        _macro(spy_price=510, sma50=500, sma200=470, vix=14, hyg_trend="rising"),
        _breadth(72),
    )
    assert out["regime"] == "risk_on"
    assert out["confidence"] >= 2


def test_detects_risk_off():
    # Below the 50d but above the 200d, vix 20-28, breadth mid.
    out = regime.detect_regime(
        _macro(spy_price=485, sma50=500, sma200=470, vix=24, hyg_trend="flat"),
        _breadth(50),
    )
    assert out["regime"] == "risk_off"


def test_detects_chop():
    # SPY range-bound around the 50d, breadth dead-center.
    out = regime.detect_regime(
        _macro(spy_price=501, sma50=500, sma200=480, vix=17, range_bound=True),
        _breadth(50),
    )
    assert out["regime"] == "chop"


def test_detects_breakdown():
    out = regime.detect_regime(
        _macro(spy_price=440, sma50=470, sma200=480, vix=33,
               vix_5d=40, hyg_trend="falling"),
        _breadth(22),
    )
    assert out["regime"] == "breakdown"
    assert out["confidence"] >= 2


def test_empty_inputs_default_to_chop():
    out = regime.detect_regime({}, {})
    assert out["regime"] == "chop"
    assert out["confidence"] == 1


def test_factors_block_present():
    out = regime.detect_regime(
        _macro(spy_price=510, sma50=500, sma200=470, vix=14, hyg_trend="rising"),
        _breadth(72),
    )
    assert "scores" in out["factors"]
    assert out["factors"]["spy_stacked_up"] is True
    assert out["factors"]["breadth_pct"] == 72


def test_frac_helper():
    assert regime._frac([True, True, True, True]) == 1.0
    assert regime._frac([True, False]) == 0.5
    assert regime._frac([]) == 0.0
    assert regime._frac([None, False]) == 0.0


# ---------------------------------------------------------------------------
# Regime-gated behavior
# ---------------------------------------------------------------------------

def _qualifying_scanner_row(ticker="ZZZ"):
    return {
        "ticker": ticker, "theme": "Mega cap tech", "held": False,
        "price": 100.0, "rsi14": 60, "macd_hist": 0.5, "vol_ratio_20d": 2.5,
        "pct_off_52w_high": -1.0,
        "stacked_uptrend": True, "above_sma200": True, "breakout_20d": True,
    }


def test_breakdown_suppresses_new_buys():
    scan = {"buckets": {"breakouts": [_qualifying_scanner_row()],
                         "oversold_bounces": []},
            "top_movers_down": [], "universe_size": 1}
    out = daily_brief._trade_from_scanner(
        scan, macro={"indices": {}}, macro_line="", exclude=set(),
        account=Account(cash=10_000, total_value=0, currency="USD", positions=[]),
        regime={"regime": "breakdown", "confidence": 3},
    )
    assert out is None  # defense only


def test_chop_disables_insider_promotion():
    # In chop, conviction.evaluate must be called with allow_insider_promotion
    # False. Verify the flag itself: a 2-of-3 candidate with a strong insider
    # cluster does NOT promote when the flag is off.
    txns = [{"filer_name": n, "role": "", "transaction_date": "2026-05-18",
             "transaction_code": "P", "acquired_disposed": "A",
             "shares": 1000.0, "price": 400.0, "total_value": 400_000.0,
             "is_planned_10b5_1": False} for n in ("A", "B", "C", "D")]
    payload = {
        "ticker": "ZZZ", "sector": "Technology",
        "rsi14": 55, "macd_hist": 0.5,
        "stacked_uptrend": True, "above_sma200": True, "breakout_20d": True,
        "news_classifications": [
            {"direction": "bullish", "magnitude": 4, "durability": "long",
             "one_line_summary": "x", "published": None},
            {"direction": "bullish", "magnitude": 3, "durability": "medium",
             "one_line_summary": "y", "published": None},
        ],
        "insider_transactions": txns,
    }
    bad_macro = {"sectors": {"Tech": {"ticker": "XLK", "ret_5d": -1, "ret_20d": -2}}}
    promoted = conviction.evaluate(payload, direction="long", macro=bad_macro,
                                   allow_insider_promotion=True)
    blocked = conviction.evaluate(payload, direction="long", macro=bad_macro,
                                  allow_insider_promotion=False)
    assert promoted["qualifies"] is True
    assert blocked["qualifies"] is False


def test_risk_off_downgrades_new_buy_sizing():
    acct = Account(cash=100_000, total_value=0, currency="USD", positions=[])
    normal = sizing.compute_size("new_buy", "ZZZ", 100.0, None, acct, regime="risk_on")
    risk_off = sizing.compute_size("new_buy", "ZZZ", 100.0, None, acct, regime="risk_off")
    # default tier: 3% normal -> $3000; 2% risk_off -> $2000
    assert normal["dollars"] > risk_off["dollars"]
    assert abs(risk_off["dollars"] - 2000) < 200


def test_regime_banner_strings():
    assert "defense only" in daily_brief._regime_banner({"regime": "breakdown"})
    assert "tightened" in daily_brief._regime_banner({"regime": "chop"})
    assert daily_brief._regime_banner({"regime": "risk_on"}) is None
    assert daily_brief._regime_banner(None) is None
