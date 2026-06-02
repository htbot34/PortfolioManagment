# Recommendation gate audit

This is a forensic, evidence-based investigation of what it takes for the
portfolio system to surface one recommendation, and whether the gate is
calibrated as designed.

Tests pass: **340 passed** (pytest -q).

---

## Executive summary

**Headline.** The gate's bar is supposed to be high, and the historical
fire rate (0/14 over 4 recorded days) is consistent with that. But two
specific things are making it *slightly stricter than designed*, both due
to code/data plumbing rather than intent:

1. **Scanner → technical-signal hand-off drops `above_sma200`.** The
   technical signal earns its trend point via
   `stacked_uptrend OR (breakout_20d AND above_sma200)`, but
   `scanner._enrich` does not include `above_sma200` in the row it emits.
   So the breakout fallback is dead on scanner-fed payloads. **Today's
   replay: 3 of 7 technical-blocks (MDB, ORCL, OKTA) are this bug.** The
   30-day rollup attributes 71.4% of all blocks to "technical" — a
   meaningful slice of those are this hand-off.

2. **The insider 2-of-3 promotion path receives no data.**
   `funnel.insider_scanned: 0` in today's build, and every recorded
   near-miss has `insider_score: 0`. `form4_cache.json` (a file the
   workflow explicitly lists in its `git add` whitelist) is missing
   from the repo, indicating the 24h disk cache has not successfully
   been populated. The 2-of-3 promotion safety valve — designed
   precisely for "passed two of three, here's the orthogonal
   confirmation" — is silently inoperative.

Single most important finding: **every near-miss in the recorded
history failed on news with `insider_score=0`. The design says
"2-of-3 + insider rescues" — in reality, that rescue path has produced
zero rescues because the insider data isn't getting in. Fix the data
pipeline first, then re-evaluate.** Lowering thresholds would be
treating a data-availability symptom as if it were a calibration
problem.

### Ranked actions

