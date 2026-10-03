# AI-Trading-System — Phase 4 Build Skill
# Portfolio Risk + Heat Map + Performance Attribution

## Prerequisites
- Phase 1 complete ✅ (scanner, signals, broker, alerts)
- Phase 2 complete ✅ (three-bucket framework, exit rules)
- Phase 2b complete ✅ (presidential monitor)
- Phase 3 complete ✅ (earnings analyzer, drop detector)
- make healthcheck shows 25/25 PASS
- 876+ tests passing

## Phase 4 Goal
Build portfolio-level risk intelligence so the system
knows not just individual position risk but TOTAL
exposure across all positions combined.

Currently: system tracks positions individually
After Phase 4: system understands portfolio as a whole

## Build Order
```
Step 1:  Portfolio heat map (sector + delta exposure)
Step 2:  Concentration alerts (sector, expiry, ticker)
Step 3:  Pre-trade risk check
Step 4:  Earnings overlap warning for open positions
Step 5:  Dividend risk alerts
Step 6:  Macro regime → strategy adjustment
Step 7:  IV rank monitoring for open short positions
Step 8:  Performance attribution
Step 9:  Signal accuracy tracker
Step 10: Daily briefing enhancement
Step 11: Dashboard pages (risk + performance)
Step 12: Tests + cleanup
```

---

## STEP 1 — Portfolio Heat Map

Create `broker_client/risk/portfolio_heat_map.py`

### PortfolioHeatMap class

**generate() → HeatMapReport**

```python
class HeatMapReport(BaseModel):
    generated_at: datetime
    total_positions: int
    total_exposure: float       # total capital at risk
    
    # Sector breakdown
    sector_exposure: dict[str, SectorExposure]
    sector_concentration_pct: dict[str, float]
    max_sector_pct: float
    max_sector_name: str
    sector_warning: bool        # True if any sector > 40%
    
    # Delta exposure
    net_delta: float            # portfolio net delta vs SPY
    delta_direction: str        # "bullish" | "bearish" | "neutral"
    beta_weighted_delta: float  # delta weighted by beta
    
    # Vega exposure (IV sensitivity)
    net_vega: float             # how much we gain/lose per 1% IV move
    vega_direction: str         # "long_vol" | "short_vol" | "neutral"
    
    # Expiry concentration
    expiry_buckets: dict[str, int]  # "this_week": 3, "next_week": 2, etc.
    same_expiry_warning: bool       # True if 5+ positions same week
    
    # Correlation risk
    correlated_clusters: list[CorrelatedCluster]
    # Positions that will move together
    
    # Cash buffer
    cash_buffer_pct: float
    cash_buffer_warning: bool   # True if < 20%

class SectorExposure(BaseModel):
    sector: str
    tickers: list[str]
    exposure_pct: float
    positions: int
    warning: bool  # True if > 40%

class CorrelatedCluster(BaseModel):
    cluster_name: str  # "Tech mega-cap", "Semiconductors", etc.
    tickers: list[str]
    combined_exposure_pct: float
    risk_note: str
    # "If Nasdaq drops 10%, all these drop together"
```

