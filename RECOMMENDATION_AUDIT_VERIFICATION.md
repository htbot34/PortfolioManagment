# Recommendation gate — verification audit (post-Trump, post-fix)

This is a *verification* pass over the conviction gate, run against current
code and the committed data. It deliberately does **not** restate the prior
`RECOMMENDATION_AUDIT.md`, which predates the Trump-mention signal and the two
fix commits (`3863867`, `f12fed2`). Every claim below is backed by a code
path, a test, or a replay against committed `data.json` / `gate_telemetry.yaml`
/ `shadow_ledger.yaml`.

Design philosophy honored throughout: the gate is **rare by design, not
broken**. A zero-rec day is correct. Nothing here proposes lowering,
bypassing, or "unsticking" the gate. The hunt is for the opposite failures:
(a) quiet loosening below the original conviction bar, and (b) silent
fail-closed-on-missing-data that hides itself.

Two read-only replay harnesses back the findings (no behavior changed):
`audit_truth_table.py` (truth table) and `audit_oneway_and_trump.py`
(one-way-flip + Trump integrity).

---

## Baseline

| Item | Value | Source |
|---|---|---|
| Test suite | **392 passed** (`python -m pytest -q`) | run this session (prior audit recorded 340 → +52 tests for trump/insider/scanner work) |
| `data.json` build | **2026-06-02 15:37 UTC** (`generated_at`) | last full "Refresh site" build; later commits are intraday-only and don't regenerate `data.json` |
| Telemetry history | **5 days**: 05-28, 05-29, 05-30, 06-01, 06-02 | `gate_telemetry.yaml` |
| Candidates evaluated | **16** total (2+2+2+8+2) | `gate_telemetry.yaml` |
| Cleared (primary or insider) | **0** | `gate_telemetry.yaml` (every day `cleared_primary=0`, `cleared_insider_promotion=0`) |
| **Fire rate** | **0 / 16 = 0% over 5 days** | computed |
| Reached confluence-1 near-miss | 4 (LEU×3, ARM×1), all `failed: news`, all `insider_score: 0` | `gate_telemetry.yaml`, `shadow_ledger.yaml` |
| 30-day rollup | `days=5, cleared=0, reached_2of3=4, top_blocker_signal=technical, top_blocker_pct=75.0` | `data.json:brief.telemetry_30d` (block totals: technical 12, news 4 → 12/16 = 75.0%) |
| Trump activity | **0 mentions / 0 endorsements / 0 attacks / 0 promotions / 0 vetoes** every recorded day | `gate_telemetry.yaml:06-02 trump`, `data.json:brief.telemetry.trump` |

A 0% fire rate over 16 evaluations is *consistent with the design*, not
evidence of breakage. The recorded universe is entirely Trump-neutral
(`trump_watchlist.yaml` is `[]`; no headline tripped `trump_mention`), so the
Trump paths have never fired in production — the gate behaves as the pre-Trump
3-of-3 rule for the entire recorded history.

---

## Truth table — old design vs. current code

Built **empirically** by `audit_truth_table.py`: it forces each underlying
signal via payloads the live signal modules score deterministically (no
mocking of the signal layer), then calls `conviction.evaluate(direction="long",
action=None, gate_config=DEFAULT)`. `action=None` isolates the qualification +
promotion logic from the entry-only overlays.

Technical is a hard prerequisite (`conviction.py:163-169`), so every qualifying
row has `tech=1`. Columns: **conf** = `sector+news+trump` confirmations;
**NEW** = current `qualifies`; **OLD** = pre-Trump rule
`tech AND ((sector AND news) OR (exactly-one-of{sector,news} AND insider≥2))`.

