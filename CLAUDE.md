# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

quant-micro — Resolution harvester for Polymarket. Targets high-probability markets near resolution with dynamic entry pricing: ≤1d→90¢, ≤2d→92¢, ≤3d→93¢, >3d→94¢ (all configurable via dashboard). Stakes $10-20, quality-scored entry, blocked themes managed via dashboard (sports, esports blocked by default). Dynamic SL (7-10%), rapid-drop guard (7¢ absolute), SL disabled ≤1 day to expiry, expired position auto-close (72h past expiry). SL blacklist prevents re-entry after stop loss. NegRisk group limit: max 1 position per negRisk event (prevents correlated risk). WebSocket-first: scanner builds watchlist, WS monitors prices. Event cascade: when negRisk market resolves YES, auto-enter NO on siblings. Designed for high win rate on near-certain resolutions.

## Commands

```bash
pip install -r requirements.txt
cp .env.example .env  # edit with real credentials
python main.py
```

Pre-launch smoke tests: `python tests/smoke_test.py` (offline source code checks + pure math). No linter configured. Logging to stdout. Deployed via Railway (`Procfile: worker: python main.py`).

## Architecture

### Pipeline Flow

```
Polymarket API → Scanner (every 2 min, 1600 markets max)
    → Theme Block Check (dashboard-managed blocked themes)
    → Quality Scoring (price, spread, days_left, volume)
    → Dynamic Entry Price (≤1d→90¢, ≤2d→92¢, ≤3d→93¢, >3d→94¢)
    → Direct or Watchlist (4¢ buffer below entry)
    → WS Subscribe (monitor watchlist prices in real-time)
    → Entry when price hits dynamic entry zone:
        → Combined Entry Check (1 query: duplicate, theme block, SL blacklist, cooldown, negRisk group)
        → Quality Gate (score ≥ 40)
        → Spread Check (< 2¢)
        → Theme Limit (max 3 per theme)
        → NegRisk Group Limit (max 1 per negRisk event)
        → Stake Calculation (5% bankroll, min $10, max $20)
        → Entry (save position + end_date + neg_risk_id)
    → Position Monitoring (WS real-time, bid-price based)
        → SL: 7-10% dynamic (disabled ≤1d to expiry)
        → Rapid Drop: exit if price drops >7¢ from entry
        → MAX_LOSS: hard cap $3 per position (always enforced)
        → Expired: auto-close 72h past end_date
        → Resolution: ≥99¢ → WIN, REST check for expired
    → Event Cascade (negRisk YES resolution → enter NO on siblings)
    → Cleanup (stale watchlist, unsubscribe WS)
```

### Module Responsibilities

- **main.py** (~1070 lines) — Orchestrator. Combined entry check (1 query replaces 5: duplicate, theme block, SL blacklist, cooldown, negRisk group). Dynamic entry price from scanner (configurable ENTRY_PRICE_1D/2D/3D). Dynamic SL (7-10%, disabled ≤1d to expiry). Rapid-drop guard (7¢, also disabled ≤1d). MAX_LOSS hard cap ($3, always enforced even when SL disabled). REST price verification before SL exits. Expired position auto-close (72h past expiry, parallel REST fetch). In-memory position cache avoids DB reads on every WS tick. DB write throttle (30s per position). Batch watchlist upserts. WS callbacks + scan loop every 2 min (wrapped in `asyncio.wait_for(600s)` timeout). Event cascade: `check_event_cascade()` detects negRisk YES resolution and enters NO on siblings. LISTEN config_reload with auto-reconnect (10s retry). Watchdog tracks `_last_scan_at`. Telegram prefixed "MICRO |". Graceful shutdown on SIGTERM/SIGINT.
- **engine/scanner.py** (~530 lines) — Fetches up to 1600 markets from Gamma API (16 pages, parallel). `dynamic_entry_price()`: ≤1d→90¢, ≤2d→92¢, ≤3d→93¢, >3d→base (configurable via ENTRY_PRICE_1D/2D/3D). Filters: volume >$50k, spread <2¢, ≤7 days to expiry. Both YES and NO sides checked. Quality scoring (0-100). Theme classification (~30 themes, sports/esports checked FIRST via comprehensive keyword lists + "vs" regex pattern). Date parsing from question text with year-rollover handling. Event siblings map for negRisk cascade. `neg_risk_id` passed through candidate dict for group limiting. Telegram links use slug for correct URLs.
- **engine/ws_client.py** (~320 lines) — Polymarket WebSocket client. Dual-purpose: watchlist price-up detection + position SL/resolution monitoring. One token can serve multiple ws_keys (YES + NO sides). Bid-price based exit pricing. Auto-reconnect with exponential backoff, heartbeat, batch subscribe/unsubscribe.
- **utils/db.py** (~440 lines) — PostgreSQL with 3 tables: micro_watchlist (composite PK: market_id + side, quality score, neg_risk_id), micro_positions (with end_date, neg_risk_id for group limiting), micro_theme_stats (theme + blocked flag only). Combined entry check (1 query: duplicate, theme block, SL blacklist, cooldown, negRisk group limit). Bankroll computed from positions (no separate stats table). Bayesian theme auto-block (shrinkage k=20, block if adj WR < 40% after 10+ trades). Atomic close (`WHERE status='open' RETURNING id`). Auto-migrations for schema changes.
- **utils/telegram.py** (~50 lines) — Async Telegram notifications with HTML escaping (preserves `<a>`, `<b>`, `<i>`, `<code>` tags) and plain text fallback.