### Sector mapping
```python
SECTOR_MAP = {
    # Technology
    'AAPL': 'Technology', 'MSFT': 'Technology', 'GOOGL': 'Technology',
    'META': 'Technology', 'NVDA': 'Technology', 'AMD': 'Technology',
    'INTC': 'Technology', 'CRWD': 'Technology', 'DDOG': 'Technology',
    'FTNT': 'Technology', 'CSCO': 'Technology', 'IBM': 'Technology',
    
    # Semiconductors (sub-sector of tech — extra correlation)
    'NVDA': 'Semiconductors', 'AMD': 'Semiconductors',
    'INTC': 'Semiconductors', 'KLAC': 'Semiconductors',
    'AMAT': 'Semiconductors', 'LRCX': 'Semiconductors',
    'MCHP': 'Semiconductors', 'NXPI': 'Semiconductors',
    'TXN': 'Semiconductors', 'ARM': 'Semiconductors',
    
    # Healthcare
    'JNJ': 'Healthcare', 'LLY': 'Healthcare', 'AMGN': 'Healthcare',
    'BIIB': 'Healthcare', 'DVA': 'Healthcare',
    
    # Energy
    'VLO': 'Energy', 'MPC': 'Energy', 'EOG': 'Energy',
    'FCX': 'Materials',
    
    # Financials
    'IVZ': 'Financials', 'BEN': 'Financials',
    
    # Consumer
    'ROST': 'Consumer', 'TJX': 'Consumer', 'MAR': 'Consumer',
    'HLT': 'Consumer', 'MNST': 'Consumer',
    
    # Industrials
    'CAT': 'Industrials', 'GWW': 'Industrials', 'HWM': 'Industrials',
    'ROK': 'Industrials', 'IEX': 'Industrials', 'FDX': 'Industrials',
    
    # Real Estate
    'EQIX': 'Real Estate', 'IRM': 'Real Estate', 'PLD': 'Real Estate',
    'SPG': 'Real Estate', 'FRT': 'Real Estate',
    
    # Utilities
    'EVRG': 'Utilities', 'AES': 'Utilities', 'PCG': 'Utilities',
    
    # Communications
    'TKO': 'Communications',
}

# Correlated clusters (move together in market stress)
CORRELATION_CLUSTERS = {
    "Tech mega-cap": ["AAPL", "MSFT", "GOOGL", "META", "NVDA"],
    "Semiconductors": ["NVDA", "AMD", "INTC", "KLAC", "AMAT", "LRCX"],
    "Cloud software": ["DDOG", "FTNT", "CRWD"],
    "Energy": ["VLO", "MPC", "EOG"],
    "REITs": ["EQIX", "IRM", "PLD", "SPG"],
}
```

### Delta calculation
```python
def _calculate_net_delta(self, positions: list[Position]) -> float:
    """
    Net delta = sum of all position deltas
    Positive = bullish (profits if market rises)
    Negative = bearish (profits if market falls)
    
    For options:
      Long call: +delta (e.g. +0.5)
      Short put: +delta (e.g. +0.4) ← we're mostly here
      Long put:  -delta (e.g. -0.5)
      Short call: -delta (e.g. -0.4)
    
    For stock:
      Long 100 shares: +1.0 delta
    """
    total_delta = 0.0
    for pos in positions:
        if pos.asset_type == "option":
            # Schwab provides delta in Greeks
            delta = pos.delta or 0.0
            if pos.position_type == "short":
                delta = -delta  # short reverses sign
            total_delta += delta * pos.quantity * 100
        elif pos.asset_type == "stock":
            shares = pos.quantity
            if pos.position_type == "short":
                shares = -shares
            total_delta += shares
    return total_delta
```

---

## STEP 2 — Concentration Alerts

Create `broker_client/risk/concentration_checker.py`

### ConcentrationChecker class

**check(positions, portfolio_value) → list[ConcentrationAlert]**

```python
class ConcentrationAlert(BaseModel):
    alert_type: str     # SECTOR | EXPIRY | TICKER | DELTA | CASH
    severity: str       # WARNING | CRITICAL
    message: str
    current_value: float
    threshold: float
    affected: list[str]  # tickers or expiry dates
    suggestion: str
```

### Concentration rules

**Sector concentration:**
```python
SECTOR_WARNING_PCT = 40     # warn if any sector > 40%
SECTOR_CRITICAL_PCT = 60    # critical if any sector > 60%

# Example alert:
ConcentrationAlert(
    alert_type="SECTOR",
    severity="WARNING",
    message="Technology concentration at 52% (threshold: 40%)",
    current_value=52.0,
    threshold=40.0,
    affected=["NVDA", "AMD", "CRWD", "DDOG"],
    suggestion="Consider hedging tech exposure or avoid new tech positions"
)
```

**Same-expiry concentration:**
```python
SAME_EXPIRY_WARNING = 5     # warn if 5+ positions expiring same week

# Group positions by expiry week
# If 5+ positions expire same week → warning
ConcentrationAlert(
    alert_type="EXPIRY",
    severity="WARNING",
    message="6 positions expire week of Jun 20 (threshold: 5)",
    current_value=6,
    threshold=5,
    affected=["NVDA", "AAPL", "AMD", "CRWD", "DDOG", "ACN"],
    suggestion="Stagger expiry dates to reduce gamma risk concentration"
)
```

**Single ticker concentration:**
```python
SINGLE_TICKER_WARNING_PCT = 10  # warn if single ticker > 10% of portfolio

ConcentrationAlert(
    alert_type="TICKER",
    severity="CRITICAL",
    message="NVDA represents 15% of portfolio (threshold: 10%)",
    current_value=15.0,
    threshold=10.0,
    affected=["NVDA"],
    suggestion="Reduce NVDA position or avoid adding more exposure"
)
```

