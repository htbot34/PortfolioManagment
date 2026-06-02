# Portfolio Advisor

**Live dashboard: <https://htbot34.github.io/PortfolioManagment/>**

Static, scheduled stock-portfolio research site. Runs entirely on GitHub. Free.
A GitHub Action refreshes every weekday at 22:00 UTC and after every change to
`portfolio.yaml`.

## How it works

- Reads holdings from `portfolio.yaml` and risk style from `risk_profile.yaml`
- Pulls prices via a fallback chain (Stooq -> Yahoo Chart -> yfinance)
- Pulls recent news from Yahoo + Google News RSS
- Generates buy / hold / trim / sell recommendations via a rule-based engine.
  An LLM (GitHub Models, gpt-4o-mini) is used for semantic news classification
  only. The recommendation engine itself is rule-based by design, so output is
  deterministic and inspectable. LLM failures fall back to keyword scoring.
- Reviews concentration / sector tilt / cash buffer against the risk profile
- Proposes new-position candidates that match the investor's themes
- Runs an **idea funnel**: merges momentum, theme fit, news/social and insider
  cluster buying into one confluence-ranked list, with an ATR-based swing-trade
  plan (entry / stop / target / hold window) on each idea
- Renders to plain HTML at the repo root and serves via GitHub Pages

## Reacting to ideas

Each idea on the candidates page has a **Give a verdict / discuss** link that
opens a prefilled issue. Pick `interested`, `watching`, or `pass`; the verdict
is recorded to `idea_queue.yaml` and feeds back into ranking on the next build
(`pass` drops the idea, `interested` boosts it). Verdicts never change
`portfolio.yaml` -- they are research metadata only.

**Advice only. No order placement. Not financial advice.**

## Why recommendations are rare

The conviction gate uses four primary signals -- **technical**, **sector
momentum**, **news**, and **Trump mention** -- with an insider-cluster
1-confirmation promotion path. Technical is a hard prerequisite; the
gate cannot reach the others without a technical pass. Most days return
`no_trade`. See `gate_telemetry.yaml` for what was evaluated and why
nothing cleared on a given day; the no-trade card on the dashboard
surfaces the closest miss and a 30-day rollup.

### Gate math

For a candidate that clears technical:

```
confirmations = pass count among (sector_momentum, news, trump)
qualifies     = confirmations >= trump_confluence_min        (default 2)
```

**Neutrality invariant.** When no qualifying Trump mention exists for a
ticker, the trump signal is a neutral fail and the math reduces to "both
sector AND news must pass" -- byte-for-byte identical to the previous
3-of-3 rule. The whole universe outside Trump-mentioned names continues
to qualify or fail exactly as before. A test
(`tests/test_conviction_trump_neutrality.py`) enumerates every
combination and would fail loudly if the Trump path ever leaked into
neutral tickers.

When a Trump endorsement DOES exist, it can substitute for one of
sector / news. An attack vetoes a new long entry; for an existing
holding it surfaces a `trump_exit_flag` annotation rather than
auto-trimming. Symmetric on the short side: attack confirms a short,
endorsement vetoes a new short.

### Trump-mention signal

Source: the existing gpt-4o-mini news classifier additionally returns
`trump_mention`, `trump_valence` (`endorse|attack|none`), and
`trump_confidence` (0..1) for each headline. There is no usable Truth
Social API, so the signal catches Presidential statements only when
they are reported in the news feed. A deterministic keyword fallback
covers the case where the LLM is unavailable.

#### Manual overrides

`trump_watchlist.yaml` is a committed, user-editable YAML list. Each
entry seeds or overrides a detection. Manual entries are treated as
confidence 1.0 and take precedence over news-derived mentions, so a
known-true statement can be surfaced even if the news feed missed it.

```yaml
- ticker: ACME
  valence: endorse        # or "attack"
  as_of: 2026-06-01
  source: https://example.com/news/article   # optional
  note: "President praised ACME on White House lawn."
```

Entries older than `gate.trump_ttl_days` (default 30) are ignored.

#### Config knobs (in `risk_profile.yaml` under `gate:`)

| Knob | Default | What it does |
|------|---------|--------------|
| `trump_signal_enabled` | `true` | Kill switch. When false the entire Trump path is inert; the gate behaves as it did before the feature. |
| `trump_ttl_days` | `30` | Mentions older than this are not signals. |
| `trump_min_confidence` | `0.6` | In-TTL mentions below this score are logged but do not pass. |
| `trump_confluence_min` | `2` | Confirmations required ON TOP of technical. The neutrality invariant depends on this being 2. |
| `trump_solo_with_technical` | `false` | When true, `technical + trump` alone qualifies (no third confirmation). Use deliberately. |
| `trump_attack_vetoes_longs` | `true` | When true, an attack vetoes a new long entry; an existing holding gets an exit annotation. |

### Shadow tracker (measurement only)

`shadow_ledger.yaml` and `shadow_calibration.yaml` accumulate evidence
for whether the gate is well-calibrated. The daily refresh records every
near-miss as a "shadow position" and tracks its realized forward return
vs SPY at 5, 10, and 20 trading days. The rollup groups results by the
signal that did the rejecting -- e.g. when `news` blocks a near-miss,
would acting anyway have paid off?

Trump firings are tracked the same way: every endorsement is logged as
a long shadow position; every attack as a short. The aggregator
produces a per-valence hit-rate so the thesis "endorsed names
outperform, attacked names underperform" is measurable over time
instead of assumed. The today page surfaces this once a non-zero
sample exists.

These files are read-only output. Nothing in the build consumes them,
no threshold is tuned from them, and the conviction gate is unchanged.
They are data for inspection, not a feedback loop.

## SEC credentials

The insider signal requires a real SEC User-Agent (see `.env.example`).
The CIK index is fetched live once and persisted to
`ticker_cik_cache.json` at the repo root; refreshes happen at most once
per `CIK_CACHE_TTL_DAYS` (default 7) so a single SEC outage can't
silently mute insider data on every build.

## Editing your portfolio

Edit `portfolio.yaml` and push. The workflow auto-refreshes within ~2 minutes.

## Running locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m app.build_site
python -m http.server 8000           # browse http://localhost:8000
```