### Key Algorithms

- **Dynamic Entry Price**: Configurable per time-to-expiry: ENTRY_PRICE_1D (default 90¢), ENTRY_PRICE_2D (92¢), ENTRY_PRICE_3D (93¢), >3d uses ENTRY_MIN_PRICE (94¢). Applied in scanner (direct/watchlist split) and WS watchlist callback. Allows more aggressive entry on near-expiry markets where resolution is imminent.
- **Quality Scoring**: 0-100 score based on price (higher=better), spread (tighter=better), days_left (closer=better), volume (higher=better). Minimum score 40 to enter (Q<40 had 0% WR in audit).
- **Entry Logic**: Buy YES/NO at best_ask price. Stake = 5% of bankroll, min $10, max $20. ROI at resolution must be ≥1.8%.
- **Dynamic SL**: ≤12h left → 10%, ≤1d → 9%, ≤2d → 8%, >2d → 7%. Disabled entirely for markets ≤1 day to expiry (let resolution play out).
- **MAX_LOSS Hard Cap**: $3 per position, always enforced even when SL is disabled. Uses REST-confirmed price.
- **SL Blacklist**: After a stop loss or rapid drop on a market, never re-enter that market+side. Prevents repeat losers.
- **Rapid Drop Guard**: If bid price drops >7¢ from entry (absolute), exit immediately regardless of SL %.
- **NegRisk Group Limit**: Max 1 open position per negRisk event group. Prevents correlated risk (e.g., "Israel strikes Iran" + "Israel strikes Fordow" are same negRisk group).
- **Expired Position Auto-Close**: Positions 72h+ past end_date are force-closed. REST API checked in parallel for all expired positions to detect resolution.
- **Theme Diversification**: Max 3 positions per theme to limit correlation risk.
- **Theme Auto-Block**: Bayesian shrinkage (k=20) tracks per-theme WR. Themes with adjusted WR < 40% after 10+ trades are auto-blocked. Recalibrated on every position close. Sports/esports blocked by default.
- **Volume-Confirmed SL**: SL is skipped if 24h volume < $5k (low volume = noise, not a real move).
- **Event Cascade**: When a negRisk market resolves YES, automatically enter NO on sibling markets in the same event group (via `event_siblings` map). Exploits the fact that if one outcome in a mutually exclusive group wins, the rest must lose.

### Database Tables (owned by quant-micro)

| Table | PK | Purpose |
|---|---|---|
| **micro_watchlist** | `(market_id, side)` | Markets being monitored. Tracks quality, spread, best_ask, days_left, end_date, tokens, neg_risk_id. |
| **micro_positions** | `id TEXT` | Open/closed micro trades. Entry/exit prices, PnL, SL/TP config, neg_risk_id for group limiting. |
| **micro_theme_stats** | `theme TEXT` | Per-theme blocked flag (managed via dashboard + Bayesian auto-block). |

### Configuration

Config loaded from environment variables at startup, then overridden at runtime by `config_live` DB table. `_reload_config()` merges DB overrides into the `CONFIG` dict (safe keys only, never credentials). Triggered instantly via `LISTEN config_reload` channel (auto-reconnects on connection loss). 19 micro parameters exposed for live editing:
- **Signals**: `ENTRY_MIN_PRICE`, `WATCHLIST_MIN_PRICE`, `MIN_ROI`, `MIN_QUALITY_SCORE`, `ENTRY_PRICE_1D`, `ENTRY_PRICE_2D`, `ENTRY_PRICE_3D`
- **Risk**: `SL_PCT`, `RAPID_DROP_PCT`, `MAX_LOSS_PER_POS`
- **Sizing**: `MAX_STAKE`, `MIN_STAKE`
- **Capacity**: `MAX_OPEN`, `MAX_PER_THEME`
- **Filters**: `MAX_DAYS_LEFT`, `MIN_VOLUME`
- **Timing**: `SCAN_INTERVAL`
- **General**: `BANKROLL`, `CONFIG_TAG`

### Risk Management

- **Stakes**: $10-20 per position (5% of bankroll)
- **Theme blocking**: Managed via dashboard (block/unblock). Sports/esports blocked by default. Bayesian auto-block for themes with WR < 40% after 10+ trades.
- **Quality gate**: Score ≥40 required (based on price, spread, days_left, volume)
- **Dynamic SL**: 7-10% depending on time to resolution. **Disabled for markets ≤1 day to expiry** (let resolution play out).
- **MAX_LOSS hard cap**: $3 per position, always enforced regardless of SL status
- **SL blacklist**: No re-entry after stop loss or rapid drop on same market+side
- **Rapid drop guard**: Exit immediately if price drops >7¢ from entry
- **NegRisk group limit**: Max 1 position per negRisk event — prevents correlated positions
- **Expired auto-close**: Force-close positions 72h past end_date
- **Theme diversification**: Max 3 positions per theme
- **Position limit**: 50 max open
- **Spread filter**: Skip markets with >2¢ spread (illiquid)
- **Bid-price exits**: Use bid (not mid) for SL calculation — matches real exit price
- **Atomic close**: `WHERE status='open' RETURNING id` prevents double-close
- **Volume-confirmed SL**: Low-volume drops (<$5k 24h) treated as noise, SL skipped
- **Event cascade**: NegRisk YES resolution triggers automatic NO entry on sibling markets
