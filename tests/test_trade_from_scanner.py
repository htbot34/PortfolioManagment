"""Tests for daily_brief._trade_from_scanner candidate selection.

Verifies the post-rewrite behavior:
  - Candidates flow from every bullish bucket (not just breakouts/oversold).
  - The light pre-filter (vol>=1.2, macd_h>0) replaces the strict
    RSI/off52/theme gates that were preventing any candidate from reaching
    the conviction gate.
  - Funnel confluence reorders the queue so multi-source candidates are
    evaluated first.
  - The 3-signal conviction gate is still the binding decision.
"""
from __future__ import annotations

from unittest.mock import patch

from app.portfolio.store import Account
from app.research import daily_brief


def _scanner_row(ticker, *, rsi=58, vol=2.0, macd=0.5, off52=-10.0,
                 theme="Mega cap tech", held=False):
    return {
        "ticker": ticker, "theme": theme, "held": held,
        "price": 100.0, "rsi14": rsi, "macd_hist": macd,
        "vol_ratio_20d": vol, "pct_off_52w_high": off52,
        "stacked_uptrend": True, "above_sma200": True, "breakout_20d": True,
        "atr14": 2.0,
    }


def _empty_buckets(**overrides):
    base = {b: [] for b in daily_brief._BULLISH_BUCKETS}
    base.update({"top_movers_down": [], "top_movers_up": []})
    base.update(overrides)
    return {"buckets": base, "universe_size": 1, "top_movers_down": [],
            "top_movers_up": []}


def _passing_gate(qualifies=True):
    """Return a fake conviction.evaluate result."""
    return {
        "qualifies": qualifies,
        "signals": {
            "technical": {"pass": True, "score": 3, "reason": "all signals up"},
            "sector_momentum": {"pass": True, "score": 2, "reason": "sector strong"},
            "news": {"pass": True, "score": 2, "reason": "bullish news"},
        },
        "summary": "all pass",
        "promoted_by_insider": False,
    }


def test_light_prefilter_admits_breakout_far_from_52w_high():
    """The old hard gate `off52 >= -2` is gone; -20% off should still be evaluated."""
    scan = _empty_buckets(breakouts=[_scanner_row("MDB", off52=-20.0)])
    with patch.object(daily_brief.conviction, "evaluate",
                       return_value=_passing_gate()) as ev:
        out = daily_brief._trade_from_scanner(
            scan, macro={"indices": {}}, macro_line="", exclude=set(),
            account=Account(cash=10_000, total_value=0, currency="USD", positions=[]))
    assert out is not None
    assert out["primary_action"]["ticker"] == "MDB"
    assert ev.called


def test_light_prefilter_admits_high_rsi_setup():
    """RSI 70 (was blocked by 55-65 band) now reaches the gate."""
    scan = _empty_buckets(new_52w_highs=[_scanner_row("OKTA", rsi=70, off52=0.0)])
    with patch.object(daily_brief.conviction, "evaluate",
                       return_value=_passing_gate()) as ev:
        out = daily_brief._trade_from_scanner(
            scan, macro={"indices": {}}, macro_line="", exclude=set(),
            account=Account(cash=10_000, total_value=0, currency="USD", positions=[]))
    assert out is not None
    assert out["primary_action"]["ticker"] == "OKTA"
    assert ev.called


def test_light_prefilter_admits_non_quality_theme():
    """Fintech (was excluded by QUALITY_THEMES whitelist) now reaches the gate."""
    scan = _empty_buckets(breakouts=[_scanner_row("HOOD", theme="Fintech / payments")])
    with patch.object(daily_brief.conviction, "evaluate",
                       return_value=_passing_gate()):
        out = daily_brief._trade_from_scanner(
            scan, macro={"indices": {}}, macro_line="", exclude=set(),
            account=Account(cash=10_000, total_value=0, currency="USD", positions=[]))
    assert out is not None
    assert out["primary_action"]["ticker"] == "HOOD"


