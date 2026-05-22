"""Unified idea funnel -- merges four idea sources into one ranked list.

Sources:
  momentum  -- mechanical breakout / momentum / pullback setups (scanner buckets)
  theme     -- theme-universe fitness score (the candidates screen)
  news      -- a name spiking on a big up-day, or named in a market headline
  insider   -- open-market insider cluster buying (SEC Form 4)

An idea hit by multiple sources ranks higher (confluence). Held names are
excluded. The momentum / theme / news sources do no network I/O -- they consume
data already gathered earlier in the build. Insider scanning is the one network
step; it is bounded by a time budget and degrades to empty on failure.
"""
from __future__ import annotations

import re
import time

from app.data import insider
from app.portfolio import idea_queue
from app.research import insider_signal, swing_plan, universe

SOURCE_META = {
    "momentum": "Momentum",
    "theme": "Theme fit",
    "news": "News / social",
    "insider": "Insider buying",
}

# bucket name -> (points, human label). Buckets not listed do not feed the funnel.
_BUCKET_POINTS = {
    "breakouts": (3.0, "20-day breakout"),
    "momentum_continuation": (2.5, "momentum continuation"),
    "new_52w_highs": (2.0, "fresh 52-week high"),
    "pullbacks_to_support": (2.0, "pullback to SMA50 support"),
    "oversold_bounces": (1.5, "oversold bounce setup"),
    "macd_bullish_cross": (1.0, "MACD bullish cross"),
}

# A top-mover only counts as a news/social catalyst above this day-move.
_MOVER_MIN_PCT = 4.0

# Technical fields lifted off scanner rows so a swing plan can be built.
_TECH_FIELDS = ("atr14", "atr_pct", "sma20", "sma50", "sma200", "high_52w",
                "rsi14", "pct_off_52w_high")

_ETF_THEME = "Sector / index ETFs"
_TOKEN_RE = re.compile(r"\b[A-Z]{3,5}\b")


def _match_headlines(headlines: list[dict], universe_tickers: set[str],
                     held: set[str]) -> dict[str, str]:
    """Map universe tickers to the first market headline that names them.

    Only 3-5 letter all-alpha tickers are matched (1-2 letter symbols like
    ``S`` or ``AI`` produce too many false positives against ordinary words).
    """
    uni = {t for t in universe_tickers if t.isalpha() and 3 <= len(t) <= 5}
    out: dict[str, str] = {}
    for h in headlines or []:
        title = h.get("title") or ""
        for tok in set(_TOKEN_RE.findall(title)):
            if tok in uni and tok not in held and tok not in out:
                out[tok] = title
    return out


def _why(source_list: list[dict], n: int) -> str:
    labels = [s["label"] for s in source_list]
    if n == 1:
        return f"{labels[0]} signal"
    joined = ", ".join(labels[:-1]) + " & " + labels[-1]
    return f"{joined} -- {n} signals aligned"


# a verdict of "interested" lifts an idea's score by this multiplier
_INTERESTED_BOOST = 1.25