| sect | news | trump | insider | conf | NEW q | OLD q | Δ |
|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:--|
| 1 | 1 | 1 | 1 | 3 | ✅ | ✅ | same |
| 1 | 1 | 1 | 0 | 3 | ✅ | ✅ | same |
| 1 | 1 | 0 | 1 | 2 | ✅ | ✅ | same |
| 1 | 1 | 0 | 0 | 2 | ✅ | ✅ | same (classic 3-of-3) |
| 1 | 0 | 1 | 1 | 2 | ✅ | ✅ | same |
| 1 | 0 | 1 | 0 | 2 | ✅ | ❌ | **WIDER — path #1 (sector+trump, news fails)** |
| 1 | 0 | 0 | 1 | 1 | ✅ (insider promo) | ✅ | same |
| 0 | 1 | 1 | 1 | 2 | ✅ | ✅ | same |
| 0 | 1 | 1 | 0 | 2 | ✅ | ❌ | **WIDER — path #2 (news+trump, sector fails)** |
| 0 | 1 | 0 | 1 | 1 | ✅ (insider promo) | ✅ | same |
| 0 | 0 | 1 | 1 | 1 | ✅ (insider promo) | ❌ | **WIDER — path #3 (trump+insider, BOTH sector & news fail)** |
| 1 | 0 | 0 | 0 | 1 | ❌ | ❌ | same |
| 0 | 1 | 0 | 0 | 1 | ❌ | ❌ | same |
| 0 | 0 | 1 | 0 | 1 | ❌ | ❌ | same |
| 0 | 0 | 0 | 1 | 0 | ❌ | ❌ | same (conf=0 ⇒ promo not attempted) |
| 0 | 0 | 0 | 0 | 0 | ❌ | ❌ | same |

**Neutrality invariant — VERIFIED.** Across all 16 `trump=0` combinations
(incl. `tech=0`), the replay found **0 divergences** from the old rule. The
README/`conviction.py:220-223` claim that the trump-neutral case is
"byte-for-byte identical to the prior 3-of-3 rule" holds, and is independently
enforced by `tests/test_conviction_trump_neutrality.py` (in the passing suite).

**Three wideners, all requiring `trump=1`:**
- **Path #1 / #2** (`sector+trump` or `news+trump`): the *intended*
  substitution — README §"Gate math" explicitly allows Trump to substitute for
  one of sector/news. Confluence count stays at 2. **Intended.**
- **Path #3** (`trump + insider`, sector AND news both failing): see Finding 1.
  **Not obviously intended; weakest qualifying path.**

---

## Findings

### Finding 1 — Insider promotion now fires on a *Trump-only* confirmation (sector AND news both failing) — `[conviction-loosening]` (latent)

> **✅ RESOLVED — Phase C, commit `42b9c7d`.** The promotion guard is now
> `confirmations == 1 and (sec_pass or news_pass) and tech["pass"] and insider>=2`
> (`conviction.py`), so a Trump mention alone can no longer open the insider-promotion
> tier. Trump still counts as a peer in the ≥2 confluence path (substitution paths #1/#2
> intact), but **no qualifying path now has both sector and news failing.** Truth-table
> replay confirms path #3 dropped, every insider-promotion row matches the original
> design, and neutrality holds (0 divergences); the bar tightened by exactly one
> combination. Tests:
> `test_conviction_trump_neutrality.py::test_trump_plus_insider_cannot_promote_when_sector_and_news_fail`
> (+ the two "still promotes on sector/news + insider" guards).

**Evidence.** `conviction.py:239-245`:

```python
if (not qualifies and confirmations == 1 and tech["pass"]
        and allow_insider_promotion):
    insider = _insider_signal(ticker, direction, ticker_payload, insider_fetcher)
    out_signals["insider"] = insider
    if insider.get("score", 0) >= 2 and insider.get("data_available", True):
        qualifies = True
        promoted_by_insider = True
```

`confirmations = sec_pass + news_pass + trump_pass` (`conviction.py:227`). The
promotion guard is `confirmations == 1` — and that one confirmation may be
**trump**. Replay row `sector=0, news=0, trump=1, insider=1` →
`qualifies=True, promoted_by_insider=True` (`audit_truth_table.py`, "WIDER"
section). So a long can clear with **both** sector and news actively failing,
on the strength of `technical + a single Trump mention + a Form-4 cluster`.

