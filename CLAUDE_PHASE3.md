# AI-Trading-System — Phase 3 Build Skill
# Earnings Analyzer + Sentiment/Event Module

## Prerequisites
- Phase 1 complete ✅ (scanner, signals, broker, alerts)
- Phase 2 complete ✅ (three-bucket framework, exit rules)
- Phase 2b complete ✅ (real-time presidential monitor)
- make healthcheck shows 25/25 PASS
- 723+ tests passing
- Moomoo OpenD running on 127.0.0.1:11111
- moomoo-api installed and tested working

## Phase 3 Goal
Build two major modules:

Module A — Earnings Analyzer
  Identifies earnings plays using Moomoo IV data
  Strategy 1: IV Crush (sell premium before earnings)
  Strategy 2: IV Spike (buy premium before earnings)
  Outputs: specific trade recommendations with real margin

Module B — Sentiment/Event Drop Detector
  Classifies stock drops as:
    - Pure sentiment (tweet/personal news)
    - Hybrid (one-time charge, temporary restriction)
    - Fundamental (real permanent damage)
  Only flags first two as bounce candidates (Bucket 3)

## Build Order
```
Step 1:  Earnings data connector (Moomoo)
Step 2:  IV history analyzer
Step 3:  Earnings strategy builder (IV crush + spike)
Step 4:  Earnings LLM assessor
Step 5:  Earnings scanner (universe + filter)
Step 6:  make earnings command
Step 7:  Drop classifier (fundamental vs sentiment)
Step 8:  Bounce scorer
Step 9:  Instrument selector for bounce plays
Step 10: Event play alert + trade suggestion
Step 11: Dashboard pages (earnings + event plays)
Step 12: Tests + cleanup
```

---

## MODULE A — EARNINGS ANALYZER

---

## STEP 1 — Moomoo Earnings Data Connector

Create `broker_client/earnings/moomoo_earnings.py`

### What Moomoo provides (from Upcoming Earnings screen)
The Moomoo desktop app shows exactly this data per ticker:
```
IV              Current IV %
Last IV Crush   IV drop after last earnings %
Hist IV Crush   Avg IV crush across history %
IV Rank         Where IV sits vs 52w range (0-100)
IV Percentile   Percentile vs history (0-100)
Expected Move   ±% market expects on earnings day
Chg Last Earn   Actual move last quarter %
Chg Hist Est    Avg actual move vs expected %
```

### MoomooEarningsConnector class

**get_upcoming_earnings(days_ahead=14) → list[EarningsEvent]**
```python
class EarningsEvent(BaseModel):
    ticker: str
    earnings_date: date
    earnings_time: str  # "BMO" before market open, "AMC" after close
    iv_current: float   # current IV %
    iv_rank: int        # 0-100
    iv_percentile: int  # 0-100
    last_iv_crush: float    # IV drop after last earnings %
    hist_iv_crush: float    # avg IV crush across history %
    expected_move: float    # ±% expected move
    last_earnings_move: float   # actual move last quarter %
    hist_earnings_move: float   # avg actual vs expected %
    forecast_revenue_yoy: float | None
    forecast_eps_yoy: float | None
```

Implementation:
```python
import moomoo as ft
from config.settings import settings

class MoomooEarningsConnector:
    def __init__(self):
        self.ctx = ft.OpenQuoteContext(
            host=settings.MOOMOO_HOST,
            port=settings.MOOMOO_PORT
        )
    
    def get_upcoming_earnings(self, days_ahead: int = 14) -> list[EarningsEvent]:
        """
        Fetch upcoming earnings from Moomoo.
        Uses get_stock_basicinfo + earnings calendar endpoints.
        """
        # Step 1: Get earnings calendar for next N days
        start_date = date.today()
        end_date = start_date + timedelta(days=days_ahead)
        
        ret, data = self.ctx.get_market_snapshot(
            code_list=self._get_watchlist_tickers()
        )
        
        # Step 2: For each ticker with upcoming earnings,
        # fetch IV data from get_option_chain or get_ipo_list
        # Moomoo provides IV data via stock snapshot
        
        events = []
        for ticker in tickers_with_earnings:
            event = self._fetch_earnings_iv(ticker)
            if event:
                events.append(event)
        
        return events
    
    def get_earnings_iv_history(self, ticker: str) -> EarningsIVHistory:
        """
        Fetch per-quarter IV history chart data.
        This is the detailed view when you click a stock
        in Moomoo's Upcoming Earnings screen.
        
        Returns data matching Moomoo's Historical Earnings Data table:
        - IV Crush per quarter
        - Expected move per quarter
        - Before 14 days, Before 1 day
        - Open, High, Low, Close on earnings day
        - After 1 day, After 14 days
        """
        pass
```

### Fallback: Manual CSV Input

Since Moomoo API endpoints for earnings IV history
may require specific permissions, implement CSV fallback:

Create `broker_client/earnings/manual_input.py`

```python
# Users can export from Moomoo desktop app and drop CSV here:
# broker_client/earnings/data/upcoming_earnings.csv

# CSV headers match Moomoo exactly:
# Ticker, Earnings Date, IV, Last IV Crush, Historical IV Crush,
# IV Rank, IV Percentile, Expected Move, Chg on Last Earnings,
# Chg on Historical Est, Forecast Revenue YoY, Forecast EPS YoY

class ManualEarningsInput:
    CSV_PATH = "broker_client/earnings/data/upcoming_earnings.csv"
    
    def load(self) -> list[EarningsEvent]:
        """Load earnings data from manually exported Moomoo CSV."""
        if not Path(self.CSV_PATH).exists():
            return []
        
        df = pd.read_csv(self.CSV_PATH)
        return [self._row_to_event(row) for _, row in df.iterrows()]
```

### Data source priority
```
Priority 1: Moomoo OpenD API (if available)
Priority 2: Manual CSV (broker_client/earnings/data/)
Priority 3: FMP earnings calendar (basic dates only, no IV)
Priority 4: yfinance earnings dates (no IV data)
```

---

## STEP 2 — IV History Analyzer

Create `broker_client/earnings/iv_analyzer.py`

### IVHistoryAnalyzer class

**analyze(ticker, earnings_history) → IVAnalysis**

```python
class IVAnalysis(BaseModel):
    ticker: str
    
    # IV Crush analysis
    avg_iv_crush: float         # avg IV drop after earnings %
    last_iv_crush: float        # most recent IV crush %
    iv_crush_consistency: float # % of quarters with significant crush
    
    # Expected move analysis
    avg_expected_move: float    # avg ±% expected by market
    avg_actual_move: float      # avg actual move from close
    breach_rate: float          # % of quarters that breached expected move
    breach_rate_upper: float    # % breaching to upside
    breach_rate_lower: float    # % breaching to downside
    
    # Current setup
    iv_rank_current: int        # 0-100
    iv_percentile_current: int  # 0-100
    expected_move_current: float # ±% this quarter
    
    # Recommendation
    strategy: str  # "IV_CRUSH_SELL" | "IV_SPIKE_BUY" | "SKIP"
    confidence: int  # 0-100
    reasoning: str
```

### IV Crush analysis logic

```python
def _analyze_iv_crush(self, history: list[QuarterlyData]) -> dict:
    """
    Analyze IV crush pattern across quarters.
    
    IV Crush trade: sell options before earnings,
    profit from IV collapse after announcement.
    """
    crushes = [q.iv_crush for q in history if q.iv_crush is not None]
    
    avg_crush = np.mean(crushes)
    consistency = len([c for c in crushes if c > 10]) / len(crushes)
    # > 10% crush = meaningful
    
    return {
        "avg_iv_crush": avg_crush,
        "crush_consistency": consistency,
        "is_reliable_crusher": avg_crush > 15 and consistency > 0.7,
        # > 15% avg crush + crushes 70%+ of quarters = reliable
    }
```

### Breach rate analysis

```python
def _analyze_breach_rate(self, history: list[QuarterlyData]) -> dict:
    """
    What % of time does stock move MORE than expected?
    
    Uses from_close as primary reference (matches
    Moomoo's 'Actual Move from Close' data).
    """
    quarters = [q for q in history if q.actual_move_close is not None]
    
    breaches = [
        q for q in quarters
        if abs(q.actual_move_close) > q.expected_move
    ]
    
    breach_rate = len(breaches) / len(quarters)
    
    upper_breaches = [b for b in breaches if b.actual_move_close > 0]
    lower_breaches = [b for b in breaches if b.actual_move_close < 0]
    
    return {
        "breach_rate": breach_rate,
        "breach_rate_upper": len(upper_breaches) / len(quarters),
        "breach_rate_lower": len(lower_breaches) / len(quarters),
        "safe_put_level": self._calculate_safe_put_strike(quarters),
        # Strike that would NOT have been breached in 85%+ of quarters
    }

def _calculate_safe_put_strike(self, quarters, safety_pct=0.85) -> float:
    """
    Find the move level that contained 85% of actual moves.
    Use this as the safe put strike distance.
    
    Example:
      Actual moves: -8%, -5%, -12%, -3%, -7%, -15%, -4%
      85th percentile (downside): -12%
      Safe put = current_price * (1 - 0.12)
    """
    moves = [abs(q.actual_move_close) for q in quarters]
    return np.percentile(moves, safety_pct * 100)
```

---

## STEP 3 — Earnings Strategy Builder

Create `broker_client/earnings/strategy_builder.py`

### Strategy 1: IV Crush (sell premium)