def merge_sources(*, screen_results: list[dict], scan_buckets: dict,
                  top_movers_up: list[dict], headlines: list[dict],
                  insider_scores: dict[str, dict], held: set[str],
                  universe_tickers: set[str],
                  queue_verdicts: dict[str, str] | None = None) -> list[dict]:
    """Pure merge + score step. No network I/O -- all inputs are pre-gathered.

    ``queue_verdicts`` maps ticker -> the user's verdict from the idea queue:
    ``pass`` drops the idea, ``interested`` boosts its score, ``watching`` is
    carried through as an annotation.
    """
    queue_verdicts = {k.upper(): v for k, v in (queue_verdicts or {}).items()}
    held = {t.upper() for t in (held or set())}
    ideas: dict[str, dict] = {}

    def _idea(ticker: str, price=None, theme=None, day_change_pct=None,
              tech=None) -> dict:
        t = ticker.upper()
        it = ideas.get(t)
        if it is None:
            it = {"ticker": t, "theme": theme, "price": price,
                  "day_change_pct": day_change_pct, "tech": dict(tech or {}),
                  "sources": {}}
            ideas[t] = it
            return it
        if it.get("price") is None and price is not None:
            it["price"] = price
        if not it.get("theme") and theme:
            it["theme"] = theme
        if it.get("day_change_pct") is None and day_change_pct is not None:
            it["day_change_pct"] = day_change_pct
        for k, v in (tech or {}).items():
            if v is not None and it["tech"].get(k) is None:
                it["tech"][k] = v
        return it

    def _add(idea: dict, source: str, points: float, detail: str) -> None:
        prev = idea["sources"].get(source)
        if prev is None or points > prev["points"]:
            idea["sources"][source] = {"points": round(points, 2), "detail": detail}

    # --- momentum: a name can hit several buckets; keep the strongest, with a
    #     small capped bonus for additional setups, and list them all. ---
    momentum_hits: dict[str, list[tuple[float, str]]] = {}
    for bucket, rows in (scan_buckets or {}).items():
        meta = _BUCKET_POINTS.get(bucket)
        if not meta:
            continue
        pts, label = meta
        for r in rows or []:
            t = (r.get("ticker") or "").upper()
            if not t or t in held:
                continue
            _idea(t, price=r.get("price"), theme=r.get("theme"),
                  day_change_pct=r.get("day_change_pct"),
                  tech={k: r.get(k) for k in _TECH_FIELDS})
            detail = label
            if bucket == "breakouts" and r.get("vol_ratio_20d"):
                detail = f"{label} on {r['vol_ratio_20d']:.1f}x volume"
            momentum_hits.setdefault(t, []).append((pts, detail))
    for t, hits in momentum_hits.items():
        hits.sort(reverse=True)
        bonus = min(1.0, 0.5 * (len(hits) - 1))
        detail = ", ".join(lbl for _, lbl in hits)
        _add(ideas[t], "momentum", hits[0][0] + bonus, detail)

    # --- theme fit: screen score is roughly -5..+5; only positive fits feed in.
    for s in screen_results or []:
        t = (s.get("ticker") or "").upper()
        if not t or t in held:
            continue
        sc = s.get("score") or 0
        if sc <= 0:
            continue
        idea = _idea(t, price=s.get("price"),
                     tech={k: s.get(k) for k in _TECH_FIELDS})
        _add(idea, "theme", min(3.0, sc * 0.6), f"theme-screen fitness {sc:+.1f}")

    # --- news / social: big up-day movers from the scanner.
    for r in top_movers_up or []:
        t = (r.get("ticker") or "").upper()
        dc = r.get("day_change_pct")
        if not t or t in held or dc is None or dc < _MOVER_MIN_PCT:
            continue
        idea = _idea(t, price=r.get("price"), theme=r.get("theme"),
                     day_change_pct=dc, tech={k: r.get(k) for k in _TECH_FIELDS})
        _add(idea, "news", min(3.0, dc / 4.0), f"up {dc:.1f}% today on heavy interest")

    # --- news / social: named in a market headline.
    for t, title in _match_headlines(headlines, universe_tickers, held).items():
        idea = _idea(t)
        _add(idea, "news", 1.5, f'named in headline: "{title}"')

    # --- insider: open-market cluster buying.
    for t, cs in (insider_scores or {}).items():
        t = t.upper()
        if t in held:
            continue
        score = cs.get("score") or 0
        if score < 1:
            continue
        idea = _idea(t)
        _add(idea, "insider", score * 1.5, cs.get("summary") or f"insider cluster {score}/3")

    out: list[dict] = []
    for t, idea in ideas.items():
        srcs = idea["sources"]
        if not srcs:
            continue
        verdict = queue_verdicts.get(t, "open")
        if verdict == "pass":
            continue
        n = len(srcs)
        base = sum(s["points"] for s in srcs.values())
        confluence = 1.0 + 0.3 * (n - 1)
        source_list = [
            {"source": key, "label": SOURCE_META[key],
             "detail": srcs[key]["detail"], "points": srcs[key]["points"]}
            for key in ("momentum", "theme", "news", "insider") if key in srcs
        ]
        plan = swing_plan.build(
            idea.get("price"), idea.get("tech"),
            [srcs.get("momentum", {}).get("detail", "")],
        )
        score = base * confluence
        if verdict == "interested":
            score *= _INTERESTED_BOOST
        out.append({
            "ticker": t,
            "theme": idea.get("theme") or universe.theme_of(t),
            "price": idea.get("price"),
            "day_change_pct": idea.get("day_change_pct"),
            "score": round(score, 2),
            "source_count": n,
            "sources": source_list,
            "why": _why(source_list, n),
            "swing_plan": plan,
            "verdict": verdict,
        })
    out.sort(key=lambda x: (x["score"], x["source_count"]), reverse=True)
    for i, idea in enumerate(out, 1):
        idea["rank"] = i
    return out