Under the old design the insider safety valve required the surviving primary to
be **sector or news** — an evidence-based momentum/catalyst signal. The new
code accepts trump as that primary, and trump can be as thin as a single
keyword-fallback headline scored at exactly the `0.6` floor
(`news_classifier.py:149-151`) or a one-line `trump_watchlist.yaml` entry at
confidence `1.0`. The inline comment at `conviction.py:234-238` calls this
"the same conservative spirit as the old 2-of-3 path"; that characterization is
weaker than reality for the trump-only case.

**Why it's not currently firing (and why that's the danger).** It is **latent**:
it needs a qualifying Trump mention *and* a Form-4 cluster ≥2 simultaneously.
Trump mentions: 0 in all recorded history. Insider data: unavailable
(Finding 3). The moment SEC access is restored (Finding 3) *and* one headline
trips `trump_mention`, this path activates with no further review — and it is
the single weakest combination the gate will ever pass.

**Smallest correct fix (described, not applied).** Keep Trump as a substitute in
the 2-confirmation confluence path (#1/#2), but require the *insider-promotion*
single-confirmation to be an evidence primary:

```python
# promote only when the surviving confirmation is sector or news,
# not trump alone — preserves the old safety-valve's evidence bar.
if (not qualifies and confirmations == 1 and tech["pass"]
        and (sec_pass or news_pass)            # <-- add this guard
        and allow_insider_promotion):
```

Alternatively, require `insider.score == 3` (not just ≥2) when the sole
confirmation is trump. Either keeps #1/#2 intact and closes #3.

---

### Finding 2 — Insider unavailability is observable for *today* but not recorded in the durable telemetry — `[fail-closed-hidden]`

> **✅ RESOLVED — Phase A, commit `df13706`.** `conviction.evaluate` now emits an
> `insider_status ∈ {scored, zero, unavailable, not_evaluated}`, threaded through
> `daily_brief._gate_entry` into `gate_telemetry.record` (per-day tally + per-near-miss
> field) and `rollup` (`insider_unavailable_days`). A near-miss with insider
> *unreachable* now serializes distinctly from a genuine score-0 in
> `gate_telemetry.yaml`, and the no-trade card surfaces it. Tests:
> `test_gate_telemetry.py::test_insider_unavailable_serializes_distinctly_from_zero`,
> `test_conviction.py::test_status_insider_unavailable_propagates`. Gate bar unchanged
> (truth table identical, 0 neutrality divergences).

**Evidence — the code fix is real and good.** A CIK/EDGAR failure now propagates
as `data_available=False` rather than a silent zero:
- `filings.py:119-136` — `_ticker_to_cik()` raises `CIKLookupError` when there's
  no live response *and* no cache, and records `LAST_CIK_ERROR`.
- `insider.py:294-320` — records the reason in `LAST_FETCH_ERRORS` and returns
  `ok=False`.
- `conviction.py:88-92, 99-106` — the gate's default insider path reads
  `LAST_FETCH_ERRORS`, sets `data_available=False`, and `insider_cluster_score`
  forces `score=0` (`insider_signal.py:77`); promotion requires
  `score>=2 AND data_available` so an unavailable signal **cannot** promote.
- The offense path passes **no** `insider_fetcher`
  (`daily_brief.py:306-309`), so this default branch is what runs in production.
- It surfaces on the today page: `index.html:161-163` renders an "Insider data
  unavailable" badge when `cik_index_error` is set.

This closes the stale audit's "silent score-0" concern. **Verified.**

**Evidence — the residual hidden gap.** The availability bit is **not** written
to the durable record:
- `daily_brief._gate_entry` (`daily_brief.py:46-61`) carries `insider_score` but
  **no** `data_available`/availability field.
- `gate_telemetry.record` near-miss rows store only
  `insider_score: int(...)` (`gate_telemetry.py:121-126`).
- `gate_telemetry.persist(brief["telemetry"])` (`build_site.py:362`) writes
  exactly that record. So in `gate_telemetry.yaml`, a near-miss with insider
  **unavailable** (`data_available=False → score 0`) is byte-identical to one
  where insider was fetched cleanly and was genuinely 0.

**Proof against committed data.** `gate_telemetry.yaml` and `shadow_ledger.yaml`
show `insider_score: 0` for every LEU/ARM near-miss with no availability marker
— yet `data.json:insider_diagnostics.cik_index_error` for the *same* build is a
hard `403 Forbidden` on `https://www.sec.gov/files/company_tickers.json`. The
"why" exists only in the ephemeral `data.json` (overwritten every build); the
30-day history cannot distinguish outage from genuine-zero.

**Smallest correct fix.** Thread `insider_data_available` (and optionally the
reason) from the gate result through `_gate_entry` into the near-miss record and
into a per-day `insider: {available: bool, tickers_unavailable: int}` block in
`gate_telemetry.record`, mirroring the existing `trump:` block.

---

### Finding 3 — Insider data still does not flow (SEC 403); the 2-of-3 → 1-of-3 promotion valve remains inoperative in production — `[fail-closed-hidden]` (now visibly)

> **◑ HARDENED — Phase B, commit `445cb52`** (root-cause clearance needs one owner action).
> Code/config side is done: `_ticker_to_cik` now distinguishes a **403** (UA rejected)
> from a **429** (throttled) in the diagnostic (`_describe_fetch_error`, with body
> snippet + actionable hint); the CIK TTL is now **30 days** so a near-static map
> rarely re-hits SEC and resolution falls back to the cached map on any transient
> outage (a 403 is never cached, so recovery is never suppressed); a true no-cache
> outage still raises `CIKLookupError → data_available=False` (no silent zero). A loud
> startup warning fires when `SEC_USER_AGENT` is a placeholder / non-deliverable
> (`config.sec_user_agent_is_placeholder` + `build_site._warn_if_placeholder_ua`), and
> `refresh.yml` now reads a `SEC_USER_AGENT` **repo secret** with a TODO. **Remaining
> owner action:** set that secret to a real monitored `Name email@domain` (no address
> was invented). Until then the signal stays loudly unavailable — correct, not silent.
> Tests: `test_insider_cache.py::test_cik_403_with_cache_serves_cached_map`,
> `::test_403_with_no_cache_propagates_data_available_false`,
> `::test_sec_user_agent_placeholder_detection`,
> `::test_build_site_warns_loudly_on_placeholder_ua`.

**Evidence.** From the committed `data.json` (2026-06-02 build):

```
insider_diagnostics.cik_index_error:
  "CIK index fetch failed: HTTPStatusError: Client error '403 Forbidden'
   for url 'https://www.sec.gov/files/company_tickers.json' ..."
idea_funnel.insider_scanned: 0
idea_funnel.source_counts:    {momentum: 24, theme: 15, news: 10, insider: 0}
```

`form4_cache.json` and `ticker_cik_cache.json` are **still absent from the
repo** (the fix added them to the workflow git-add whitelist at
`.github/workflows/refresh.yml:111`, with `|| true` so a missing file is
silently skipped — `:112`). Because the live CIK fetch 403s, nothing is ever
written to cache, so nothing is ever committed, so every cold container 403s
again. The default `SEC_USER_AGENT` (`config.py:23-26`) is a placeholder SEC
rejects; the workflow is expected to inject a real one and evidently isn't (or
SEC is rejecting it).