**Criteria for IV Crush trade:**
```python
def should_sell_iv_crush(analysis: IVAnalysis) -> bool:
    return all([
        analysis.iv_rank_current >= 50,      # IV elevated
        analysis.iv_percentile_current >= 60, # Historically high IV
        analysis.avg_iv_crush >= 15,          # Reliable crusher
        analysis.iv_crush_consistency >= 0.6, # Consistent pattern
        analysis.breach_rate <= 0.25,         # Stock stays in range 75%+
    ])
```

**Trade construction:**
```python
class IVCrushTrade(BaseModel):
    ticker: str
    strategy: str = "IV_CRUSH"
    earnings_date: date
    earnings_time: str  # BMO or AMC
    
    # Put side (recoverable — Bucket 2A)
    put_strike: float
    put_expiry: date    # 1-2 days after earnings
    put_premium: float  # expected premium
    put_safety_pct: float  # how far OTM
    
    # Call spread side (defined risk — Bucket 2B)
    call_spread_long_strike: float
    call_spread_short_strike: float
    call_spread_expiry: date
    call_spread_max_loss: float
    call_spread_premium: float
    
    # Combined metrics
    total_premium: float
    margin_required: float  # from Schwab previewOrder
    roi_margin: float       # premium / margin
    roi_cash_secured: float # premium / (strike * 100)
    
    # Risk metrics
    expected_move: float
    put_breach_probability: float  # from breach_rate history
    break_even_pct: float  # how much stock can fall before loss
```

**Put strike selection:**
```python
def select_put_strike(
    current_price: float,
    expected_move: float,
    safe_move_level: float,
    safety_buffer: float = 1.1  # extra 10% buffer beyond safe level
) -> float:
    """
    Select put strike that combines:
    1. Historical safe level (85th percentile of actual moves)
    2. Expected move as reference
    3. Extra safety buffer
    
    Example:
      NVDA at $135
      Expected move: ±8%
      Historical safe level: 10% (stock rarely falls more than 10%)
      Safety buffer: 10%
      
      Put strike = 135 * (1 - 0.10 * 1.1) = 135 * 0.89 = $120.15
      → Use $120 put
    """
    safe_distance = max(expected_move, safe_move_level) * safety_buffer
    raw_strike = current_price * (1 - safe_distance / 100)
    
    # Round to nearest standard strike
    return round(raw_strike / 5) * 5  # round to nearest $5
```

### Strategy 2: IV Spike (buy premium before earnings)

**Criteria for IV Spike trade:**
```python
def should_buy_iv_spike(analysis: IVAnalysis) -> bool:
    return all([
        analysis.iv_rank_current <= 35,       # IV LOW right now
        analysis.iv_percentile_current <= 40,  # Historically low
        analysis.avg_actual_move >= 8,         # Stock moves big on earnings
        analysis.breach_rate >= 0.40,          # Breaches expected move often
    ])
```

**CRITICAL RULE — IV Spike trades:**
```
NEVER hold through earnings.
Enter 5-10 days before earnings when IV is low.
Exit 1 day BEFORE earnings announcement.

Profit from IV rising into earnings (IV expansion).
Do NOT try to profit from the actual move.

This is the ONLY defined-risk Bucket 2B trade
that gets a HOLD recommendation near earnings —
hold until 1 day before, then EXIT.
```

**Trade construction:**
```python
class IVSpikeTrade(BaseModel):
    ticker: str
    strategy: str = "IV_SPIKE"
    earnings_date: date
    
    entry_date: date      # 5-10 days before earnings
    exit_date: date       # 1 day BEFORE earnings (hard rule)
    
    # Straddle or strangle
    instrument: str       # "straddle" | "strangle"
    call_strike: float
    put_strike: float
    expiry: date          # earnings date or 1 DTE after
    
    total_premium_paid: float
    max_loss: float       # = total_premium_paid
    target_profit_pct: float  # typically 20-40% before earnings
    
    # IV metrics
    iv_rank_at_entry: int
    iv_expansion_expected: float  # how much IV should rise
```

---

## STEP 4 — Earnings LLM Assessor

Create `broker_client/earnings/llm_assessor.py`

### LLMEarningsAssessor class

**assess(ticker, iv_analysis, trade) → EarningsAssessment**

