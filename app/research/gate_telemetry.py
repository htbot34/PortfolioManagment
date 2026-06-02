"""Gate telemetry - records why candidates did or did not clear the conviction
gate, so a "No trade today" verdict is explainable rather than opaque.

``record()`` is a pure function: it folds a list of per-candidate gate
evaluations into one daily summary. ``persist()`` rolls the last 30 distinct
dates into ``gate_telemetry.yaml`` at the repo root. ``rollup()`` summarises a
history list for the 30-day line shown on no-trade days.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
TELEMETRY_PATH = ROOT / "gate_telemetry.yaml"
MAX_DATES = 30
MAX_NEAR_MISS = 5

_PRIMARY_SIGNALS = ("technical", "sector_momentum", "news", "trump")
_BLOCK_KEYS = ("technical", "sector_momentum", "news", "trump",
               "earnings_window", "regime", "soft_veto",
               "trump_block")


def _blank_blocked_by() -> dict:
    return {k: 0 for k in _BLOCK_KEYS}


def record(candidates: list, gate_evals: list, date_str: str) -> dict:
    """Fold per-candidate gate evaluations into one daily telemetry record.

    ``candidates`` is the list of candidates considered (used for the count).
    ``gate_evals`` is a parallel list of per-candidate dicts, each either a
    pre-gate block or a conviction-gate result. The shape is::

        {"ticker": str,
         "pre_block": "regime" | "soft_veto" | None,
         "qualifies": bool,
         "promoted_by_insider": bool,
         "earnings_block": str | None,
         "trump_block": str | None,    # Trump-attack veto on entry
         "reasons": {"technical": bool, "sector_momentum": bool,
                      "news": bool, "trump": bool},
         "insider_score": int,
         "trump_mention": bool,
         "trump_valence": "endorse" | "attack" | "none",
         "trump_promoted": bool}       # qualified because trump substituted

    The new Trump-related fields are surfaced so today's brief can show
    "N candidates carried a Trump mention; M cleared via Trump
    substitution; K were vetoed by an attack" without spelunking.
    """
    blocked_by = _blank_blocked_by()
    cleared_primary = 0
    cleared_insider = 0
    near_miss: list[dict] = []
    trump_mentions = 0
    trump_endorsements = 0
    trump_attacks = 0
    trump_promotions = 0
    trump_vetoes = 0
    trump_firings: list[dict] = []

    for ev in gate_evals or []:
        pre = ev.get("pre_block")
        if pre in ("regime", "soft_veto"):
            blocked_by[pre] += 1
            continue
        reasons = ev.get("reasons") or {}
        passed = [s for s in _PRIMARY_SIGNALS if reasons.get(s)]
        # Trump telemetry (independent of qualification outcome).
        if ev.get("trump_mention"):
            trump_mentions += 1
            valence = ev.get("trump_valence")
            if valence == "endorse":
                trump_endorsements += 1
            elif valence == "attack":
                trump_attacks += 1
            trump_firings.append({
                "ticker": ev.get("ticker", ""),
                "valence": valence,
                "as_of": ev.get("trump_as_of"),
                "source": ev.get("trump_source"),
                "qualifies": bool(ev.get("qualifies")),
            })
        if ev.get("trump_promoted"):
            trump_promotions += 1
        if ev.get("trump_block"):
            blocked_by["trump_block"] += 1
            trump_vetoes += 1
            continue
        if ev.get("qualifies"):
            if ev.get("promoted_by_insider"):
                cleared_insider += 1
            else:
                cleared_primary += 1
            continue
        # Did not qualify - attribute a single primary block reason.
        # Earnings block only fires when all 3 of (tech, sector, news)
        # passed, so it remains a "passed 3" check (Trump is not part of
        # the earnings-window precondition).
        primary3_passed = sum(1 for s in ("technical", "sector_momentum", "news")
                              if reasons.get(s))
        if ev.get("earnings_block") and primary3_passed == 3:
            blocked_by["earnings_window"] += 1
        else:
            for s in _PRIMARY_SIGNALS:
                if not reasons.get(s):
                    blocked_by[s] += 1
                    break
        # Near-miss: tech + exactly one of (sector, news, trump). Under
        # the new confluence rule the threshold is 2 of those three, so
        # one short is the near-miss. 3-of-4 (tech + all three other
        # primaries) actually CLEARED -- it only ends up here when an
        # overlay (earnings, valuation, correlation, trump_block)
        # vetoed it, which already attributes its own blocker above.
        if len(passed) == 2 and reasons.get("technical"):
            failed_list = [s for s in ("sector_momentum", "news", "trump")
                            if not reasons.get(s)]
            near_miss.append({
                "ticker": ev.get("ticker", ""),
                "passed": passed,
                "failed": failed_list[0] if failed_list else None,
                "insider_score": int(ev.get("insider_score", 0) or 0),
            })

    near_miss.sort(key=lambda n: (-n["insider_score"], n["ticker"]))
    near_miss = near_miss[:MAX_NEAR_MISS]

    return {
        "date": date_str,
        "candidates_evaluated": len(candidates or []),
        "cleared_primary": cleared_primary,
        "cleared_insider_promotion": cleared_insider,
        "blocked_by": blocked_by,
        "near_miss": near_miss,
        "trump": {
            "mentions": trump_mentions,
            "endorsements": trump_endorsements,
            "attacks": trump_attacks,
            "promotions": trump_promotions,
            "vetoes": trump_vetoes,
            "firings": trump_firings,
        },
    }


def load(path: Path | None = None) -> list[dict]:
    """Load the telemetry history list. Missing/malformed file -> []."""
    p = path or TELEMETRY_PATH
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text()) or []
    except Exception:
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _merge(history: list[dict], today_record: dict) -> list[dict]:
    """Insert/replace today's record, keep the last MAX_DATES distinct dates."""
    today = today_record.get("date")
    merged = [r for r in history if r.get("date") != today]
    merged.append(today_record)
    merged.sort(key=lambda r: r.get("date") or "")
    return merged[-MAX_DATES:]