def scan_insider_clusters(tickers: list[str], *, lookback_days: int = 30,
                          time_budget_s: float = 240.0) -> dict[str, dict]:
    """Scan Form 4 buying across ``tickers``. Bounded by a wall-clock budget so
    a slow SEC endpoint can never blow up the build. Returns only names with a
    cluster score >= 1.
    """
    out: dict[str, dict] = {}
    start = time.monotonic()
    for t in tickers:
        if time.monotonic() - start > time_budget_s:
            break
        try:
            txns = insider.recent_form4_transactions(t, days=lookback_days)
            cs = insider_signal.insider_cluster_score(t, txns, lookback_days=lookback_days)
        except Exception:
            continue
        if (cs.get("score") or 0) >= 1:
            out[t] = cs
    return out


def build(scan_result: dict, screen_results: list[dict], headlines: list[dict],
          account, *, insider_scan: bool = True, limit: int = 40,
          queue_path=None) -> dict:
    """Assemble the ranked idea funnel for the build.

    ``scan_result`` is the scanner output, ``screen_results`` is the candidates
    screen, ``account`` supplies the held set. ``insider_scan`` toggles the one
    network-bound source. The persistent idea queue is loaded for verdicts,
    then synced with today's ranking.
    """
    held = {p.ticker.upper() for p in account.positions}
    universe_tickers = set(universe.all_tickers())

    insider_scores: dict[str, dict] = {}
    if insider_scan:
        pool = [t for t in universe.all_tickers(exclude=held)
                if universe.theme_of(t) != _ETF_THEME]
        insider_scores = scan_insider_clusters(pool)

    queue = idea_queue.load(queue_path)
    ideas = merge_sources(
        screen_results=screen_results,
        scan_buckets=(scan_result or {}).get("buckets", {}),
        top_movers_up=(scan_result or {}).get("top_movers_up", []),
        headlines=headlines,
        insider_scores=insider_scores,
        held=held,
        universe_tickers=universe_tickers,
        queue_verdicts=idea_queue.verdict_map(queue),
    )

    source_counts = {k: 0 for k in SOURCE_META}
    for idea in ideas:
        for s in idea["sources"]:
            source_counts[s["source"]] += 1

    shown = ideas[:limit]
    queue = idea_queue.sync_from_funnel(shown, queue, path=queue_path)
    shown_tickers = {i["ticker"] for i in shown}
    watching = [e for e in queue
                if e.get("verdict") == "watching"
                and e.get("ticker") not in shown_tickers]

    return {
        "ideas": shown,
        "total_ideas": len(ideas),
        "source_counts": source_counts,
        "insider_scanned": len(insider_scores),
        "swing_plans": sum(1 for i in shown if i.get("swing_plan")),
        "confluence": [i for i in shown if i["source_count"] >= 2],
        "verdicts": {v: sum(1 for i in shown if i.get("verdict") == v)
                     for v in ("interested", "watching")},
        "watching_offlist": watching,
    }


