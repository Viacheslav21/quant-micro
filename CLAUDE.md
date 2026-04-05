# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

quant-micro — Resolution harvester for Polymarket. Targets high-probability NON-RISKY markets (≥95¢) expiring within 10 days. Stakes $5-50, quality-scored entry, risky themes excluded (sports, esports, israel, military). Dynamic SL (7-10%), rapid-drop guard (7¢ absolute), SL disabled ≤3 days to expiry, expired position auto-close (24h past expiry). SL blacklist prevents re-entry after stop loss. WebSocket-first: scanner builds watchlist, WS monitors prices. Designed for high win rate on near-certain resolutions.

## Commands

```bash
pip install -r requirements.txt
cp .env.example .env  # edit with real credentials
python main.py
```

No test suite or linter. Logging to `micro.log` and stdout. Deployed via Railway (`Procfile: worker: python main.py`).

## Architecture

### Pipeline Flow

```
Polymarket API → Scanner (every 2 min, 1600 markets max)
    → Risky Theme Filter (exclude sports/esports/israel/military)
    → Risky Pattern Filter (21 patterns: price bets, vs matches, etc.)
    → Quality Scoring (price, spread, days_left, volume)
    → Direct (≥95¢) or Watchlist (90-95¢)
    → WS Subscribe (monitor watchlist prices in real-time)
    → Entry when price hits 95¢ zone:
        → Quality Gate (score ≥ 25)
        → Spread Check (< 2¢)
        → Theme Limit (max 5 per theme)
        → Stake Calculation (5% bankroll, min $5, max $50)
        → Entry (save position + end_date, mark in WS)
    → Position Monitoring (WS real-time, bid-price based)
        → SL: 7-10% dynamic (wider to survive fluctuations)
        → Rapid Drop: exit if price drops >7¢ from entry
        → Expired: auto-close 24h past end_date
        → Resolution: ≥99¢ → WIN
    → Cleanup (stale watchlist, unsubscribe WS)
```

### Module Responsibilities

- **main.py** (~830 lines) — Orchestrator. Quality-gated entry with combined DB check (1 query replaces 4: duplicate, theme block, SL blacklist, cooldown). Dynamic SL (7-10%, disabled ≤3d to expiry), rapid-drop guard (7¢, also disabled ≤3d), REST price verification before SL/rapid-drop exits, expired position auto-close (24h past expiry). In-memory position cache avoids DB reads on every WS tick. DB write throttle (30s per position). Risky market exception for ≥96¢. Portfolio correlation penalty. Batch watchlist upserts. WS callbacks + scan loop every 2 min. Graceful shutdown on SIGTERM/SIGINT.
- **engine/scanner.py** (~400 lines) — Fetches up to 1600 markets from Gamma API (16 pages). Filters: volume >$50k, spread <2¢, price in 90-97¢ zone, ≤10 days to expiry. Risky theme exclusion (sports, esports, israel, military — 4 themes). 21 risky pattern regexes (price bets, vs matches, counting, weather). Both YES and NO sides checked. Quality scoring (0-100). Theme classification (15 themes). Date parsing from question text with year-rollover handling.
- **engine/ws_client.py** (~320 lines) — Polymarket WebSocket client. Dual-purpose: watchlist price-up detection + position SL/resolution monitoring. One token can serve multiple ws_keys (YES + NO sides). Bid-price based exit pricing. Auto-reconnect with exponential backoff, heartbeat, batch subscribe/unsubscribe.
- **utils/db.py** (~520 lines) — PostgreSQL with 5 tables: micro_watchlist (composite PK: market_id + side, quality score), micro_positions (with end_date for expiry tracking), micro_stats, micro_log, micro_theme_stats (Bayesian per-theme calibration with auto-block). SL blacklist (has_sl_loss). Recent close cooldown (has_recent_close). Atomic close (WHERE status='open' RETURNING id). Auto-migrations for schema changes.
- **utils/telegram.py** (~47 lines) — Async Telegram notifications with HTML escaping and plain text fallback.

### Key Algorithms