```python
SYSTEM_PROMPT = """
You are an expert options trader specializing in earnings plays.
You analyze IV patterns, historical breach rates, and fundamental 
context to assess the risk/reward of earnings trades.

Always respond in JSON format.
"""

USER_PROMPT = """
Analyze this earnings trade setup:

Ticker: {ticker}
Earnings: {earnings_date} ({earnings_time})
Strategy: {strategy}

IV Analysis:
- Current IV Rank: {iv_rank}/100
- Avg Historical IV Crush: {avg_iv_crush:.1f}%
- IV Crush Consistency: {consistency:.0%} of quarters
- Breach Rate: {breach_rate:.0%} (stock exceeds expected move)
- Expected Move this quarter: ±{expected_move:.1f}%
- Historical avg actual move: {avg_actual_move:.1f}%

Trade Setup:
{trade_details}

Fundamental context:
- Forecast Revenue YoY: {revenue_yoy}%
- Forecast EPS YoY: {eps_yoy}%
- Recent price action: {price_action}

Respond with JSON:
{{
  "recommendation": "EXECUTE" | "SKIP" | "REDUCE_SIZE",
  "confidence": 0-100,
  "probability_of_profit": 0-100,
  "key_risks": ["risk1", "risk2"],
  "key_strengths": ["strength1", "strength2"],
  "sizing_suggestion": "full" | "half" | "quarter",
  "reasoning": "2-3 sentence assessment",
  "exit_plan": "specific exit instructions",
  "watch_for": "what would change this assessment"
}}
"""

def assess(self, ticker, analysis, trade) -> EarningsAssessment:
    try:
        response = anthropic_client.messages.create(
            model=settings.LLM_MODEL,
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": USER_PROMPT.format(...)
            }],
            system=SYSTEM_PROMPT
        )
        return EarningsAssessment(**json.loads(response.content[0].text))
    except Exception as e:
        logger.error(f"LLM assessment failed: {e}")
        return self._rule_based_assessment(analysis, trade)
```

---

## STEP 5 — Earnings Universe Scanner

Create `broker_client/earnings/earnings_scanner.py`

### EarningsScanner class

**scan(days_ahead=14) → list[EarningsOpportunity]**

```python
class EarningsOpportunity(BaseModel):
    ticker: str
    earnings_date: date
    earnings_time: str
    strategy: str           # IV_CRUSH | IV_SPIKE | SKIP
    iv_analysis: IVAnalysis
    trade: IVCrushTrade | IVSpikeTrade | None
    llm_assessment: EarningsAssessment | None
    overall_score: int      # 0-100
    priority: str           # HIGH | MEDIUM | LOW | SKIP

def scan(self, days_ahead: int = 14) -> list[EarningsOpportunity]:
    """
    Full earnings scan pipeline:
    
    1. Get upcoming earnings (Moomoo or CSV)
    2. For each ticker with earnings:
       a. Check if on watchlist (quality filter)
       b. Fetch IV history
       c. Analyze IV pattern
       d. Determine strategy (crush/spike/skip)
       e. Build trade if applicable
       f. LLM assessment
       g. Score and rank
    3. Return sorted by score DESC
    """
```

### Filtering criteria

**Must pass ALL to be considered:**
```python
MINIMUM_CRITERIA = {
    "iv_rank_min_for_crush": 50,      # IV must be elevated to sell
    "iv_rank_max_for_spike": 35,      # IV must be low to buy
    "min_options_volume": 1000,        # Liquid options market
    "min_open_interest": 5000,         # Enough OI for good fills
    "max_days_to_earnings": 14,        # Within 2 weeks
    "min_days_to_earnings": 1,         # At least 1 day
    "on_watchlist_required": False,    # Can include non-watchlist stocks
    "min_stock_price": 20,             # Avoid penny stocks
}
```

---

## STEP 6 — make earnings Command

Add to `Makefile`:

```makefile
# Earnings analyzer
earnings:
	PYTHONPATH=. python -m broker_client.earnings.cli

# Earnings for specific ticker
earnings-ticker:
	PYTHONPATH=. python -m broker_client.earnings.cli --ticker $(ticker)

# Best earnings plays this week
earnings-week:
	PYTHONPATH=. python -m broker_client.earnings.cli --days 7
```

Create `broker_client/earnings/cli.py`:

```python
def main():
    scanner = EarningsScanner()
    opportunities = scanner.scan(days_ahead=14)
    
    print("\n" + "="*70)
    print(f"  EARNINGS OPPORTUNITIES — Next 14 Days")
    print(f"  {len(opportunities)} plays found")
    print("="*70)
    
    for opp in opportunities:
        if opp.strategy == "SKIP":
            continue
            
        print(f"\n{'─'*70}")
        print(f"  {opp.ticker} — {opp.earnings_date} {opp.earnings_time}")
        print(f"  Strategy: {opp.strategy} | Score: {opp.overall_score}/100")
        print(f"  IV Rank: {opp.iv_analysis.iv_rank_current}/100")
        print(f"  Expected Move: ±{opp.iv_analysis.expected_move_current:.1f}%")
        print(f"  Avg IV Crush: {opp.iv_analysis.avg_iv_crush:.1f}%")
        print(f"  Breach Rate: {opp.iv_analysis.breach_rate:.0%}")
        
        if opp.trade:
            print(f"\n  TRADE:")
            if opp.strategy == "IV_CRUSH":
                print(f"    Sell {opp.trade.put_strike}P @ ${opp.trade.put_premium:.2f}")
                print(f"    Margin: ${opp.trade.margin_required:,.0f}")
                print(f"    ROI (margin): {opp.trade.roi_margin:.1%}")
                print(f"    Break-even: {opp.trade.break_even_pct:.1f}% drop needed to lose")
            elif opp.strategy == "IV_SPIKE":
                print(f"    Buy straddle: {opp.trade.call_strike}C/{opp.trade.put_strike}P")
                print(f"    Cost: ${opp.trade.total_premium_paid:.2f}")
                print(f"    Exit: {opp.trade.exit_date} (1 day BEFORE earnings)")
        
        if opp.llm_assessment:
            print(f"\n  LLM: {opp.llm_assessment.recommendation} "
                  f"({opp.llm_assessment.confidence}% confidence)")
            print(f"  '{opp.llm_assessment.reasoning}'")
    
    print("\n" + "="*70)
```