**Net effect.** The insider-promotion path — the gate's safety valve for
confluence-1 misses — has produced **zero** rescues across all recorded history,
not because thresholds are wrong but because it receives no data. This is the
correct fail-closed behavior (the gate must not promote on data it doesn't
have), and it is now **loud** (today-page badge + `insider_diagnostics`), which
is the improvement over the stale audit. But it remains invisible in the durable
record (Finding 2) and means the gate sits **above** its designed bar on
confluence-1 misses: the rescue the design promises is structurally unavailable.

**Smallest correct fix.** Operational, not code: set a valid
`SEC_USER_AGENT` secret (`"Name email"`) in the workflow env and confirm
`ticker_cik_cache.json` lands in the next commit. Until then the loud diagnostic
is the right behavior — do **not** paper over it.

---

### Finding 4 — News-fetch failure is indistinguishable from "no news" and is surfaced nowhere — `[fail-closed-hidden]`

> **✅ RESOLVED — Phase A, commit `df13706`.** The dual-feed boundary in `news.py`
> now reports a status: `_yahoo_news`/`_google_news` return `(items, ok)`, and the
> new `company_news_with_status` returns `(items, status)` with
> `status ∈ {ok, empty, outage}` — a both-feeds-down outage is no longer coalesced
> into an empty list. `conviction.evaluate` records `news_status ∈ {ok, empty, outage,
> unknown}` (the gate fetches via `company_news_with_status`), threaded into
> `gate_telemetry` (per-day tally, per-near-miss field, `news_outage_days` in the
> rollup) and surfaced on the no-trade card. `company_news` keeps its list contract
> for `analyst.py`. Tests:
> `test_news_filter.py::test_status_outage_when_both_feeds_fail`,
> `test_gate_telemetry.py::test_news_outage_serializes_distinctly_from_empty`,
> `test_conviction.py::test_status_news_outage_from_status_fetcher`.

