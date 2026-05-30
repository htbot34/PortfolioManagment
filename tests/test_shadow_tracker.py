"""Tests for app.research.shadow_tracker.

The shadow tracker is a measurement-only feature: it records the gate's
near-misses and tracks how the rejected tickers performed over forward
horizons. These tests cover record creation, return computation, direction-
aware hit logic, per-signal calibration aggregation, idempotent re-runs,
graceful handling of missing price data, and an explicit guarantee that the
tracker never mutates gate state or thresholds.
"""
from __future__ import annotations

import hashlib
import importlib
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
import yaml

from app.research import shadow_tracker


# --------------------------- helpers ---------------------------------------


def _bday_index(start: str, periods: int) -> pd.DatetimeIndex:
    """Business-day index starting at ``start`` (inclusive)."""
    return pd.bdate_range(start=start, periods=periods)


def _frame(index: pd.DatetimeIndex, prices: list[float]) -> pd.DataFrame:
    assert len(index) == len(prices)
    return pd.DataFrame({"Close": prices}, index=index)


def _write_telemetry(path: Path, rows: list[dict]) -> None:
    path.write_text(yaml.safe_dump(rows, sort_keys=False))


def _make_history_fn(frames: dict[str, pd.DataFrame]):
    def _fn(ticker: str):
        return frames.get(ticker.upper())
    return _fn


# A simple deterministic price world used across tests.
# Ticker LEU climbs steadily; SPY climbs slower. Excess return is positive.
_INDEX = _bday_index("2026-05-04", 40)
# 0..39 -> 100, 101, 102, ...
_LEU_PRICES = [100.0 + i for i in range(len(_INDEX))]
# SPY climbs at half the rate.
_SPY_PRICES = [500.0 + 0.5 * i for i in range(len(_INDEX))]
# A "flat" ticker for negative-hit scenarios.
_FLAT_PRICES = [100.0 for _ in range(len(_INDEX))]


def _frames_default() -> dict[str, pd.DataFrame]:
    return {
        "LEU": _frame(_INDEX, _LEU_PRICES),
        "SPY": _frame(_INDEX, _SPY_PRICES),
        "FLAT": _frame(_INDEX, _FLAT_PRICES),
    }


def _telemetry_one(ticker: str = "LEU", failed: str = "news",
                   miss_date: str = "2026-05-11") -> list[dict]:
    return [{
        "date": miss_date,
        "candidates_evaluated": 1,
        "cleared_primary": 0,
        "cleared_insider_promotion": 0,
        "blocked_by": {"technical": 0, "sector_momentum": 0,
                       "news": 1 if failed == "news" else 0,
                       "earnings_window": 0, "regime": 0, "soft_veto": 0},
        "near_miss": [{
            "ticker": ticker,
            "passed": [s for s in ("technical", "sector_momentum", "news")
                       if s != failed],
            "failed": failed,
            "insider_score": 0,
        }],
    }]


# --------------------------- tests -----------------------------------------


def test_record_created_from_telemetry_near_miss(tmp_path):
    tel = tmp_path / "gate_telemetry.yaml"
    ledger = tmp_path / "shadow_ledger.yaml"
    calib = tmp_path / "shadow_calibration.yaml"
    _write_telemetry(tel, _telemetry_one())

    shadow_tracker.update(
        telemetry_path=tel, ledger_path=ledger, calibration_path=calib,
        today=date(2026, 7, 1),
        history_fn=_make_history_fn(_frames_default()),
    )
    records = shadow_tracker.load_ledger(ledger)
    assert len(records) == 1
    r = records[0]
    assert r["ticker"] == "LEU"
    assert r["miss_date"] == "2026-05-11"
    assert r["failed_signal"] == "news"
    assert r["direction"] == "long"
    assert r["entry_price"] is not None
    # Entry close is the first trading row at or after 2026-05-11 (a Monday).
    assert r["entry_price"] == pytest.approx(105.0)