---

## MODULE B — SENTIMENT/EVENT DROP DETECTOR

---

## STEP 7 — Drop Classifier

Create `signals/drop_classifier.py`

### What it detects

When a stock drops significantly (>4% in 1-2 days),
classify the cause:

```
PURE_SENTIMENT:
  Stock drops but no fundamental news
  Cause: tweet, political noise, CEO personal drama
  Bounce confidence: HIGH
  Example: TSLA drops on Musk tweet drama
  
HYBRID:
  Real event but market misreads magnitude
  Cause: one-time charge, temporary restriction
  Recovery: thesis-dependent
  Bounce confidence: MEDIUM-HIGH
  Example: NVDA drops on H20 chip export restriction
           (one-time charge, new chip ready)
  
FUNDAMENTAL:
  Real permanent damage to business
  Cause: revenue miss, competition threat, fraud
  Bounce confidence: LOW — don't trade against it
  Example: Stock drops on losing major customer forever
  
EARNINGS_MISS:
  Post-earnings drop
  Analyze: was it priced in? guidance cut?
  Bounce confidence: varies
```

### DropClassifier class

**classify(ticker, drop_pct, news_headlines) → DropClassification**

```python
class DropClassification(BaseModel):
    ticker: str
    drop_pct: float
    classification: str  # PURE_SENTIMENT | HYBRID | FUNDAMENTAL | EARNINGS_MISS
    confidence: int      # 0-100
    cause_summary: str   # brief description of what caused the drop
    recovery_expected: bool
    recovery_timeframe: str  # "days" | "weeks" | "months" | "unknown"
    bounce_confidence: int   # 0-100
    reasoning: str
    
    # Supporting evidence
    fundamental_signals: list[str]  # earnings revision, analyst cuts, etc.
    sentiment_signals: list[str]    # tweet/political/personal news
    institutional_action: str       # "buying" | "selling" | "neutral"
    sector_peers: str               # "also down" | "flat" | "up"
```

### Classification logic

**Step 1 — Fundamental damage check:**
```python
def _check_fundamental_signals(self, ticker, date_range) -> list[str]:
    """
    Check for real fundamental damage.
    If ANY of these present → lean toward FUNDAMENTAL.
    """
    signals = []
    
    # Analyst revisions
    if self._check_earnings_revision_down(ticker):
        signals.append("Analyst EPS estimates cut")
    
    # Guidance cut
    if self._check_guidance_cut(ticker):
        signals.append("Company guidance lowered")
    
    # Major customer loss
    if self._check_customer_loss(ticker):
        signals.append("Major customer cancellation reported")
    
    # Revenue miss (not just EPS)
    if self._check_revenue_miss(ticker):
        signals.append("Revenue miss vs estimates")
    
    # Competition threat
    if self._check_competition_news(ticker):
        signals.append("Direct competition announcement")
    
    return signals
```

**Step 2 — Sentiment signals:**
```python
def _check_sentiment_signals(self, headlines) -> list[str]:
    """
    Signs this is sentiment not fundamental.
    """
    signals = []
    
    sentiment_keywords = [
        "tweet", "posted", "said", "claims", "rumor",
        "political", "controversy", "personal", "divorce",
        "arrested", "lawsuit (personal)", "fired CEO",
        "government scrutiny", "investigation (no charges)"
    ]
    
    for kw in sentiment_keywords:
        if any(kw in h.lower() for h in headlines):
            signals.append(f"Sentiment trigger: {kw}")
    
    return signals
```

**Step 3 — One-time vs recurring check (hybrid):**
```python
def _check_one_time_vs_recurring(self, headlines) -> dict:
    """
    Key distinction for HYBRID classification.
    
    One-time → HYBRID (recoverable)
    Recurring → FUNDAMENTAL (avoid)
    """
    one_time_keywords = [
        "charge", "write-down", "write-off", "impairment",
        "one-time", "non-recurring", "settlement",
        "export restriction", "regulatory fine",
        "supply chain disruption"
    ]
    
    recurring_risk_keywords = [
        "recurring", "ongoing", "permanent ban",
        "lost contract", "competition gaining share",
        "margin compression", "pricing power lost"
    ]
    
    is_one_time = any(kw in ' '.join(headlines).lower() 
                      for kw in one_time_keywords)
    is_recurring = any(kw in ' '.join(headlines).lower() 
                       for kw in recurring_risk_keywords)
    
    return {
        "is_one_time": is_one_time,
        "is_recurring": is_recurring
    }
```

