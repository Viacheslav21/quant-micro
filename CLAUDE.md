# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

quant-micro — Resolution harvester for Polymarket. Targets high-probability markets near resolution with dynamic entry pricing: ≤1d→90¢, ≤2d→92¢, ≤3d→93¢, >3d→94¢ (all configurable via dashboard). Stakes $10-20, quality-scored entry, blocked themes managed via dashboard (sports, esports blocked by default). Dynamic SL (7-10%), rapid-drop guard (configurable via RAPID_DROP_PCT, default 7¢), SL disabled ≤1 day to expiry, expired position auto-close (72h past expiry). SL blacklist prevents re-entry after stop loss. NegRisk group limit: max 3 positions per negRisk event (configurable via MAX_PER_NEG_RISK). WebSocket-first: scanner builds watchlist, WS monitors prices. Event cascade: when negRisk market resolves YES, auto-enter NO on siblings. Designed for high win rate on near-certain resolutions.

## Commands

```bash
pip install -r requirements.txt
cp .env.example .env  # edit with real credentials
python main.py
```

Tests: `python tests/smoke_test.py` (90 offline source code checks) + `python tests/test_logic.py` (104 unit tests with mocked DB/WS/Telegram — tests entry rejections, dynamic pricing, binary risk filter, SL, rapid drop, resolution, MAX_LOSS, sim costs, dynamic quality thresholds, shared utilities) + `python tests/test_bugfixes.py` (49 regression tests — WS multi-key, NO price, year rollover, HTML escaping, WS book guard, shared utilities, theme classification). No linter configured. Logging to stdout. Deployed via Railway (`Procfile: worker: python main.py`).

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
        → Quality Gate (dynamic: ≤1d→Q≥40, 1-3d→Q≥55, 3-5d→Q≥70, 5d+→Q≥80)
        → Spread Check (< 2¢)
        → Theme Limit (max 3 per theme)
        → NegRisk Group Limit (max 3 per negRisk event, configurable)
        → Stake Calculation (5% bankroll, min $10, max $20)
        → Entry (save position + end_date + neg_risk_id)
    → Position Monitoring (WS real-time, bid-price based)
        → Resolution: ≥99¢ → WIN, ≤1¢ → LOSS (no fees)
        → MAX_LOSS: hard cap $3 per position (always enforced, REST verified)
        → SL + Rapid Drop (unified, single REST call, disabled ≤1d):
            → SL: 7-10% dynamic, volume-confirmed
            → Rapid Drop: configurable via RAPID_DROP_PCT (default 7¢)
            → Both share REST cooldown (60s) and volume check
        → Expired: auto-close 72h past end_date
    → Event Cascade (negRisk YES resolution → enter NO on siblings)
    → Cleanup (stale watchlist, unsubscribe WS)
