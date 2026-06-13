# AI-Trading-System — Phase 2 Build Skill

## Prerequisites
- Phase 1 complete ✅ (scanner, broker, signals, alerts all working)
- make healthcheck shows 24/24 PASS
- 546+ tests passing
- Discord alerts working
- Schwab token valid

## Phase 2 Goal
Build the Three-Bucket Trading Framework with exit rules,
21 DTE alerts, LLM exit decisions, wheel tracker, and daily briefing.

## Build Order
Follow this exact order — each step depends on the previous.

```
Step 1: Database schema updates
Step 2: Bucket classification system
Step 3: Capital allocation + settings
Step 4: Exit rules engine
Step 5: 21 DTE alert system
Step 6: LLM exit decision engine
Step 7: Scale-out (ladder) execution
Step 8: Wheel strategy tracker
Step 9: Daily briefing (8 AM)
Step 10: Dashboard updates
Step 11: Tests + cleanup
```

---

## STEP 1 — Database Schema Updates

Add these columns to the positions table and create new tables.

### positions table (add columns)
```sql
ALTER TABLE positions ADD COLUMN bucket INTEGER DEFAULT 1;
-- 1=MSP/Wheel, 2=Earnings, 3=Event/LEAP

ALTER TABLE positions ADD COLUMN sub_type TEXT DEFAULT 'msp';
-- msp | wheel_put | wheel_call | earnings_put | 
-- earnings_spread | leap | event_call | event_put

ALTER TABLE positions ADD COLUMN recoverable BOOLEAN DEFAULT TRUE;
-- True: can roll/recover. False: defined loss, accept it.

ALTER TABLE positions ADD COLUMN entry_thesis TEXT;
-- Why we entered: "tariff_crash", "earnings_iv_crush", 
-- "iran_geopolitical_bounce", etc.

ALTER TABLE positions ADD COLUMN thesis_status TEXT DEFAULT 'active';
-- active | partial | complete | broken

ALTER TABLE positions ADD COLUMN thesis_completion_pct REAL DEFAULT 0.0;
-- 0-100%: how much of the expected move has occurred

ALTER TABLE positions ADD COLUMN exit_target_pct REAL;
-- % profit target for this position

ALTER TABLE positions ADD COLUMN stop_loss_pct REAL;
-- % loss at which to exit (for defined-risk positions)

ALTER TABLE positions ADD COLUMN last_exit_review TEXT;
-- ISO timestamp of last LLM exit review

ALTER TABLE positions ADD COLUMN exit_recommendation TEXT;
-- FULL_EXIT | ROLL_UP | LADDER | HOLD — from LLM
```

### New table: bucket_performance
```sql
CREATE TABLE IF NOT EXISTS bucket_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    bucket INTEGER NOT NULL,
    sub_type TEXT,
    realized_pnl REAL DEFAULT 0.0,
    unrealized_pnl REAL DEFAULT 0.0,
    premium_collected REAL DEFAULT 0.0,
    positions_opened INTEGER DEFAULT 0,
    positions_closed INTEGER DEFAULT 0,
    win_count INTEGER DEFAULT 0,
    loss_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
```

### New table: wheel_cycles
```sql
CREATE TABLE IF NOT EXISTS wheel_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    phase TEXT NOT NULL,
    -- sell_put | assigned | sell_call | called_away | restart
    phase_entry_date TEXT,
    phase_exit_date TEXT,
    strike REAL,
    shares INTEGER DEFAULT 100,
    premium_collected REAL DEFAULT 0.0,
    cost_basis REAL,
    total_cycle_return REAL DEFAULT 0.0,
    cycle_number INTEGER DEFAULT 1,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
```

