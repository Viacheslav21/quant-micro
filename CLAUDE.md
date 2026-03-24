# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

quant-micro — Micro-stake sniper bot for Polymarket. Targets high-probability markets (85-95¢) with tiny $1-5 bets, waiting for price dips to enter. WebSocket-first architecture: scanner builds a watchlist, WS monitors for dip triggers in real-time, tight SL (10%) protects capital. Designed for high win rate and smooth equity curve.

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
Polymarket API → Scanner (every 2 min, finds 85-95¢ markets)
    → Watchlist DB (upsert candidates, track peak prices)
    → WS Subscribe (monitor watchlist prices in real-time)
    → Dip Detection (price drops ≥2% from peak → entry trigger)
        → Spread Check (< 3¢)
        → Cooldown Check (5 min per market)
        → Theme Limit Check (max 20 per theme)
        → Stake Calculation (1% bankroll, capped at $5)
        → Entry (save position, mark in WS as position)
    → Position Monitoring (WS real-time, bid-price based)
        → SL: 10% from entry (hard)
        → TP: 5% from entry or resolution (≥99¢)
    → Cleanup (stale watchlist, unsubscribe WS)
```

### Module Responsibilities

- **main.py** (~300 lines) — Orchestrator. Two callbacks: `check_dip_entry` (watchlist dip → open position) and `check_position_price` (position SL/TP/resolution). REST scan every 2 min for watchlist updates. WS for real-time dip detection and position monitoring. Cooldown 5 min per market. Graceful shutdown on SIGTERM/SIGINT.
- **engine/scanner.py** (~130 lines) — Fetches up to 300 markets from Gamma API. Filters: volume >$50k, liquidity >$5k, spread <3¢, price in 85-95¢ zone. Theme classification (13 themes). Returns watchlist candidates.
- **engine/ws_client.py** (~280 lines) — Polymarket WebSocket client. Dual-purpose: watchlist dip detection + position SL/TP monitoring. Tracks peak price per market for dip calculation. Bid-price based exit pricing. Auto-reconnect, heartbeat, batch subscribe.
- **utils/db.py** (~250 lines) — PostgreSQL with 4 tables: micro_watchlist, micro_positions, micro_stats, micro_log. Atomic close (WHERE status='open' RETURNING id). Peak price tracking in watchlist (GREATEST on upsert).
- **utils/telegram.py** (~35 lines) — Async Telegram notifications with HTML formatting and plain text fallback.

### Key Algorithms

- **Dip Detection**: Track peak price per watchlist market via WS. When current price drops ≥ DIP_TRIGGER_PCT (2%) from peak → entry trigger. Peak resets on new scanner scan (upserted via GREATEST).
- **Entry Logic**: Buy YES at best_ask price. Stake = min(MAX_STAKE, 1% bankroll). ROI at resolution must be ≥3%.
- **Exit Logic**: Bid-price based (realistic exit). SL at -10% from entry (hard cut). TP at +5% or resolution (≥99¢ → full payout). Resolution uses linear payout (not binary) as safety.
- **Cooldown**: 5 min per market after entry to prevent re-entry spam.
- **Theme Diversification**: Max 20 positions per theme to prevent concentration.

### Database Tables (owned by quant-micro)

| Table | PK | Purpose |
|---|---|---|
| **micro_watchlist** | `market_id TEXT` | Markets in 85-95¢ zone being monitored. Tracks peak_price, spread, tokens. |
| **micro_positions** | `id TEXT` | Open/closed micro trades. Entry/exit prices, PnL, SL/TP config. |
| **micro_stats** | `id INTEGER` (singleton) | Bankroll, total_pnl, wins, losses, peak_equity. |
| **micro_log** | `id BIGSERIAL` | Append-only event log: STARTUP, SHUTDOWN, SCAN, OPEN, CLOSE_SL, CLOSE_TP, CLOSE_RESOLVED. |

### Configuration

All config via environment variables:
- `SIMULATION=true` (default, no real trades)
- `SCAN_INTERVAL=120` (seconds between REST scans)
- `MAX_STAKE=5.0` (max $5 per position)
- `MAX_OPEN=150` (many small positions)
- `SL_PCT=0.10` (10% stop loss)
- `TP_PCT=0.05` (5% take profit, or wait for resolution)
- `WATCHLIST_MIN_PRICE=0.85` / `WATCHLIST_MAX_PRICE=0.95` (watchlist zone)
- `DIP_TRIGGER_PCT=0.02` (2% dip from peak triggers entry)
- `MIN_VOLUME=50000`, `MIN_LIQUIDITY=5000`, `MAX_SPREAD=0.03` (market filters)
- `MAX_PER_THEME=20` (theme concentration limit)
- `CONFIG_TAG=micro-v1`

### Risk Management

- **Micro stakes**: $1-5 per position, max 1% of bankroll
- **Hard SL**: 10% from entry — if market drops that much at 90¢, something fundamentally changed
- **Theme diversification**: Max 20 positions per theme
- **Position limit**: 150 max open
- **Spread filter**: Skip markets with >3¢ spread (illiquid)
- **Cooldown**: 5 min per market after entry
- **Bid-price exits**: Use bid (not mid) for SL/TP calculation — matches real exit price
- **Atomic close**: `WHERE status='open' RETURNING id` prevents double-close