**Evidence.** `news.py:44-46` (`_yahoo_news`) and `news.py:67-69`
(`_google_news`) both `except Exception → return []`, logged only at
`log.debug`. `company_news` merges them, so a dual-feed outage yields `[]`,
which `signals.news_signal` reports as `"no recent classified news"`
(`signals.py:215-216`) → `pass=False`. Nothing distinguishes "the feeds failed"
from "the company genuinely had no news," and unlike the insider path, news
received **no** diagnostics treatment — it appears in neither `data.json`, the
brief, nor `gate_telemetry.yaml`.

This matters because **news is the single binding blocker on every recorded
near-miss** (4/4 `failed: news`). If any of those were fetch outages rather than
genuine no-catalyst, the telemetry would look identical, and a fetch outage
fails the gate closed and silently.

**Smallest correct fix.** Have `company_news` (or a thin wrapper) distinguish
"both providers raised" from "both returned empty," set a `news_fetch_failed`
bit on the news signal, and thread it into `_gate_entry` /
`gate_telemetry.record` (same mechanism as the insider fix in Finding 2).

---

### Finding 5 — One-way-flip property holds; overlays are deny-only — `[verified — no defect]`

**Evidence (exhaustive).** Every assignment to `qualifies` in `conviction.py`:

```
228  qualifies = confirmations >= cfg["trump_confluence_min"]   # initial math
231  qualifies = True            # trump_solo — OFF by default (see below)
244  qualifies = True            # insider promotion (the only intended F→T)
261  result["qualifies"] = False # trump veto (long)
271  result["qualifies"] = False # trump veto (short)
286  result["qualifies"] = False # correlation block
306  result["qualifies"] = False # valuation extreme block
325  result["qualifies"] = False # earnings window block
```

After the initial math, the **only** `False→True` transitions are line 244
(insider promotion) and line 231 (`trump_solo_with_technical`, which
`risk_profile.yaml:53` sets `false`, `conviction.py:122` defaults `false`, and a
grep confirms nothing in `app/` flips on — only tests pass it explicitly). All
five overlays are strictly deny-only.

**Replay proof** (`audit_oneway_and_trump.py`):
- Qualifying long + Trump attack (`new_buy`) → `qualifies False`, `trump_block`
  set (True→False). ✅
- **Non**-qualifying long + Trump attack → stays `False` (veto cannot
  manufacture a True). ✅
- **Valuation score-3 override only PRESERVES:** a non-qualifying long fed an
  `extreme` tier stays `False` with no `valuation_override` (the override branch
  requires `promoted_by_insider AND score==3`, `conviction.py:304`); an
  insider-score-3 promoted long keeps `qualifies True` with `valuation_override`
  set. The override never creates a True from a False. ✅

---

### Finding 6 — Trump signal integrity is sound — `[verified — no defect]`