### New table: exit_reviews
```sql
CREATE TABLE IF NOT EXISTS exit_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER,
    ticker TEXT NOT NULL,
    review_date TEXT NOT NULL,
    profit_pct REAL,
    dte_remaining INTEGER,
    thesis_status TEXT,
    macro_regime TEXT,
    recommendation TEXT,
    -- FULL_EXIT | ROLL_UP | LADDER | HOLD
    confidence INTEGER,
    reasoning TEXT,
    suggested_action TEXT,
    -- JSON: {sell: ..., buy: ..., net_credit: ...}
    executed BOOLEAN DEFAULT FALSE,
    created_at TEXT DEFAULT (datetime('now'))
);
```

---

## STEP 2 — Bucket Classification System

Create `broker_client/buckets/bucket_manager.py`

### BucketClassification pydantic model
```python
class BucketClassification(BaseModel):
    bucket: int  # 1, 2, or 3
    sub_type: str
    recoverable: bool
    description: str
    exit_rules: str  # human-readable exit rule summary
```

### BucketManager class — key methods

**classify_position(position) → BucketClassification**
```
Logic:
  If strategy in [cash_secured_put, margin_secured_put, covered_call]:
    bucket=1, sub_type=msp or wheel_call, recoverable=True
    
  If strategy == earnings_put:
    bucket=2, sub_type=earnings_put, recoverable=True
    
  If strategy in [call_spread, put_spread, iron_condor, 
                  straddle, strangle]:
    bucket=2, sub_type=earnings_spread, recoverable=False
    
  If strategy in [long_call, long_put] AND dte > 90:
    bucket=3, sub_type=leap, recoverable=False
    
  If strategy in [long_call, long_put] AND dte <= 90:
    bucket=3, sub_type=event_call or event_put, recoverable=False
```

**get_bucket_positions(bucket_num) → list[Position]**
- Query positions table filtered by bucket column

**check_bucket_capacity(bucket, new_position_value) → bool**
```
Logic:
  total_portfolio = paper_account.equity
  bucket_max = total_portfolio * (BUCKET{N}_ALLOCATION_PCT / 100)
  current_bucket_exposure = sum(open positions in bucket)
  return current_bucket_exposure + new_position_value <= bucket_max
```

**get_bucket_pnl(bucket_num) → BucketPnL**
```python
class BucketPnL(BaseModel):
    bucket: int
    realized_pnl: float
    unrealized_pnl: float
    premium_collected: float
    win_rate: float
    open_positions: int
    mtd_return: float
```

**get_bucket_summary() → dict**
- Returns all three buckets' P&L for dashboard

---

## STEP 3 — Capital Allocation Settings

Add to `config/settings.py`:

```python
# Trading profile
TRADING_PROFILE: str = "moderate"
# Options: conservative | moderate | aggressive | custom

# Bucket allocations (must sum <= 100, remainder = cash buffer)
BUCKET1_ALLOCATION_PCT: int = 70   # Margin Secured Put + Wheel
BUCKET2_ALLOCATION_PCT: int = 20   # Earnings plays
BUCKET3_ALLOCATION_PCT: int = 10   # Event + LEAP plays
# Cash buffer = 100 - 70 - 20 - 10 = 0% (adjust as needed)
# Recommended: keep at least 10% cash for rolls/assignments

# Per-trade limits
BUCKET3_MAX_SINGLE_TRADE_PCT: float = 2.0
# Never more than 2% of portfolio on one Bucket 3 lotto play

BUCKET2_DEFINED_RISK_MAX_LOSS_MULTIPLE: float = 2.0
# Close Bucket 2B positions at 2x premium collected (stop loss)

# Profile presets (used when TRADING_PROFILE is set)
# conservative: B1=90, B2=5, B3=0, cash=5
# moderate:     B1=70, B2=20, B3=10, cash=0
# aggressive:   B1=60, B2=20, B3=15, cash=5

# Validator: sum must not exceed 100
@validator('BUCKET3_ALLOCATION_PCT')
def allocations_valid(cls, v, values):
    total = values.get('BUCKET1_ALLOCATION_PCT', 0) + \
            values.get('BUCKET2_ALLOCATION_PCT', 0) + v
    if total > 100:
        raise ValueError(f"Bucket allocations sum to {total} > 100")
    return v
```

