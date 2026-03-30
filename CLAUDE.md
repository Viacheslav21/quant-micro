# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

quant-micro — Resolution harvester for Polymarket. Targets high-probability NON-RISKY markets (93-97¢) expiring within 10 days. Micro stakes $1-5, quality-scored entry, risky themes excluded (crypto price, sports, esports, israel, military). Dynamic SL (7-10%), rapid-drop guard (7¢ absolute), time-based exit (<4h to resolution if losing), expired position auto-close (24h past expiry). SL blacklist prevents re-entry after stop loss. WebSocket-first: scanner builds watchlist, WS monitors prices. Designed for high win rate on near-certain resolutions.

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
Polymarket API → Scanner (every 2 min, 800 markets max)
    → Risky Theme Filter (exclude crypto/sports/esports/markets/israel/military)
    → Risky Pattern Filter (exclude price bets, vs matches)
    → Quality Scoring (price, spread, days_left, volume)
    → Direct (≥93¢) or Watchlist (88-93¢)
    → WS Subscribe (monitor watchlist prices in real-time)
    → Entry when price hits 93¢ zone:
        → Quality Gate (score ≥ 30)
        → Spread Check (< 2¢)
        → Theme Limit (max 10 per theme)
        → Stake Calculation (min $1, max $3)
        → Entry (save position + end_date, mark in WS)
    → Position Monitoring (WS real-time, bid-price based)
        → SL: 7-10% dynamic (wider to survive fluctuations)
        → Rapid Drop: exit if price drops >7¢ from entry
        → Time Exit: close if losing with <4h to resolution
        → Expired: auto-close 24h past end_date
        → Resolution: ≥99¢ → WIN
    → Cleanup (stale watchlist, unsubscribe WS)