def test_prefilter_still_blocks_weak_volume_or_falling_macd():
    """Light pre-filter still catches obvious garbage."""
    scan = _empty_buckets(
        breakouts=[_scanner_row("LOWVOL", vol=0.5),       # vol too low
                    _scanner_row("BADMACD", macd=-0.5)])  # MACD falling
    with patch.object(daily_brief.conviction, "evaluate",
                       return_value=_passing_gate()) as ev:
        out = daily_brief._trade_from_scanner(
            scan, macro={"indices": {}}, macro_line="", exclude=set(),
            account=Account(cash=10_000, total_value=0, currency="USD", positions=[]))
    assert out is None
    assert not ev.called  # Nothing reached the gate.


def test_multiple_bullish_buckets_feed_the_gate():
    """A ticker in momentum_continuation (previously ignored) is now evaluated."""
    scan = _empty_buckets(
        momentum_continuation=[_scanner_row("KLAC")],
        macd_bullish_cross=[_scanner_row("MSFT")],
        pullbacks_to_support=[_scanner_row("GEV")])
    with patch.object(daily_brief.conviction, "evaluate",
                       return_value=_passing_gate()) as ev:
        out = daily_brief._trade_from_scanner(
            scan, macro={"indices": {}}, macro_line="", exclude=set(),
            account=Account(cash=10_000, total_value=0, currency="USD", positions=[]))
    assert out is not None
    # Whatever ran first; what matters is the gate WAS evaluated.
    assert ev.call_count >= 1


def test_funnel_confluence_reorders_queue():
    """A funnel-confluence ticker is evaluated before non-confluence ones."""
    scan = _empty_buckets(
        breakouts=[_scanner_row("AAA"), _scanner_row("BBB"), _scanner_row("ZZZ")])
    funnel = {"confluence": [{"ticker": "ZZZ", "points": 8.0}]}
    seen_tickers = []

    def _spy(payload, **kwargs):
        seen_tickers.append(payload["ticker"])
        # First call fails, so we can observe the order tried.
        return _passing_gate(qualifies=False)

    with patch.object(daily_brief.conviction, "evaluate", side_effect=_spy):
        daily_brief._trade_from_scanner(
            scan, macro={"indices": {}}, macro_line="", exclude=set(),
            funnel=funnel,
            account=Account(cash=10_000, total_value=0, currency="USD", positions=[]))
    assert seen_tickers[0] == "ZZZ"  # funnel candidate evaluated first


def test_gate_log_records_each_evaluated_candidate():
    """Telemetry should see one entry per ticker that hit conviction.evaluate."""
    scan = _empty_buckets(
        breakouts=[_scanner_row("AAA"), _scanner_row("BBB")])
    gate_log: list = []
    with patch.object(daily_brief.conviction, "evaluate",
                       return_value=_passing_gate(qualifies=False)):
        daily_brief._trade_from_scanner(
            scan, macro={"indices": {}}, macro_line="", exclude=set(),
            gate_log=gate_log,
            account=Account(cash=10_000, total_value=0, currency="USD", positions=[]))
    tickers_logged = [e.get("ticker") for e in gate_log]
    assert "AAA" in tickers_logged
    assert "BBB" in tickers_logged


def test_breakdown_regime_still_blocks_all_new_buys():
    """Defense-only regime gate is unchanged."""
    scan = _empty_buckets(breakouts=[_scanner_row("ZZZ")])
    with patch.object(daily_brief.conviction, "evaluate",
                       return_value=_passing_gate()) as ev:
        out = daily_brief._trade_from_scanner(
            scan, macro={"indices": {}}, macro_line="", exclude=set(),
            account=Account(cash=10_000, total_value=0, currency="USD", positions=[]),
            regime={"regime": "breakdown", "confidence": 3})
    assert out is None
    assert not ev.called


def test_held_and_excluded_tickers_skipped():
    """Already-held or explicitly-excluded names are not re-recommended."""
    scan = _empty_buckets(
        breakouts=[_scanner_row("HELD", held=True),
                    _scanner_row("EXCL"),
                    _scanner_row("OK")])
    with patch.object(daily_brief.conviction, "evaluate",
                       return_value=_passing_gate()) as ev:
        out = daily_brief._trade_from_scanner(
            scan, macro={"indices": {}}, macro_line="", exclude={"EXCL"},
            account=Account(cash=10_000, total_value=0, currency="USD", positions=[]))
    assert out is not None
    assert out["primary_action"]["ticker"] == "OK"
    # Only OK should reach the gate.
    assert ev.call_count == 1