def test_forward_returns_and_excess_at_each_horizon(tmp_path):
    tel = tmp_path / "gate_telemetry.yaml"
    ledger = tmp_path / "shadow_ledger.yaml"
    calib = tmp_path / "shadow_calibration.yaml"
    _write_telemetry(tel, _telemetry_one())

    shadow_tracker.update(
        telemetry_path=tel, ledger_path=ledger, calibration_path=calib,
        today=date(2026, 7, 1),
        history_fn=_make_history_fn(_frames_default()),
    )
    rec = shadow_tracker.load_ledger(ledger)[0]
    # Entry pos = index of 2026-05-11 (the 6th business day, index 5 -> price 105).
    # SPY entry = 500 + 0.5*5 = 502.5
    assert rec["benchmark_entry_price"] == pytest.approx(502.5)

    for h in (5, 10, 20):
        slot = rec["horizons"][str(h)]
        assert slot["status"] == "realized"
        # Exit close = price at index 5+h.
        expected_exit = 100.0 + (5 + h)
        expected_spy_exit = 500.0 + 0.5 * (5 + h)
        assert slot["exit_price"] == pytest.approx(expected_exit)
        assert slot["benchmark_exit_price"] == pytest.approx(expected_spy_exit)
        expected_fwd = (expected_exit - 105.0) / 105.0
        expected_spy_fwd = (expected_spy_exit - 502.5) / 502.5
        assert slot["forward_return"] == pytest.approx(expected_fwd)
        assert slot["benchmark_forward_return"] == pytest.approx(expected_spy_fwd)
        assert slot["excess_return"] == pytest.approx(expected_fwd - expected_spy_fwd)


def test_direction_aware_hit_logic_long():
    """For a long near-miss, positive excess return -> hit=True (rejection
    looked wrong); zero or negative excess -> hit=False."""
    assert shadow_tracker._hit_for_direction("long", 0.01) is True
    assert shadow_tracker._hit_for_direction("long", -0.01) is False
    assert shadow_tracker._hit_for_direction("long", 0.0) is False
    # Unrealized horizons stay None.
    assert shadow_tracker._hit_for_direction("long", None) is None


def test_direction_aware_hit_logic_short():
    """For a short near-miss the relationship inverts: negative excess
    return -> hit=True (rejection looked wrong)."""
    assert shadow_tracker._hit_for_direction("short", -0.01) is True
    assert shadow_tracker._hit_for_direction("short", 0.01) is False
    assert shadow_tracker._hit_for_direction("short", 0.0) is False


def test_flat_ticker_against_rising_spy_is_a_miss(tmp_path):
    tel = tmp_path / "gate_telemetry.yaml"
    ledger = tmp_path / "shadow_ledger.yaml"
    calib = tmp_path / "shadow_calibration.yaml"
    _write_telemetry(tel, _telemetry_one(ticker="FLAT"))

    shadow_tracker.update(
        telemetry_path=tel, ledger_path=ledger, calibration_path=calib,
        today=date(2026, 7, 1),
        history_fn=_make_history_fn(_frames_default()),
    )
    rec = shadow_tracker.load_ledger(ledger)[0]
    for h in (5, 10, 20):
        slot = rec["horizons"][str(h)]
        assert slot["status"] == "realized"
        # Flat ticker underperforms a rising benchmark -> not a hit.
        assert slot["excess_return"] < 0
        assert slot["hit"] is False


def test_per_signal_calibration_aggregation_includes_news(tmp_path):
    tel = tmp_path / "gate_telemetry.yaml"
    ledger = tmp_path / "shadow_ledger.yaml"
    calib = tmp_path / "shadow_calibration.yaml"
    _write_telemetry(tel, [
        _telemetry_one(ticker="LEU",  failed="news",            miss_date="2026-05-11")[0],
        _telemetry_one(ticker="FLAT", failed="news",            miss_date="2026-05-12")[0],
        _telemetry_one(ticker="LEU",  failed="sector_momentum", miss_date="2026-05-13")[0],
    ])

    rollup = shadow_tracker.update(
        telemetry_path=tel, ledger_path=ledger, calibration_path=calib,
        today=date(2026, 7, 1),
        history_fn=_make_history_fn(_frames_default()),
    )
    assert rollup["total_records"] == 3
    by_sig = rollup["by_failed_signal"]
    assert "news" in by_sig
    assert by_sig["news"]["count"] == 2
    # At horizon=5 the news subset has 1 hit (LEU) and 1 miss (FLAT).
    news_h5 = by_sig["news"]["horizons"]["5"]
    assert news_h5["n_realized"] == 2
    assert news_h5["hit_rate"] == pytest.approx(0.5)
    # sector_momentum subset is the rising LEU -> 100% hit at every horizon.
    sec_h5 = by_sig["sector_momentum"]["horizons"]["5"]
    assert sec_h5["hit_rate"] == pytest.approx(1.0)
    # Overall covers all three records.
    assert rollup["overall"]["count"] == 3
    assert rollup["overall"]["horizons"]["5"]["n_realized"] == 3

    # The calibration file is persisted with the same content.
    on_disk = yaml.safe_load(calib.read_text())
    assert on_disk["total_records"] == 3
    assert "news" in on_disk["by_failed_signal"]


