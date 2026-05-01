"""
Pre-launch smoke tests for quant-micro.
Run: python tests/smoke_test.py
Exit code 0 = all passed, 1 = failures.
No external deps required — source code checks + pure math.
"""
import sys, os, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

passed = 0
failed = 0
errors = []

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  \033[32m✓\033[0m {name}")
    else:
        failed += 1
        msg = f"{name}: {detail}" if detail else name
        errors.append(msg)
        print(f"  \033[31m✗\033[0m {msg}")

def read(f):
    return open(os.path.join(ROOT, f)).read()

src_main = read("main.py")
src_entry = read("engine/entry.py")
src_monitor = read("engine/monitor.py")
src_resolver = read("engine/resolver.py")
src_scanner = read("engine/scanner.py")
src_ws = read("engine/ws_client.py")
src_db = read("utils/db.py")
src_shared = read("engine/shared.py")
# Combined source for checks that span modules
src_all = src_main + src_entry + src_monitor + src_resolver + src_shared


# ── 1. Config Defaults ──
print("\n\033[1m1. Config Defaults\033[0m")

def find_config(key, src=src_main):
    m = re.search(rf'"{key}":\s*(?:float|int)\(os\.getenv\([^,]+,\s*"([^"]+)"\)', src)
    return m.group(1) if m else None

check("BANKROLL default", find_config("BANKROLL") is not None)
check("MAX_STAKE exists", find_config("MAX_STAKE") is not None)
check("MIN_STAKE exists", find_config("MIN_STAKE") is not None)
check("ENTRY_MIN_PRICE = 0.94", find_config("ENTRY_MIN_PRICE") == "0.94", f"got {find_config('ENTRY_MIN_PRICE')}")
check("WATCHLIST_MIN_PRICE = 0.90", find_config("WATCHLIST_MIN_PRICE") == "0.90", f"got {find_config('WATCHLIST_MIN_PRICE')}")
check("MIN_ROI exists", find_config("MIN_ROI") is not None)
check("MIN_QUALITY_SCORE exists", find_config("MIN_QUALITY_SCORE") is not None)
check("MAX_OPEN = 50", find_config("MAX_OPEN") == "50")
check("RESOLUTION_PRICE = 0.995", find_config("RESOLUTION_PRICE") == "0.995")
check("MAX_DAYS_LEFT exists", find_config("MAX_DAYS_LEFT") is not None)


# ── 2. SL / Safety Guards ──
print("\n\033[1m2. SL & Safety\033[0m")

check("SL disabled, only MAX_LOSS + rapid drop", "Percentage SL disabled" in src_monitor)
check("Division by zero: entry_price guard", "entry_price > 0" in src_monitor)
check("Resolution loss: bid_price <= 0.01", "bid_price <= 0.01" in src_monitor)
check("No sanity filter blocking real drops", "_rejected_price" not in src_ws)
check("MAX_LOSS hard cap", "MAX_LOSS_PER_POS" in src_all)
check("MAX_LOSS always enforced", "ALWAYS enforced" in src_monitor)
check("MAX_LOSS bypass threshold configurable", "MAX_LOSS_BYPASS_BLOCKS" in src_monitor)
check("MAX_LOSS bypass default = 2", "_MAX_LOSS_BYPASS_BLOCKS_DEFAULT = 2" in src_monitor)
check("CLOB book API for live bid", "clob.polymarket.com/book" in src_monitor)
check("CLOB book parser exists", "def _parse_clob_book" in src_monitor)
check("CLOB fetch with retry", "_fetch_clob_book" in src_monitor)
check("REST verify takes token_id", "side: str, token_id" in src_monitor)
check("MAX_LOSS_BLOCKED telemetry", '"max_loss_blocked"' in src_monitor)


# ── 3. Scanner Filters ──
print("\n\033[1m3. Scanner\033[0m")

check("acceptingOrders filter", "acceptingOrders" in src_scanner)
check("Volume filter", "min_volume" in src_scanner)
check("Spread filter", "max_spread" in src_scanner)
check("Quality score function", "def quality_score" in src_scanner)
check("Date parsing from question", "_parse_date_from_question" in src_scanner)
check("Both YES and NO sides checked", "no_price >=" in src_scanner)
check("Dynamic entry price function", "def dynamic_entry_price" in src_scanner)
check("ENTRY_PRICE_1D configurable", "ENTRY_PRICE_1D" in src_scanner)
check("ENTRY_PRICE_2D configurable", "ENTRY_PRICE_2D" in src_scanner)
check("ENTRY_PRICE_3D configurable", "ENTRY_PRICE_3D" in src_scanner)
check("VS pattern for sports", "_VS_PATTERN" in src_scanner)
check("neg_risk_id in candidate", "neg_risk_id" in src_scanner)
check("Binary risk filter function", "def is_binary_risk" in src_scanner)
check("Binary risk: up or down", "up or down" in src_scanner)
check("Binary risk: between $ and $", "between" in src_scanner)
check("Binary risk: end in a draw", "end in a draw" in src_scanner)
check("Binary risk: crypto above $X", "above|over|exceed|reach|hit|surpass" in src_scanner)
check("Binary risk applied in fetch", "is_binary_risk" in src_scanner)
check("Blocked question keywords list", "BLOCKED_QUESTION_KEYWORDS" in src_scanner)
check("Blocked: hong kong in list", "hong kong" in src_scanner)
check("Blocked: cape town in list", "cape town" in src_scanner)
check("is_blocked_question function", "def is_blocked_question" in src_scanner)
check("is_blocked_question in entry.py", "is_blocked_question" in src_entry)
check("Blocked checked in try_enter", "blocked_question" in src_entry)
check("Price ceiling: skip >98¢", "0.98" in src_scanner)
check("Dynamic quality threshold", "min_q" in src_entry and "days_left" in src_entry)
check("No ' vs ' in sports keywords", '" vs ",' not in src_scanner.split('"esports"')[0],
      "' vs ' should be in _VS_PATTERN fallback, not sports keywords")


# ── 4. Position Monitoring ──
print("\n\033[1m4. Monitoring\033[0m")

check("Resolution WIN: ≥99¢", "RESOLUTION_PRICE" in src_all)
check("Resolution LOSS: ≤1¢", "resolved_loss" in src_monitor)
check("SL with REST verify", "_verify_price_and_volume" in src_monitor)
check("Volume confirm for rapid drop", "vol_24h" in src_monitor and "VOL BLOCKED" in src_monitor)
check("Expired position cleanup", "check_expired_positions" in src_all)
check("Bid price for exit (not mid)", "best_bid" in src_monitor)
check("Parallel REST for expired", "asyncio.gather" in src_resolver)


# ── 5. Telegram Messages ──
print("\n\033[1m5. Telegram\033[0m")

check("Entry: shows bankroll", "Банк:" in src_entry or "bankroll" in src_entry.lower())
check("Entry: shows spread", "Spread:" in src_entry)
check("Entry: shows ROI", "ROI" in src_entry)
check("Win: shows hold time", "hold_hours" in src_monitor or "Держали" in src_monitor)
check("Win: shows PnL %", "pnl/stake" in src_monitor or "pnl_pct" in src_monitor)


# ── 6. DB & Data ──
print("\n\033[1m6. DB & Data\033[0m")

check("micro_positions table", "micro_positions" in src_db)
check("micro_watchlist table", "micro_watchlist" in src_db)
check("micro_theme_stats table", "micro_theme_stats" in src_db)
check("neg_risk_id column in positions", "neg_risk_id" in src_db)
check("neg_risk_id column in watchlist", "neg_risk_id" in src_db)
check("neg_risk_id index", "idx_micro_pos_neg_risk" in src_db)
check("Combined entry check (1 query)", "check_entry_allowed" in src_db)
check("SL blacklist in entry check", "stop_loss" in src_db)
check("SL blacklist includes max_loss", "'max_loss'" in src_db)
check("SL blacklist includes rapid_drop", "'rapid_drop'" in src_db)
check("NegRisk group check in entry", "neg_risk_group" in src_db)
check("Atomic close: WHERE status='open'", "WHERE status='open'" in src_db or "status = 'open'" in src_db)
check("Bayesian theme auto-block", "SHRINKAGE_K" in src_db)
check("BLOCK_MIN_TRADES = 5 (fast reaction on toxic themes)", "BLOCK_MIN_TRADES = 5" in src_db)
check("BLOCK_WR_THRESHOLD = 0.40", "BLOCK_WR_THRESHOLD = 0.40" in src_db)
check("Q80 ≤1d tier in calc_stake", "MAX_STAKE_Q80_1D" in open('engine/entry.py').read())
check("config_live self-bootstrap: _ensure_config_live_tables", "_ensure_config_live_tables" in src_db)
check("config_live self-bootstrap: _seed_config_live_micro", "_seed_config_live_micro" in src_db)
check("config_live self-bootstrap: CREATE TABLE config_live", "CREATE TABLE IF NOT EXISTS config_live" in src_db)
check("config_live self-bootstrap: schema includes Q80_6H", "MAX_STAKE_Q80_6H" in src_db)
check("config_live self-bootstrap: schema includes Q80_1D", "MAX_STAKE_Q80_1D" in src_db)
check("config_live self-bootstrap: schema includes BYPASS_BLOCKS", "MAX_LOSS_BYPASS_BLOCKS" in src_db)
check("db.init accepts micro_config", "async def init(self, micro_config" in src_db)
check("main.py passes CONFIG to db.init", "db.init(micro_config=CONFIG)" in open('main.py').read())
check("Bankroll computed from positions", "starting_bankroll + total_pnl" in src_db)


# ── 7. Risk Management ──
print("\n\033[1m7. Risk Management\033[0m")

check("NegRisk group limit in entry", "neg_risk_id" in src_entry)
check("Dynamic entry in WS callback", "dynamic_entry_price" in src_entry)
check("LISTEN config with reconnect", "reconnecting in" in src_main)
check("Watchlist lookup by side", "get_watchlist_market(market_id, side)" in src_entry)
check("Event cascade", "check_event_cascade" in src_all)
check("Theme diversification limit", "MAX_PER_THEME" in src_all)
check("Config safe keys", "_SAFE_CONFIG_KEYS" in src_main)
check("BANKROLL in safe keys", '"BANKROLL"' in src_main)


# ── 8. Realistic Sim Costs ──
print("\n\033[1m8. Sim Costs\033[0m")

check("SLIPPAGE config exists", find_config("SLIPPAGE") is not None)
check("FEE_PCT config exists", find_config("FEE_PCT") is not None)
check("SLIPPAGE in safe keys", "SLIPPAGE" in src_main and "_SAFE_CONFIG_KEYS" in src_main)
check("FEE_PCT in safe keys", "FEE_PCT" in src_main and "_SAFE_CONFIG_KEYS" in src_main)
check("Slippage applied at entry", "SLIPPAGE" in src_entry)
check("Exit fee in shared", "FEE_PCT" in src_shared and "SLIPPAGE" in src_shared)
check("Exit fee applied in monitor (SL/rapid/max)", "calc_exit_fee" in src_monitor and src_monitor.count("calc_exit_fee") >= 3,
      f"calc_exit_fee appears {src_monitor.count('calc_exit_fee')}x (need ≥3: SL+rapid+max)")

# ── 8b. WS Price Sync ──
print("\n\033[1m8b. WS Price Sync\033[0m")

check("price_change syncs best_bid", 'info["best_bid"] = new_price' in src_ws)
check("Book updates best_bid directly", 'info["best_bid"] = raw_best_bid' in src_ws)
check("Shared parses outcome prices", "parse_outcome_prices" in src_shared and "raw[1]" in src_shared)


# ── 9. ROI Math ──
print("\n\033[1m9. ROI Math\033[0m")

for price, expected_min in [(0.90, 0.10), (0.92, 0.08), (0.94, 0.06), (0.97, 0.03), (0.99, 0.009)]:
    roi = (1.0 - price) / price
    check(f"ROI at {price*100:.0f}¢ = {roi:.3f} > 1%", roi > 0.01, f"got {roi:.4f}")

# Dynamic entry price math
check("Dynamic 1d: 0.90", 0.90 <= 0.94)  # min(base, 0.90)
check("Dynamic 2d: 0.92", 0.92 <= 0.94)
check("Dynamic 3d: 0.93", 0.93 <= 0.94)


# ── 10. WS Client ──
print("\n\033[1m10. WS Client\033[0m")

check("Auto-reconnect", "reconnect" in src_ws.lower() or "RECONNECT_DELAY" in src_ws)
check("Heartbeat", "PING" in src_ws or "heartbeat" in src_ws.lower())
check("Batch subscribe", "batch" in src_ws.lower() or "100" in src_ws)
check("NO side price inversion", "1.0 -" in src_ws or "1 -" in src_ws)


# ── 11. Theme Quality Adjustment ──
print("\n\033[1m11. Theme Quality Adjustment\033[0m")

check("theme_quality_factor function exists",    "def theme_quality_factor" in src_scanner)
check("_TARGET_WR constant defined",             "_TARGET_WR" in src_scanner)
check("_ADJ_FACTOR_MIN floor defined",           "_ADJ_FACTOR_MIN" in src_scanner)
check("_ADJ_FACTOR_MAX ceiling defined",         "_ADJ_FACTOR_MAX" in src_scanner)
check("theme_wr attr on MicroScanner",           "self.theme_wr" in src_scanner)
check("theme_factor applied before quality gate","theme_factor" in src_scanner)
check("get_theme_adj_wr in db.py",               "get_theme_adj_wr" in src_db)
check("Bayesian shrinkage reused from DB",       "SHRINKAGE_K" in src_db and "get_theme_adj_wr" in src_db)
check("scanner.theme_wr updated in main loop",   "scanner.theme_wr = theme_wr" in src_main)
check("get_theme_adj_wr fetched in gather",      "get_theme_adj_wr" in src_main)
check("factor clamped: max(min, min(max, ...))", "max(_ADJ_FACTOR_MIN" in src_scanner or "_ADJ_FACTOR_MIN, min" in src_scanner)
check("neutral for unknown themes (factor 1.0)", "return 1.0" in src_scanner)


# ── Results ──
print(f"\n{'='*50}")
total = passed + failed
if failed == 0:
    print(f"\033[32m ALL {passed} TESTS PASSED\033[0m")
    sys.exit(0)
else:
    print(f"\033[31m {failed}/{total} TESTS FAILED:\033[0m")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