---

## STEP 4 — Exit Rules Engine

Create `broker_client/buckets/exit_rules.py`

### ExitRecommendation pydantic model
```python
class ExitRecommendation(BaseModel):
    action: str  # FULL_EXIT | ROLL | HOLD | LADDER | STOP_LOSS
    urgency: str  # IMMEDIATE | TODAY | THIS_WEEK | MONITOR
    reason: str
    profit_pct: float
    dte_remaining: int
    rule_triggered: str  # which rule fired
```

### ExitRulesEngine class

**check_exit(position) → ExitRecommendation | None**

#### Bucket 1 — MSP/Wheel exit rules
```
SHORT DTE (DTE <= 14):
  IF profit_pct >= 50:
    → FULL_EXIT, IMMEDIATE
    reason: "50% profit target hit on short DTE"
    
  IF profit_pct >= 70 AND dte <= 3:
    → FULL_EXIT, IMMEDIATE  
    reason: "70-80% profit target with 2-3 days left"
    
  IF dte <= 1:
    → FULL_EXIT or ROLL, IMMEDIATE
    reason: "Never hold to expiry — assignment risk"
    
  IF dte <= 7 AND position is ITM:
    → ROLL, IMMEDIATE
    reason: "ITM with 7 days left — roll now"

MEDIUM DTE (DTE 15-44):
  IF profit_pct >= 50:
    → FULL_EXIT, TODAY
    reason: "50% profit — redeploy capital"
    
  IF dte <= 21:
    → FULL_EXIT, TODAY
    reason: "21 DTE rule — gamma risk increasing"

LONG DTE (DTE >= 45):
  IF profit_pct >= 25 AND days_held <= 7:
    → FULL_EXIT, TODAY
    reason: "25% in week 1 — excellent capital efficiency"
    
  IF dte <= 21:
    → FULL_EXIT, TODAY
    reason: "21 DTE rule — always exit"
    
UNIVERSAL BUCKET 1 RULES:
  NEVER recommend STOP_LOSS for Bucket 1
  Always prefer ROLL over STOP_LOSS
  If deeply ITM and roll credit < $0.20 → HOLD or accept assignment
```

#### Bucket 2A — Earnings Put (recoverable)
```
Same rules as Bucket 1
PLUS:
  IF earnings_date within 2 days AND position held:
    → EXIT immediately (never hold through earnings)
    reason: "Earnings approaching — IV crush trade complete"
    
  Day after earnings:
    → FULL_EXIT at market open
    reason: "Post-earnings — collect remaining IV crush profit"
```

#### Bucket 2B — Defined Risk (spreads, condors)
```
IF profit_pct >= 50:
  → FULL_EXIT, TODAY
  reason: "50% profit on defined risk — don't get greedy"
  
IF loss_pct >= 200:  # lost 2x premium collected
  → STOP_LOSS, IMMEDIATE
  reason: "Stop loss triggered — accept defined loss"
  
IF dte <= 21:
  → FULL_EXIT, TODAY
  reason: "21 DTE — gamma risk on spread"

NEVER recommend ROLL for Bucket 2B
NEVER recommend HOLD when stop loss triggered
```

#### Bucket 3 — LEAP / Event
```
IF profit_pct >= 40:
  → Trigger LLM exit review (see Step 6)
  reason: "40% profit — evaluate FULL_EXIT vs ROLL_UP vs LADDER"
  
IF thesis_status == 'complete':
  → FULL_EXIT, IMMEDIATE
  reason: "Thesis complete — mission accomplished"
  
IF dte_used_pct >= 50:  # used half the time bought
  → Trigger LLM exit review
  reason: "50% of DTE used — reassess"
  
IF loss_pct >= 100:  # full premium loss
  → STOP_LOSS (position expired worthless)
  reason: "Lotto play expired — binary outcome"

NEVER recommend ROLL for Bucket 3 (binary outcome)
```