**Cash buffer:**
```python
CASH_BUFFER_WARNING_PCT = 20   # warn if cash < 20%
CASH_BUFFER_CRITICAL_PCT = 10  # critical if cash < 10%

ConcentrationAlert(
    alert_type="CASH",
    severity="WARNING",
    message="Cash buffer at 15% (minimum: 20%)",
    current_value=15.0,
    threshold=20.0,
    affected=[],
    suggestion="Reduce position sizes or close some positions before adding new ones"
)
```

---

## STEP 3 — Pre-Trade Risk Check

Create `broker_client/risk/pre_trade_checker.py`

### PreTradeChecker class

**check(proposed_trade, positions, portfolio_value) → PreTradeResult**

```python
class PreTradeResult(BaseModel):
    approved: bool
    hard_blocks: list[str]      # must fix before trading
    warnings: list[str]         # should review but can proceed
    concentration_after: dict   # what concentration looks like after trade
    cash_buffer_after: float    # cash remaining after trade
    bucket_capacity_after: dict # bucket allocation after trade
    recommendation: str         # PROCEED | REVIEW | BLOCK
```

### Check sequence

```python
def check(self, trade, positions, portfolio_value) -> PreTradeResult:
    blocks = []
    warnings = []
    
    # 1. Cash buffer check
    margin_required = trade.margin_estimated
    cash_after = self._get_cash() - margin_required
    cash_pct_after = cash_after / portfolio_value * 100
    
    if cash_pct_after < settings.CASH_BUFFER_CRITICAL_PCT:
        blocks.append(
            f"Insufficient cash: {cash_pct_after:.1f}% remaining "
            f"(minimum {settings.CASH_BUFFER_CRITICAL_PCT}%)"
        )
    elif cash_pct_after < settings.CASH_BUFFER_WARNING_PCT:
        warnings.append(
            f"Cash buffer low after trade: {cash_pct_after:.1f}%"
        )
    
    # 2. Bucket capacity check
    bucket = self._get_bucket(trade)
    bucket_exposure = self._get_bucket_exposure(bucket)
    bucket_max = portfolio_value * (
        settings.BUCKET1_ALLOCATION_PCT / 100 if bucket == 1
        else settings.BUCKET2_ALLOCATION_PCT / 100 if bucket == 2
        else settings.BUCKET3_ALLOCATION_PCT / 100
    )
    
    if bucket_exposure + margin_required > bucket_max:
        blocks.append(
            f"Bucket {bucket} capacity exceeded: "
            f"${bucket_exposure:,.0f} + ${margin_required:,.0f} "
            f"> ${bucket_max:,.0f} limit"
        )
    
    # 3. Sector concentration check
    ticker_sector = SECTOR_MAP.get(trade.ticker, 'Unknown')
    sector_exposure_after = self._get_sector_exposure_after(
        trade.ticker, margin_required
    )
    
    if sector_exposure_after > settings.SECTOR_CRITICAL_PCT:
        blocks.append(
            f"Sector concentration critical after trade: "
            f"{ticker_sector} would be {sector_exposure_after:.1f}%"
        )
    elif sector_exposure_after > settings.SECTOR_WARNING_PCT:
        warnings.append(
            f"Sector concentration warning: "
            f"{ticker_sector} would be {sector_exposure_after:.1f}%"
        )
    
    # 4. Same-expiry check
    same_expiry_count = self._count_same_expiry(trade.expiry)
    if same_expiry_count >= settings.SAME_EXPIRY_WARNING:
        warnings.append(
            f"Expiry concentration: {same_expiry_count + 1} positions "
            f"would expire week of {trade.expiry}"
        )
    
    # 5. Earnings check
    days_to_earnings = self._get_days_to_earnings(trade.ticker)
    if days_to_earnings and days_to_earnings < 14:
        if trade.bucket != 2:  # not an earnings play
            warnings.append(
                f"{trade.ticker} reports earnings in {days_to_earnings} days "
                f"— consider this before opening non-earnings position"
            )
    
    # 6. Assignment readiness (Bucket 1 only)
    if trade.bucket == 1:
        if not self._is_on_watchlist(trade.ticker):
            warnings.append(
                f"{trade.ticker} is not on watchlist — "
                f"are you comfortable owning it at assignment?"
            )
    
    # 7. Bucket 3 size check
    if trade.bucket == 3:
        max_b3_trade = portfolio_value * settings.BUCKET3_MAX_SINGLE_TRADE_PCT / 100
        if trade.premium_paid > max_b3_trade:
            blocks.append(
                f"Bucket 3 single trade limit exceeded: "
                f"${trade.premium_paid:,.0f} > ${max_b3_trade:,.0f} "
                f"({settings.BUCKET3_MAX_SINGLE_TRADE_PCT}% limit)"
            )
    
    return PreTradeResult(
        approved=len(blocks) == 0,
        hard_blocks=blocks,
        warnings=warnings,
        recommendation="BLOCK" if blocks else "REVIEW" if warnings else "PROCEED"
    )
```

