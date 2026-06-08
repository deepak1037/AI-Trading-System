# CHANGELOG — deviations & additions beyond CLAUDE.md

This file tracks everything built or changed that is **not** in the original
`CLAUDE.md` / `CLAUDE_SETTINGS.md` spec, or that corrects a spec assumption that
turned out to be wrong in practice. CLAUDE.md says "Never deviate from these
decisions without updating this file first" — this is that record.

Keep newest first. Reference the git commit where relevant.

---

## Tuning: MACRO_SURPRISE_MODERATE 1.0 → 0.5 (NFP backtest 62.2% → 67.6%)

The NFP backtest reports at `min_z=0.5` but MacroEngine only assigned a direction
at `|z| ≥ MACRO_SURPRISE_MODERATE` (1.0), so the 8 events with `0.5 ≤ |z| < 1.0`
were predicted **neutral** — and neutral counts as wrong. Lowering the threshold
to 0.5 gives those events a direction; 2 (2020-02-07, 2024-01-05) had real long
moves and now score correct → **25/37 = 67.6% (PASS)**, 0 neutrals. The `min_z=1.0`
slice is unchanged at 79.3% (those events are already directional). This was NOT
a regression: the 79.3% number was a `min_z=1.0` run; nothing in the model or the
(scanner) weights changed. Setting is used only in `macro_engine` direction
mapping; updated in settings.py + .env + .env.example.

## New feature: LLM-augmented put-selling ROI analyzer (not in original spec)

A new capability inspired by the user's reference tool `CharlesSchwabVS`.

- **`broker_client/llm_roi_analyzer.py`** — `PutROIResult`, `LLMAssessment`,
  `LLMROIAnalysis`, `LLMROIAnalyzer`. Computes put-selling ROI (real Schwab
  margin + dual cash-secured basis + net premium) and gets a Claude
  probability-adjusted assessment (STRONG_SELL..AVOID).
- **`scripts/analyze_put.py`** + `make analyze ticker=HOOD strike=8 expiry=...`
- **`dashboard/pages/6_options_analyzer.py`** — ROI table across expiries +
  color-coded recommendation badge. (Slot `5_` was taken by the orders page.)
- Commits: `3eff740`, `b23902d`, `ec4f8f3`, `668fe80`, `465347d`, `97f53b5`,
  `753a2d9`.

### Key design facts learned (and baked in)
- **Real margin = available-funds delta, NOT buying-power delta.** Schwab's
  `buyingPower` is leveraged (~4×), so its delta overstates margin. Use
  `availableFunds − projectedAvailableFund`. (Fixed in `465347d`.)
- **Schwab put-margin is constant across expiries** — it's strike-driven
  collateral, not time-driven. Not a bug. (`97f53b5`.)
- **Do NOT gate margin on `preview.is_valid`** — Schwab flags naked-put previews
  with a benign alert (`is_valid=False`, empty rejection) while the balance
  projection is still valid.
- **IV units differ by source** — Schwab `volatility` is a percent (70.82),
  yfinance `impliedVolatility` is a fraction (0.62). Normalized via `_normalize_iv`.
- **Greeks sentinels** — Schwab uses `±999` for missing delta/IV; dropped via
  `_clean_greek`.

---

## Per-stock composite scoring (replaced hardcoded constants)

`update_from_stage4` previously scored every stock with constants (fund=70,
inst=70, tech=80, squeeze=50, tf=60 → flat 68 for all). Now each name is scored
from its own data, threaded through the stages:
- The three `screen()` methods return `list[dict]` (fundamental flags /
  accumulation data / technical data) instead of `list[str]`. Bool wrappers
  (`_passes_accumulation`) kept where other code depends on them.
- `scanner/pipeline.run_funnel()` merges Stage 3+4 data by ticker;
  `update_from_stage4(stage4_data: dict)` runs Stage 5 and computes the real
  composite via the scorer's `*_from_*` methods. `scripts/run_scanner.py`
  (make scanner) + `build_watchlist.py` use it.
- Fixed a staleness bug: a name still in Stage 5 but now scoring below threshold
  was neither re-scored nor removed (kept the old flat 68). Watchlist is now set
  to exactly the qualifying Stage-5 survivors with fresh scores.