---

## STEP 5 — 21 DTE Alert System

Add to `broker_client/position_watcher.py`:

**check_dte_alerts() — runs every position check cycle**
```python
def check_dte_alerts(self):
    positions = self.get_open_positions()
    for pos in positions:
        dte = pos.days_to_expiry
        profit_pct = pos.unrealized_pnl_pct
        
        # 21 DTE alert
        if dte <= 21 and not pos.alerted_21dte:
            exit_rec = exit_rules.check_exit(pos)
            self.alert_engine.send_alert(
                title=f"⏰ 21 DTE ALERT: {pos.ticker}",
                body=f"""
Position: {pos.ticker} {pos.strike}{pos.option_type} {pos.expiry}
Bucket: {pos.bucket} — {pos.sub_type}
Current P&L: {profit_pct:+.1f}%
DTE remaining: {dte}
Recommendation: {exit_rec.action}
Reason: {exit_rec.reason}
                """,
                channel='alerts',
                color='yellow'
            )
            pos.alerted_21dte = True
            
        # 7 DTE urgent alert  
        if dte <= 7 and not pos.alerted_7dte:
            self.alert_engine.send_alert(
                title=f"🚨 URGENT 7 DTE: {pos.ticker}",
                body=f"Only {dte} days left — action required today",
                channel='alerts',
                color='red'
            )
            pos.alerted_7dte = True
            
        # Profit target alerts
        exit_rec = exit_rules.check_exit(pos)
        if exit_rec and exit_rec.urgency == 'IMMEDIATE':
            self.alert_engine.send_alert(
                title=f"💰 EXIT SIGNAL: {pos.ticker}",
                body=f"{exit_rec.reason}\nP&L: {profit_pct:+.1f}%",
                channel='opportunities',
                color='green'
            )
```

---

## STEP 6 — LLM Exit Decision Engine

Create `broker_client/buckets/llm_exit_engine.py`

**LLMExitEngine class**

**evaluate_exit(position, market_context) → ExitReview**

### Input context to LLM
```python
context = {
    # Position details
    "ticker": position.ticker,
    "strategy": position.sub_type,
    "bucket": position.bucket,
    "entry_price": position.entry_price,
    "current_price": position.current_price,
    "profit_pct": position.unrealized_pnl_pct,
    "dte_remaining": position.days_to_expiry,
    "dte_original": position.original_dte,
    "dte_used_pct": position.dte_used_pct,
    "delta": position.delta,
    
    # Thesis
    "entry_thesis": position.entry_thesis,
    "thesis_status": position.thesis_status,
    "thesis_completion_pct": position.thesis_completion_pct,
    
    # Market context
    "macro_regime": market_state.current_regime,
    "macro_composite": market_state.composite_score,
    "sector_momentum": get_sector_momentum(position.ticker),
    "iv_rank_current": position.iv_rank,
    "iv_at_entry": position.iv_at_entry,
    "institutional_flow": get_dark_pool_signal(position.ticker),
    
    # Portfolio context
    "same_ticker_exposure": get_ticker_exposure(position.ticker),
    "sector_concentration": get_sector_concentration(),
    "cash_buffer_pct": get_cash_buffer_pct(),
    "better_opportunities": get_top_opportunities(limit=3),
    
    # Technical
    "rsi": get_rsi(position.ticker),
    "above_200ma": is_above_200ma(position.ticker),
    "volume_trend": get_volume_trend(position.ticker),
}
```