---

## STEP 4 — Earnings Overlap Warning

Add to `broker_client/position_watcher.py`:

**check_earnings_overlaps() — runs daily at 8 AM**

```python
def check_earnings_overlaps(self):
    """
    Cross-reference open positions with upcoming earnings calendar.
    Alert if any open position has earnings within its expiry window.
    """
    positions = self.get_open_positions()
    
    for pos in positions:
        if not pos.expiry:
            continue
            
        earnings_date = self._get_next_earnings(pos.ticker)
        if not earnings_date:
            continue
        
        # Check if earnings falls within position's expiry window
        if pos.entry_date <= earnings_date <= pos.expiry:
            days_to_earnings = (earnings_date - date.today()).days
            
            severity = "CRITICAL" if days_to_earnings <= 3 else "WARNING"
            
            self.alert_engine.send_alert(
                title=f"⚠️ EARNINGS OVERLAP: {pos.ticker}",
                body=f"""
Open position expires {pos.expiry}
{pos.ticker} reports earnings {earnings_date} 
({days_to_earnings} days away)

Earnings fall INSIDE your expiry window.

Position: {pos.ticker} {pos.strike}{pos.option_type}
Bucket: {pos.bucket} ({pos.sub_type})

{'🚨 URGENT: Earnings in 3 days or less' if days_to_earnings <= 3 else ''}
Suggested action: 
  - Roll expiry beyond earnings date
  - OR close position before earnings
  - OR this is intentional earnings play (Bucket 2)
                """,
                channel='alerts',
                color='red' if severity == "CRITICAL" else 'yellow'
            )
```

---

## STEP 5 — Dividend Risk Alerts

Create `broker_client/risk/dividend_checker.py`

**check_dividend_risk(positions) → list[DividendAlert]**

```python
class DividendAlert(BaseModel):
    ticker: str
    ex_div_date: date
    dividend_amount: float
    dividend_pct: float         # dividend as % of stock price
    affected_put_strike: float  # your put strike
    put_itm_risk: bool          # True if dividend > put OTM buffer
    premium_collected: float
    risk_summary: str

def check_dividend_risk(self, positions):
    alerts = []
    
    for pos in positions:
        if pos.option_type != 'P':  # only puts
            continue
        
        ex_div = self._get_ex_dividend_date(pos.ticker)
        if not ex_div:
            continue
        
        # Check if ex-div date falls before expiry
        if ex_div > pos.expiry:
            continue
        
        dividend = self._get_dividend_amount(pos.ticker)
        current_price = self._get_price(pos.ticker)
        dividend_pct = dividend / current_price * 100
        
        # Stock drops by dividend amount on ex-div date
        # Does this put us closer to ITM?
        otm_buffer = (current_price - pos.strike) / current_price * 100
        
        if dividend_pct > otm_buffer * 0.5:  # dividend > 50% of buffer
            alerts.append(DividendAlert(
                ticker=pos.ticker,
                ex_div_date=ex_div,
                dividend_amount=dividend,
                dividend_pct=dividend_pct,
                affected_put_strike=pos.strike,
                put_itm_risk=dividend_pct > otm_buffer,
                premium_collected=pos.premium_collected,
                risk_summary=f"{pos.ticker} ex-div {ex_div}: "
                             f"${dividend:.2f} ({dividend_pct:.2f}%) "
                             f"vs {otm_buffer:.2f}% OTM buffer"
            ))
    
    return alerts
```

---

## STEP 6 — Macro Regime → Strategy Adjustment

Create `broker_client/risk/regime_advisor.py`

