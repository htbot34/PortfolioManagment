"""READ-ONLY audit replay #2: one-way-flip proof + Trump signal integrity.

Proves:
 (A) Overlays (trump veto, correlation, valuation, earnings) are deny-only:
     they can flip qualifies True->False but NEVER False->True. Only the
     insider-promotion path (and the off-by-default trump_solo flag) may
     flip False->True.
 (B) The valuation score-3 insider override only PRESERVES an already-True
     qualifies; it cannot create one.
 (C) Trump signal integrity: confidence floor, TTL expiry, manual override
     semantics, low-confidence logged-not-passed, short-side endorse veto.

Nothing is written. No conviction code is modified.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.research import conviction, trump_signal, signals

DEFAULT_CFG = {
    "trump_signal_enabled": True, "trump_ttl_days": 30,
    "trump_min_confidence": 0.6, "trump_confluence_min": 2,
    "trump_solo_with_technical": False, "trump_attack_vetoes_longs": True,
}


def _tech_pass():
    return {"rsi14": 55, "macd_hist": 0.5, "stacked_uptrend": True,
            "above_sma200": True, "breakout_20d": True}


def _macro(ok=True):
    s = 1 if ok else -1
    return {"sectors": {"Tech": {"ticker": "XLK", "ret_5d": s * 2.5, "ret_20d": s * 6.0}}}


def _bull(n=2):
    return [{"direction": "bullish", "magnitude": 5, "durability": "long",
             "one_line_summary": f"good {i}", "published": "2026-06-01",
             "trump_mention": False, "trump_valence": "none", "trump_confidence": 0.0}
            for i in range(n)]


def _bear(n=2):
    return [{"direction": "bearish", "magnitude": 5, "durability": "long",
             "one_line_summary": f"bad {i}", "published": "2026-06-01",
             "trump_mention": False, "trump_valence": "none", "trump_confidence": 0.0}
            for i in range(n)]


print("=" * 80)
print("(A) TRUMP VETO IS ONE-WAY (long attack)")
print("=" * 80)
# Qualifying long (tech+sector+news) + attack -> must flip True->False.
p = {"ticker": "ACME", "sector": "Technology", **_tech_pass(),
     "news_classifications": _bull(),
     "trump_signal_result": {"mention": True, "valence": "attack",
                             "confidence": 0.9, "as_of": "2026-06-01",
                             "source": "TS", "summary": "slammed",
                             "manual": False, "low_confidence_seen": []}}
out = conviction.evaluate(dict(p), direction="long", macro=_macro(True),
                          action="new_buy", gate_config=DEFAULT_CFG)
print(f"  qualifying long + attack(new_buy): qualifies={out['qualifies']} "
      f"trump_block={'trump_block' in out}  (expect False / True)")

# NON-qualifying long (tech only, sector+news fail) + attack -> stays False,
# veto cannot manufacture a True.
p2 = {"ticker": "ACME", "sector": "Technology", **_tech_pass(),
      "news_classifications": _bear(),
      "trump_signal_result": {"mention": True, "valence": "attack",
                              "confidence": 0.9, "as_of": "2026-06-01",
                              "source": "TS", "summary": "slammed",
                              "manual": False, "low_confidence_seen": []}}
out2 = conviction.evaluate(dict(p2), direction="long", macro=_macro(False),
                           action="new_buy", gate_config=DEFAULT_CFG)
print(f"  non-qualifying long + attack: qualifies={out2['qualifies']}  (expect False)")

print("\n" + "=" * 80)
print("(A2) SHORT-SIDE ENDORSE VETO IS ONE-WAY; trim-action gap")
print("=" * 80)
# Qualifying short (tech+sector+news bearish) + endorse -> veto on entry.
ps = {"ticker": "ACME", "sector": "Technology",
      "rsi14": 75, "macd_hist": -0.5, "stacked_downtrend": True,
      "above_sma200": False, "pct_off_52w_high": -30,
      "news_classifications": _bear(),
      "trump_signal_result": {"mention": True, "valence": "endorse",
                              "confidence": 0.9, "as_of": "2026-06-01",
                              "source": "WH", "summary": "praised",
                              "manual": False, "low_confidence_seen": []}}
for act in ("short", "new_short", "sell", "trim"):
    o = conviction.evaluate(dict(ps), direction="short", macro=_macro(False),
                            action=act, gate_config=DEFAULT_CFG)
    print(f"  short+endorse action={act:9s}: qualifies={o['qualifies']} "
          f"trump_block={'trump_block' in o}")

print("\n" + "=" * 80)
print("(B) VALUATION score-3 insider override only PRESERVES, never CREATES")
print("=" * 80)
# Stub fundamentals/comparables to force tier='extreme'. A non-qualifying
# base must remain non-qualifying even with the override branch reachable.
import app.research.valuation as valmod


def _extreme_val(ticker, fundamentals, comparables):
    return {"tier": "extreme", "percentile_in_sector": 99.0, "score": 3}


orig_vs = valmod.valuation_score
valmod.valuation_score = _extreme_val
try:
    # Non-qualifying long: confirmations=1 (sector only), no trump, no insider.
    base = {"ticker": "ACME", "sector": "Technology", **_tech_pass(),
            "news_classifications": _bear(),
            "insider_transactions": [],
            "trump_signal_result": {"mention": False, "valence": "none",
                                    "confidence": 0.0, "as_of": None,
                                    "source": "", "summary": "", "manual": False,
                                    "low_confidence_seen": []}}
    o = conviction.evaluate(dict(base), direction="long", macro=_macro(True),
                            action="new_buy", fundamentals={"sector": "Technology"},
                            sector_comparables=[], gate_config=DEFAULT_CFG)
    print(f"  non-qualifying + extreme-valuation: qualifies={o['qualifies']} "
          f"valuation_override={'valuation_override' in o}  (expect False / False)")

    # Qualifying via insider score-3 (sector+tech, news fail, insider cluster
    # of 4 buyers @ $600k -> tier 3) + extreme valuation -> override PRESERVES.
    d = (date.today() - timedelta(days=5)).isoformat()
    txns = [{"filer_name": f, "role": "Director", "transaction_date": d,
             "transaction_code": "P", "acquired_disposed": "A", "shares": 6000.0,
             "price": 100.0, "total_value": 600_000.0, "is_planned_10b5_1": False}
            for f in ("A", "B", "C", "D")]
    promo = {"ticker": "ACME", "sector": "Technology", **_tech_pass(),
             "news_classifications": _bear(), "insider_transactions": txns,
             "trump_signal_result": {"mention": False, "valence": "none",
                                     "confidence": 0.0, "as_of": None, "source": "",
                                     "summary": "", "manual": False,
                                     "low_confidence_seen": []}}
    o2 = conviction.evaluate(dict(promo), direction="long", macro=_macro(True),
                             action="new_buy", fundamentals={"sector": "Technology"},
                             sector_comparables=[], gate_config=DEFAULT_CFG)
    print(f"  insider-score3 promoted + extreme-valuation: qualifies={o2['qualifies']} "
          f"promoted={o2.get('promoted_by_insider')} "
          f"override={'valuation_override' in o2} insider_score={o2.get('insider_score')}"
          f"  (expect True / True / True / 3)")
finally:
    valmod.valuation_score = orig_vs

print("\n" + "=" * 80)
print("(C) TRUMP CONFIDENCE FLOOR + TTL + MANUAL OVERRIDE")
print("=" * 80)
today = date(2026, 6, 2)
# Low confidence: logged, not passed.
low = [{"trump_mention": True, "trump_valence": "endorse", "trump_confidence": 0.5,
        "published": "2026-06-01", "headline": "low conf", "one_line_summary": "x"}]
f_low = trump_signal.evaluate("ACME", low, today=today, min_confidence=0.6)
print(f"  conf=0.5 (floor 0.6): mention={f_low['mention']} "
      f"low_confidence_seen={len(f_low['low_confidence_seen'])}  (expect False / 1)")

# Exactly at floor: passes.
at = [{"trump_mention": True, "trump_valence": "endorse", "trump_confidence": 0.6,
       "published": "2026-06-01", "headline": "at floor", "one_line_summary": "x"}]
f_at = trump_signal.evaluate("ACME", at, today=today, min_confidence=0.6)
print(f"  conf=0.6 (floor 0.6): mention={f_at['mention']} valence={f_at['valence']}"
      f"  (expect True / endorse)")

# Stale (40 days old, TTL 30): expired.
stale = [{"trump_mention": True, "trump_valence": "endorse", "trump_confidence": 1.0,
          "published": (today - timedelta(days=40)).isoformat(),
          "headline": "stale", "one_line_summary": "x"}]
f_stale = trump_signal.evaluate("ACME", stale, today=today, ttl_days=30)
print(f"  40d-old (TTL 30): mention={f_stale['mention']}  (expect False)")

# Manual override is TTL-gated + valence-validated (confidence 1.0 by design).
import tempfile, os, yaml
from pathlib import Path
wl_fresh = [{"ticker": "ACME", "valence": "endorse", "as_of": "2026-06-01"}]
wl_stale = [{"ticker": "ACME", "valence": "endorse", "as_of": "2026-04-01"}]
wl_badval = [{"ticker": "ACME", "valence": "bogus", "as_of": "2026-06-01"}]
for label, wl, exp in (("fresh", wl_fresh, True), ("stale(>TTL)", wl_stale, False),
                       ("bad-valence", wl_badval, False)):
    tf = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    yaml.safe_dump(wl, tf); tf.close()
    f = trump_signal.evaluate("ACME", [], today=today,
                              manual_overrides_path=Path(tf.name), ttl_days=30)
    os.unlink(tf.name)
    print(f"  manual {label:12s}: mention={f['mention']} conf={f['confidence']} "
          f"manual={f['manual']}  (expect mention={exp})")