**Step 4 — Peer comparison:**
```python
def _check_sector_peers(self, ticker, drop_pct) -> str:
    """
    If sector peers also dropped → macro/sector issue
    If only this stock dropped → company-specific
    
    Company-specific sentiment drop = higher bounce confidence
    """
    sector = self._get_sector(ticker)
    sector_etf = SECTOR_ETF_MAP.get(sector, 'SPY')
    
    etf_move = self._get_price_change(sector_etf, days=2)
    
    if etf_move < -2:
        return "sector_wide"    # macro issue
    elif etf_move < -0.5:
        return "partial_sector" # some sympathy selling
    else:
        return "isolated"       # company-specific = cleaner bounce
```

**Step 5 — LLM classification:**
```python
CLASSIFICATION_PROMPT = """
A stock dropped {drop_pct:.1f}% in the last {days} days.

News headlines:
{headlines}

Fundamental signals detected: {fundamental_signals}
Sentiment signals detected: {sentiment_signals}
One-time charge present: {is_one_time}
Sector peers movement: {peer_action}
Analyst revisions: {analyst_action}

Classify this drop:

PURE_SENTIMENT: No fundamental damage, pure noise/fear
HYBRID: Real event but one-time, market overreacted  
FUNDAMENTAL: Real permanent damage to business
EARNINGS_MISS: Post-earnings drop

Respond in JSON:
{{
  "classification": "PURE_SENTIMENT|HYBRID|FUNDAMENTAL|EARNINGS_MISS",
  "confidence": 0-100,
  "cause_summary": "brief cause description",
  "recovery_expected": true/false,
  "recovery_timeframe": "days|weeks|months|unknown",
  "bounce_confidence": 0-100,
  "reasoning": "2-3 sentences",
  "do_not_trade_if": "conditions that would change this"
}}
"""
```

---

## STEP 8 — Bounce Scorer

Create `signals/bounce_scorer.py`

### BounceScorer class

**score(ticker, classification, market_context) → BounceScore**

```python
class BounceScore(BaseModel):
    ticker: str
    overall_score: int      # 0-100
    classification: str     # from DropClassifier
    
    # Score components (each 0-100)
    cause_score: int        # how clear/temporary is the cause?
    fundamental_score: int  # are fundamentals still intact?
    institutional_score: int # what is smart money doing?
    technical_score: int    # oversold indicators?
    timing_score: int       # good entry point?
    
    # Decision
    trade_recommendation: str  # STRONG_BUY | BUY | WAIT | SKIP
    suggested_bucket: int      # 3 (always Bucket 3 for bounces)
    suggested_instrument: str  # "calls" | "stock" | "call_spread"
    urgency: str               # IMMEDIATE | TODAY | THIS_WEEK | SKIP
```

### Scoring weights
```python
SCORE_WEIGHTS = {
    "cause_score": 0.30,        # Most important — what caused it?
    "fundamental_score": 0.25,  # Fundamentals still intact?
    "institutional_score": 0.20, # Smart money buying?
    "technical_score": 0.15,    # Oversold?
    "timing_score": 0.10,       # Good entry timing?
}
```

### Cause score logic
```python
def _score_cause(self, classification: str) -> int:
    return {
        "PURE_SENTIMENT": 90,   # Clear cause, high conviction
        "HYBRID": 70,           # Real but temporary
        "FUNDAMENTAL": 10,      # Don't trade against it
        "EARNINGS_MISS": 40,    # Depends on details
    }.get(classification, 0)
```

### Institutional score
```python
def _score_institutional(self, ticker) -> int:
    """
    Check dark pool / institutional buying during dip.
    If institutions are buying the dip → strong signal.
    
    Use unusual options activity as proxy:
    Large call buying during dip = institutional conviction
    """
    # Check options flow
    unusual_calls = self._check_unusual_call_buying(ticker)
    dark_pool = self._check_dark_pool_activity(ticker)
    
    if unusual_calls and dark_pool == "buying":
        return 90
    elif unusual_calls or dark_pool == "buying":
        return 70
    elif dark_pool == "selling":
        return 20
    else:
        return 50  # neutral
```

---

## STEP 9 — Instrument Selector

Create `signals/bounce_instrument_selector.py`

### InstrumentSelector class

**select(ticker, bounce_score, market_context) → BounceTradeSetup**