### RegimeAdvisor class

**advise(market_state, positions) → RegimeAdvice**

```python
class RegimeAdvice(BaseModel):
    regime: str         # "bull" | "bear" | "volatile" | "choppy"
    confidence: int     # 0-100
    
    # Strategy adjustments
    bucket1_adjustment: str  # "aggressive" | "normal" | "conservative"
    bucket2_adjustment: str
    bucket3_adjustment: str
    
    # Specific guidance
    guidance: list[str]
    warnings: list[str]
    
    # Strike width recommendation
    put_otm_buffer_multiplier: float
    # 1.0 = normal, 1.2 = wider (more conservative), 0.9 = tighter

def advise(self, market_state, positions):
    regime = market_state.current_regime
    composite = market_state.composite_score
    
    if composite >= 65:
        # Bullish regime
        return RegimeAdvice(
            regime="bull",
            bucket1_adjustment="aggressive",  # sell puts more aggressively
            bucket2_adjustment="normal",
            bucket3_adjustment="aggressive",  # bounces recover faster
            put_otm_buffer_multiplier=0.95,   # can go slightly closer
            guidance=[
                "Bullish regime: put selling favorable",
                "Bounces recover quickly — Bucket 3 plays higher conviction",
                "Consider selling puts slightly closer to ATM for more premium",
            ]
        )
    
    elif composite <= 35:
        # Bearish/volatile regime
        return RegimeAdvice(
            regime="bear",
            bucket1_adjustment="conservative",  # sell puts further OTM
            bucket2_adjustment="conservative",  # wider strikes
            bucket3_adjustment="reduced",        # smaller size
            put_otm_buffer_multiplier=1.3,       # wider buffer
            guidance=[
                "Bearish regime: increase put strike distance",
                "Reduce Bucket 3 position sizes",
                "Bounces may be dead cat bounces — require higher conviction",
                "Consider iron condors over naked puts",
            ],
            warnings=[
                "Market in bearish regime — all positions should be reviewed",
            ]
        )
    
    else:
        # Neutral/choppy
        return RegimeAdvice(
            regime="choppy",
            bucket1_adjustment="normal",
            bucket2_adjustment="normal",    # earnings plays most reliable
            bucket3_adjustment="reduced",
            put_otm_buffer_multiplier=1.1,
            guidance=[
                "Choppy market: earnings plays most reliable strategy",
                "Avoid directional Bucket 3 plays",
                "Iron condors work well in range-bound market",
            ]
        )
```

---

## STEP 7 — IV Rank Monitoring for Open Shorts

Add to `broker_client/position_watcher.py`:

**check_iv_rank_changes() — runs every 30 minutes during session**

```python
def check_iv_rank_changes(self):
    """
    Monitor IV rank changes on open short positions.
    If IV rank drops significantly → your edge is gone → alert.
    If IV rank spikes → position gaining value against you → alert.
    """
    short_positions = [
        p for p in self.get_open_positions()
        if p.position_type == "short"
    ]
    
    for pos in short_positions:
        current_iv_rank = self._get_current_iv_rank(pos.ticker)
        if current_iv_rank is None:
            continue
        
        entry_iv_rank = pos.iv_rank_at_entry or 50
        iv_rank_change = current_iv_rank - entry_iv_rank
        
        # IV dropped significantly → edge is gone
        if current_iv_rank < 30 and entry_iv_rank >= 50:
            self.alert_engine.send_alert(
                title=f"📉 IV RANK DROP: {pos.ticker}",
                body=f"""
Short position: {pos.ticker} {pos.strike}{pos.option_type}
IV Rank at entry: {entry_iv_rank}
IV Rank now: {current_iv_rank}

Your premium-selling edge has diminished.
Consider closing early to lock in remaining profit.
Current P&L: {pos.unrealized_pnl_pct:+.1f}%
                """,
                channel='opportunities',
                color='yellow'
            )
        
        # IV spiked → position gaining value against you
        elif current_iv_rank > 80 and iv_rank_change > 20:
            self.alert_engine.send_alert(
                title=f"⚠️ IV SPIKE: {pos.ticker}",
                body=f"""
Short position: {pos.ticker} {pos.strike}{pos.option_type}
IV Rank at entry: {entry_iv_rank}
IV Rank now: {current_iv_rank} (↑{iv_rank_change} points)

IV spike is working against your short position.
Review position and consider rolling or closing.
Current P&L: {pos.unrealized_pnl_pct:+.1f}%
                """,
                channel='alerts',
                color='red'
            )
```