def merged_history(today_record: dict, path: Path | None = None) -> list[dict]:
    """Return the history list with today's record merged in - no file write."""
    return _merge(load(path), today_record)


def persist(today_record: dict, path: Path | None = None) -> list[dict]:
    """Merge today's record into the history file and write it back."""
    p = path or TELEMETRY_PATH
    merged = _merge(load(p), today_record)
    try:
        p.write_text(yaml.safe_dump(merged, sort_keys=False))
    except Exception:
        pass
    return merged


def rollup(history: list[dict]) -> dict:
    """Summarise a telemetry history list for the 30-day status line."""
    history = [r for r in (history or []) if isinstance(r, dict)]
    if not history:
        return {"days": 0, "cleared": 0, "reached_2of3": 0,
                "mean_near_miss_per_day": 0.0,
                "top_blocker_signal": None, "top_blocker_pct": 0.0}
    cleared = sum((r.get("cleared_primary", 0) or 0)
                  + (r.get("cleared_insider_promotion", 0) or 0)
                  for r in history)
    near_miss_total = sum(len(r.get("near_miss") or []) for r in history)
    reached_2of3 = near_miss_total + sum(
        r.get("cleared_insider_promotion", 0) or 0 for r in history)
    block_totals: dict[str, int] = {}
    for r in history:
        for k, v in (r.get("blocked_by") or {}).items():
            block_totals[k] = block_totals.get(k, 0) + (v or 0)
    total_blocks = sum(block_totals.values())
    top_signal = None
    top_pct = 0.0
    if total_blocks:
        top_signal = max(block_totals, key=lambda k: block_totals[k])
        top_pct = round(block_totals[top_signal] / total_blocks * 100, 1)
    return {
        "days": len(history),
        "cleared": cleared,
        "reached_2of3": reached_2of3,
        "mean_near_miss_per_day": round(near_miss_total / len(history), 2),
        "top_blocker_signal": top_signal,
        "top_blocker_pct": top_pct,
    }