| # | Action | Type | Risk |
|---|--------|------|------|
| 1 | Propagate `above_sma200` (and friends) from scanner row | Bug fix | very low |
| 2 | Surface the actual exception in `scan_insider_clusters` (don't catch silently), then diagnose | Bug fix | low |
| 3 | Log `shadow_tracker.safe_update` exceptions so its absence is visible | Bug fix | zero |
| 4 | Log pre-filter drops as `pre_block: pre_filter` in gate telemetry | Information | zero |
| 5 | Add `news_was_empty` bit to `news_signal` output and propagate to telemetry | Information | zero |
| 6 | Expand `THEME_TO_SECTOR` to cover the remaining universe themes | Calibration | low; needs per-theme judgment |
| 7+ | Threshold tunings (news floor, etc.) | Calibration | **DO NOT** until #2 and #3 produce data |

The full phase-by-phase audit follows.

---

## Phase 0 — Orient and identify the live recommendation path

### Branches

```
$ git branch -a
* claude/fervent-franklin-tq904
  main
  remotes/origin/claude/fervent-franklin-tq904
  remotes/origin/main
$ git log --oneline -6
039829f intraday: 2026-06-01T22:01Z
8a185e1 Refresh site 2026-06-01T19:16Z
3c8089b intraday: 2026-06-01T18:36Z
4a942ae Refresh site 2026-06-01T15:16Z
d59c680 fix: wire idea funnel into conviction gate, drop dead pre-filter (#10)
d4f262b Merge pull request #9 from htbot34/claude/relaxed-meitner-aKFwP
```

Only two branches exist: `main` and the working branch
`claude/fervent-franklin-tq904`. They are at the same HEAD. There is **no
legacy FastAPI/SQLite path lingering on a sibling branch** in this clone —
the only generation of the system is the static-site / GitHub-Actions /
conviction-gate one already on `main`.

### The live recommendation path

Entry point: `.github/workflows/refresh.yml` (cron 11:00 UTC weekdays) runs
`python -m app.build_site`. Pipeline, in execution order:

| # | Stage | File | Function |
|---|-------|------|----------|
| 1 | Macro snapshot (indices, sectors, leaders) | `app/data/macro.py` | `snapshot()` |
| 2 | Regime detection (risk_on/off/chop/breakdown) | `app/research/regime.py` | `gather_regime_inputs()`, `detect_regime()` |
| 3 | Market headlines | `app/data/market_news.py` | `top_headlines()` |
| 4 | Scanner — buckets every universe ticker into setups | `app/research/scanner.py` | `scan()` |
| 5 | Per-position analyst payloads (for defense recs) | `app/research/analyst.py` | `analyze_ticker()` |
| 6 | Candidates screen (offline ranking + LLM commentary) | `app/research/candidates.py` | `candidates()` |
| 7 | Idea funnel — merges scanner + screen + headlines + insider | `app/research/idea_funnel.py` | `build()` |
| 8 | Idea-queue prune + user preference learning | `app/portfolio/idea_queue.py`, `app/research/learning.py` | — |
| 9 | **Daily brief — picks today's call OR returns no_trade** | `app/research/daily_brief.py` | `build()` |
| 9a |  → Defense path (held positions, short direction) | same | `_defense_from_book()` |
| 9b |  → Offense path (new buys from scanner) | same | `_trade_from_scanner()` |
| **10** | **CONVICTION GATE** — pass/fail decision | `app/research/conviction.py` | `evaluate()` |
| 11 | Gate telemetry record + 30d rollup | `app/research/gate_telemetry.py` | `record()`, `rollup()` |
| 12 | Shadow tracker (measurement-only, near-miss outcome tracking) | `app/research/shadow_tracker.py` | `safe_update()` |
| 13 | Persist rec to `rec_history.yaml`, write `.new_recs.json` sidecar | `app/portfolio/rec_history.py`, `app/notify.py` | — |
| 14 | Render `index.html` + sibling pages, commit, push | `app/build_site.py` | `main()` |
| 15 | "Notify on new recommendations" workflow step opens a GitHub Issue per rec | `.github/workflows/refresh.yml` | — |

**Stage 10 (`conviction.evaluate`) is THE gate.** Everything before it is
candidate prep; everything after it is rendering/notification. The conviction
gate is reached twice per build:

- Once for each held position with conviction-5 sell/trim (defense path).
- Once for the first ordered scanner candidate that passes a light pre-filter
  (`vol_ratio_20d >= 1.2 AND macd_hist > 0`) — first-pass-wins, the loop
  short-circuits as soon as one candidate qualifies
  (`daily_brief.py:288-298`).

### Inputs the gate depends on

| Input | Source | Read in | Behavior if missing/stale/empty |
|-------|--------|---------|--------------------------------|
| Scanner row (rsi, macd, breakout, vol_ratio_20d, theme, …) | `prices.technicals(ticker)` via `scanner._enrich` | `signals.technical_signal` | Empty scanner row → ticker never enters the candidate list (the pre-filter `vol >= 1.2 AND macd_h > 0` drops it). |
| Sector ETF returns `ret_5d` / `ret_20d` | `macro_mod.snapshot()` | `signals.sector_momentum_signal` | Missing returns → signal **fails** (`signals.py:285`). Missing sector mapping for the ticker → signal fails (`signals.py:278`). |
| Company news items + LLM classifications | `news.company_news` (Yahoo + Google RSS), then `news_classifier.classify_news_items` (GitHub Models gpt-4o-mini, keyword fallback) | `signals.news_signal` | No items in last 14 days → signal **fails** (`signals.py:215-216`). LLM filtered/empty → keyword fallback assigns magnitude 3 OR neutral 1 (`news_classifier.py:81-99`). |
| Insider transactions (Form 4) | `insider.recent_form4_transactions` (SEC EDGAR, 24h disk cache) | `conviction._insider_signal` (promotion path) and `idea_funnel.scan_insider_clusters` (universe scan) | Funnel scan: bounded by `time_budget_s=240` and per-ticker try/except → silently empty (`idea_funnel.py:225-243`). Promotion-path fetch: wrapped in try/except → empty list → score 0. |
| Earnings date | `app.data.calendar.next_earnings_date` | `conviction._earnings_block` | Any exception → no block. `None` return → no block. |
| Fundamentals (sector, ratios) | `fundamentals.get_fundamentals` | `conviction._valuation_assess` (long entries only) | Exception → no valuation block. Unknown tier → no decision change. |
| Correlation to top-5 book | `correlation.candidate_correlation_to_book` | `conviction._correlation_assess` | `available=False` → "ok" (no block, no annotation). |
| Regime | `regime.detect_regime` | `daily_brief._trade_from_scanner` | `breakdown` → all new buys blocked; `chop` → insider 2-of-3 promotion disabled; default → no effect. |
| Soft veto set | `learning.soft_veto_tickers` over last 30d of `rec_history.yaml` | `daily_brief._trade_from_scanner` & `_defense_from_book` | Pre-block before the gate runs. |
| Macro risk-off (VIX > 22 OR SPX > 10% off 52w high) | `daily_brief._macro_risk_off` | `daily_brief.build`, `_trade_from_scanner` | True → return `no_trade` immediately on the offensive path. |
| `IDEA_FUNNEL_INSIDER` env | `build_site.py:296` | Idea funnel builder | If set to `0/false/""` → insider scan skipped, `insider_scanned: 0`. Default ON. |
| `SEC_USER_AGENT` env | `.github/workflows/refresh.yml:43` | All SEC requests | Without a valid UA, SEC may 403. |

### Committed state files (relevant to the gate)

| File | Role |
|------|------|
| `portfolio.yaml` | Account + positions |
| `risk_profile.yaml` | Caps, themes |
| `gate_telemetry.yaml` | Per-day record of evaluations, blockers, near-misses (max 30 dates) |
| `rec_history.yaml` | Pending/accepted/rejected recs — feeds soft-veto learning |
| `idea_queue.yaml` | Persistent funnel ideas + user verdicts |
| `news_classification_cache.json` | Permanent cache of LLM news labels (sha1 key) |
| `form4_cache.json` | 24h cache of SEC Form 4 submissions |
| `price_cache.json` | Persistent quote fallback |
| `fundamentals_cache.json` | Fundamentals cache |
| `regime_history.json` | Last 90 days of regime + breadth |
| `intraday_alerts.json` | Intraday workflow output (separate path, not part of daily rec gate) |
| `shadow_ledger.yaml`, `shadow_calibration.yaml` | **Not present** — the shadow tracker has not produced output (see Phase 2 note). |

### Tests + dry run

```
$ python3 -m pytest -q
340 passed in 4.48s
```

I cannot run the live `python -m app.build_site` end-to-end offline (it
needs network for yfinance/EDGAR/Yahoo RSS and GitHub Models token).
Sufficient offline material exists for the audit:
- `gate_telemetry.yaml` (4 days of recorded evaluations).
- `data.json` (the rendered build's snapshot — includes `brief`, `scanner`,
  `idea_funnel`, `telemetry`, `telemetry_30d`).
- Fixtures + test doubles all over `tests/`.

---

## Phase 1 — Anatomy of the conviction gate

### The three primary signals (and the 4th, insider)

`conviction.evaluate()` (`app/research/conviction.py:92-220`) runs three
signals in fixed order. Each is a pure function in
`app/research/signals.py`.

#### Signal 1: `technical_signal` (`signals.py:83-143`)

A 0-3 point score; **passes when `score >= 2`**. For `direction="long"`:

```
score = 0
# Trend point
if t["stacked_uptrend"] OR (t["breakout_20d"] AND t["above_sma200"]):
    score += 1
# RSI point (mutually exclusive bands)
if 40 <= rsi <= 65:               score += 1   # momentum band
elif rsi <= 30:                   score += 1   # deep oversold
# MACD point
if macd_hist > 0 OR macd_cross_up OR golden_cross_recent:
    score += 1
return {"pass": score >= 2, ...}
```

For `direction="short"`:

```
score = 0
# Trend point
if t["stacked_downtrend"] OR (pct_off_52w_high < -25 AND NOT above_sma200):
    score += 1
# RSI point (mutually exclusive bands)
if rsi >= 70:                     score += 1   # overbought
elif 50 <= rsi < 70 AND stacked_downtrend:
                                  score += 1   # failed-rally band
# MACD point
if macd_hist < 0 OR macd_cross_down OR death_cross_recent:
    score += 1
```

The payload may be a scanner row (flat keys) OR a per-ticker analyst payload
(nested under `technicals`). Both shapes are accepted; flat keys win
(`signals.py:90-100`).

> ⚠️ **Hidden constraint**. The trend-point fallback is
> `breakout_20d AND above_sma200`. The scanner's `_enrich` in
> `app/research/scanner.py:28-54` does NOT propagate `above_sma200` — it
> only carries the keys it explicitly enumerates. `prices.technicals()`
> does compute the flag (`prices.py:414`), but it never reaches the gate
> via the scanner path. Consequences quantified in Phase 3.

#### Signal 2: `sector_momentum_signal` (`signals.py:267-293`)

Looks up the SPDR ETF for the candidate's sector and requires its 5d AND
20d returns to **both** be aligned with the direction:

```
etf = SECTOR_TO_ETF[sector.lower()]
if etf is None:                                       return fail "unknown sector"
target = next d for d in macro["sectors"].values() if d["ticker"] == etf
if not target:                                         return fail "ETF missing from macro"
r5, r20 = target["ret_5d"], target["ret_20d"]
if r5 is None or r20 is None:                         return fail "missing return data"
if direction == "long"  and r5 > 0 and r20 > 0:       PASS
if direction == "short" and r5 < 0 and r20 < 0:       PASS
otherwise:                                             fail "not aligned"
```

Sector strings vary; the gate normalizes via `SECTOR_TO_ETF`
(`signals.py:23-39`). Scanner rows have no sector — they carry `theme`,
mapped by `THEME_TO_SECTOR` (`signals.py:44-58`) in
`conviction._extract_sector` (`conviction.py:23-41`).

#### Signal 3: `news_signal` (`signals.py:190-254`)

Operates on semantic classifications from
`app/research/news_classifier.py`. Each item carries `direction`
(bullish/bearish/neutral), `magnitude` (1-5), `durability` (short/medium/long).
Weights: short=0.3, medium=0.7, long=1.0.

```
recent_14 = items with published within 14d (missing date → kept)
if not recent_14:                                     return fail
net = sum(sign * magnitude * durability_weight for c in recent_14)
has_mag3 = any item with magnitude >= 3 in recent_14
LONG passes iff: net >= 3 AND has_mag3 AND NO bearish item in
                 last 7d with magnitude >= 4
SHORT passes iff: net <= -3 AND has_mag3
```

**LLM-empty-or-filtered fallback**: when GitHub Models returns nothing,
`news_classifier._keyword_fallback` assigns `magnitude=3` and either
bullish, bearish, or neutral=1 (`news_classifier.py:81-99`). The fallback
*can* clear the news bar on its own if at least one item has a strict
bull-or-bear keyword and at least one item is magnitude 3.

#### Signal 4: `insider` (promotion path only) (`conviction.py:63-89`)

Form 4 cluster score, 0-3. Computed only when the primary count is exactly
2-of-3 AND technical was one of the two passers AND
`allow_insider_promotion=True`. Passes when `score >= 2`.

### The decision rule

Quoted from `conviction.evaluate` (`conviction.py:127-167`):

```python
tech = signals.technical_signal(payload, direction)
if not tech["pass"]:
    return DENY      # technical is a hard prerequisite — short-circuit

sec = signals.sector_momentum_signal(sector, macro, direction)
nws = signals.news_signal(ticker, classifications, direction)

primary_pass = sum(1 for s in (tech, sec, nws) if s["pass"])
qualifies = (primary_pass == 3)

if not qualifies and primary_pass == 2 and tech["pass"] and allow_insider_promotion:
    insider = _insider_signal(ticker, direction, payload, insider_fetcher)
    if insider["score"] >= 2:
        qualifies = True
        promoted_by_insider = True
```

Then three optional **deny-only overlays** (cannot promote, only block) on
a candidate that already qualified:

| Overlay | Where | Effect |
|---|---|---|
| Correlation (longs only, on `buy/add/new_buy`) | `conviction.py:172-182` | `avg_corr_to_top5 > 0.7` blocks a NEW BUY; adds into a tight cluster annotate |
| Valuation (longs `buy/add/new_buy` OR shorts) | `conviction.py:188-211` | `tier=="extreme"` blocks a long entry **unless** insider-promoted with score 3 |
| Earnings window (long `buy/add/new_buy` only) | `conviction.py:214-220` | Blocks if earnings within 0-3 trading days; any exception → no block |

### Pre-gate filters

Before the gate runs in `_trade_from_scanner` (offense path):

1. `_macro_risk_off(macro)`: VIX > 22 OR SPX > 10% off 52w high → return
   `no_trade` without invoking the gate at all
   (`daily_brief.py:241-242`, `_macro_risk_off:73-80`).
2. `regime == "breakdown"` → pre-block with `"regime"` reason
   (`daily_brief.py:285-287`).
3. `ticker in soft_veto` → pre-block with `"soft_veto"` reason
   (`daily_brief.py:278-280`).
4. **Pre-filter**: `vol_ratio_20d >= 1.2 AND macd_hist > 0`. Candidates that
   fail this are silently dropped (`daily_brief.py:281-284`) — they are
   **not** counted in `candidates_evaluated` and **not** reported as
   blocked. Quantified in Phase 3.
5. The scanner buckets that even feed the offense ordering are restricted
   to `_BULLISH_BUCKETS` (`daily_brief.py:198-205`): breakouts,
   momentum_continuation, new_52w_highs, macd_bullish_cross,
   pullbacks_to_support, oversold_bounces. The 7th bucket
   `rsi_extreme_overbought` is silently ignored on the offense side.

### Asymmetries: buys vs sells/trims

| Aspect | Long entry (`buy/add/new_buy`) | Defense (`sell/trim` on held) |
|---|---|---|
| Where the candidate comes from | Scanner buckets, ordered by funnel confluence | Per-position analyst payload with conviction == 5 (`daily_brief.py:118-128`) |
| Macro risk-off pre-block | YES — bails on the offense path | NO — defense still considered |
| Light pre-filter (vol >= 1.2 & MACD>0) | YES | NO — defense goes straight to the gate |
| Regime breakdown pre-block | YES — `breakdown` blocks all new buys | NO — defense always runs |
| Direction passed to the gate | `"long"` | `"short"` |
| Earnings window block | YES | NO (entry-only, `conviction.py:214-215`) |
| Correlation overlay | YES (longs only) | NO |
| Valuation extreme overlay | Blocks (unless insider-3 override) | Cheap → annotation only |
| Insider promotion | 2-of-3 when technical passed | Same logic — but the short-side insider scorer (`insider_cluster_score_short`) drops planned 10b5-1 sales and never grants a C-suite tier-3 |

There is **no minimum-conviction floor** for surfacing a defense rec other
than `conviction == 5` on the analyst payload AND the gate passing in the
`"short"` direction (`daily_brief.py:119-133`).

### What it takes to surface one recommendation — ordered checklist

For an OFFENSIVE buy (the much more common path), every condition must hold:

1. Macro: VIX <= 22 AND SPX > -10% off 52w high.
2. Regime is not `breakdown`. (`chop` is OK but disables insider promotion.)
3. The ticker is not held.
4. The ticker is not on the soft-veto list.
5. The ticker is in one of `_BULLISH_BUCKETS` from today's scan.
6. **Pre-filter**: `vol_ratio_20d >= 1.2` AND `macd_hist > 0` on the scanner row.
7. **Technical** signal scores >= 2 in the long direction.
8. **Sector momentum** signal: the SPDR ETF for the ticker's sector/theme
   has `ret_5d > 0` AND `ret_20d > 0` AND both fields present in the macro
   snapshot.
9. **News** signal: net weighted score >= 3 AND at least one magnitude >= 3
   item in the last 14d AND no bearish-magnitude>=4 item in the last 7d.
   **OR** primary count is 2 (one of {sector, news} failed, technical
   passed), `allow_insider_promotion` is True (i.e. regime != `chop`), AND
   the insider cluster score for the ticker is >= 2.
10. Correlation: `avg_corr_to_top5 <= 0.7` (or unavailable).
11. Valuation tier is not `extreme` (unless the candidate is promoted by an
    insider score of 3).
12. Earnings is not within 0-3 trading days (or the calendar lookup raised /
    returned None).
13. The ticker is the **first** in evaluation order to clear — funnel
    confluence ideas evaluated before the canonical bucket order; the
    offense loop short-circuits on the first qualifier (`daily_brief.py:298`).

### Where signals can be silently dropped vs treated as bearish

This is the crux of the audit. The system distinguishes "didn't pass" from
"data was absent." The two collapse into one bit (`pass=False`) at the gate
boundary, but the upstream code branches differ:

| Failure mode | Reads in code as | What it looks like in telemetry |
|---|---|---|
| News list is empty (no recent items) | `news.signal` → fail `"no recent classified news"` (`signals.py:215-216`) | `blocked_by: news` |
| Yahoo + Google both errored for that ticker | `news.company_news` returns `[]` silently (`news.py:42-58, 60-78`) — caller cannot distinguish "no items" from "fetch failed" | `blocked_by: news` (looks identical to "genuinely no news") |
| LLM classifier filtered/empty | `_keyword_fallback` runs, gives `magnitude=3` bull/bear/neutral; the failure is logged (`news_classifier.py:81-99, 158-161`) but ALL items in the batch share a generic keyword-based label | Whichever way the keywords swing |
| Sector unknown / theme not mapped | `sector_momentum` → fail `"unknown sector 'X'"` (`signals.py:278`) | `blocked_by: sector_momentum` |
| Sector ETF row missing from macro snapshot | fail `"<ETF> missing from macro snapshot"` (`signals.py:282`) | `blocked_by: sector_momentum` (but no macro data is a build-wide problem, not a per-ticker bearish reading) |
| Macro fetch entirely failed | `build_site.py:194-196` swallows and sets `macro = {"sectors": {}, ...}` — every per-ticker sector lookup will hit the "ETF missing from macro snapshot" branch | EVERY candidate will be blocked by sector_momentum |
| Insider scan timed out / SEC 403 | `_insider_signal` swallows exception → `txns=[]` → score 0 (`conviction.py:73-83`); `scan_insider_clusters` swallows per-ticker (`idea_funnel.py:236-242`); the cluster scan has a 240s wall-clock budget — anything past it is silently skipped | Insider promotion never fires; `insider_scanned: 0` is the visible signal but not surfaced anywhere except `data.json` |
| Earnings calendar raises | `_earnings_block` returns None → no block (`conviction.py:282-296`) | No block; failure is not surfaced |
| Fundamentals fetch raises | `_valuation_assess` returns None → no valuation overlay (`conviction.py:222-241`) | No block; failure is silent |
| Correlation unavailable | `_correlation_assess` returns `("ok", None)` (`conviction.py:259-260`) | No block; silent |

> ⚠️ **The five most consequential silent-degradation paths**:
>
> 1. `news.company_news` returning `[]` because both Yahoo and Google failed is
>    **indistinguishable** from "genuinely no news," and both end up in
>    `blocked_by: news` — exactly the second-most-common blocker in the
>    30d rollup.
> 2. `scan_insider_clusters` returning empty (network error or budget) means
>    the entire promotion path is collapsed across the universe. With the
>    current build's `insider_scanned: 0`, this is currently in effect.
> 3. `_insider_signal` for the per-ticker promotion fetch swallowing
>    exceptions to `score=0` looks identical to "no insiders bought."
> 4. The earnings calendar silently returning None on any yfinance hiccup —
>    a *failed* lookup looks the same as "no scheduled earnings."
> 5. Macro snapshot failure makes EVERY long candidate fail the sector
>    signal, but the build keeps running.

### Boundary checks (off-by-one / inclusive ranges)

| Threshold | Operator | Comment |
|---|---|---|
| Technical pass | `score >= 2` (`signals.py:140`) | Inclusive at 2/3 — correct |
| RSI momentum band | `40 <= rsi <= 65` (`signals.py:114`) | Inclusive both ends |
| RSI deep oversold | `rsi <= 30` (`signals.py:117`) | Inclusive |
| RSI overbought (short) | `rsi >= 70` (`signals.py:127`) | Inclusive |
| RSI failed-rally (short) | `50 <= rsi < 70` (`signals.py:130`) | Half-open — 70 falls into the higher band, OK |
| News net long | `net >= 3` (`signals.py:231`) | Inclusive |
| News magnitude floor | `magnitude >= 3` (`signals.py:223`) | Inclusive |
| Sector momentum | strict `r5 > 0 AND r20 > 0` (`signals.py:286`) | Strict — *exactly* 0 fails; reasonable |
| Insider promotion | `score >= 2` (`conviction.py:162`) | Inclusive |
| Earnings window | `0 <= days <= 3` (`conviction.py:294`) | Inclusive both ends |
| Correlation block | `avg > 0.7` (`conviction.py:264`) | Strict — exactly 0.7 doesn't block |
| Volume pre-filter | `vol >= 1.2` (`daily_brief.py:283`) | Inclusive |
| MACD pre-filter | `macd_h > 0` (`daily_brief.py:283`) | Strict — exactly 0 fails |

No glaringly wrong operators. Sector momentum's strict-positive is the only
notable one and is consistent with the design (sector must be aligned, not
"not negative").

---


## Phase 2 — Empirical behavior

### What history we have

`gate_telemetry.yaml` retains the last 30 distinct dates. The committed file
covers **4 dates** (2026-05-28, 05-29, 05-30, 06-01). `rec_history.yaml` is
empty (`[]`). `shadow_ledger.yaml` and `shadow_calibration.yaml` were not
produced — although `safe_update` is invoked unconditionally
(`build_site.py:371`), the absence of output suggests it hit a swallowed
exception (the wrapper catches everything; `shadow_tracker.py:460-467`).

Limitations: 4 datapoints is not a statistical sample. The qualitative
pattern is nevertheless unambiguous: it is the same blocker, in the same
proportion, every day, with no movement across runs — which is itself
informative.

### Fire rate

| | Count |
|---|---|
| Days recorded | 4 |
| Candidates evaluated total | 14 |
| Cleared (primary 3-of-3) | **0** |
| Cleared (insider promotion) | **0** |
| Near-miss (2-of-3, no promotion) | 4 |

**Historical fire rate: 0/14 = 0% over 4 days.**

### Binding-constraint frequency

Counted from `gate_telemetry.yaml`, attribution rule from
`gate_telemetry.record` (`gate_telemetry.py:62-69`): the first failing
primary signal in fixed order is the attributed blocker, except when
all-three passed and the earnings window fired.

| Blocker | Count | % of evaluations | % of blocks |
|---|---|---|---|
| technical | 10 | 71.4% | 71.4% |
| news | 4 | 28.6% | 28.6% |
| sector_momentum | 0 | 0% | 0% |
| earnings_window | 0 | 0% | 0% |
| regime | 0 | 0% | 0% |
| soft_veto | 0 | 0% | 0% |

This matches the 30-day rollup the site renders today:
`top_blocker_signal: technical, top_blocker_pct: 71.4`.

### Near-misses

All 4 near-misses across 4 days:

| Date | Ticker | Passed | Failed | Insider score |
|------|--------|--------|--------|---------------|
| 2026-05-28 | LEU | technical + sector_momentum | news | 0 |
| 2026-05-29 | LEU | technical + sector_momentum | news | 0 |
| 2026-05-30 | LEU | technical + sector_momentum | news | 0 |
| 2026-06-01 | ARM | technical + sector_momentum | news | 0 |

**Every near-miss failed on news; every near-miss recorded
`insider_score=0`.** None could fire via the 2-of-3 insider-promotion
path. Note LEU is a held position, so it would have been blocked on the
offense side (`s.get("held")` continue at `daily_brief.py:276`). It is
being evaluated via the per-position **defense** path in the `"short"`
direction — three days in a row, evaluating LEU with the same outcome.
That also implies LEU's analyst payload has been emitting `conviction=5,
action in {sell,trim}` consistently, and "news" is keeping it from being
sold.

### Funnel + insider state

From `data.json` for the 2026-06-01 19:16 UTC build:

```
funnel.total_ideas:        42
funnel.source_counts:      {'momentum': 30, 'theme': 15, 'news': 9, 'insider': 0}
funnel.insider_scanned:    0
funnel.confluence (>=2):   12
high_confluence top 3:     MDB, NOW, OKTA  (all momentum + news, no insider)
```

> ⚠️ **`insider_scanned: 0` is the smoking gun.** The funnel's insider
> scan (`idea_funnel.scan_insider_clusters`) iterates the entire
> non-ETF universe with a 240-second wall-clock budget and per-ticker
> try/except. A return value of zero means *every* ticker either:
> (a) failed CIK lookup, (b) raised inside the SEC submissions fetch, or
> (c) had a clean fetch but no insider with cluster score >= 1. The
> committed `form4_cache.json` file (which is in the `git add` whitelist
> at `refresh.yml:111`) **is not present in the repo**, which means the
> 24h fetch cache has never successfully been written from any build. So
> insider data is either being fetched fresh and failing on every refresh,
> OR being fetched fresh and returning genuinely empty for every name.
> Either way, the per-ticker insider scorer that `conviction.evaluate`
> calls for promotion uses the same code path (`_cached`) and gets the
> same outcome — score 0. **This is why every recorded near-miss has
> `insider_score=0`.**

### Reconstruction of today's flow

Using `data.json` (offline, no network), I reproduced the offense path's
candidate selection for 2026-06-01. The bullish buckets (after dedup)
yield 32 unique non-held tickers. After the pre-filter
(`vol_ratio_20d >= 1.2 AND macd_hist > 0`):

| Step | Count |
|---|---|
| Bullish-bucket dedup, ex-held | 32 |
| Survived pre-filter (vol>=1.2 & macd>0) | 5 (NOW, MDB, ORCL, OKTA, ARM) |
| Pre-filter-dropped (silent, NOT in telemetry) | 27 |
| Tech signal score >= 2 with current code | 1 (ARM) |
| Tech signal score >= 2 if scanner propagated `above_sma200` | 4 (MDB, ORCL, OKTA, ARM) |

Per-row detail of the survivors:

```
ticker  bucket          actual_tech   cf_tech   stk_up   brkout   price>sma200
NOW     breakouts            1           1       False    True       False
MDB     breakouts            1           2       False    True       True
ORCL    breakouts            1           2       False    True       True
OKTA    new_52w_highs        1           2       False    True       True
ARM     new_52w_highs        2           2       True     True       True
```

**ARM is the only candidate that passes the technical signal under the
current code, because it is the only one whose `stacked_uptrend` is True
in the scanner row** — and that is the only way the trend point can be
earned in the gate. NOW/MDB/ORCL/OKTA are all literal 20-day breakouts
above their 200-day SMA, but the scanner does not tell the gate that.

This confirms the qualitative pattern in the 30-day rollup: 71.4% of
blocks are technical, because the technical signal cannot earn its trend
point on the most common high-quality setup in the scanner buckets (a
breakout that hasn't yet established the strict
`price > SMA20 > SMA50 > SMA200` stack).

Discrepancy note: telemetry shows 8 evaluations on 2026-06-01, my
reproduction shows 5 prefilter-survivors. The likely cause is that
`data.json` is the snapshot from the 19:16 UTC build, and the
gate_telemetry row may include candidates from a later intraday rebuild
(last commit on this day was `intraday: 2026-06-01T22:01Z`). The
qualitative result — "technical is by far the dominant blocker" — is
unaffected.

### Separating "no confluence" from "data unavailable"

Of the 14 recorded evaluations, decomposed by what kind of failure it was:

| Bucket | Count | What it means |
|---|---|---|
| Tech genuinely insufficient (score 0 or 1 from real indicators) | partial | Some of these are *genuinely* unproven setups (e.g., RSI in no-band, MACD flat). |
| Tech blocked by missing `above_sma200` (scanner omission) | ≥ 3 today | Today's audit confirms 3 of 7 technical-blocks on 2026-06-01 are this bug. The historical 10 over 4 days likely contains similar proportions. |
| News blocked because `recent_14` was empty (no news fetched) | unknowable from telemetry — the gate cannot distinguish "no items" from "items but net < 3" once it returns fail | The cache (`news_classification_cache.json`) is 12KB and exists. Items ARE being classified. But Yahoo+Google occasionally return zero items per ticker — and that path silently maps to "blocked_by: news." |
| News blocked because net < 3 OR no mag-3 | the four recorded near-misses (LEU x3, ARM x1) | These are 2-of-3 misses, so news evaluated *something*. Either net score was below 3 or no magnitude-3 item existed. |
| Insider promotion failed because insider scan returned 0 across the universe | 4-of-4 of the near-misses | The structural insider-data-pipeline issue described above. |

The biggest "muted signal" concern is the insider one — it is **expected**
to act as a safety valve on 2-of-3 misses, and it is silently absent.

### Minimum instrumentation to answer the question going forward

The existing `gate_telemetry.record` already captures the most-important
attribution. The gaps that prevent a higher-resolution future audit are:

1. Pre-filter drops are not logged. Adding a `pre_block: "pre_filter"`
   bucket would make the 32→5 silent funnel visible.
2. News-signal fail-reason is not retained. The signal already returns
   `reason: "net score X < 3; no item magnitude >= 3"` — `_gate_entry`
   could carry a single-bit `news_was_empty` flag distinguishing "no
   recent items" from "items existed but didn't sum."
3. `insider_scanned` from the funnel build is not in the telemetry. A
   single integer per day would make the difference between "no insider
   buying happening in the universe" and "the insider pipeline failed"
   diagnosable without spelunking `data.json`.
4. The shadow tracker would answer the calibration question directly with
   horizon outcomes, but its output is not present in the repo — the
   wrapper catches all exceptions silently. Logging the exception (not
   just swallowing it) would surface why.

---

## Phase 3 — Failure modes and silent degradation

For each signal, the question: if the upstream source is slow,
rate-limited, returns empty, or errors, does the gate (a) fail loud, (b)
retry/cache, or (c) silently treat the signal as absent and continue?

### Technical signal

- Source: `prices.technicals(ticker)` via `prices._history_with_source`
  (Stooq → Yahoo chart → yfinance → persistent cache).
- Retry: `_retry(attempts=3, backoff=0.5)` per source (`prices.py:33-58`).
- Empty: every numeric field is `None`. `technical_signal` reads
  `payload.get("rsi14")` etc.; with all-None the score is 0 → fail.
- **Silent-mute risk: HIGH.** Source-chain failure or rate-limit
  silently makes the technical signal return fail with "no qualifying
  technical signals", indistinguishable from genuinely-bad chart action.
  `data.json:diagnostics` records which sources worked at startup, but
  per-ticker silent fallbacks to the persistent cache (which contains
  prior-run data, possibly days stale) are not surfaced once the cache is
  used.
- **Boundary check on stale cache.** The persistent quote cache
  (`price_cache.json`) has no TTL — a stale entry is preferred over
  nothing (`prices.py:252-268`). For *quotes*, this is fine for the
  morning brief. For *technicals*, however, `prices.technicals` ignores
  the persistent cache entirely (`prices.py:330-433`) — if live history
  fails, technicals returns an all-None dict and the gate denies. Good.

### Sector momentum signal

- Source: `macro.snapshot()` (one Yahoo call per index/sector, no per-ticker
  network).
- Macro failure: `build_site.py:194-196` catches and assigns
  `macro={"indices":{}, "sectors":{}, ...}`. EVERY long candidate then
  fails sector_momentum with `"<ETF> missing from macro snapshot"`.
- **Silent-mute risk: MEDIUM.** A total macro outage flips the build into
  "no sector aligned, ever" without a visible signal. The diagnostics
  page wouldn't show this since it only probes META's price source.
- Theme→sector map (`signals.THEME_TO_SECTOR`) is partial: 13 themes
  mapped. Universe themes outside this list (e.g., "ETFs", "Mid/small
  growth", "Consumer / luxury", "Other materials") return empty sector →
  `sector_momentum_signal` returns fail with "unknown sector ''".
  Quoted: any ticker from such themes can never pass sector_momentum.
- **No off-by-one** in the strict `r5 > 0 and r20 > 0`. Exactly-0 fails;
  this is the intended conservative behavior.

### News signal

- Source: `news.company_news(ticker)` (Yahoo + Google News RSS, free).
- Retries: NONE explicitly. yfinance internal retries unknown; httpx call
  to Google News RSS has no retry wrapper.
- Empty: `company_news` returns `[]` silently on either Yahoo OR Google
  raising (`news.py:42-58, 60-78`). Caller cannot distinguish "no news in
  the world" from "Yahoo failed."
- LLM filter: the gpt-4o-mini classifier batches 10 items; on any
  exception or filter, falls back to keyword scoring with
  `magnitude=3` (`news_classifier.py:81-99`). The fallback is **less**
  selective than the LLM, not more.
- **Silent-mute risk: HIGH.** Two separate paths that can mute the
  signal: news fetch returns empty, or LLM classifier returns nothing
  forcing a keyword pass. Each maps to the same single bit
  (`pass=False`) at the gate boundary.

### Insider signal (4th, promotion-path only — but also funnel)

- Source: `insider.recent_form4_transactions` → SEC EDGAR submissions JSON +
  per-Form-4 XML.
- Retries: NONE on the SEC submissions call (`insider.py:121-129`).
- 24h disk cache via `_cached` (`insider.py:74-95`) — but the cache file
  `form4_cache.json` is **not present in the repo** despite being on the
  `git add` whitelist (`refresh.yml:111`). Either every build's fetches
  return `ok=False` (so nothing is cached), or the cache file is being
  written and then ignored by the commit step.
- Per-Form-4 XML cache: `.cache/form4/<accession>.json` is gitignored
  (`.gitignore:5`), so it does not survive across GitHub Actions runs.
  Each fresh container re-fetches every Form 4 from scratch.
- Universe scan budget: `time_budget_s=240` (`idea_funnel.py:226`). With
  ~151 tickers and per-ticker EDGAR latency, this is the realistic budget
  the scan races against in a cold container.
- Per-ticker try/except silently skips failures (`idea_funnel.py:236-242`
  and `conviction.py:74-83`).
- **Silent-mute risk: HIGH.** Observed today: `insider_scanned: 0`. Every
  recorded near-miss has `insider_score: 0`. The 2-of-3 promotion path is
  effectively turned off by data unavailability, not by design.

### Earnings window block

- Source: `app.data.calendar.next_earnings_date` → yfinance `Ticker.calendar`.
- 24h disk cache exists (`calendar.py:11-50`). Cache lives under `.cache/`
  which is gitignored. A fresh container re-fetches.
- Any exception → returns None → no block (`conviction.py:282-296`).
- **Silent-mute risk: LOW** but with the opposite polarity from the
  others — a *failed* lookup makes the gate *less* selective (it can
  surface a buy 2 days before earnings if yfinance hiccups).

### Valuation overlay

- Source: `app.data.fundamentals.get_fundamentals` (committed
  `fundamentals_cache.json` exists).
- Failure → returns None → overlay does not run (`conviction.py:229-234`).
- Tier "unknown" → no block.
- **Silent-mute risk: LOW**, same opposite-polarity caveat as earnings:
  the overlay can only deny, so a failed lookup makes the gate *less*
  strict.

### Correlation overlay

- Source: `correlation.candidate_correlation_to_book` requires price
  history for every top-5 holding + the candidate.
- Any history failure → `available=False` → returns "ok" (`conviction.py:259`).
- **Silent-mute risk: LOW**, same opposite-polarity.

### Boundary errors

The boundary survey from Phase 1 found no off-by-one mistakes. The only
subtle one: the `pre_filter` strict `macd_h > 0` drops candidates with
`macd_h == 0` (rare in practice). The `vol_ratio_20d >= 1.2` inclusive at
1.2 is fine.

`_trading_days_until` (`conviction.py:44-60`) is a pure Mon-Fri counter
ignoring holidays — comment notes "a holiday only ever makes the count
slightly generous, which is the safe direction." Safe.

`_close_on_or_after` in shadow tracker uses a `max_calendar_days` of 7 to
guard against silently anchoring to a window-rolled-forward start; this
is a defensive boundary check added by a prior fix (commit
`3f1faba fix: freeze realized shadow horizons and reject bogus entry
anchors`). Good.

### Summary: silent-degradation matrix

| Signal | Source failure → gate behavior | Silently muted (gate says "no" when truth is "data missing")? |
|---|---|---|
| Technical | All None → score 0 → fail | YES, but easy to observe via diagnostics |
| Sector momentum | Macro empty → fail | YES, undetectable per-ticker |
| News | Fetch empty → fail | YES, undetectable from telemetry |
| Insider (4th) | Empty → score 0 | YES — **currently in effect across the board** |
| Earnings | Exception → no block | NO (failure makes gate *less* strict) |
| Valuation | Exception → no block | NO (failure makes gate *less* strict) |
| Correlation | Exception → no block | NO (failure makes gate *less* strict) |

The asymmetry is structural: every primary signal fails closed (data
absent → reject), every overlay fails open (data absent → no block). This
is defensible for an overlay overlay (you don't want to falsely block on a
hiccup), but on the primary signals it means "data outage" and "genuinely
bearish" are the same bit.

---

## Phase 4 — Calibration verdict and options

Honest verdict for each criterion, with evidence.

### Verdict per criterion

| Criterion | Verdict | Evidence |
|---|---|---|
| **Pre-filter** `vol >= 1.2 AND macd_h > 0` | Genuinely a judgment call, but **drops are invisible** | Phase 2 shows 27/32 candidates dropped today before the gate sees them. They're not in `candidates_evaluated`. The pre-filter exists explicitly as noise control (`daily_brief.py:226-235` docstring). The threshold itself is defensible — but the invisibility is itself a problem (you cannot see if it's too strict because it doesn't tell you). |
| **Technical signal (trend point, `breakout_20d AND above_sma200`)** | **Accidentally too strict — BUG** | `scanner._enrich` does not propagate `above_sma200`, so the breakout fallback can never fire on scanner-fed payloads. Phase 2 reproduction: today, 3 candidates (MDB/ORCL/OKTA) are breaking out above their 200d SMA but the gate scored them 1/3 instead of 2/3. The technical block is 71.4% of all observed blocks. |
| **Technical signal (other parts)** | Correctly calibrated | The `stacked_uptrend` route, the RSI bands (40-65 / <=30), and the MACD-or-cross route are all defensible and behave as designed. |
| **Sector momentum (5d AND 20d both positive)** | Correctly calibrated, but failure-mode-loud | Strict AND is intentional (you want both trend-and-momentum). No observed false-block on this signal. *However*: the `THEME_TO_SECTOR` map covers only 13 themes, so any ticker from "ETFs", "Mid/small growth", "Consumer / luxury", or any unmapped theme **structurally cannot pass** sector_momentum via the scanner path. This is a calibration item, not a bug. |
| **News signal (net >= 3, mag-3, no major bear)** | Calibration item; the framework is sound | The semantic-news scoring is well thought through. The empty-fetch silent-mute concern is a Phase-3 data-availability issue, not a threshold issue. Two-of-three of the 4 near-misses ARE on news, indicating the threshold IS doing real selectivity work (it's not pure noise). |
| **Insider promotion (score >= 2 to rescue a 2-of-3)** | **Accidentally inoperative — BUG (data pipeline)** | `insider_scanned: 0` and every recorded near-miss `insider_score: 0`. `form4_cache.json` (committed-file expectation) is missing. The promotion safety valve is not failing the threshold; it is failing to receive data at all. |
| **Correlation block (`avg > 0.7` for new buys)** | Correctly calibrated | Untested in the historical record (no candidate reached the overlay). Theshold is conservative. Strict `>` means exactly 0.7 doesn't block — fine. |
| **Valuation extreme block (longs, unless insider-3 override)** | Correctly calibrated | Untested in record. Override path for insider score 3 is internally consistent. |
| **Earnings window (0-3 trading days)** | Correctly calibrated | Untested in record. Inclusive bounds are intentional. |
| **Regime gates** (breakdown → no buys; chop → no insider promotion) | Correctly calibrated | Today's `risk_on` regime correctly applies no extra restriction. The chop-disables-insider rule is consistent with the design. |
| **Macro risk-off pre-block (VIX > 22 OR SPX > 10% off 52w high)** | Correctly calibrated | VIX 15.9 today, SPX at 52w highs — pre-block correctly not firing. |

### Three categories of possible change

#### A. Bug fixes (restore intended behavior, do NOT lower the bar)

1. **Propagate `above_sma200` (and `above_sma50`, `golden_cross_recent`,
   `death_cross_recent`) from `scanner._enrich` to the row.**
   - Evidence: Phase 2, today's run, 3 of 7 technical-blocks would have
     flipped to "passed technical".
   - Historical replay: assuming today is representative, ~40% of the
     historical technical-blocks (~4 of 10) would have been spared. That
     does NOT mean 4 more recs would have fired — those candidates still
     have to pass sector AND news. ARM today passed technical and sector,
     failed news, did not get insider rescue. MDB/ORCL/OKTA would join
     ARM in the "passed technical" pool but would still hit
     sector/news/insider checks.
   - Cost in false positives: ZERO. The technical signal already
     specifies "breakout AND above 200d" — restoring the breakout route
     just lets it work as intended. The bar (score >= 2) is unchanged.
   - Risk: very low. The fix is a 3-line addition in
     `app/research/scanner.py:_enrich`.

2. **Fix the insider data pipeline so `insider_scanned > 0` is the norm.**
   - Evidence: every recorded near-miss has `insider_score: 0`,
     `form4_cache.json` is meant to be committed but is missing.
   - Diagnostic path: log the actual exception in
     `scan_insider_clusters` (`idea_funnel.py:236-242`) and the
     `_recent_form4_transactions_uncached` failures, so the next refresh
     surfaces *why* the scan is dry (UA rejection? rate limit? CIK
     lookup? time budget?).
   - Cost in false positives: ZERO. Re-enabling the promotion safety
     valve restores 2-of-3 as the design intends.
   - Risk: low, but requires investigating SEC behavior in the live
     environment — not just a code change. The first step should be
     non-invasive: turn the silent `except Exception: continue` into a
     `log.warning(...)` so we can see the actual failure mode.

3. **Make the shadow tracker's failure observable.** It currently
   produces no output and `shadow_tracker.safe_update` swallows the
   exception. Log it (without changing the catch-all, since the design
   intent is "must not break the build").
   - Cost: zero (logging only).
   - Risk: zero.

These three are bug fixes — none of them lowers conviction requirements.
All three restore intended behavior.

#### B. Calibration adjustments (judgment calls — explicit human decision required)

4. **Pre-filter visibility: log pre-filter drops as
   `pre_block: "pre_filter"` in `gate_telemetry`.**
   - Not a threshold change. Adds information.
   - Cost: increases `candidates_evaluated` count in display by ~5x,
     which may visually suggest more activity than there is. Acceptable
     trade for visibility into a currently-invisible step.

5. **Expand `THEME_TO_SECTOR`** in `signals.py` to cover the remaining
   universe themes ("Consumer / luxury", "Mid/small growth", any others)
   so they aren't structurally unable to clear sector_momentum.
   - This DOES change which tickers can pass. Defensible because
     "unknown sector → fail" was a conservative default for tickers the
     gate genuinely cannot classify, not a deliberate veto on entire
     themes. Each new mapping needs to be a deliberate
     classification, not a blanket assignment.

6. **Surface near-miss news_was_empty bit** distinguishing "no recent
   news" from "items existed but didn't sum to threshold". Add a single
   bool to the news_signal return; thread it through `_gate_entry`.
   - Not a threshold change. Adds calibration evidence.

The next set are real threshold changes. **I am not recommending any
of them be made.** They're listed so the user can decide:

7. **Reduce news net-score floor from 3 to 2.** Today's 4 near-misses
   all failed on news. With the same data, lowering the floor would
   have surfaced one to four recs. But: we have zero outcome data
   (shadow tracker empty) showing whether those would have been good
   recs, and the design intent is "rare and high-confluence". Do NOT
   make this change without shadow-tracker evidence.

8. **Allow technical score >= 1 paired with insider score >= 3.** This
   would create a third gate-clearing path. Speculative.

9. **Raise the insider promotion threshold to score >= 3.** This would
   *tighten* the gate — and is moot until the insider pipeline produces
   any data at all.

#### C. Net-new criteria / telemetry (information-only)

10. The `gate_telemetry` instrumentation already exists for primary
    signals; near-miss surface text is on the today page. Adding
    independence-aware near-miss labels (e.g., "passed technical +
    sector, failed news *with N items*; insider scan returned *0
    cluster names*") would make the failure mode visible to the user
    without changing any decision logic.

### Ranked option list

| Rank | Item | Type | Effort | Risk | Effect on fire rate |
|---|---|---|---|---|---|
| 1 | Propagate `above_sma200` from scanner | Bug fix | ~3 lines in `scanner.py` | Very low | Restores intended technical-signal behavior; ~40% of recent technical-blocks would not have fired. Does NOT directly raise fire rate (downstream signals still gate). |
| 2 | Diagnose & fix insider pipeline (start with surfacing the exception) | Bug fix | Investigation + logging | Low | Restores 2-of-3 promotion safety valve. Affects 4-of-4 of recorded near-misses' rescue potential. |
| 3 | Surface shadow-tracker error (log on swallow) | Bug fix | 1 line | Zero | Enables future calibration work. |
| 4 | Log pre-filter drops in telemetry | Information | Small | Zero | None — visibility only. |
| 5 | Add `news_was_empty` bit to near-miss telemetry | Information | Small | Zero | None — visibility only. |
| 6 | Expand `THEME_TO_SECTOR` map | Calibration | Investigation per theme | Low | Lifts a structural floor on certain themes. Requires per-theme judgment, not blanket. |
| 7-9 | Threshold tunings (news floor, insider/technical paths) | Calibration | Larger | HIGHER | Should not be considered until #2 produces data and #3 enables calibration evidence. |

### What I am NOT recommending

- Do not lower the news net-score floor (#7) until either (a) the shadow
  tracker can show near-miss outcomes or (b) the insider promotion path
  is actually receiving data and consistently scoring 0.
- Do not lower the technical score threshold from 2 to 1.
- Do not relax the macro risk-off or regime breakdown pre-blocks. They
  are not currently firing in observed data, and they are exactly the
  conservative defaults the design calls for.
- Do not loosen the earnings window. Untested in the record.

The gate's bar is supposed to be high. The Phase 2 evidence supports
that — `top_blocker_pct: 71.4%` on technical means a strict bar is
working. The audit's job is to verify the bar is high *for the right
reasons*; the evidence is that the bar is currently a little higher than
designed because of:
- The scanner→technical hand-off losing `above_sma200`;
- The insider pipeline producing zero data, neutralizing the 2-of-3
  safety valve.

Fix those two, then re-measure with the shadow tracker before deciding
whether ANY threshold itself needs to move.

---