### LLM prompt
```python
SYSTEM_PROMPT = """
You are an expert options trader analyzing a position exit decision.
You follow a strict three-bucket framework:
- Bucket 1 (MSP/Wheel): Always prefer rolling over cutting losses
- Bucket 2B (Defined Risk): Accept losses at stop loss, never roll
- Bucket 3 (LEAP/Event): Binary outcome, exit at profit or expiry

Respond ONLY in JSON format with these exact fields:
{
  "recommendation": "FULL_EXIT | ROLL_UP | LADDER | HOLD",
  "confidence": 0-100,
  "reasoning": "2-3 sentence explanation",
  "suggested_action": {
    "description": "What to do exactly",
    "sell": "current position description",
    "buy": "new position if rolling (null if exiting)",
    "net_credit": estimated_credit_if_rolling
  },
  "exit_trigger": "What would change this recommendation",
  "tax_note": "Short term vs long term gain consideration if relevant"
}
"""

USER_PROMPT = f"""
Analyze this position and recommend an exit action:

{json.dumps(context, indent=2)}

Consider:
1. Is the thesis still intact or complete?
2. Is rolling beneficial given DTE and credit available?
3. Are there better opportunities for this capital?
4. What does the macro regime suggest?
5. Tax implications (held {days_held} days)?
"""
```

### Discord alert format
```
💡 EXIT REVIEW: {ticker} {sub_type}

Current P&L: {profit_pct:+.1f}%
DTE remaining: {dte}
Thesis: {thesis_status} ({thesis_completion_pct:.0f}% complete)

🤖 LLM RECOMMENDATION: {recommendation} (confidence {confidence}%)
"{reasoning}"

Options:
  A) FULL EXIT    → Bank {profit_pct:+.1f}% profit
  B) ROLL UP      → {suggested_action.description}
  C) SCALE OUT    → Close 50%, let 50% ride
  D) HOLD         → {exit_trigger}

[Review on dashboard: localhost:8501/positions]
```

---

## STEP 7 — Scale Out (Ladder) Execution

Add to `broker_client/order_router.py`:

**execute_ladder(position, tranches) → list[Order]**
```python
def execute_ladder(self, position, tranches=[0.3, 0.4, 0.3]):
    """
    Close position in tranches.
    Default: 30% now, 40% at next target, 30% ride with stop.
    
    tranches: list of percentages, must sum to 1.0
    """
    total_contracts = position.quantity
    orders = []
    
    for i, pct in enumerate(tranches):
        contracts = max(1, int(total_contracts * pct))
        if i == 0:
            # Close immediately
            order = self.close_position(position, quantity=contracts)
            orders.append(order)
        elif i == 1:
            # Close at next profit target (e.g. 70% profit)
            # Set a limit order or alert for manual execution
            orders.append(self._create_deferred_exit(
                position, contracts, 
                trigger_profit_pct=70
            ))
        else:
            # Let ride with breakeven stop
            orders.append(self._create_trailing_stop(
                position, contracts,
                stop_at_pct=0  # breakeven
            ))
    
    return orders
```

---

## STEP 8 — Wheel Strategy Tracker

Create `broker_client/buckets/wheel_tracker.py`

### WheelCycle pydantic model
```python
class WheelCycle(BaseModel):
    ticker: str
    cycle_number: int
    current_phase: str
    # sell_put → assigned → sell_call → called_away → restart
    
    # Put phase
    put_strike: float | None = None
    put_premium: float | None = None
    put_expiry: str | None = None
    put_filled_date: str | None = None
    
    # Assignment
    assigned: bool = False
    assignment_date: str | None = None
    cost_basis: float | None = None  # strike - put_premium
    shares: int = 100
    
    # Call phase
    call_strike: float | None = None
    call_premium: float | None = None
    call_expiry: str | None = None
    call_filled_date: str | None = None
    
    # Cycle summary
    total_premium_collected: float = 0.0
    cycle_start_date: str | None = None
    cycle_end_date: str | None = None
    cycle_return_pct: float | None = None
    annualized_return_pct: float | None = None
```

### WheelTracker class — key methods

