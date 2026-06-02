"""Tests for the conviction.evaluate orchestrator."""
from app.research import conviction


def _macro(r5: float = 1.0, r20: float = 3.0) -> dict:
    return {"sectors": {"Tech": {"ticker": "XLK", "ret_5d": r5, "ret_20d": r20}}}


def _strong_long_payload(**overrides) -> dict:
    base = {
        "ticker": "ACME",
        "sector": "Technology",
        "rsi14": 55,
        "macd_hist": 0.5,
        "stacked_uptrend": True,
        "above_sma200": True,
        "breakout_20d": True,
        "news": [
            {"headline": "Acme beats Q3 earnings"},
            {"headline": "Acme raises full-year guidance"},
        ],
        # Empty list keeps the insider-promotion path from hitting the network
        # in tests that land on the 2-of-3 branch.
        "insider_transactions": [],
    }
    base.update(overrides)
    return base


def test_clean_buy_passes_all_three():
    out = conviction.evaluate(_strong_long_payload(), direction="long", macro=_macro())
    assert out["qualifies"] is True
    # Trump signal is always evaluated (neutral fail when no mention).
    # Neutrality is verified separately in test_conviction_trump_neutrality.
    assert {"technical", "sector_momentum", "news", "trump"}.issubset(out["signals"])
    assert "technical=PASS" in out["summary"]
    assert "sector_momentum=PASS" in out["summary"]
    assert "news=PASS" in out["summary"]
    # No Trump mention -> trump signal is a neutral fail, not a confirmation.
    assert out["signals"]["trump"]["pass"] is False
    assert out["signals"]["trump"]["valence"] == "none"


def test_buy_fails_on_sector_without_insider_does_not_qualify():
    # Technical + news pass, sector fails. With no insider cluster the
    # 2-of-3 promotion path does not fire, so the rec does not qualify.
    out = conviction.evaluate(
        _strong_long_payload(),
        direction="long",
        macro=_macro(r5=-1, r20=-2),
    )
    assert out["qualifies"] is False
    assert out["promoted_by_insider"] is False
    assert out["signals"]["sector_momentum"]["pass"] is False
    # News IS now evaluated even when sector fails (needed for 2-of-3).
    assert "news" in out["signals"]


def test_buy_fails_on_technical_short_circuits_before_sector():
    payload = _strong_long_payload(rsi14=80, macd_hist=-0.1,
                                    stacked_uptrend=False, breakout_20d=False)
    out = conviction.evaluate(payload, direction="long", macro=_macro())
    assert out["qualifies"] is False
    assert "sector_momentum" not in out["signals"]


def test_buy_fails_on_news_only():
    payload = _strong_long_payload(news=[{"headline": "neutral company update"}])
    out = conviction.evaluate(payload, direction="long", macro=_macro())
    assert out["qualifies"] is False
    assert out["signals"]["technical"]["pass"] is True
    assert out["signals"]["sector_momentum"]["pass"] is True
    assert out["signals"]["news"]["pass"] is False


def test_news_fetcher_invoked_only_when_news_missing():
    payload = _strong_long_payload()
    payload.pop("news")
    calls = []

    def fake_fetcher(t):
        calls.append(t)
        return [
            {"headline": "Acme beats Q3 earnings"},
            {"headline": "Acme raises full-year guidance"},
        ]

    out = conviction.evaluate(payload, direction="long", macro=_macro(),
                              news_fetcher=fake_fetcher)
    assert out["qualifies"] is True
    assert calls == ["ACME"]


def test_news_fetcher_skipped_when_payload_has_news():
    calls = []

    def fake(t):
        calls.append(t)
        return []

    out = conviction.evaluate(_strong_long_payload(), direction="long",
                              macro=_macro(), news_fetcher=fake)
    assert out["qualifies"] is True
    assert calls == []  # never called


def test_sell_passes_when_short_aligned():
    payload = {
        "ticker": "ACME",
        "sector": "Technology",
        "stacked_downtrend": True,
        "rsi14": 72,
        "macd_hist": -0.3,
        "death_cross_recent": True,
        "above_sma200": False,
        "news": [
            {"headline": "Acme cuts guidance"},
            {"headline": "Acme downgraded after weak quarter"},
        ],
    }
    out = conviction.evaluate(payload, direction="short", macro=_macro(r5=-1, r20=-2))
    assert out["qualifies"] is True


def test_scanner_row_uses_theme_when_sector_missing():
    payload = {
        "ticker": "ACME",
        "theme": "Mega cap tech",
        "rsi14": 55, "macd_hist": 0.5,
        "stacked_uptrend": True, "above_sma200": True, "breakout_20d": True,
        "news": [
            {"headline": "Acme beats earnings"},
            {"headline": "Acme raises full-year guidance"},
        ],
    }
    out = conviction.evaluate(payload, direction="long", macro=_macro())
    assert out["qualifies"] is True