# ---------------------------------------------------------------------------
# Independence-weighted confluence
#
# The raw funnel score rewards any multi-signal idea equally. But two signals
# that key off the same thing (momentum + theme both ride sector strength)
# are weaker corroboration than two genuinely independent ones (news + insider
# buying). These multipliers re-weight a confluence idea's score by HOW
# independent its signals are, so the today page surfaces the most genuinely
# corroborated ideas first.
# ---------------------------------------------------------------------------

_PAIR_MULTIPLIER = {
    frozenset({"momentum", "theme"}): 0.7,    # both key off sector strength
    frozenset({"momentum", "news"}): 1.0,
    frozenset({"momentum", "insider"}): 1.2,  # orthogonal
    frozenset({"pullback", "news"}): 1.15,
    frozenset({"pullback", "insider"}): 1.25,
    frozenset({"news", "insider"}): 1.3,      # genuinely independent
    frozenset({"theme", "news"}): 1.0,
    frozenset({"theme", "insider"}): 1.0,
}

_KIND_LABEL = {
    "momentum": "momentum", "pullback": "pullback", "theme": "theme fit",
    "news": "news", "insider": "insider buying",
}


def _momentum_kind(detail: str) -> str:
    """Classify a momentum source as ``pullback`` (buys weakness) or
    ``momentum`` (buys strength) from its strongest bucket label."""
    primary = (detail or "").split(",")[0].lower()
    if "pullback" in primary or "oversold" in primary:
        return "pullback"
    return "momentum"


def _idea_kinds(idea: dict) -> list[str]:
    """The independence kinds present on an idea (momentum splits into
    momentum / pullback; the other sources map straight through)."""
    kinds: list[str] = []
    for s in idea.get("sources") or []:
        src = s.get("source")
        if src == "momentum":
            kinds.append(_momentum_kind(s.get("detail") or ""))
        elif src:
            kinds.append(src)
    return kinds


def _independence_multiplier(kinds: list[str]) -> float:
    """The independence multiplier for a set of signal kinds. For 2 kinds it
    is that pair's weight; for 3+ it is the max pairwise weight. Unknown pairs
    default to 1.0 (neutral)."""
    uniq = sorted(set(kinds))
    if len(uniq) < 2:
        return 1.0
    best = 1.0
    seen = False
    for i in range(len(uniq)):
        for j in range(i + 1, len(uniq)):
            m = _PAIR_MULTIPLIER.get(frozenset({uniq[i], uniq[j]}), 1.0)
            best = m if not seen else max(best, m)
            seen = True
    return best


def _confluence_label(kinds: list[str]) -> str:
    parts = [_KIND_LABEL.get(k, k) for k in kinds]
    joined = " + ".join(parts)
    joined = joined[:1].upper() + joined[1:]
    return f"{joined} -- {len(kinds)} independent signals"


def top_independent_confluence(funnel: dict, n: int = 3) -> list[dict]:
    """Return the top ``n`` multi-signal funnel ideas, re-ranked by an
    independence-weighted confluence score. Single-signal ideas are excluded.

    Each returned idea is a copy of the funnel idea with three added fields:
    ``confluence_multiplier``, ``confluence_score`` (raw score x multiplier)
    and ``confluence_label``. The existing ``swing_plan`` is carried through.
    """
    ideas = (funnel or {}).get("ideas") or []
    scored: list[dict] = []
    for idea in ideas:
        if len(idea.get("sources") or []) < 2:
            continue
        kinds = _idea_kinds(idea)
        mult = _independence_multiplier(kinds)
        out = dict(idea)
        out["confluence_multiplier"] = mult
        out["confluence_score"] = round((idea.get("score") or 0.0) * mult, 2)
        out["confluence_label"] = _confluence_label(kinds)
        scored.append(out)
    scored.sort(key=lambda x: (x["confluence_score"], x.get("source_count", 0)),
                reverse=True)
    return scored[:n]