```

### Module Responsibilities

- **main.py** (~390 lines) — Slim orchestrator. Config loading, startup (DB/WS/Telegram init, position restore), scan loop (every 2 min, `asyncio.wait_for(600s)` timeout), LISTEN config_reload with auto-reconnect, watchdog, health endpoint, graceful shutdown on SIGTERM/SIGINT.
- **engine/shared.py** (~60 lines) — Shared utilities used across engine modules. `calc_days_left()` (unified date→days conversion with configurable fallback), `parse_outcome_prices()` (handles dict/str/list formats from Gamma API), `calc_exit_fee()` (SLIPPAGE + FEE_PCT calculation), `hours_since()` (time delta helper). Eliminates 4+ copies of date parsing and 3 copies of fee logic.
- **engine/entry.py** (~210 lines) — Entry logic. `try_enter()` with combined entry check (1 query: duplicate, theme block, SL blacklist, cooldown, negRisk group). Returns reason string on rejection for skip tracking. Dynamic SL (7-10% by days_left). `check_watchlist_price()` WS callback with dynamic entry price. `calc_stake()` (5% bankroll, min/max bounds). Uses `shared.calc_days_left()`.
- **engine/monitor.py** (~220 lines) — Position monitoring via WS. Unified exit evaluator: resolution → MAX_LOSS → SL/rapid_drop (priority order). `_verify_price_and_volume()` — single REST call with tenacity retry (1 retry on timeout/network error) replaces separate price + volume calls. `_do_close()` — shared atomic close + WS cleanup + theme recalibration. `_tg_base()` — shared telegram message builder. RAPID_DROP_PCT read from config (was hardcoded). SL and rapid drop share one REST call and one cooldown (no mutual blocking). Volume confirmation applied to both SL and rapid drop.
- **engine/resolver.py** (~210 lines) — Resolution for expired/stale positions. Parallel REST fetch for positions past end_date OR disappeared from scanner (market closed). Uses `shared.parse_outcome_prices()`. Updates WS state via `ws.update_last_seen()` (no direct mutation). Event cascade: detects negRisk YES resolution, enters NO on siblings using `shared.calc_days_left()`.
- **engine/scanner.py** (~570 lines) — Fetches up to 1600 markets from Gamma API (16 pages, parallel). `dynamic_entry_price()`: ≤1d→90¢, ≤2d→92¢, ≤3d→93¢, >3d→base (configurable via ENTRY_PRICE_1D/2D/3D). `is_binary_risk()` filter blocks markets that can lose entire stake instantly (range bets "between $X and $Y", coin flips "Up or Down", "Green or Red"). Filters: volume >$50k, spread <2¢, ≤7 days to expiry. Both YES and NO sides checked. Quality scoring (0-100). Price ceiling: both sides >98¢ skipped (ROI too low after costs). Theme classification (~30 themes, sports/esports checked FIRST via keyword lists, `_VS_PATTERN` regex fallback handles FC/BSC/AFC abbreviations, `_WIN_ON_DATE` pattern). Date parsing from question text with year-rollover handling. Event siblings map for negRisk cascade.
- **engine/ws_client.py** (~350 lines) — Polymarket WebSocket client. Dual-purpose: watchlist price-up detection + position SL/resolution monitoring. One token can serve multiple ws_keys (YES + NO sides). Bid-price based exit pricing. Book guard: incremental book events cannot lower `best_bid` below authoritative `price` from last `price_change` event (prevents phantom SL triggers). `update_last_seen()` method for resolver. Auto-reconnect with exponential backoff, heartbeat, batch subscribe/unsubscribe.
- **utils/db.py** (~440 lines) — PostgreSQL with 3 tables: micro_watchlist (composite PK: market_id + side, quality score, neg_risk_id), micro_positions (with end_date, neg_risk_id for group limiting), micro_theme_stats (theme + blocked flag only). Combined entry check (1 query: duplicate, theme block, SL blacklist, cooldown, negRisk group limit). Bankroll computed from positions (no separate stats table). Bayesian theme auto-block (shrinkage k=20, block if adj WR < 40% after 10+ trades). Atomic close (`WHERE status='open' RETURNING id`). Auto-migrations for schema changes.
- **utils/telegram.py** (~50 lines) — Async Telegram notifications with HTML escaping (preserves `<a>`, `<b>`, `<i>`, `<code>` tags) and plain text fallback.

### Key Algorithms

- **Dynamic Entry Price**: Configurable per time-to-expiry: ENTRY_PRICE_1D (default 90¢), ENTRY_PRICE_2D (92¢), ENTRY_PRICE_3D (93¢), >3d uses ENTRY_MIN_PRICE (94¢). Applied in scanner (direct/watchlist split) and WS watchlist callback. Allows more aggressive entry on near-expiry markets where resolution is imminent.
- **Quality Scoring**: 0-100 score based on price (higher=better), spread (tighter=better), days_left (closer=better), volume (higher=better). **Dynamic quality threshold by time-to-resolution**: ≤1d→Q≥40 (base), 1-3d→Q≥55, 3-5d→Q≥70, 5d+→Q≥80. Far markets need stronger signals. Data: Q40-60 had 66.7% WR vs Q80+ at 97.3%.
- **Scanner price ceiling**: Markets where both sides >98¢ are skipped (ROI<2%, after costs ≈ break-even).
- **Entry Logic**: Buy YES/NO at best_ask price. Stake = 5% of bankroll, min $10, max $20. ROI at resolution must be ≥1.8%.
- **Dynamic SL**: ≤12h left → 10%, ≤1d → 9%, ≤2d → 8%, >2d → 7%. Disabled entirely for markets ≤1 day to expiry (let resolution play out).
- **MAX_LOSS Hard Cap**: $3 per position, always enforced even when SL is disabled. Uses REST-confirmed price with tenacity retry.
- **SL Blacklist**: After a stop loss or rapid drop on a market, never re-enter that market+side. Prevents repeat losers.
- **Rapid Drop Guard**: If bid price drops more than RAPID_DROP_PCT (default 7¢) from entry, exit immediately. Configurable via dashboard. Volume-confirmed (same as SL).
- **Unified Exit Evaluator**: Single REST call (`_verify_price_and_volume`) answers both SL and rapid drop checks. Priority: resolution → MAX_LOSS → SL → rapid drop. Single cooldown per market (60s). tenacity retry (1 retry, 1s wait) on timeout/network errors for reliable SL verification.
- **NegRisk Group Limit**: Max 3 open positions per negRisk event group (configurable via MAX_PER_NEG_RISK). Prevents correlated risk (e.g., "Israel strikes Iran" + "Israel strikes Fordow" are same negRisk group).
- **Expired Position Auto-Close**: Positions 72h+ past end_date are force-closed. REST API checked in parallel for all expired positions to detect resolution.
- **Theme Diversification**: Max 3 positions per theme to limit correlation risk.
- **Theme Auto-Block**: Bayesian shrinkage (k=20) tracks per-theme WR. Themes with adjusted WR < 40% after 10+ trades are auto-blocked. Recalibrated on every position close. Sports/esports blocked by default.
- **Volume-Confirmed Exits**: Both SL and rapid drop are skipped if 24h volume < $5k (low volume = noise, not a real move).
- **Event Cascade**: When a negRisk market resolves YES, automatically enter NO on sibling markets in the same event group (via `event_siblings` map). Exploits the fact that if one outcome in a mutually exclusive group wins, the rest must lose.
- **Realistic Sim Costs**: Entry slippage (`SLIPPAGE`, default 0.5¢) added to entry_price — simulates worse fill than quoted bestAsk. Exit costs via `shared.calc_exit_fee()`: slippage + round-trip fee (`FEE_PCT`, default 2%) deducted from PnL on early exits (SL, rapid drop, MAX_LOSS). Resolution payouts have no exit costs (automatic settlement). Both configurable via dashboard.
- **WS Book Guard**: `price_change` events (authoritative) sync `best_bid` on every update. `book` events (incremental) are guarded: `best_bid = max(raw_best_bid, auth_price)` — cannot drop below latest `price` from `price_change`. Prevents phantom SL triggers from stale lower-level order book updates. Real drops arrive via `price_change` first.
- **REST PnL on Exit**: SL, rapid drop, and MAX_LOSS use REST-verified price for PnL calculation when available (not stale WS bid). Outcome prices parsed via `shared.parse_outcome_prices()`.
- **Theme Classification**: ~30 themes with keyword matching. Sports/esports checked FIRST (dict insertion order). Handles: CBA basketball teams (Pioneers, Rockets, Ducks, Monkey King, etc.), Chinese soccer clubs (Shenhua, Haigang), tennis venues (Linz, Monza, Barletta), "end in a draw" soccer pattern. `_VS_PATTERN` regex supports abbreviations (FC, BSC, AFC). `_WIN_ON_DATE` pattern catches "Will X win on 2026-04-07?" club matches. Tech keyword `" ai "` (with spaces) avoids false positives on "Shanghai".

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
- **Capacity**: `MAX_OPEN`, `MAX_PER_THEME`, `MAX_PER_NEG_RISK`
- **Filters**: `MAX_DAYS_LEFT`, `MIN_VOLUME`
- **Timing**: `SCAN_INTERVAL`
- **General**: `BANKROLL`, `CONFIG_TAG`
- **Sim Costs**: `SLIPPAGE` (default 0.005 = 0.5¢ per side), `FEE_PCT` (default 0.02 = 2% round-trip)

### Risk Management

- **Stakes**: $10-20 per position (5% of bankroll)
- **Binary risk filter**: Blocks markets where price jumps to 0 instantly without gradual decline: range bets ("between $X and $Y"), coin flips ("Up or Down", "Green or Red", "Higher or Lower"). SL/MAX_LOSS can't catch these.
- **Theme blocking**: Managed via dashboard (block/unblock). Sports/esports blocked by default. Bayesian auto-block for themes with WR < 40% after 10+ trades.
- **Quality gate**: Dynamic threshold by time-to-resolution (≤1d→Q≥40, 1-3d→Q≥55, 3-5d→Q≥70, 5d+→Q≥80). Score based on price, spread, days_left, volume
- **Dynamic SL**: 7-10% depending on time to resolution. **Disabled for markets ≤1 day to expiry** (let resolution play out).
- **MAX_LOSS hard cap**: $3 per position, always enforced regardless of SL status. REST verified with retry.
- **SL blacklist**: No re-entry after stop loss or rapid drop on same market+side
- **Rapid drop guard**: Exit if price drops more than RAPID_DROP_PCT (default 7¢) from entry. Configurable via dashboard.
- **NegRisk group limit**: Max 3 positions per negRisk event (configurable via MAX_PER_NEG_RISK)
- **Expired auto-close**: Force-close positions 72h past end_date
- **Theme diversification**: Max 3 positions per theme
- **Position limit**: 50 max open
- **Spread filter**: Skip markets with >2¢ spread (illiquid)
- **Bid-price exits**: Use bid (not mid) for SL calculation — matches real exit price
- **Atomic close**: `WHERE status='open' RETURNING id` prevents double-close
- **Volume-confirmed exits**: Low-volume drops (<$5k 24h) treated as noise, both SL and rapid drop skipped
- **WS book guard**: Incremental book events can't lower best_bid below authoritative price_change — prevents phantom SL triggers
- **Event cascade**: NegRisk YES resolution triggers automatic NO entry on sibling markets
- **Sim cost modeling**: Entry slippage (0.5¢), exit fee via `shared.calc_exit_fee()` (2%) on early exits — PnL ~15-30% more conservative than raw prices
- **REST retry**: SL/MAX_LOSS verification retries once on timeout (tenacity) — prevents missed exits from transient network errors
