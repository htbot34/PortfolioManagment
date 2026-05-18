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
# news_signal
# ---------------------------------------------------------------------------

def test_news_long_bullish_catalysts_pass():
    items = [
        {"headline": "Acme beats Q3 earnings and raises guidance"},
        {"headline": "Acme expands partnership with Globex"},
    ]
    out = signals.news_signal("ACME", items, None, "long")
    assert out["pass"] is True
    assert len(out["evidence_refs"]) >= 1


def test_news_long_mixed_tape_fails():
    items = [
        {"headline": "Acme beats earnings"},
        {"headline": "Acme faces investigation and lawsuit"},
    ]
    out = signals.news_signal("ACME", items, None, "long")
    assert out["pass"] is False
    assert "mixed" in out["reason"].lower()


def test_news_long_empty_fails():
    assert signals.news_signal("ACME", [], None, "long")["pass"] is False


def test_news_short_bearish_passes():
    items = [
        {"headline": "Acme misses revenue, cuts guidance"},
        {"headline": "Analyst downgrades Acme to underperform"},
    ]
    out = signals.news_signal("ACME", items, None, "short")
    assert out["pass"] is True


def test_news_filings_summary_contributes():
    items = [{"headline": "Some neutral coverage"}]
    out = signals.news_signal("ACME", items, "Revenue growth exceeded expectations.", "long")
    assert out["pass"] is True


def test_news_old_items_dropped():
    # Item dated more than 14 days ago should not count.
    items = [{"headline": "Acme beats earnings", "published": "2020-01-01"}]
    out = signals.news_signal("ACME", items, None, "long")
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