def test_idempotent_rerun_does_not_duplicate_rows(tmp_path):
    tel = tmp_path / "gate_telemetry.yaml"
    ledger = tmp_path / "shadow_ledger.yaml"
    calib = tmp_path / "shadow_calibration.yaml"
    _write_telemetry(tel, _telemetry_one())

    hist_fn = _make_history_fn(_frames_default())
    for _ in range(3):
        shadow_tracker.update(
            telemetry_path=tel, ledger_path=ledger, calibration_path=calib,
            today=date(2026, 7, 1), history_fn=hist_fn,
        )
    records = shadow_tracker.load_ledger(ledger)
    assert len(records) == 1
    assert records[0]["ticker"] == "LEU"


def test_pending_when_price_data_missing(tmp_path):
    tel = tmp_path / "gate_telemetry.yaml"
    ledger = tmp_path / "shadow_ledger.yaml"
    calib = tmp_path / "shadow_calibration.yaml"
    _write_telemetry(tel, _telemetry_one())

    # No price data at all - history_fn returns None for everything.
    rollup = shadow_tracker.update(
        telemetry_path=tel, ledger_path=ledger, calibration_path=calib,
        today=date(2026, 7, 1),
        history_fn=lambda t: None,
    )
    rec = shadow_tracker.load_ledger(ledger)[0]
    assert rec["entry_price"] is None
    for h in (5, 10, 20):
        slot = rec["horizons"][str(h)]
        assert slot["status"] == "pending"
        assert slot["forward_return"] is None
        assert slot["hit"] is None
    # And the rollup reports 0 realized.
    assert rollup["overall"]["horizons"]["5"]["n_realized"] == 0
    assert rollup["overall"]["horizons"]["5"]["n_pending"] == 1


def test_pending_when_horizon_has_not_elapsed(tmp_path):
    tel = tmp_path / "gate_telemetry.yaml"
    ledger = tmp_path / "shadow_ledger.yaml"
    calib = tmp_path / "shadow_calibration.yaml"
    # A miss only a few trading days before the latest available data:
    # 5-day horizon realized, 10/20 still pending.
    short_index = _bday_index("2026-05-04", 12)  # 12 business days
    leu = _frame(short_index, [100.0 + i for i in range(12)])
    spy = _frame(short_index, [500.0 + 0.5 * i for i in range(12)])
    _write_telemetry(tel, _telemetry_one(miss_date="2026-05-11"))

    shadow_tracker.update(
        telemetry_path=tel, ledger_path=ledger, calibration_path=calib,
        today=date(2026, 5, 20),
        history_fn=_make_history_fn({"LEU": leu, "SPY": spy}),
    )
    rec = shadow_tracker.load_ledger(ledger)[0]
    # Entry row pos = 5 (2026-05-11). 5+5=10 <= 11 -> realized.
    assert rec["horizons"]["5"]["status"] == "realized"
    # 5+10=15 > 11 -> pending.
    assert rec["horizons"]["10"]["status"] == "pending"
    assert rec["horizons"]["20"]["status"] == "pending"


def test_history_fn_exception_does_not_crash(tmp_path):
    tel = tmp_path / "gate_telemetry.yaml"
    ledger = tmp_path / "shadow_ledger.yaml"
    calib = tmp_path / "shadow_calibration.yaml"
    _write_telemetry(tel, _telemetry_one())

    def _boom(_t):
        raise RuntimeError("network down")

    # update() catches per-ticker failures internally and leaves rows pending.
    rollup = shadow_tracker.update(
        telemetry_path=tel, ledger_path=ledger, calibration_path=calib,
        today=date(2026, 7, 1), history_fn=_boom,
    )
    rec = shadow_tracker.load_ledger(ledger)[0]
    assert rec["horizons"]["5"]["status"] == "pending"
    assert rollup["overall"]["horizons"]["5"]["n_realized"] == 0