Live (paced, 502-name universe): 82 names, scores **65–80**, 15 distinct values.

**Re-weighted** (fundamental 35 / institutional 25 / technical 30 / squeeze 5 /
tf 5, from 30/25/20/15/10) via new `SCANNER_WEIGHT_*` settings — squeeze/tf cut
because quality momentum leaders rarely have short-squeeze setups. Result: range
65–83, 96 names; NVDA=74, ASML=71, AMD=74 now rank above F=67, CVS=67, INTC(<65).

**Moomoo OpenD is now the primary Stage 3 source** (free, local, accurate
quarterly EPS). Priority: Moomoo → FMP(paid) → yfinance. `_moomoo_flags` uses
`get_financials_statements` (income statement; diluted EPS 8048 / revenue 8001),
filters `report_list` to quarters, derives eps_accelerating (QoQ|YoY) +
rev_reaccelerating (YoY>5%). OpenD caps financials at 30 req/30s → paced at
`MOOMOO_PACE_SECONDS=1.1`. Because Moomoo is local, Stage 3 no longer consumes
the yfinance budget. Live result: Stage 3 431/516, watchlist **89 names, range
65–83**, and **CRWD now appears (score 68)** — its real quarterly EPS (0.11/0.15
recovering from losses) flips `eps_accelerating` True, which the GAAP yfinance
fallback missed. settings: MOOMOO_HOST/PORT/PACE_SECONDS.

**FMP free tier is unusable for fundamentals.** FMP deprecated `/api/v3`
(Aug 2025) and made per-symbol income statements paid — a free key returns HTTP
402 for non-demo symbols. `fundamental_screen` now uses the `/stable` API and
probes capability once (income-statement for CRWD); a free/legacy key auto-falls
back to yfinance `.info` (GAAP earnings) with a clear warning, and logs "Scanner
data source: FMP" only when fundamentals are genuinely accessible (paid plan).
The GAAP fallback misranks growth names (e.g. CRWD marked `eps_accelerating=False`
→ stays below threshold). A **paid FMP plan** is the only fix for accurate
growth-stock fundamentals.

## New settings (config/settings.py + .env.example)

Not in `CLAUDE_SETTINGS.md`:

| Setting | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | `""` | Claude assessment in the ROI analyzer |
| `LLM_MODEL` | `claude-sonnet-4-6` | Model for assessments |
| `LLM_MAX_TOKENS` | `1024` | |
| `LLM_TIMEOUT_SECONDS` | `60.0` | |
| `OPTIONS_CONTRACT_MULTIPLIER` | `100` | Shares per contract |
| `OPTIONS_MARGIN_BASIS` | `reg_t` | `reg_t` \| `cash_secured` (formula fallback) |
| `FMP_API_KEY` | `""` | Financial Modeling Prep — Stage 3 fundamentals |

---

## Broker model additions (broker_core/base_broker.py)

- `Account.available_funds` — cash available before leverage (margin basis).
- `OrderPreview.projected_available_fund` — projected available funds from a
  preview. Both populated by `schwab_broker` from `currentBalances.availableFunds`
  and `orderBalance.projectedAvailableFund`. Needed for correct real margin.

---

## Bug fixes to original modules

- **`broker_core/schwab_broker.py` `get_options_chain`**: passed a `str` to
  schwab-py's `from_date`/`to_date`, which require `datetime.date`. Now converts
  via `date.fromisoformat`. (Surfaced by the options analyzer; `3eff740`.)

- **`scanner/universe.py` (Stage 2)**: yfinance rate-limiting reduced the liquid
  set drastically. Mitigations: batch size 25, 1.5s inter-batch sleep,
  exponential backoff + retry on `YFRateLimitError`, `threads=False`, and
  suppression of per-ticker delisted-ticker log spam (single summary count).
  (`885b8c5`, `ffd70ff`.)

- **`scanner/fundamental_screen.py` (Stage 3)**: returned 0 stocks because
  yfinance's earnings calendar (`earnings_dates`/`quarterly_earnings`) now
  returns "No earnings dates found" for nearly every ticker. **Rewritten** to:
  - Use **FMP** free API (quarterly EPS/revenue + estimate revisions) when
    `FMP_API_KEY` is set, else
  - Fall back to yfinance `.info` (`trailingEps`, `revenueGrowth`,
    `earningsGrowth`, `grossMargins`) with lenient OR thresholds.
  - Earnings calendar is no longer used at all. (`710cfcf`.)

