"""Tests for intraday_check and the freshness filter."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from app import intraday_check
from app.data.prices import Quote
from app.portfolio.store import Account, Position


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _q(price=None, day_change_pct=None) -> Quote:
    return Quote(
        ticker="X", price=price, prev_close=None, day_change_pct=day_change_pct,
        market_cap=None, pe_ratio=None, high_52w=None, low_52w=None,
        sector=None, industry=None,
    )


@pytest.fixture
def patch_quote(monkeypatch):
    """Return a dict that the test can populate ticker -> Quote."""
    quotes: dict[str, Quote] = {}
    def fake(ticker, fast=False):
        return quotes.get(ticker.upper(), _q())
    monkeypatch.setattr(intraday_check.prices, "quote", fake)
    return quotes


@pytest.fixture
def patch_technicals(monkeypatch):
    techs: dict[str, dict] = {}
    monkeypatch.setattr(intraday_check.prices, "technicals",
                          lambda t: techs.get(t.upper(), {}))
    return techs


# ---------------------------------------------------------------------------
# Macro
# ---------------------------------------------------------------------------

def test_macro_vix_over_25_fires(patch_quote):
    patch_quote["^VIX"] = _q(price=27.1, day_change_pct=2.0)
    patch_quote["^GSPC"] = _q(price=5000, day_change_pct=0.1)
    alerts = intraday_check.check_macro()
    kinds = [a["text"] for a in alerts]
    assert any("VIX at 27.1" in t for t in kinds)


def test_macro_vix_spike_fires(patch_quote):
    patch_quote["^VIX"] = _q(price=20, day_change_pct=18.5)
    patch_quote["^GSPC"] = _q(price=5000, day_change_pct=0.1)
    alerts = intraday_check.check_macro()
    assert any("spiked" in a["text"] for a in alerts)


def test_macro_spx_minus_2_fires(patch_quote):
    patch_quote["^VIX"] = _q(price=15, day_change_pct=2)
    patch_quote["^GSPC"] = _q(price=5000, day_change_pct=-2.3)
    alerts = intraday_check.check_macro()
    assert any("SPX down" in a["text"] for a in alerts)


def test_macro_quiet_day_no_alerts(patch_quote):
    patch_quote["^VIX"] = _q(price=15, day_change_pct=1)
    patch_quote["^GSPC"] = _q(price=5000, day_change_pct=0.3)
    assert intraday_check.check_macro() == []


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def test_position_gap_down_fires(patch_quote, patch_technicals):
    patch_quote["META"] = _q(price=550, day_change_pct=-6.2)
    acct = Account(cash=0, total_value=0, currency="USD",
                    positions=[Position("META", 11, 677)])
    alerts = intraday_check.check_positions(acct)
    assert len(alerts) == 1
    assert "gapped down" in alerts[0]["text"]
    assert alerts[0]["severity"] == "high"


def test_position_sma50_break_on_heavy_volume_fires(patch_quote, patch_technicals):
    patch_quote["META"] = _q(price=590, day_change_pct=-1.0)
    patch_technicals["META"] = {"sma50": 610, "vol_ratio_20d": 1.8}
    acct = Account(cash=0, total_value=0, currency="USD",
                    positions=[Position("META", 11, 677)])
    alerts = intraday_check.check_positions(acct)
    assert len(alerts) == 1
    assert "lost SMA50" in alerts[0]["text"]
    assert alerts[0]["severity"] == "med"


def test_position_sma50_break_on_normal_volume_quiet(patch_quote, patch_technicals):
    patch_quote["META"] = _q(price=590, day_change_pct=-1.0)
    patch_technicals["META"] = {"sma50": 610, "vol_ratio_20d": 1.0}
    acct = Account(cash=0, total_value=0, currency="USD",
                    positions=[Position("META", 11, 677)])
    assert intraday_check.check_positions(acct) == []


def test_position_no_positions_returns_empty(patch_quote, patch_technicals):
    acct = Account(cash=0, total_value=0, currency="USD", positions=[])
    assert intraday_check.check_positions(acct) == []


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

def test_watchlist_breakout_in_buy_zone(tmp_path, patch_quote, patch_technicals):
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps({"scanner": {"buckets": {
        "breakouts": [{"ticker": "NVDA", "price": 145.00}],
        "oversold_bounces": [],
    }}}))
    patch_quote["NVDA"] = _q(price=144.50, day_change_pct=1)
    alerts = intraday_check.check_watchlist(data_json_path=data_path)
    assert len(alerts) == 1
    assert "NVDA" in alerts[0]["text"]
    assert alerts[0]["kind"] == "watchlist_entry"


def test_watchlist_oversold_rising_through_30(tmp_path, patch_quote, patch_technicals):
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps({"scanner": {"buckets": {
        "breakouts": [],
        "oversold_bounces": [{"ticker": "PLTR", "price": 35.0}],
    }}}))
    patch_quote["PLTR"] = _q(price=35.5, day_change_pct=1)
    patch_technicals["PLTR"] = {"rsi14": 33}
    alerts = intraday_check.check_watchlist(data_json_path=data_path)
    assert len(alerts) == 1
    assert "RSI" in alerts[0]["text"]


def test_watchlist_no_data_json_returns_empty(tmp_path):
    alerts = intraday_check.check_watchlist(data_json_path=tmp_path / "missing.json")
    assert alerts == []


def test_watchlist_below_breakout_threshold_quiet(tmp_path, patch_quote, patch_technicals):
    data_path = tmp_path / "data.json"
    data_path.write_text(json.dumps({"scanner": {"buckets": {
        "breakouts": [{"ticker": "NVDA", "price": 145.00}],
        "oversold_bounces": [],
    }}}))
    patch_quote["NVDA"] = _q(price=140.0)  # below 99.5% of 145
    assert intraday_check.check_watchlist(data_json_path=data_path) == []


# ---------------------------------------------------------------------------
# Orchestration + freshness filter
# ---------------------------------------------------------------------------

def test_run_writes_payload(patch_quote, patch_technicals, monkeypatch, tmp_path):
    monkeypatch.setattr(intraday_check, "store", _StoreStub(
        Account(cash=0, total_value=0, currency="USD", positions=[]),
    ))
    monkeypatch.setattr(intraday_check, "DATA_JSON_PATH", tmp_path / "data.json")
    payload = intraday_check.run()
    assert "checked_at" in payload and "alerts" in payload
    assert isinstance(payload["alerts"], list)


def test_freshness_filter_under_90_min(tmp_path, monkeypatch):
    """Verify build_site._load_intraday_alerts surfaces fresh alerts."""
    from app import build_site
    p = tmp_path / "intraday_alerts.json"
    now = datetime.now(timezone.utc) - timedelta(minutes=45)
    p.write_text(json.dumps({
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "alerts": [{"severity": "high", "kind": "macro_shock", "text": "VIX 27"}],
    }))
    monkeypatch.setattr(build_site, "INTRADAY_ALERTS_PATH", p)
    out = build_site._load_intraday_alerts()
    assert out is not None
    assert out["alerts"][0]["text"] == "VIX 27"
    assert out["age_minutes"] <= 90


def test_freshness_filter_drops_stale(tmp_path, monkeypatch):
    from app import build_site
    p = tmp_path / "intraday_alerts.json"
    stale = datetime.now(timezone.utc) - timedelta(minutes=120)
    p.write_text(json.dumps({
        "checked_at": stale.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "alerts": [{"severity": "high", "kind": "macro_shock", "text": "old"}],
    }))
    monkeypatch.setattr(build_site, "INTRADAY_ALERTS_PATH", p)
    assert build_site._load_intraday_alerts() is None


def test_freshness_filter_missing_file(tmp_path, monkeypatch):
    from app import build_site
    monkeypatch.setattr(build_site, "INTRADAY_ALERTS_PATH", tmp_path / "missing.json")
    assert build_site._load_intraday_alerts() is None


# ---------------------------------------------------------------------------
# Small store stub used by test_run_writes_payload
# ---------------------------------------------------------------------------

class _StoreStub:
    def __init__(self, account):
        self._a = account
    def load(self):
        return self._a
