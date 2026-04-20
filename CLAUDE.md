# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

quant-micro — Resolution harvester for Polymarket. Targets high-probability markets near resolution with dynamic entry pricing: ≤1d→90¢, ≤2d→92¢, ≤3d→93¢, >3d→94¢ (all configurable via dashboard). Stakes $10-20, quality-scored entry, blocked themes managed entirely via dashboard (no hardcoded defaults). No percentage SL — only MAX_LOSS hard cap + rapid drop absolute threshold. Rapid-drop guard (configurable via RAPID_DROP_PCT, default 7¢). Expired position auto-close (72h past expiry). SL blacklist prevents re-entry after rapid drop/max_loss. NegRisk group limit: max 3 positions per negRisk event (configurable via MAX_PER_NEG_RISK). WebSocket-first: scanner builds watchlist, WS monitors prices. Book events update best_bid directly (no guard — real drops must be visible). Event cascade: when negRisk market resolves YES, auto-enter NO on siblings. Daily telegram report at 9:00 UTC. Designed for high win rate on near-certain resolutions.

## Commands

```bash
pip install -r requirements.txt
cp .env.example .env  # edit with real credentials
python main.py
```

Tests: `python tests/smoke_test.py` (101 offline source code checks) + `python tests/test_logic.py` (164 unit tests with mocked DB/WS/Telegram — tests entry rejections, dynamic pricing, binary risk filter, rapid drop, resolution, MAX_LOSS, sim costs, quality scoring sweet spot, theme quality adjustment, shared utilities, rapid drop block counter, REST poll stale positions, price history throttle) + `python tests/test_bugfixes.py` (49 regression tests — WS multi-key, NO price, year rollover, HTML escaping, WS book events, shared utilities, theme classification). No linter configured. Logging to stdout. Deployed via Railway (`Procfile: worker: python main.py`).

## Architecture

### Pipeline Flow

```
Polymarket API → Scanner (every 2 min, 1600 markets max)
    → Theme Block Check (dashboard-managed blocked themes, no hardcoded defaults)
    → Quality Scoring (price, spread, days_left, volume)
    → Dynamic Entry Price (≤1d→90¢, ≤2d→92¢, ≤3d→93¢, >3d→94¢)
    → Direct or Watchlist (4¢ buffer below entry)
    → WS Subscribe (monitor watchlist prices in real-time)
    → Entry when price hits dynamic entry zone:
        → Combined Entry Check (1 query: duplicate, theme block, SL blacklist, cooldown, negRisk group)
        → Quality Gate (dynamic: ≤1d→Q≥40, 1-3d→Q≥55, 3-5d→Q≥70, 5d+→Q≥80)
        → Spread Check (< 2¢)
        → Theme Limit (max 3 per theme)
        → NegRisk Group Limit (max 3 per negRisk event, configurable)
        → Stake Calculation (5% bankroll, min $10, max $20)
        → Entry (save position + end_date + neg_risk_id)
    → Position Monitoring (WS real-time, bid-price based)
        → Resolution: ≥99.5¢ → WIN, ≤1¢ → LOSS (no fees)
        → MAX_LOSS: hard cap (default $3), always enforced, REST verified with retry
        → Rapid Drop: configurable via RAPID_DROP_PCT (default 7¢), REST verified
        → Expired: auto-close 72h past end_date
    → Event Cascade (negRisk YES resolution → enter NO on siblings)
    → Daily Report (Telegram, 9:00 UTC)
    → Cleanup (stale watchlist, unsubscribe WS)
```

### Module Responsibilities

