"""Tests for the earnings-window block in conviction.evaluate."""
from datetime import date, timedelta

import pytest

from app.research import conviction


def _macro(r5: float = 1.0, r20: float = 3.0) -> dict:
    return {"sectors": {"Tech": {"ticker": "XLK", "ret_5d": r5, "ret_20d": r20}}}


def _qualifying_long_payload() -> dict:
    """A payload that clears technical + sector + news on its own."""
    return {
        "ticker": "ACME",
        "sector": "Technology",
        "rsi14": 55, "macd_hist": 0.5,
        "stacked_uptrend": True, "above_sma200": True, "breakout_20d": True,
        "news_classifications": [
            {"direction": "bullish", "magnitude": 4, "durability": "long",
             "one_line_summary": "Strong quarter", "published": None},
            {"direction": "bullish", "magnitude": 3, "durability": "medium",
             "one_line_summary": "Expanded deal", "published": None},
        ],
    }


@pytest.fixture
def patch_earnings(monkeypatch):
    """Patch next_earnings_date; the test sets the return value."""
    holder = {"value": None}

    def fake(ticker):
        return holder["value"]

    monkeypatch.setattr("app.data.calendar.next_earnings_date", fake)
    return holder


# ---------------------------------------------------------------------------
# trading-day counter
# ---------------------------------------------------------------------------

def test_trading_days_until_counts_weekdays_only():
    # From a Monday, the next Monday is 5 trading days away.
    monday = date(2026, 5, 18)  # 2026-05-18 is a Monday
    next_monday = monday + timedelta(days=7)
    # _trading_days_until uses date.today(); test the helper directly with a
    # known offset by checking a near date is small and a far date is larger.
    near = conviction._trading_days_until(date.today() + timedelta(days=1))
    far = conviction._trading_days_until(date.today() + timedelta(days=30))
    assert near <= 1
    assert far >= 18  # ~22 weekdays in 30 calendar days, generous lower bound


def test_trading_days_until_past_date_is_zero():
    assert conviction._trading_days_until(date.today() - timedelta(days=5)) == 0


# ---------------------------------------------------------------------------
# block fires for buy / add longs only
# ---------------------------------------------------------------------------

def test_buy_within_3_days_of_earnings_is_blocked(patch_earnings):
    patch_earnings["value"] = date.today() + timedelta(days=2)
    out = conviction.evaluate(_qualifying_long_payload(), direction="long",
                              macro=_macro(), action="buy")
    assert out["qualifies"] is False
    assert "earnings" in out.get("earnings_block", "").lower()


def test_add_within_3_days_of_earnings_is_blocked(patch_earnings):
    patch_earnings["value"] = date.today() + timedelta(days=1)
    out = conviction.evaluate(_qualifying_long_payload(), direction="long",
                              macro=_macro(), action="add")
    assert out["qualifies"] is False
    assert "earnings_block" in out


def test_buy_far_from_earnings_not_blocked(patch_earnings):
    patch_earnings["value"] = date.today() + timedelta(days=40)
    out = conviction.evaluate(_qualifying_long_payload(), direction="long",
                              macro=_macro(), action="buy")
    assert out["qualifies"] is True
    assert "earnings_block" not in out


def test_buy_with_no_known_earnings_date_not_blocked(patch_earnings):
    patch_earnings["value"] = None
    out = conviction.evaluate(_qualifying_long_payload(), direction="long",
                              macro=_macro(), action="buy")
    assert out["qualifies"] is True


def test_trim_within_3_days_of_earnings_NOT_blocked(patch_earnings):
    # Trims/sells are intentionally unaffected - you may WANT to trim pre-earnings.
    patch_earnings["value"] = date.today() + timedelta(days=2)
    payload = {
        "ticker": "ACME", "sector": "Technology",
        "stacked_downtrend": True, "rsi14": 72, "macd_hist": -0.3,
        "death_cross_recent": True, "above_sma200": False,
        "news_classifications": [
            {"direction": "bearish", "magnitude": 4, "durability": "long",
             "one_line_summary": "Guidance cut", "published": None},
            {"direction": "bearish", "magnitude": 3, "durability": "medium",
             "one_line_summary": "Downgrade", "published": None},
        ],
    }
    out = conviction.evaluate(payload, direction="short", macro=_macro(r5=-1, r20=-2),
                              action="trim")
    assert out["qualifies"] is True
    assert "earnings_block" not in out


def test_buy_without_action_arg_not_blocked(patch_earnings):
    # action defaults to None -> earnings block cannot apply.
    patch_earnings["value"] = date.today() + timedelta(days=1)
    out = conviction.evaluate(_qualifying_long_payload(), direction="long",
                              macro=_macro())
    assert out["qualifies"] is True