```

### Module Responsibilities

- **main.py** (~400 lines) — Orchestrator. Quality-gated entry, dynamic SL (7-10%), rapid-drop guard (7¢), time-based exit (<4h if losing), expired position auto-close (24h past expiry), SL blacklist (no re-entry after stop loss). Risky market double-check. Throttled "stake too small" warnings. WS callbacks + scan loop every 2 min. Graceful shutdown on SIGTERM/SIGINT.
- **engine/scanner.py** (~200 lines) — Fetches up to 1600 markets from Gamma API. Filters: volume >$50k, liquidity >$2500, spread <2¢, price in 88-97¢ zone, ≤10 days to expiry. Risky theme exclusion (crypto, sports, esports, markets, israel, military). Risky pattern exclusion (price bets, vs matches). Quality scoring (0-100). Theme classification (13 themes).
- **engine/ws_client.py** (~260 lines) — Polymarket WebSocket client. Dual-purpose: watchlist price-up detection + position SL/resolution monitoring. Bid-price based exit pricing. Auto-reconnect, heartbeat, batch subscribe.
- **utils/db.py** (~320 lines) — PostgreSQL with 5 tables: micro_watchlist (with quality score), micro_positions (with end_date for time exit), micro_stats, micro_log, micro_theme_stats (Bayesian per-theme calibration). SL blacklist (has_sl_loss). Atomic close (WHERE status='open' RETURNING id).
- **utils/telegram.py** (~35 lines) — Async Telegram notifications with HTML formatting and plain text fallback.

### Key Algorithms

- **Risky Market Filter**: Excludes themes (crypto, sports, esports, markets, israel, military) and question patterns (price bets, vs matches) that have high gap risk — price jumps from 90¢ to 0¢ on resolution.
- **Quality Scoring**: 0-100 score based on price (higher=better), spread (tighter=better), days_left (closer=better), volume (higher=better). Minimum score 35 to enter.
- **Entry Logic**: Buy YES/NO at best_ask price. Stake = max(MIN_STAKE, 1% bankroll), capped at MAX_STAKE. ROI at resolution must be ≥3%.
- **Dynamic SL**: ≤12h left → 10%, ≤1d → 9%, ≤2d → 8%, >2d → 7%. Wide enough to survive normal fluctuations — audit showed 0% WR on old tight SL.
- **SL Blacklist**: After a stop loss or rapid drop on a market, never re-enter that market+side. Prevents repeat losers.
- **Rapid Drop Guard**: If bid price drops >7¢ from entry (absolute), exit immediately regardless of SL %.
- **Expired Position Auto-Close**: Positions 24h+ past end_date are force-closed with current price. Prevents stuck capital.
- **Time Exit**: If position is in the red with <4 hours to resolution, exit. Don't gamble on last-minute resolution.
- **Theme Diversification**: Max 5 positions per theme to prevent concentration.
- **Theme Auto-Block**: Bayesian shrinkage (k=20) tracks per-theme WR. Themes with adjusted WR < 40% after 10+ trades are auto-blocked. Recalibrated on every position close.
- **Volume-Confirmed SL**: SL is skipped if 24h volume < $5k (low volume = noise, not a real move).
- **Portfolio Correlation Penalty**: Treats positions in same theme as correlated (ρ=0.5). effective_n = n/(1+(n-1)*ρ). Blocks entry if effective stake > 5% bankroll or worst-case SL > 15% bankroll.

### Database Tables (owned by quant-micro)

| Table | PK | Purpose |
|---|---|---|
| **micro_watchlist** | `market_id TEXT` | Markets in 85-95¢ zone being monitored. Tracks peak_price, spread, tokens. |
| **micro_positions** | `id TEXT` | Open/closed micro trades. Entry/exit prices, PnL, SL/TP config. |
| **micro_stats** | `id INTEGER` (singleton) | Bankroll, total_pnl, wins, losses, peak_equity. |
| **micro_theme_stats** | `theme TEXT` | Per-theme Bayesian calibration: trades, wins, losses, raw/adj WR, blocked flag. |
| **micro_log** | `id BIGSERIAL` | Append-only event log: STARTUP, SHUTDOWN, SCAN, OPEN, CLOSE_SL, CLOSE_TP, CLOSE_RESOLVED, CLOSE_EXPIRED. |

### Configuration

All config via environment variables:
- `SIMULATION=true` (default, no real trades)
- `SCAN_INTERVAL=120` (seconds between REST scans)
- `MAX_STAKE=5.0` (max $5 per position)
- `MIN_STAKE=1.0` (min $1 per position)
- `MAX_OPEN=50` (focused positions)
- `SL_PCT=0.05` (5% default stop loss, dynamic 7-10%)
- `ENTRY_MIN_PRICE=0.93` (direct entry zone)
- `WATCHLIST_MIN_PRICE=0.88` (watchlist zone)
- `MAX_DAYS_LEFT=10` (max 10 days to resolution)
- `MIN_ROI=0.03` (min 3% ROI at resolution)
- `MIN_VOLUME=50000`, `MIN_LIQUIDITY_MULT=500`, `MAX_SPREAD=0.02` (market filters)
- `MAX_PER_THEME=5` (theme concentration limit)
- `SCAN_PAGES=16` (1600 markets scanned)
- `MIN_QUALITY_SCORE=35` (minimum quality to enter)
- `TIME_EXIT_HOURS=4` (exit if losing <4h to resolution)
- `CONFIG_TAG=micro-v4`

### Risk Management

- **Micro stakes**: $1-5 per position, max $5
- **Risky market exclusion**: Crypto price bets, sports/esports, financial markets, israel, military excluded — these have gap risk (price jumps 90¢→0¢ on resolution)
- **Quality gate**: Score ≥35 required (based on price, spread, days_left, volume)
- **Dynamic SL**: 7-10% depending on time to resolution
- **SL blacklist**: No re-entry after stop loss or rapid drop on same market+side
- **Rapid drop guard**: Exit immediately if price drops >7¢ from entry
- **Expired auto-close**: Force-close positions 24h past end_date
- **Time exit**: Close losing positions with <4h to resolution
- **Theme diversification**: Max 5 positions per theme
- **Position limit**: 50 max open
- **Spread filter**: Skip markets with >2¢ spread (illiquid)
- **Bid-price exits**: Use bid (not mid) for SL calculation — matches real exit price
- **Atomic close**: `WHERE status='open' RETURNING id` prevents double-close
- **Theme auto-block**: Bayesian calibration blocks themes with WR < 40% after 10+ trades
- **Volume-confirmed SL**: Low-volume drops ($<5k 24h) treated as noise, SL skipped
- **Correlation penalty**: Same-theme positions treated as ρ=0.5 correlated; blocks entry if effective exposure > 5% bankroll or worst-case > 15%