```python
class BounceTradeSetup(BaseModel):
    ticker: str
    bucket: int = 3     # always Bucket 3
    sub_type: str       # "event_call" | "leap" | "event_stock"
    
    # Instrument
    instrument: str     # "call" | "stock" | "call_spread"
    strike: float | None
    expiry: date | None
    delta: float | None  # target delta for calls
    
    # Sizing
    max_capital: float   # BUCKET3_MAX_SINGLE_TRADE_PCT * portfolio
    contracts: int | None
    shares: int | None
    
    # Exit
    profit_target_pct: float    # exit at this profit %
    stop_loss_pct: float | None  # for stocks only
    thesis_complete_signal: str  # what would trigger thesis-complete exit
    
    # Timing
    entry_urgency: str   # "IMMEDIATE" | "TODAY" | "THIS_WEEK"
    hold_period: str     # "days" | "weeks" | "months"
```

### Selection logic

```python
def select(self, ticker, bounce_score, drop_cause, market_context):
    """
    Instrument selection based on bounce confidence and timing.
    """
    
    # HIGH confidence + immediate bounce expected
    if bounce_score.overall_score >= 75 and \
       drop_cause == "PURE_SENTIMENT":
        return BounceTradeSetup(
            instrument="call",
            delta=0.6,          # slightly OTM — more leverage
            expiry=2_weeks_out, # enough time for sentiment to clear
            profit_target_pct=50,
            sub_type="event_call",
            entry_urgency="IMMEDIATE"
        )
    
    # HIGH confidence + macro crash (takes longer)
    elif bounce_score.overall_score >= 75 and \
         drop_cause == "HYBRID" and \
         market_context.regime == "crash":
        return BounceTradeSetup(
            instrument="call",
            delta=0.8,          # deep ITM — acts like stock
            expiry=18_months_out, # long time horizon
            profit_target_pct=40, # exit quickly on bounce
            sub_type="leap",
            entry_urgency="TODAY"
        )
    
    # MEDIUM confidence — defined risk
    elif bounce_score.overall_score >= 55:
        return BounceTradeSetup(
            instrument="call_spread",
            # Buy ATM call, sell OTM call
            # Limits cost, limits upside
            profit_target_pct=40,
            sub_type="event_call",
            entry_urgency="TODAY"
        )
    
    # LOW confidence — skip
    else:
        return None
```

---

## STEP 10 — Event Play Alert + Trade Suggestion

Add to `alerts/alert_engine.py`:

**send_earnings_alert(opportunity)**
```
🎯 EARNINGS OPPORTUNITY: {ticker}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Earnings: {date} {time}
Strategy: {strategy}
IV Rank: {iv_rank}/100
Expected Move: ±{expected_move}%
Avg IV Crush: {avg_iv_crush}%
Breach Rate: {breach_rate}%

SUGGESTED TRADE:
  {trade_description}
  Premium: ${premium}
  Margin: ${margin}
  ROI: {roi}%
  Break-even: {break_even}% drop needed

LLM: {recommendation} ({confidence}% confidence)
"{reasoning}"

Score: {score}/100 | Priority: {priority}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**send_bounce_alert(ticker, score, trade_setup)**
```
📉 BOUNCE PLAY DETECTED: {ticker}

Drop: {drop_pct}% in {days} days
Cause: {classification} — {cause_summary}
Fundamentals: {fundamental_status}
Institutional: {institutional_action}

Bounce confidence: {bounce_confidence}%
Overall score: {score}/100

SUGGESTED TRADE (Bucket 3):
  {instrument} — {trade_description}
  Max loss: ${max_loss}
  Profit target: {profit_target}%
  Exit when: {thesis_complete_signal}

⚡ Urgency: {entry_urgency}
```

---

## STEP 11 — Dashboard Pages

### New page: Earnings Analyzer
`dashboard/pages/8_earnings.py`

Sections:
```
Upcoming Earnings Opportunities
  - Table: ticker, date, strategy, IV rank, score, trade
  - Color coded: green=IV crush, blue=IV spike, gray=skip
  - Click row → full analysis popup

Active Earnings Positions
  - Current P&L on open earnings plays
  - DTE countdown
  - Exit recommendation

Historical Earnings Performance
  - Win rate by strategy type
  - Avg IV crush captured
  - Breach rate vs predictions
```

### New page: Event Plays
`dashboard/pages/9_event_plays.py`

Sections:
```
Current Drop Alerts
  - Recent drops >4% in last 48 hours
  - Classification: sentiment/hybrid/fundamental
  - Bounce score
  - Suggested trade if applicable

Active Event Positions
  - Open Bucket 3 positions from event plays
  - Thesis status and completion %
  - LLM exit recommendation

Historical Event Play Performance
  - Classification accuracy
  - Win rate by classification type
```

---

## STEP 12 — Tests + Cleanup

### Required tests

```
tests/test_iv_analyzer.py:
  - test_high_iv_crush_detected
  - test_low_iv_spike_detected
  - test_breach_rate_calculation
  - test_safe_put_strike_selection
  - test_skip_when_criteria_not_met