def test_safe_update_returns_none_on_catastrophic_failure(tmp_path, monkeypatch):
    """A bug inside update() must not break the daily build."""
    def _broken(*a, **kw):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(shadow_tracker, "update", _broken)
    assert shadow_tracker.safe_update() is None


def test_backfill_processes_all_existing_telemetry_near_misses(tmp_path):
    tel = tmp_path / "gate_telemetry.yaml"
    ledger = tmp_path / "shadow_ledger.yaml"
    calib = tmp_path / "shadow_calibration.yaml"
    # Three days, two distinct tickers, including a date with no near-miss.
    rows = [
        _telemetry_one(ticker="LEU",  miss_date="2026-05-11")[0],
        {"date": "2026-05-12", "candidates_evaluated": 0,
         "cleared_primary": 0, "cleared_insider_promotion": 0,
         "blocked_by": {"technical": 0, "sector_momentum": 0, "news": 0,
                        "earnings_window": 0, "regime": 0, "soft_veto": 0},
         "near_miss": []},
        _telemetry_one(ticker="FLAT", miss_date="2026-05-13")[0],
    ]
    _write_telemetry(tel, rows)
    shadow_tracker.update(
        telemetry_path=tel, ledger_path=ledger, calibration_path=calib,
        today=date(2026, 7, 1),
        history_fn=_make_history_fn(_frames_default()),
    )
    records = shadow_tracker.load_ledger(ledger)
    tickers = sorted({r["ticker"] for r in records})
    assert tickers == ["FLAT", "LEU"]
    assert len(records) == 2


# ----------------- measurement-only / no-mutation guarantee ----------------


def _hash_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_tracker_does_not_mutate_gate_telemetry_file(tmp_path):
    tel = tmp_path / "gate_telemetry.yaml"
    ledger = tmp_path / "shadow_ledger.yaml"
    calib = tmp_path / "shadow_calibration.yaml"
    _write_telemetry(tel, _telemetry_one())
    before = _hash_file(tel)
    shadow_tracker.update(
        telemetry_path=tel, ledger_path=ledger, calibration_path=calib,
        today=date(2026, 7, 1),
        history_fn=_make_history_fn(_frames_default()),
    )
    assert _hash_file(tel) == before, "shadow_tracker must not write gate telemetry"


def test_tracker_does_not_import_gate_logic_modules():
    """Measurement-only guarantee: shadow_tracker may only touch the gate's
    OUTPUT (the telemetry file) and the price layer. It must not pull in
    modules that implement or could mutate the conviction gate or its
    signal thresholds.
    """
    mod = importlib.import_module("app.research.shadow_tracker")
    forbidden = {
        "app.research.conviction",
        "app.research.signals",
        "app.research.news_classifier",
        "app.research.insider_signal",
        "app.research.rules",
        "app.research.candidates",
        "app.research.scanner",
        "app.research.daily_brief",
    }
    # The module's top-level dependencies (anything it has bound a name for).
    referenced = set()
    for attr in dir(mod):
        obj = getattr(mod, attr, None)
        modname = getattr(obj, "__module__", None) or getattr(obj, "__name__", None)
        if isinstance(modname, str):
            referenced.add(modname)
    leaked = forbidden & referenced
    assert not leaked, f"shadow_tracker should not import gate logic: {leaked}"


def test_tracker_only_calls_read_only_telemetry_api(monkeypatch, tmp_path):
    """Wrap ``gate_telemetry.persist`` to raise if the tracker ever calls it."""
    from app.research import gate_telemetry as gt

    def _refuse(*a, **kw):
        raise AssertionError("shadow_tracker must not call gate_telemetry.persist")

    monkeypatch.setattr(gt, "persist", _refuse)
    tel = tmp_path / "gate_telemetry.yaml"
    ledger = tmp_path / "shadow_ledger.yaml"
    calib = tmp_path / "shadow_calibration.yaml"
    _write_telemetry(tel, _telemetry_one())
    shadow_tracker.update(
        telemetry_path=tel, ledger_path=ledger, calibration_path=calib,
        today=date(2026, 7, 1),
        history_fn=_make_history_fn(_frames_default()),
    )