**record_put_sale(ticker, strike, premium, expiry)**
- Creates new WheelCycle in wheel_cycles table
- phase = 'sell_put'

**record_assignment(ticker, shares, cost_basis)**
- Updates WheelCycle phase to 'assigned'
- Records cost_basis (strike - all premiums so far)
- Sends Discord alert: "HOOD assigned at $10 — now own 1000 shares"

**record_call_sale(ticker, strike, premium, expiry)**
- Updates WheelCycle phase to 'sell_call'
- Records call premium

**record_called_away(ticker, proceeds)**
- Updates WheelCycle phase to 'called_away'
- Calculates total cycle return
- Calculates annualized return
- Suggests: "Restart wheel? New put opportunity: {strike} at {premium}"

**get_wheel_summary(ticker) → WheelSummary**
```python
class WheelSummary(BaseModel):
    ticker: str
    total_cycles: int
    current_phase: str
    total_premium_collected: float
    avg_cycle_return_pct: float
    avg_annualized_return_pct: float
    best_cycle: WheelCycle
    worst_cycle: WheelCycle
```

**get_all_wheels() → list[WheelSummary]**
- Returns summary for all tickers with wheel history

---

## STEP 9 — Daily Briefing (8 AM ET)

Add to `watcher/scheduler.py`:

**daily_briefing_job — fires 8:05 AM ET every trading day**

Create `alerts/daily_briefing.py`

### DailyBriefing class

**generate_briefing() → str**

```python
def generate_briefing(self) -> str:
    # Collect all data
    market_state = signal_fusion.get_latest()
    positions = position_watcher.get_open_positions()
    watchlist = watchlist_manager.get_active()
    paper_perf = paper_account.get_performance()
    bucket_pnl = bucket_manager.get_bucket_summary()
    
    # Today's events
    events = get_todays_events()  # CPI, Fed, earnings
    
    # Position alerts
    dte_alerts = [p for p in positions if p.days_to_expiry <= 21]
    earnings_overlaps = get_earnings_in_expiry_window(positions)
    roll_alerts = [p for p in positions if needs_roll(p)]
    
    # Opportunities
    earnings_plays = get_upcoming_earnings_plays(days=7)
    event_plays = get_current_event_plays()
    
    # Format briefing
    return f"""
🌅 GOOD MORNING — AI Trading System Daily Brief
{datetime.now().strftime('%A %B %d, %Y')}
{'━' * 50}

📊 MACRO REGIME: {market_state.current_regime.upper()} 
   Signal score: {market_state.composite_score}/100
   Today's events: {', '.join(events) or 'None scheduled'}

💼 OPEN POSITIONS ({len(positions)} total):
{format_position_alerts(dte_alerts, earnings_overlaps, roll_alerts)}

🎯 TODAY'S OPPORTUNITIES:
{format_opportunities(earnings_plays, event_plays)}

🛡️ PORTFOLIO HEALTH:
   Cash buffer: {get_cash_buffer_pct():.1f}%
   Tech concentration: {get_sector_concentration('Technology'):.1f}%
   Net delta: {get_portfolio_delta():.2f}

📈 BUCKET PERFORMANCE (MTD):
   Bucket 1 (MSP):      {bucket_pnl[1].mtd_return:+.2f}%  
                         ${bucket_pnl[1].premium_collected:,.0f} premium
   Bucket 2 (Earnings): {bucket_pnl[2].mtd_return:+.2f}%
   Bucket 3 (Events):   {bucket_pnl[3].mtd_return:+.2f}%
   
   Paper account total: {paper_perf.total_return:+.2f}%
{'━' * 50}
AI Trading System | {datetime.now().strftime('%H:%M ET')}
"""
```

**Send to Discord #daily-briefing channel (blue embed)**

---

## STEP 10 — Dashboard Updates

### Positions page updates (dashboard/pages/2_positions.py)