**Evidence** (`audit_oneway_and_trump.py`, against live `trump_signal.evaluate`):

| Property | Result | Code |
|---|---|---|
| Confidence floor enforced | conf `0.5` < `0.6` → `mention=False`, logged in `low_confidence_seen` | `trump_signal.py:152-155` |
| Floor is inclusive | conf `0.6` == floor → `mention=True` (a low-conf mention logs but cannot pass) | `trump_signal.py:152` |
| TTL expiry | 40-day-old, TTL 30 → `mention=False` | `trump_signal.py:138-139` |
| Manual override is TTL-gated | stale watchlist entry (>TTL) → `mention=False` | `trump_signal.py:102-103` |
| Manual override is valence-validated | `valence: bogus` → `mention=False` | `trump_signal.py:99-100` |
| Manual override precedence | fresh entry → `mention=True, confidence=1.0, manual=True` | `trump_signal.py:194-205` |
| `trump_solo_with_technical` | OFF; nothing in `app/` enables it | grep + `risk_profile.yaml:53` |

**Veto direction guards (Finding 2 of the brief's seam list).** The `is_entry`
action-set mixes long and short verbs (`conviction.py:255-256`), but every
effect is nested inside `if direction == "long"` / `elif direction == "short"`
(`:257-274`), and the veto only ever assigns `result["qualifies"] = False`. So a
cross direction/action pairing (never produced by the build — offense is
`long/new_buy`, defense is `short/{sell,trim}`) could at worst add an extra veto
(stricter), never loosen. **A veto can only flip True→False.** ✅

**Minor asymmetry (cosmetic).** On the short side, `action="trim"` is **not** in
the `is_entry` set, so a Trump *endorsement* on a held name being trimmed neither
vetoes nor annotates — the endorsement is silently dropped on the trim path
(`audit_oneway_and_trump.py` (A2): `short+endorse action=trim → qualifies=True,
trump_block=False`). This is defense-side only (it does not loosen the buy gate)
and arguably correct (a trim isn't a new short), but the bullish endorsement is
invisible on that decision. Low priority.

---

### Finding 7 — Scanner→technical `above_sma200` hand-off — `[verified FIXED]`

**Evidence.** `scanner.py:39-40` now emits `above_sma50`/`above_sma200` on the
row. In committed `data.json`, **29/29** bullish scanner rows carry
`above_sma200` (replay). `technical_signal` reads the flat key
(`signals.py:95-100`) and scores the `breakout_20d AND above_sma200` trend route
(`signals.py:111`). Direct replay on the audit's exact dead case (a fresh
breakout above SMA200, not yet stacked, RSI out of band): score **1 (fail)**
without the key → **2 (pass)** with it. Guarded by
`tests/test_trade_from_scanner.py::test_scanner_row_propagates_above_sma_to_technical_signal`
(passing).

**Replay of today's candidates (plumbing vs. genuine).** Of 24 non-held bullish
candidates in the 15:37 build, **17 pass technical, 7 fail (genuine), and 0 flip
fail→pass due to the fix** — because today's breakout candidates already satisfy
`stacked_uptrend` (so they earn the trend point via the stacked route
regardless). The fix is live and correct but **data-dependently inert today**;
on the stale audit's day it would have rescued 3 (MDB/ORCL/OKTA). This bug is
**closed** and the technical signal is restored to its *designed* (not stricter)
bar.

---

### Finding 8 — Overlay/calendar dependencies fail *open* and invisibly — `[cosmetic]` (opposite polarity)

For completeness on the seam "what happens on failure": the **overlays** fail
*open* (a failed lookup makes the gate *less* strict, never blocks):
`_earnings_block` (`conviction.py:393-403`, any exception → `None` → no block),
`_valuation_assess` (`:336-348`, exception → `None` → no overlay),
`_correlation_assess` (`:366-367`, `available=False` → "ok"). None is surfaced
in telemetry. This is the safe polarity for deny-only overlays (you don't want a
data hiccup to manufacture a *block*), so it is not a fail-closed-hidden defect —
but a silently-skipped earnings block could let a buy through 1–2 days before
earnings on a calendar hiccup. Worth a one-line telemetry note, not a fix.

Also minor: `insider.reset_diagnostics()` (`insider.py:378`) is defined but
**never called** (grep). Harmless in a fresh GitHub-Actions container (clean
process), but means `LAST_FETCH_ERRORS` is process-cumulative if ever reused.

---

### Finding 9 — `conviction.py` module docstring is stale and misleading — `[cosmetic]`

> **✅ RESOLVED — Phase C, commit `42b9c7d`.** The module docstring was rewritten to
> describe the actual two-path design (≥2 confluence PRIMARY path incl. Trump; insider
> PROMOTION requiring a fundamental surviving primary) and the deny-only overlays.

**Evidence.** `conviction.py:1-14` still describes the **pre-Trump** gate:

> `INSIDER PROMOTION  exactly two of the three pass, technical is one of them
>  ... The failing signal must be sector or news, never technical.`

The live code promotes at `confirmations == 1` over `(sector, news, trump)`
(`:239`) and never mentions Trump in the module docstring. A reader trusting the
docstring would not discover Finding 1's path #3. The inline comment at
`:234-238` is current; the module-level docstring is not. Update the docstring to
match (and, if Finding 1's fix is adopted, to state that the surviving
confirmation must be sector or news).

---

## Verdict: is the gate at, above, or below its designed bar?

**Currently: at — to slightly above — its designed bar in production. Not below.**

- **Neutral universe (the entire recorded reality).** With `trump_watchlist`
  empty and 0 `trump_mention` firings, the gate reduces byte-for-byte to the old
  3-of-3 rule — proven by replay (0/16 neutrality divergences) and by
  `test_conviction_trump_neutrality.py`. **At bar.**
- **`above_sma200` plumbing.** Verified fixed and live; the technical signal is
  restored to its designed bar (the stale audit's "accidentally too strict" is
  resolved). **At bar.**
- **Insider rescue.** Structurally unavailable (SEC 403). Confluence-1 misses
  get no rescue, so the gate is effectively **slightly above** bar on exactly
  the misses the safety valve was meant to catch — but this now fails closed
  **visibly** (today-page badge), which is the correct direction. **Above bar,
  honestly.**

**Latent below-bar risk — CLOSED (Phase C, `42b9c7d`).** Finding 1's path #3
(`technical + trump + insider`, sector and news both failing) was genuinely below the
old conviction bar and would have activated silently once SEC access was restored. The
promotion tier now requires a fundamental surviving primary (sector or news), so the
trapdoor is shut: a rec can no longer clear with both sector and news failing. Verified
by truth-table replay (path #3 → `qualifies=False`) with neutrality still at 0
divergences and the bar tightened, not loosened.

**Hidden fail-closed gaps (real, but not a loosening):** insider availability
(Finding 2) and news-fetch failure (Finding 4) are both invisible in the durable
`gate_telemetry.yaml`. A gate that fails closed and hides *why* is, per the
brief's own standard, as bad as a miscalibrated one — these are the highest-value
instrumentation fixes even though neither lowers the bar.

**Priority of the proposed (un-applied) fixes:**
1. Finding 1 — close path #3 (require sector|news for insider promotion, or
   demand insider score 3 on a trump-only confirmation). *Prevents a silent
   future loosening.* **✅ DONE — Phase C, commit `42b9c7d`** (required sector|news).
2. Finding 2 + 4 — thread `data_available` / `news_fetch_failed` into
   `gate_telemetry.yaml`. *Makes the fail-closed states auditable in history.*
   **✅ DONE — Phase A, commit `df13706`.**
3. Finding 3 — fix `SEC_USER_AGENT` (operational).
   **◑ HARDENED — Phase B, commit `445cb52`** (resilience + 403/429 diagnostics + loud
   warning + secret wiring done; owner must set the `SEC_USER_AGENT` secret).
4. Finding 9 — correct the stale docstring.

No code behavior was changed in this pass.