tests/test_earnings_strategy.py:
  - test_iv_crush_trade_construction
  - test_iv_spike_exit_date_before_earnings
  - test_put_strike_safety_buffer
  - test_roi_calculation_matches_schwab

tests/test_drop_classifier.py:
  - test_pure_sentiment_tsla_tweet
  - test_hybrid_nvda_export_restriction
  - test_fundamental_revenue_miss
  - test_one_time_charge_detection
  - test_peer_comparison_isolated_vs_sector
  - test_llm_fallback_on_error

tests/test_bounce_scorer.py:
  - test_high_score_pure_sentiment
  - test_medium_score_hybrid
  - test_low_score_fundamental
  - test_institutional_buying_boosts_score
  - test_sector_wide_drop_reduces_score

tests/test_instrument_selector.py:
  - test_immediate_sentiment_gets_calls
  - test_macro_crash_gets_leap
  - test_medium_confidence_gets_spread
  - test_low_confidence_returns_none
```

### Success criteria
```
✅ All 723+ existing tests still pass
✅ New tests pass (target: 800+ total)
✅ ruff clean on all new files
✅ mypy clean on all new files
✅ make healthcheck shows 25/25 PASS
✅ make earnings runs without error
✅ Earnings opportunities sorted by score
✅ IV crush trades have real Schwab margin (previewOrder)
✅ IV spike trades always have exit BEFORE earnings
✅ Drop classifier correctly classifies NVDA H20 → HYBRID
✅ Bounce scorer gives TSLA tweet drop HIGH score
✅ Dashboard shows new earnings + event play pages
✅ Alerts fire to Discord #opportunities channel
```

---

## Settings to Add

```python
# Earnings analyzer
EARNINGS_DAYS_AHEAD: int = 14
EARNINGS_IV_RANK_SELL_THRESHOLD: int = 50
EARNINGS_IV_RANK_BUY_THRESHOLD: int = 35
EARNINGS_MIN_IV_CRUSH_HISTORY: int = 6  # min quarters of data
EARNINGS_MAX_BREACH_RATE: float = 0.25  # max 25% breach for crush trade
EARNINGS_MIN_BREACH_RATE: float = 0.40  # min 40% breach for spike trade
EARNINGS_SAFETY_BUFFER: float = 1.1     # 10% extra OTM buffer

# Drop detector
DROP_ALERT_THRESHOLD_PCT: float = 4.0   # alert on drops > 4%
DROP_LOOKBACK_DAYS: int = 2             # check last 2 days
DROP_FUNDAMENTAL_MIN_SIGNALS: int = 2   # 2+ fundamental signals = FUNDAMENTAL
DROP_BOUNCE_MIN_SCORE: int = 55         # min score to suggest trade
```

---

## Commit Convention
```
feat: moomoo earnings data connector
feat: IV history analyzer (crush + breach rate)
feat: earnings strategy builder (IV crush + spike)
feat: earnings LLM assessor
feat: earnings universe scanner
feat: make earnings command
feat: drop classifier (sentiment vs fundamental)
feat: bounce scorer
feat: bounce instrument selector
feat: event play alerts + trade suggestions
feat: earnings + event play dashboard pages
test: earnings analyzer, drop classifier, bounce scorer
```

---

## Notes for Claude Code

1. Moomoo OpenD must be running on 127.0.0.1:11111
   Test connection first: nc -zv 127.0.0.1 11111

2. Moomoo pacing: 30 req/30s max → pace at 1.1s/call
   Same as scanner Stage 3

3. IV Spike trades: exit_date MUST be before earnings_date
   Add validator to IVSpikeTrade model:
   @validator('exit_date')
   def exit_before_earnings(cls, v, values):
       assert v < values['earnings_date'], "Must exit before earnings"
       return v

4. Bucket 2A (earnings put) = recoverable = can roll
   Bucket 2B (defined risk) = NOT recoverable = accept loss
   NEVER confuse these — it's the core of the framework

5. Use real Schwab previewOrder for margin on crush trades
   Same as make analyze command already working

6. The drop classifier is for EXISTING positions context too:
   "Is this drop in my open position sentiment or fundamental?"
   Wire into position_watcher for existing positions

7. Never hold IV spike trades through earnings
   Add hard check in exit_rules.py:
   IF strategy == IV_SPIKE AND dte == 1:
     → FORCE EXIT regardless of profit

8. manual_input.csv goes in broker_client/earnings/data/
   Add that folder to .gitignore data/ subfolder
   (user data, not code)

9. All new files go in:
   broker_client/earnings/ (earnings analyzer)
   signals/ (drop classifier, bounce scorer, instrument selector)

10. Build iteratively — commit after each step passes tests
    Don't try to build all 12 steps in one go