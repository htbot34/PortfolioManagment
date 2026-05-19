"""Tests for the three individual conviction signals."""
from app.research import signals


# ---------------------------------------------------------------------------
# technical_signal
# ---------------------------------------------------------------------------

def test_technical_long_strong_setup_passes():
    payload = {
        "rsi14": 55, "macd_hist": 0.5, "macd_cross_up": False,
        "stacked_uptrend": True, "above_sma200": True, "breakout_20d": True,
    }
    out = signals.technical_signal(payload, direction="long")
    assert out["pass"] is True
    assert out["score"] >= 2


def test_technical_long_oversold_with_macd_cross_passes():
    payload = {"rsi14": 25, "macd_hist": 0.05, "macd_cross_up": True}
    out = signals.technical_signal(payload, direction="long")
    assert out["pass"] is True


def test_technical_long_overbought_alone_fails():
    payload = {"rsi14": 78, "macd_hist": 0.1}
    out = signals.technical_signal(payload, direction="long")
    assert out["pass"] is False


def test_technical_short_downtrend_passes():
    payload = {"stacked_downtrend": True, "rsi14": 72, "macd_hist": -0.3,
               "death_cross_recent": False, "above_sma200": False}
    out = signals.technical_signal(payload, direction="short")
    assert out["pass"] is True


def test_technical_short_oversold_fails():
    # An oversold name is a long candidate, not a short - this should fail.
    payload = {"rsi14": 22, "macd_hist": 0.1}
    out = signals.technical_signal(payload, direction="short")
    assert out["pass"] is False


def test_technical_reads_nested_technicals_dict():
    payload = {"technicals": {"rsi14": 55, "stacked_uptrend": True,
                                "above_sma200": True, "breakout_20d": True,
                                "macd_hist": 0.5}}
    out = signals.technical_signal(payload, direction="long")
    assert out["pass"] is True


def test_technical_invalid_direction_raises():
    try:
        signals.technical_signal({}, direction="sideways")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


# ---------------------------------------------------------------------------
# news_signal -- now consumes semantic classifications, not raw headlines
# ---------------------------------------------------------------------------

def _cls(direction, magnitude, durability="medium", summary="x", published=None):
    return {
        "direction": direction, "magnitude": magnitude, "durability": durability,
        "one_line_summary": summary, "headline": summary, "published": published,
    }


def test_news_long_bullish_classifications_pass():
    classifications = [
        _cls("bullish", 3, "long", "Beat and raised guidance"),
        _cls("bullish", 3, "medium", "Expanded partnership"),
    ]
    out = signals.news_signal("ACME", classifications, "long")
    assert out["pass"] is True
    assert len(out["evidence_refs"]) >= 1


def test_news_long_net_below_three_fails():
    # One medium bullish mag 3 => 3 * 0.7 = 2.1 < 3.
    out = signals.news_signal("ACME", [_cls("bullish", 3, "medium")], "long")
    assert out["pass"] is False
    assert "< 3" in out["reason"]


def test_news_long_major_bearish_blocks():
    classifications = [
        _cls("bullish", 5, "long"),     # +5.0
        _cls("bullish", 3, "long"),     # +3.0  -> net 8.0, well above 3
        _cls("bearish", 4, "short"),    # a magnitude-4 bearish item
    ]
    out = signals.news_signal("ACME", classifications, "long")
    assert out["pass"] is False
    assert "bearish" in out["reason"]


def test_news_long_empty_fails():
    assert signals.news_signal("ACME", [], "long")["pass"] is False


def test_news_long_requires_a_magnitude_3_item():
    # Lots of tiny bullish items can clear net 3 but lack a real catalyst.
    classifications = [_cls("bullish", 2, "long") for _ in range(3)]  # 3 * 2.0 = 6.0
    out = signals.news_signal("ACME", classifications, "long")
    assert out["pass"] is False
    assert "magnitude" in out["reason"]


def test_news_short_bearish_passes():
    classifications = [
        _cls("bearish", 3, "long", "Missed and cut guidance"),
        _cls("bearish", 3, "medium", "Analyst downgrade"),
    ]
    out = signals.news_signal("ACME", classifications, "short")
    assert out["pass"] is True


def test_news_old_items_dropped():
    # Item dated more than 14 days ago should not count toward the score.
    classifications = [_cls("bullish", 5, "long", published="2020-01-01")]
    out = signals.news_signal("ACME", classifications, "long")
    assert out["pass"] is False


# ---------------------------------------------------------------------------
# sector_momentum_signal
# ---------------------------------------------------------------------------

def _macro_with_xlk(r5: float, r20: float) -> dict:
    return {"sectors": {"Tech": {"ticker": "XLK", "ret_5d": r5, "ret_20d": r20}}}


def test_sector_long_aligned_passes():
    out = signals.sector_momentum_signal("Technology", _macro_with_xlk(1.2, 3.5), "long")
    assert out["pass"] is True


def test_sector_long_mixed_fails():
    out = signals.sector_momentum_signal("Technology", _macro_with_xlk(-0.5, 3.5), "long")
    assert out["pass"] is False


def test_sector_short_aligned_passes():
    out = signals.sector_momentum_signal("Technology", _macro_with_xlk(-1.0, -2.5), "short")
    assert out["pass"] is True


def test_sector_unknown_fails_closed():
    out = signals.sector_momentum_signal("Crypto", _macro_with_xlk(1, 2), "long")
    assert out["pass"] is False


def test_sector_missing_etf_data_fails_closed():
    macro = {"sectors": {"Tech": {"ticker": "XLK"}}}  # no returns
    out = signals.sector_momentum_signal("Technology", macro, "long")
    assert out["pass"] is False