- **`scanner/technical_screen.py` (Stage 5)**: could **never** pass a stock —
  `close.rolling(252).max()` needs 252 rows but 1 year of daily bars is ~251, so
  `high_52w`/`low_52w` were NaN and every `last >= 0.75*NaN` comparison was False.
  (Hidden until Stage 3 was fixed and the funnel finally reached Stage 5.) Fixed
  with `min_periods=100`. Also: fetch each ticker's OHLCV once (was downloading
  twice — once for Stage 2, again for RS), RS is now a true **percentile rank**
  within the universe, and a tight base is a bonus not a hard gate (requiring a
  base AND a breakout simultaneously is near-contradictory). (`<this commit>`.)

- **yfinance rate-limiting starved the funnel**: Stages 3+4 burned ~970 yfinance
  calls and tripped the rate limit before Stage 5 ran, so Stage 5 got empty
  frames → 0. The full 15,749-ticker Stage 1/2 made it worse (exhausted yfinance
  for ~30 min). Mitigations: new `SCANNER_YF_PACE_SECONDS` (default 0) paces
  per-ticker calls in Stages 3/4/5; `scripts/build_watchlist.py` uses a curated
  liquid universe (no 15k download) + pacing + recovery-wait to build the
  watchlist reliably.

- **`scanner/accumulation_screen.py` (Stage 4)**: the package imports as
  **`edgar`**, not `edgartools`, so `import edgartools` always failed into the
  TODO/proxy path. Fixed: `import edgar` + `edgar.set_identity(...)`, Form 4
  open-market buys via `Form4.common_stock_purchases`, and insider buying made a
  *rescue* OR-signal rather than a mandatory gate (open-market buys are rare).
  edgar is only queried for names failing the institutional gate. (`710cfcf`.)

---

## Dependency changes (requirements.txt)

| Package | Was | Now | Why |
|---|---|---|---|
| `anthropic` | 0.39.0 | **0.107.0** | 0.39 passed removed `proxies=` kwarg to httpx>=0.28 → client crash |
| `edgartools` | 3.5.0 | **5.35.1** | match installed/working version; 5.x Form4 API |

> Note: `httpx` is pinned by schwab-py/edgartools at 0.28.1 — do not downgrade it
> to fix anthropic; upgrade anthropic instead.

---

## Earlier additions (prior sessions, also beyond original spec)

- **`scripts/healthcheck.py`** + `make healthcheck` — 24-component system health
  check (config, DB, broker auth/quote/chain, signals, scheduler, paper, dashboard
  pages, etc.). (`8aa5259`.)
- **`dashboard/pages/5_orders.py`** — manual equity/options order entry with
  preview + triple-gate live confirmation. (`fc83155`.)
- **Positions page stop-loss/take-profit editor** — writes SL/TP to SQLite,
  picked up by PositionWatcher; creates a stub row for broker positions not yet
  tracked. (`efae6c5`.)
- **`Makefile`** — 12+ operational commands (start/stop/dashboard/scanner/
  watchlist/positions/status/logs/test/lint/healthcheck/analyze/paper-balance).
  `dashboard` target now `pkill -f streamlit` first to avoid stale-module bugs.
  (`e84b9e8`.)
- **Universe sources** — replaced Wikipedia (403s) with NASDAQ Trader FTP + SEC
  EDGAR `company_tickers.json` + GitHub S&P500 CSV. (`17b6b76`.)

---

## Open items / known limitations

- **13F reverse-lookup** (which institutions hold a given ticker) is not feasible
  per-ticker without downloading thousands of filings — Stage 4 institutional
  ownership still comes from yfinance; edgar powers only the Form 4 insider side.
- **Stage 2 yfinance rate-limiting** can still throttle the liquid universe down
  on a bad run (seen as low as ~116 of ~15,749). A pre-filtered universe (S&P500
  + liquid mid-caps) is a faster, more reliable alternative if needed.
- **Stage 3 fallback is lenient by design** (no FMP key) — ~92% of S&P 500 names
  pass; the broader universe filters harder. Set `FMP_API_KEY` for a stricter,
  data-richer screen.
