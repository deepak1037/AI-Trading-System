# CHANGELOG — deviations & additions beyond CLAUDE.md

This file tracks everything built or changed that is **not** in the original
`CLAUDE.md` / `CLAUDE_SETTINGS.md` spec, or that corrects a spec assumption that
turned out to be wrong in practice. CLAUDE.md says "Never deviate from these
decisions without updating this file first" — this is that record.

Keep newest first. Reference the git commit where relevant.

---

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
