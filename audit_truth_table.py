"""READ-ONLY audit replay: derive the conviction-gate truth table empirically.

Enumerates every (technical, sector, news, trump, insider) combination,
forces each underlying signal to a known pass/fail by constructing payloads
the LIVE signal modules score deterministically (no mocking of the signal
layer), calls conviction.evaluate, and records qualifies + promoted_by_insider.

It then compares each row to the pre-Trump "old design" rule:
    qualifies_old = tech AND ( (sector AND news)
                               OR (exactly-one-of(sector,news) AND insider>=2) )
Trump did not exist in the old rule, so the old column ignores trump.

Nothing is written. This script changes no state.
"""
from __future__ import annotations

import itertools
from datetime import date, timedelta

from app.research import conviction

DEFAULT_CFG = {
    "trump_signal_enabled": True,
    "trump_ttl_days": 30,
    "trump_min_confidence": 0.6,
    "trump_confluence_min": 2,
    "trump_solo_with_technical": False,
    "trump_attack_vetoes_longs": True,
}


def _macro(sector_pass: bool) -> dict:
    if sector_pass:
        return {"sectors": {"Tech": {"ticker": "XLK", "ret_5d": 2.5, "ret_20d": 6.0}}}
    return {"sectors": {"Tech": {"ticker": "XLK", "ret_5d": -2.5, "ret_20d": -6.0}}}


_BULL = [{"direction": "bullish", "magnitude": 5, "durability": "long",
          "one_line_summary": f"ACME good {i}", "published": "2026-06-01",
          "trump_mention": False, "trump_valence": "none", "trump_confidence": 0.0}
         for i in range(2)]
_BEAR = [{"direction": "bearish", "magnitude": 5, "durability": "long",
          "one_line_summary": f"ACME bad {i}", "published": "2026-06-01",
          "trump_mention": False, "trump_valence": "none", "trump_confidence": 0.0}
         for i in range(2)]


def _payload(tech, news, insider):
    if tech:
        tf = {"rsi14": 55, "macd_hist": 0.5, "stacked_uptrend": True,
              "above_sma200": True, "breakout_20d": True}
    else:
        tf = {"rsi14": 78, "macd_hist": -0.1, "stacked_uptrend": False,
              "above_sma200": False, "breakout_20d": False}
    txns = []
    if insider:
        d = (date.today() - timedelta(days=5)).isoformat()
        for filer in ("Alice", "Bob", "Carol", "Dave"):
            txns.append({"filer_name": filer, "role": "Director",
                         "transaction_date": d, "transaction_code": "P",
                         "acquired_disposed": "A", "shares": 6000.0,
                         "price": 100.0, "total_value": 600_000.0,
                         "is_planned_10b5_1": False})
    return {"ticker": "ACME", "sector": "Technology", **tf,
            "news_classifications": _BULL if news else _BEAR,
            "insider_transactions": txns}


def _trump_finding(trump_pass):
    if trump_pass:
        return {"mention": True, "valence": "endorse", "confidence": 0.9,
                "as_of": "2026-06-01", "source": "WH", "summary": "Praised",
                "manual": False, "low_confidence_seen": []}
    return {"mention": False, "valence": "none", "confidence": 0.0,
            "as_of": None, "source": "", "summary": "", "manual": False,
            "low_confidence_seen": []}


def _old_rule(tech, sector, news, insider):
    if not tech:
        return False
    pp = sum((tech, sector, news))
    if pp == 3:
        return True
    if pp == 2 and insider:   # tech + one of sector/news, rescued
        return True
    return False


def main():
    rows = []
    neutrality_violations = 0
    for tech, sector, news, trump, insider in itertools.product([True, False], repeat=5):
        p = _payload(tech, news, insider)
        p["trump_signal_result"] = _trump_finding(trump)
        out = conviction.evaluate(p, direction="long", macro=_macro(sector),
                                  action=None, gate_config=DEFAULT_CFG)
        q = out["qualifies"]
        promoted = out.get("promoted_by_insider", False)
        old = _old_rule(tech, sector, news, insider)
        # Neutrality invariant: trump=False rows must match old rule exactly.
        if not trump and q != old:
            neutrality_violations += 1
        rows.append((tech, sector, news, trump, insider, q, promoted, old))

    # Print only qualifying rows (the truth table the audit asks for) + the
    # neutrality summary.
    print("=" * 92)
    print("QUALIFYING COMBINATIONS (qualifies=True), long direction, action=None")
    print("=" * 92)
    print(f"{'tech':>4} {'sect':>4} {'news':>4} {'trmp':>4} {'insd':>4} | "
          f"{'NEW q':>6} {'promoted':>9} {'OLD q':>6} {'vs OLD':>10}")
    print("-" * 92)
    for tech, sector, news, trump, insider, q, promoted, old in rows:
        if not q:
            continue
        if q and not old:
            verdict = "WIDER"
        elif q == old:
            verdict = "same"
        else:
            verdict = "stricter"
        print(f"{int(tech):>4} {int(sector):>4} {int(news):>4} {int(trump):>4} "
              f"{int(insider):>4} | {str(q):>6} {str(promoted):>9} "
              f"{str(old):>6} {verdict:>10}")

    print("\n" + "=" * 92)
    print("NEW-PATH ROWS THAT THE OLD RULE WOULD HAVE DENIED (trump-active wideners)")
    print("=" * 92)
    for tech, sector, news, trump, insider, q, promoted, old in rows:
        if q and not old:
            print(f"  tech={int(tech)} sector={int(sector)} news={int(news)} "
                  f"trump={int(trump)} insider={int(insider)}  "
                  f"-> qualifies (promoted_by_insider={promoted}); "
                  f"BOTH sector&news fail = {not sector and not news}")

    print("\n" + "=" * 92)
    print(f"NEUTRALITY (trump=False) rows checked: 16; "
          f"violations vs old rule: {neutrality_violations}")
    print("=" * 92)


if __name__ == "__main__":
    main()