---

## STEP 8 — Performance Attribution

Create `broker_client/analytics/performance_attribution.py`

### PerformanceAttribution class

**generate_report(period='MTD') → AttributionReport**

```python
class AttributionReport(BaseModel):
    period: str
    total_pnl: float
    total_return_pct: float
    
    # By bucket
    bucket_attribution: dict[int, BucketAttribution]
    best_bucket: int
    worst_bucket: int
    
    # By strategy
    strategy_attribution: dict[str, StrategyAttribution]
    best_strategy: str
    worst_strategy: str
    
    # By signal source
    signal_attribution: dict[str, SignalAttribution]
    # Which signals led to winning trades?
    
    # Win/loss stats
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_winner_pct: float
    avg_loser_pct: float
    profit_factor: float    # gross_profit / gross_loss
    
    # Best/worst trades
    best_trade: TradeRecord
    worst_trade: TradeRecord

class BucketAttribution(BaseModel):
    bucket: int
    realized_pnl: float
    unrealized_pnl: float
    total_trades: int
    win_rate: float
    avg_hold_days: float
    premium_collected: float  # for Bucket 1/2
    avg_roi_on_margin: float

class StrategyAttribution(BaseModel):
    strategy: str           # msp | wheel | earnings_crush | earnings_spike | leap | event
    realized_pnl: float
    trade_count: int
    win_rate: float
    avg_return_pct: float
    avg_hold_days: float
```

### Key metrics
```python
def _calculate_profit_factor(self, trades) -> float:
    """
    Profit factor = gross profit / gross loss
    > 1.5 = good system
    > 2.0 = excellent system
    < 1.0 = losing system
    """
    gross_profit = sum(t.pnl for t in trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in trades if t.pnl < 0))
    return gross_profit / gross_loss if gross_loss > 0 else float('inf')

def _vs_spy_benchmark(self, period) -> float:
    """
    Compare system return vs SPY buy-and-hold.
    Positive = beating the market.
    """
    spy_return = self._get_spy_return(period)
    system_return = self._get_system_return(period)
    return system_return - spy_return
```

---

## STEP 9 — Signal Accuracy Tracker

Create `broker_client/analytics/signal_accuracy.py`

### SignalAccuracyTracker class

**track_signal(signal, trade_result)**
**generate_report() → SignalAccuracyReport**

```python
class SignalAccuracyReport(BaseModel):
    period: str
    
    # Per signal source accuracy
    technical_accuracy: float
    sentiment_accuracy: float
    yield_accuracy: float
    macro_accuracy: float
    premarket_accuracy: float
    presidential_accuracy: float
    geopolitical_accuracy: float
    
    # Combined signal accuracy
    high_confidence_accuracy: float  # when confidence > 65
    critical_accuracy: float         # when confidence > 80
    
    # Direction accuracy
    long_accuracy: float
    short_accuracy: float
    neutral_accuracy: float
    
    # Recommendations
    best_signal_source: str
    worst_signal_source: str
    optimal_confidence_threshold: int
    # The confidence level that maximizes accuracy
```

### Tracking logic
```python
def track_signal(self, signal: MarketState, trade: TradeRecord):
    """
    After a trade closes, check if the signal was correct.
    
    Signal was CORRECT if:
    - Signal=long AND trade was profitable
    - Signal=short AND trade would have been profitable short
    - Signal=neutral AND market moved < 0.5%
    
    Signal was WRONG if:
    - Signal=long AND trade lost money on directional basis
    """
    was_correct = self._evaluate_signal_accuracy(signal, trade)
    
    # Log to signal_accuracy table
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO signal_accuracy 
            (signal_date, direction, confidence, composite_score,
             sources, trade_id, was_correct, actual_move_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal.timestamp, signal.current_regime,
            signal.confidence, signal.composite_score,
            json.dumps(signal.sources),
            trade.id, was_correct, trade.actual_move_pct
        ))
```

---

## STEP 10 — Daily Briefing Enhancement

Update `alerts/daily_briefing.py` to include Phase 4 data:

```python
def generate_briefing(self) -> str:
    # Existing data
    market_state = signal_fusion.get_latest()
    positions = position_watcher.get_open_positions()
    
    # NEW: Phase 4 additions
    heat_map = portfolio_heat_map.generate()
    regime_advice = regime_advisor.advise(market_state, positions)
    attribution = performance_attribution.generate_report('MTD')
    concentration_alerts = concentration_checker.check(positions)
    
    return f"""
🌅 GOOD MORNING — AI Trading System Daily Brief
{datetime.now().strftime('%A %B %d, %Y')}
{'━' * 50}

📊 MACRO REGIME: {market_state.current_regime.upper()} ({market_state.composite_score}/100)
   Strategy mode: {regime_advice.bucket1_adjustment.upper()}
   Today's events: {format_events()}

🛡️ PORTFOLIO RISK:
   Net delta: {heat_map.net_delta:+.2f} ({heat_map.delta_direction})
   Cash buffer: {heat_map.cash_buffer_pct:.1f}% {'⚠️' if heat_map.cash_buffer_warning else '✅'}
   Max sector: {heat_map.max_sector_name} {heat_map.max_sector_pct:.1f}% {'⚠️' if heat_map.sector_warning else '✅'}
   
{format_concentration_alerts(concentration_alerts)}

💼 OPEN POSITIONS ({len(positions)} total):
{format_position_alerts(positions)}

📈 PERFORMANCE (MTD):
   Total return: {attribution.total_return_pct:+.2f}%
   vs SPY: {attribution.vs_spy:+.2f}%
   Win rate: {attribution.win_rate:.0%}
   Profit factor: {attribution.profit_factor:.2f}
   
   Bucket 1: {attribution.bucket_attribution[1].realized_pnl:+,.0f}
   Bucket 2: {attribution.bucket_attribution[2].realized_pnl:+,.0f}
   Bucket 3: {attribution.bucket_attribution[3].realized_pnl:+,.0f}

🎯 TODAY'S OPPORTUNITIES:
{format_opportunities()}
{'━' * 50}
"""
```

---

## STEP 11 — Dashboard Pages

### New page: Risk Monitor
`dashboard/pages/10_risk.py`

Sections:
```
Portfolio Heat Map
  - Sector pie chart (color coded by concentration level)
  - Delta gauge (bullish/neutral/bearish)
  - Cash buffer gauge
  - Expiry timeline (positions by expiry week)

Concentration Alerts
  - Active warnings listed
  - Sector breakdown table
  - Correlated cluster warnings

Regime Advisor
  - Current regime + confidence
  - Strategy adjustments per bucket
  - Specific guidance bullets
```

### New page: Performance
`dashboard/pages/11_performance.py`

Sections:
```
P&L Summary
  - Total return vs SPY benchmark chart
  - MTD / QTD / YTD breakdown
  - Daily P&L bar chart

Attribution
  - By bucket (bar chart)
  - By strategy (bar chart)
  - By signal source (table)

Trade History
  - All closed trades table
  - Win/loss breakdown
  - Best and worst trades
```

### Update Overview page
Add risk summary widget:
```
RISK STATUS
  Delta: {net_delta:+.2f} (neutral)
  Max sector: Tech 42% ⚠️
  Cash: 23% ✅
  Expiry concentration: 3 positions Jun 20 ✅
```

---

## STEP 12 — Tests + Cleanup

### Required tests

```
tests/test_portfolio_heat_map.py:
  - test_sector_concentration_calculation
  - test_sector_warning_above_40pct
  - test_net_delta_calculation_short_puts
  - test_net_delta_calculation_long_calls
  - test_same_expiry_concentration
  - test_correlated_cluster_detection
  - test_cash_buffer_calculation

tests/test_concentration_checker.py:
  - test_sector_warning_fires
  - test_sector_critical_fires
  - test_same_expiry_warning
  - test_single_ticker_concentration
  - test_cash_buffer_warning
  - test_no_alerts_on_healthy_portfolio

tests/test_pre_trade_checker.py:
  - test_blocks_when_cash_insufficient
  - test_blocks_when_bucket_capacity_exceeded
  - test_blocks_when_sector_critical
  - test_warns_when_earnings_overlap
  - test_warns_when_not_on_watchlist
  - test_approves_healthy_trade
  - test_bucket3_size_limit

tests/test_earnings_overlap.py:
  - test_alerts_when_earnings_in_window
  - test_no_alert_when_earnings_after_expiry
  - test_critical_alert_when_earnings_3_days

tests/test_dividend_checker.py:
  - test_alerts_when_dividend_threatens_put
  - test_no_alert_when_dividend_small
  - test_no_alert_when_ex_div_after_expiry

tests/test_regime_advisor.py:
  - test_bullish_regime_aggressive_guidance
  - test_bearish_regime_conservative_guidance
  - test_choppy_regime_earnings_preferred

tests/test_performance_attribution.py:
  - test_bucket_pnl_calculation
  - test_win_rate_calculation
  - test_profit_factor_calculation
  - test_vs_spy_benchmark
  - test_best_worst_strategy_detection

tests/test_signal_accuracy.py:
  - test_long_signal_correct_when_profitable
  - test_neutral_signal_correct_when_flat
  - test_accuracy_by_source
  - test_optimal_threshold_calculation
```