- **Risky Market Filter**: Excludes themes (sports, esports, israel, military) and 21 question patterns (price bets, vs matches, counting, weather) that have high gap risk. Exception: markets ≥96¢ bypass risky filter.
- **Quality Scoring**: 0-100 score based on price (higher=better), spread (tighter=better), days_left (closer=better), volume (higher=better). Minimum score 25 to enter.
- **Entry Logic**: Buy YES/NO at best_ask price. Stake = 5% of bankroll, min $5, max $50. ROI at resolution must be ≥1%.
- **Dynamic SL**: ≤12h left → 10%, ≤1d → 9%, ≤2d → 8%, >2d → 7%. Wide enough to survive normal fluctuations — audit showed 0% WR on old tight SL.
- **SL Blacklist**: After a stop loss or rapid drop on a market, never re-enter that market+side. Prevents repeat losers.
- **Rapid Drop Guard**: If bid price drops >7¢ from entry (absolute), exit immediately regardless of SL %.
- **Expired Position Auto-Close**: Positions 24h+ past end_date are force-closed with current price. Prevents stuck capital.
- **Theme Diversification**: Max 5 positions per theme to prevent concentration.
- **Theme Auto-Block**: Bayesian shrinkage (k=20) tracks per-theme WR. Themes with adjusted WR < 40% after 10+ trades are auto-blocked. Recalibrated on every position close.
- **Volume-Confirmed SL**: SL is skipped if 24h volume < $5k (low volume = noise, not a real move).
- **Portfolio Correlation Penalty**: Treats positions in same theme as correlated (ρ=0.5). effective_n = n/(1+(n-1)*ρ). Blocks entry if effective stake > 5% bankroll or worst-case SL > 15% bankroll.

### Database Tables (owned by quant-micro)

| Table | PK | Purpose |
|---|---|---|
| **micro_watchlist** | `(market_id, side)` | Markets in 90-95¢ zone being monitored. Tracks quality, spread, best_ask, days_left, end_date, tokens. |
| **micro_positions** | `id TEXT` | Open/closed micro trades. Entry/exit prices, PnL, SL/TP config. |
| **micro_stats** | `id INTEGER` (singleton) | Bankroll, total_pnl, wins, losses, peak_equity. |
| **micro_theme_stats** | `theme TEXT` | Per-theme Bayesian calibration: trades, wins, losses, raw/adj WR, blocked flag. |
| **micro_log** | `id BIGSERIAL` | Append-only event log: STARTUP, SHUTDOWN, SCAN, OPEN, CLOSE_SL, CLOSE_TP, CLOSE_RESOLVED, CLOSE_EXPIRED. |

### Configuration

All config via environment variables:
- `SIMULATION=true` (default, no real trades)
- `SCAN_INTERVAL=120` (seconds between REST scans)
- `MAX_STAKE=50.0` (max $50 per position)
- `MIN_STAKE=5.0` (min $5 per position)
- `MAX_OPEN=50` (focused positions)
- `SL_PCT=0.05` (5% default stop loss, dynamic 7-10%)
- `ENTRY_MIN_PRICE=0.95` (direct entry zone)
- `WATCHLIST_MIN_PRICE=0.90` (watchlist zone)
- `MAX_DAYS_LEFT=10` (max 10 days to resolution)
- `MIN_ROI=0.01` (min 1% ROI at resolution)
- `MIN_VOLUME=50000`, `MIN_LIQUIDITY_MULT=100`, `MAX_SPREAD=0.02` (market filters)
- `MAX_PER_THEME=5` (theme concentration limit)
- `SCAN_PAGES=16` (1600 markets scanned)
- `MIN_QUALITY_SCORE=25` (minimum quality to enter)
- `CONFIG_TAG=micro-v4`

### Risk Management

- **Stakes**: $5-50 per position (5% of bankroll)
- **Risky market exclusion**: Sports/esports, israel, military themes excluded + 21 risky question patterns (price bets, vs matches, etc.). Exception: ≥96¢ markets bypass filter.
- **Quality gate**: Score ≥25 required (based on price, spread, days_left, volume)
- **Dynamic SL**: 7-10% depending on time to resolution. **Disabled entirely for markets ≤3 days to expiry** (let resolution play out).
- **SL blacklist**: No re-entry after stop loss or rapid drop on same market+side
- **Rapid drop guard**: Exit immediately if price drops >7¢ from entry
- **Expired auto-close**: Force-close positions 24h past end_date
- **Theme diversification**: Max 5 positions per theme
- **Position limit**: 50 max open
- **Spread filter**: Skip markets with >2¢ spread (illiquid)
- **Bid-price exits**: Use bid (not mid) for SL calculation — matches real exit price
- **Atomic close**: `WHERE status='open' RETURNING id` prevents double-close
- **Theme auto-block**: Bayesian calibration blocks themes with WR < 40% after 10+ trades
- **Volume-confirmed SL**: Low-volume drops ($<5k 24h) treated as noise, SL skipped
- **Correlation penalty**: Same-theme positions treated as ρ=0.5 correlated; blocks entry if effective exposure > 5% bankroll or worst-case > 15%