Add per-position:
- Bucket badge: 🟢 B1-MSP | 🟡 B2-Earnings | 🔴 B3-LEAP
- Recoverable flag: ♻️ Recoverable | ⚠️ Defined Risk
- Exit recommendation from exit_rules engine
- DTE progress bar (visual countdown)
- Thesis status and completion %

### New Overview page section
```
BUCKET SUMMARY
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Bucket 1 — Income Engine
  Positions: 0  |  Capital: $0  |  MTD: +$0

Bucket 2 — Earnings Plays
  Positions: 0  |  Capital: $0  |  MTD: +$0

Bucket 3 — Event/LEAP
  Positions: 0  |  Capital: $0  |  MTD: +$0
━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### New Dashboard page: Wheel Tracker
- `dashboard/pages/7_wheel_tracker.py`
- Shows active wheel cycles per ticker
- Shows cumulative premium collected
- Shows current phase with next action suggested

---

## STEP 11 — Tests + Cleanup

### Required tests (add to tests/)

```
tests/test_bucket_manager.py:
  - test_classify_msp_position → bucket=1
  - test_classify_earnings_put → bucket=2a, recoverable=True
  - test_classify_iron_condor → bucket=2b, recoverable=False
  - test_classify_leap → bucket=3
  - test_bucket_capacity_check → respects allocation %
  - test_bucket_pnl_calculation

tests/test_exit_rules.py:
  - test_bucket1_short_dte_50pct_profit → FULL_EXIT
  - test_bucket1_21dte_rule → FULL_EXIT regardless of profit
  - test_bucket1_never_stop_loss → always ROLL not STOP_LOSS
  - test_bucket2b_stop_loss_at_200pct → STOP_LOSS
  - test_bucket2b_no_roll → ROLL never recommended
  - test_bucket3_40pct_profit → triggers LLM review
  - test_bucket3_thesis_complete → FULL_EXIT

tests/test_wheel_tracker.py:
  - test_full_wheel_cycle
  - test_premium_accumulation
  - test_annualized_return_calculation
  - test_phase_transitions

tests/test_daily_briefing.py:
  - test_briefing_generates_without_error
  - test_briefing_contains_required_sections
  - test_briefing_sends_to_discord
```

### Success criteria
```
✅ All existing tests still pass (546+)
✅ New tests pass (target: 600+ total)
✅ ruff clean (0 errors)
✅ mypy clean (0 errors)
✅ make healthcheck shows 24/24 PASS
✅ make test-alerts still works
✅ Dashboard shows bucket badges on positions
✅ Daily briefing fires at 8:05 AM ET
✅ 21 DTE alert fires correctly
✅ Wheel tracker creates cycle on put sale
```

---

## Commit Convention
```
feat: three-bucket framework + exit rules engine
feat: 21 DTE alert system
feat: LLM exit decision engine  
feat: wheel strategy tracker
feat: daily 8 AM briefing
feat: dashboard bucket badges + wheel page
test: bucket manager, exit rules, wheel tracker
```

---

## Notes for Claude Code

1. **Do NOT change** any existing signal modules, scanner, or broker code
2. **Do NOT change** the existing position schema beyond adding columns
3. Add columns with DEFAULT values — never break existing data
4. All new modules go in `broker_client/buckets/` (new folder)
5. Daily briefing goes in `alerts/daily_briefing.py`
6. Wheel tracker data goes in `wheel_cycles` table (new)
7. LLM calls use `settings.ANTHROPIC_API_KEY` and `settings.LLM_MODEL`
8. All thresholds in settings.py — never hardcode numbers
9. Every alert goes through `AlertEngine` — never call Discord directly
10. Build iteratively — commit after each step passes tests
11. If LLM call fails → fall back to rule-based recommendation
12. Bucket 1 NEVER gets a STOP_LOSS recommendation — this is critical
13. The exit rules engine is the source of truth — LLM supplements it
14. Paper trading only — no live order execution in Phase 2