### Success criteria
```
✅ All 876+ existing tests still pass
✅ New tests pass (target: 950+ total)
✅ ruff clean on all new files
✅ mypy clean on all new files
✅ make healthcheck shows 25/25 PASS
✅ Dashboard shows risk + performance pages
✅ Daily briefing includes risk data
✅ Pre-trade check fires on concentration breach
✅ Earnings overlap alert fires correctly
✅ Dividend risk alert fires correctly
✅ Regime advisor changes recommendations with market state
✅ Performance attribution shows bucket breakdown
```

---

## Settings to Add

```python
# Portfolio risk thresholds
SECTOR_WARNING_PCT: float = 40.0
SECTOR_CRITICAL_PCT: float = 60.0
SAME_EXPIRY_WARNING: int = 5
SINGLE_TICKER_WARNING_PCT: float = 10.0
CASH_BUFFER_WARNING_PCT: float = 20.0
CASH_BUFFER_CRITICAL_PCT: float = 10.0

# Dividend risk
DIVIDEND_RISK_THRESHOLD_PCT: float = 0.5
# Alert if dividend > 50% of put OTM buffer

# IV monitoring
IV_RANK_EDGE_GONE_THRESHOLD: int = 30
# Alert when IV rank drops below this on open shorts
IV_RANK_SPIKE_THRESHOLD: int = 80
# Alert when IV rank rises above this on open shorts

# Performance tracking
BENCHMARK_TICKER: str = "SPY"
PERFORMANCE_LOOKBACK_DAYS: int = 30
MIN_TRADES_FOR_ATTRIBUTION: int = 5
# Need at least 5 trades before showing attribution

# Signal accuracy
MIN_SIGNALS_FOR_ACCURACY: int = 10
# Need at least 10 signals before showing accuracy
```

---

## Commit Convention
```
feat: portfolio heat map (sector + delta + expiry)
feat: concentration alerts (sector/ticker/expiry/cash)
feat: pre-trade risk check
feat: earnings overlap warning for open positions
feat: dividend risk alerts
feat: macro regime advisor
feat: IV rank monitoring for open shorts
feat: performance attribution (bucket/strategy/signal)
feat: signal accuracy tracker
feat: enhanced daily briefing with risk data
feat: risk monitor + performance dashboard pages
test: portfolio risk, pre-trade, attribution, accuracy
```

---

## Notes for Claude Code

1. Portfolio heat map runs on PAPER positions first
   Real Schwab positions added when live trading starts
   
2. Delta calculation uses Schwab Greeks when available
   Fall back to Black-Scholes approximation if Greeks missing

3. Sector map in SECTOR_MAP covers watchlist stocks
   Unknown tickers → "Other" sector

4. Performance attribution only meaningful after 5+ closed trades
   Show "Insufficient data" message until then

5. Signal accuracy needs signal logged WITH trade entry
   Add signal_id foreign key to trades table

6. Pre-trade check is ADVISORY not blocking in paper mode
   In live mode → hard blocks actually prevent execution

7. Dividend data from yfinance:
   yf.Ticker(ticker).dividends → ex-div dates + amounts
   yf.Ticker(ticker).calendar → ex-dividend date

8. Regime advisor output feeds into:
   - Daily briefing (strategy guidance)
   - Pre-trade checker (put_otm_buffer_multiplier)
   - NOT into signal weights (those are fixed)

9. All new modules go in:
   broker_client/risk/ (risk management)
   broker_client/analytics/ (performance/accuracy)

10. Build iteratively — commit after each step
    Don't build all 12 steps in one session
    Steps 1-3 are most critical — do these first