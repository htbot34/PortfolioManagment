"""Tests for the swing-trade plan builder."""
from app.research import swing_plan


def test_no_plan_without_atr():
    assert swing_plan.build(100.0, {}, ["breakout"]) is None
    assert swing_plan.build(100.0, {"atr14": 0}, ["breakout"]) is None


def test_no_plan_without_price():
    assert swing_plan.build(None, {"atr14": 3.0}) is None
    assert swing_plan.build(0, {"atr14": 3.0}) is None


def test_breakout_plan_shape():
    p = swing_plan.build(100.0, {"atr14": 3.0}, ["20-day breakout"])
    assert p is not None
    # stop below the entry band, target above it
    assert p["stop"] < p["entry_low"] < p["entry_high"] < p["target"]
    assert p["stop_pct"] < 0 < p["target_pct"]
    assert p["hold_window"].endswith("weeks")


def test_reward_risk_is_two_to_one():
    p = swing_plan.build(100.0, {"atr14": 3.0}, ["momentum continuation"])
    mid = (p["entry_low"] + p["entry_high"]) / 2.0
    reward = p["target"] - mid
    risk = mid - p["stop"]
    assert abs(reward / risk - 2.0) < 0.01


def test_pullback_entry_skews_below_breakout_entry():
    breakout = swing_plan.build(100.0, {"atr14": 3.0}, ["20-day breakout"])
    pullback = swing_plan.build(100.0, {"atr14": 3.0}, ["pullback to SMA50 support"])
    # a pullback setup buys weakness -> its entry band sits lower
    assert pullback["entry_low"] < breakout["entry_low"]
    assert pullback["entry_high"] < breakout["entry_high"]


def test_wider_atr_widens_stop_distance():
    tight = swing_plan.build(100.0, {"atr14": 2.0}, ["breakout"])
    wide = swing_plan.build(100.0, {"atr14": 6.0}, ["breakout"])
    assert (wide["entry_low"] - wide["stop"]) > (tight["entry_low"] - tight["stop"])