- **main.py** (~460 lines) — Slim orchestrator. Config loading, startup (DB/WS/Telegram init, position restore), scan loop (every 2 min, `asyncio.wait_for(600s)` timeout), LISTEN config_reload with auto-reconnect, watchdog, health endpoint, daily telegram report (9:00 UTC), graceful shutdown on SIGTERM/SIGINT.
- **engine/shared.py** (~60 lines) — Shared utilities used across engine modules. `calc_days_left()` (unified date→days conversion with configurable fallback), `parse_outcome_prices()` (handles dict/str/list formats from Gamma API), `calc_exit_fee()` (SLIPPAGE + FEE_PCT calculation), `hours_since()` (time delta helper). Eliminates 4+ copies of date parsing and 3 copies of fee logic.
- **engine/entry.py** (~200 lines) — Entry logic. `try_enter()` with combined entry check (1 query: duplicate, theme block, SL blacklist, cooldown, negRisk group). Returns reason string on rejection for skip tracking. `check_watchlist_price()` WS callback with dynamic entry price. `calc_stake()` (5% bankroll, min/max bounds). Uses `shared.calc_days_left()`.
- **engine/monitor.py** (~290 lines) — Position monitoring via WS. Exit priority: resolution → early TP → MAX_LOSS → rapid drop. `_verify_price_and_volume()` — single REST call with tenacity retry (1 retry on timeout/network error). `_do_close()` — shared atomic close + WS cleanup + theme recalibration. `_tg_base()` — shared telegram message builder. No percentage SL — only MAX_LOSS + rapid drop. RAPID_DROP_PCT from config. REST cooldown 60s per market. Volume confirmation for rapid drop. **Rapid drop block counter** (`_rapid_drop_blocks`): after 3 consecutive REST blocks, bypasses REST verification and trusts WS bid directly — Gamma REST midpoint lags order book during fast moves. **REST poll safety net** (`rest_poll_stale_positions`): called every scan cycle, fetches REST price for positions where WS has been silent ≥5 min, injects price into WS state and runs full exit logic. **Price history recording**: on each WS tick, records to `micro_price_history` when price moves ≥0.3¢ or ≥30s since last record (throttled).
- **engine/resolver.py** (~215 lines) — Resolution for expired/stale positions. Parallel REST fetch for positions past end_date OR disappeared from scanner (market closed). Uses `shared.parse_outcome_prices()`. Updates WS state via `ws.update_last_seen()` (no direct mutation). Event cascade: detects negRisk YES resolution, enters NO on siblings using `shared.calc_days_left()`. Price-based resolution fires at ≥99¢ / ≤1¢ (more aggressive than monitor's 99.5¢ bid-based threshold — API `closed` flag may lag).
- **engine/scanner.py** (~590 lines) — Fetches up to 1600 markets from Gamma API (16 pages, parallel). `dynamic_entry_price()`: ≤1d→90¢, ≤2d→92¢, ≤3d→93¢, >3d→base (configurable via ENTRY_PRICE_1D/2D/3D). `is_binary_risk()` filter blocks markets that can lose entire stake instantly (range bets "between $X and $Y", coin flips "Up or Down", "Green or Red"). Filters: volume >$50k, spread <2¢, ≤7 days to expiry. Both YES and NO sides checked. Quality scoring (0-100). Price ceiling: both sides >98¢ skipped (ROI too low after costs). Theme classification (~30 themes, sports/esports checked FIRST via keyword lists, `_VS_PATTERN` regex fallback handles FC/BSC/AFC abbreviations, `_WIN_ON_DATE` pattern). Date parsing from question text with year-rollover handling. Event siblings map for negRisk cascade.
- **engine/ws_client.py** (~325 lines) — Polymarket WebSocket client. Dual-purpose: watchlist price-up detection + position monitoring. One token can serve multiple ws_keys (YES + NO sides). Bid-price based exit pricing. Book events update `best_bid` directly (no guard — real order book drops must be visible for MAX_LOSS/rapid drop). `price_change` events sync both `price` and `best_bid` (authoritative). Raw price range filter (rejects prices ≤0 or >1.0). `update_last_seen()` method for resolver. Auto-reconnect with exponential backoff, heartbeat, batch subscribe/unsubscribe. MAX_LOSS/rapid drop exits are REST-verified in monitor.py, so WS glitches cannot force a false exit.
- **utils/db.py** (~545 lines) — PostgreSQL with 4 tables: micro_watchlist (composite PK: market_id + side, quality score, neg_risk_id), micro_positions (with end_date, neg_risk_id for group limiting), micro_theme_stats (theme + blocked flag only), **micro_price_history** (market_id, side, price, source, ts — price path for debugging). Combined entry check (1 query: duplicate, theme block, SL blacklist, cooldown, negRisk group limit). Bankroll computed from positions (no separate stats table). Bayesian theme auto-block (shrinkage k=20, block if adj WR < 40% after 10+ trades). `get_daily_report()` aggregates today/alltime/themes/reasons in single DB roundtrip. Atomic close (`WHERE status='open' RETURNING id`). No hardcoded theme blocks on startup (managed via dashboard). Auto-migrations for schema changes.
- **utils/telegram.py** (~50 lines) — Async Telegram notifications with HTML escaping (preserves `<a>`, `<b>`, `<i>`, `<code>` tags) and plain text fallback.

### Key Algorithms

- **Dynamic Entry Price**: Configurable per time-to-expiry: ENTRY_PRICE_1D (default 90¢), ENTRY_PRICE_2D (92¢), ENTRY_PRICE_3D (93¢), >3d uses ENTRY_MIN_PRICE (94¢). Applied in scanner (direct/watchlist split) and WS watchlist callback.
- **Quality Scoring**: 0-100 base score with **sweet spot 93-96¢** (not linear high=better). Price (0-40): peak 93-96¢, penalizes ≥98¢ (thin ROI after fees) and <90¢ (uncertain). Spread (0-15) tighter=better. Days (0-30) closer=better (≤0.5d gets max). Volume (0-15). **Dynamic quality threshold by time-to-resolution**: ≤1d→Q≥40 (base), 1-3d→Q≥55, 3-5d→Q≥70, 5d+→Q≥80.
- **Theme-Adjusted Quality**: Base quality is multiplied by a Bayesian theme factor before the quality gate. `adj_q = base_q × (theme_adj_wr / TARGET_WR)`, clamped to [0.75, 1.05]. `theme_adj_wr` uses the same Bayesian shrinkage (k=20) as `recalibrate_theme()`. Effect: crypto (WR≈81%) factor≈0.87 → Q70 becomes Q61; climate (WR≈94%) factor≈1.01 → Q70 stays Q71; election (WR≈70%) hits floor 0.75 → Q70 becomes Q52. Themes with <10 trades use factor=1.0 (no adjustment). Updated each scan cycle from `db.get_theme_adj_wr()` in parallel with `get_open_positions()`, stored on `scanner.theme_wr` (1-cycle lag, intentional). Self-correcting: if a theme improves over time, its factor rises automatically.
- **Scanner price ceiling**: Markets where both sides >98¢ are skipped (ROI<2%, after costs ≈ break-even).
- **Entry Logic**: Buy YES/NO at best_ask price. Stake = 5% of bankroll, dynamically capped by time-to-expiry (≤6h→MAX_STAKE_6H=$50, ≤1d→MAX_STAKE_1D=$35, >1d→MAX_STAKE=$20). ROI at resolution must be ≥1.8%.
- **Early Take-Profit**: When WS bid ≥ TAKE_PROFIT_PRICE (default 98¢) and days_to_expiry > TAKE_PROFIT_MIN_DAYS (default 1d), REST-verify and exit immediately. Frees capital for redeployment. Exit fee applies. Cooldown 60s to avoid repeated REST calls on sustained high price.
- **MAX_LOSS Hard Cap**: Default $3 per position, always enforced. REST-verified with tenacity retry (1 retry, 1s wait). Records real PnL at exit price (not capped).
- **Rapid Drop Guard**: Exit if bid drops more than RAPID_DROP_PCT (default 7¢) from entry. REST-verified, volume-confirmed. Configurable via dashboard.
- **No Percentage SL**: Disabled — fights the resolution harvesting strategy. Only MAX_LOSS ($3 hard cap) + rapid drop (absolute ¢ threshold) protect positions. This avoids false exits on temporary dips that would resolve as wins.
- **Rapid Drop Block Counter**: Gamma REST API returns midpoint/last-trade price which lags the live order book during fast market moves (e.g. BTC approaching a price target). If REST blocks a rapid drop 3 consecutive times, the 4th attempt bypasses REST and trusts the WS bid directly. Counter resets when price recovers above the threshold or position closes.
- **REST Poll Safety Net**: Every scan cycle (~2 min), positions where WS has been silent ≥5 min are REST-polled. Fetches live price, records to `micro_price_history` with source=`rest_poll`, injects into WS state, then runs full exit logic. Catches cases where Polymarket WS stops sending events for a market (low liquidity, reconnect).
- **Price Path History**: Every WS tick that moves price ≥0.3¢ or occurs ≥30s after the last record is written to `micro_price_history` (source=`ws`). REST poll also writes (source=`rest_poll`). Data kept indefinitely for debugging. Query: `SELECT price*100, source, ts FROM micro_price_history WHERE market_id='...' AND side='NO' ORDER BY ts`.
- **Exit Priority**: Resolution (≤1¢ LOSS / ≥RESOLUTION_PRICE WIN, default 99.5¢ on WS bid) → MAX_LOSS → Rapid Drop. Resolution checked first with no fees. MAX_LOSS always enforced. Rapid drop uses REST cooldown (60s). Resolver (scan-loop path) uses a more aggressive 99¢ / 1¢ price threshold as a fallback when the Gamma API `closed`/`resolved` flag is lagging.
- **SL Blacklist**: After rapid drop or max_loss on a market, never re-enter that market+side. Prevents repeat losers.
- **NegRisk Group Limit**: Max 3 open positions per negRisk event group (configurable via MAX_PER_NEG_RISK). Prevents correlated risk.
- **Expired Position Auto-Close**: Positions 72h+ past end_date are force-closed. REST API checked in parallel for all expired positions to detect resolution.
- **Theme Management**: Managed entirely via dashboard (block/unblock) + Bayesian auto-block (shrinkage k=20, block if adj WR < 40% after 10+ trades). No hardcoded blocked themes on startup. Max 3 positions per theme.
- **Volume-Confirmed Rapid Drop**: Rapid drop skipped if 24h volume < $5k (low volume = noise, not a real move).
- **Event Cascade**: When a negRisk market resolves YES, automatically enter NO on sibling markets in the same event group (via `event_siblings` map).
- **Realistic Sim Costs**: Entry slippage (`SLIPPAGE`, default 0.5¢) added to entry_price. Exit costs via `shared.calc_exit_fee()`: slippage + round-trip fee (`FEE_PCT`, default 2%) deducted from PnL on early exits (rapid drop, MAX_LOSS). Resolution payouts have no exit costs (automatic settlement).
- **WS Price Handling**: `price_change` events (authoritative) sync both `price` and `best_bid`. `book` events update `best_bid` directly — no guard, real order book drops must be visible for exit logic. Only sanity filter at WS layer: raw price must be in (0, 1.0]. False-exit protection lives at the exit decision instead — MAX_LOSS and rapid drop both REST-verify the price before closing (see monitor.py).
- **REST PnL on Exit**: Rapid drop and MAX_LOSS use REST-verified price for PnL calculation when available. Outcome prices parsed via `shared.parse_outcome_prices()`.
- **Theme Classification**: ~30 themes with keyword matching. Sports/esports checked FIRST (dict insertion order). Handles: CBA basketball teams, Chinese soccer clubs (Shenhua, Haigang), tennis venues (Linz, Monza, Barletta), "end in a draw" soccer pattern. `_VS_PATTERN` regex supports abbreviations (FC, BSC, AFC). Tech keyword `" ai "` (with spaces) avoids false positives on "Shanghai".
- **Daily Report**: Telegram summary at 9:00 UTC — today's trades/WR/PnL, close reasons, top themes, portfolio state (bankroll/equity/open positions), all-time stats.

### Database Tables (owned by quant-micro)

| Table | PK | Purpose |
|---|---|---|
| **micro_watchlist** | `(market_id, side)` | Markets being monitored. Tracks quality, spread, best_ask, days_left, end_date, tokens, neg_risk_id. |
| **micro_positions** | `id TEXT` | Open/closed micro trades. Entry/exit prices, PnL, config, neg_risk_id for group limiting. |
| **micro_theme_stats** | `theme TEXT` | Per-theme blocked flag (managed via dashboard + Bayesian auto-block). |

### Configuration

Config loaded from environment variables at startup, then overridden at runtime by `config_live` DB table (seeded by engine's `_seed_config_live`). `_reload_config()` merges DB overrides into the `CONFIG` dict (safe keys only, never credentials). Triggered instantly via `LISTEN config_reload` channel (auto-reconnects on connection loss). 21 micro parameters exposed for live editing in the dashboard:
- **Signals**: `ENTRY_MIN_PRICE`, `WATCHLIST_MIN_PRICE`, `MIN_ROI`, `MIN_QUALITY_SCORE`, `ENTRY_PRICE_1D`, `ENTRY_PRICE_2D`, `ENTRY_PRICE_3D`
- **Risk**: `RAPID_DROP_PCT`, `MAX_LOSS_PER_POS`
- **Sizing**: `MAX_STAKE`, `MIN_STAKE`
- **Capacity**: `MAX_OPEN`, `MAX_PER_THEME`, `MAX_PER_NEG_RISK`
- **Filters**: `MAX_DAYS_LEFT`, `MIN_VOLUME`
- **Timing**: `SCAN_INTERVAL`
- **Sim**: `SLIPPAGE` (default 0.005 = 0.5¢ per side), `FEE_PCT` (default 0.02 = 2% round-trip on early exits)
- **General**: `BANKROLL`, `CONFIG_TAG`

### Risk Management

- **Stakes**: $10-20 per position (5% of bankroll)
- **Binary risk filter**: Blocks markets where price jumps to 0 instantly: range bets ("between $X and $Y"), coin flips ("Up or Down", "Green or Red", "Higher or Lower"), per-game/per-map esports winner markets ("Game 2 Winner", "Map 3 Winner") — a single live game resolves in 15-40 min and can flip from 95¢+ to $0 on a single upset. BO3/BO5 series winner markets are allowed.
- **Theme blocking**: Managed entirely via dashboard. Bayesian auto-block for themes with WR < 40% after 10+ trades. No hardcoded defaults.
- **Quality gate**: Dynamic threshold (≤1d→Q≥40, 1-3d→Q≥55, 3-5d→Q≥70, 5d+→Q≥80)
- **MAX_LOSS hard cap**: Default $3 per position, always enforced. REST verified with retry. Real PnL recorded.
- **No percentage SL**: Disabled — fights resolution harvesting. MAX_LOSS + rapid drop are sufficient.
- **Rapid drop guard**: Exit if price drops >RAPID_DROP_PCT (default 7¢) from entry. REST + volume confirmed.
- **SL blacklist**: No re-entry after rapid drop or max_loss on same market+side
- **NegRisk group limit**: Max 3 positions per negRisk event (configurable)
- **Expired auto-close**: Force-close positions 72h past end_date
- **Theme diversification**: Max 3 positions per theme
- **Position limit**: 50 max open
- **Spread filter**: Skip markets with >2¢ spread (illiquid)
- **Bid-price exits**: Use bid (not mid) for exit pricing — matches real exit price
- **Atomic close**: `WHERE status='open' RETURNING id` prevents double-close
- **WS book events**: Update best_bid directly — no guard blocking real drops
- **Event cascade**: NegRisk YES resolution triggers automatic NO entry on sibling markets
- **Sim cost modeling**: Entry slippage (0.5¢), exit fee (2%) on early exits
- **REST retry**: MAX_LOSS/rapid drop verification retries once on timeout (tenacity)
- **Daily report**: Telegram summary at 9:00 UTC — trades, P&L, themes, portfolio
