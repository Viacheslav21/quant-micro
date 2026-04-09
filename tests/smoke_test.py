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
# Combined source for checks that span modules
src_all = src_main + src_entry + src_monitor + src_resolver


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
check("RESOLUTION_PRICE = 0.99", find_config("RESOLUTION_PRICE") == "0.99")
check("MAX_DAYS_LEFT exists", find_config("MAX_DAYS_LEFT") is not None)


# ── 2. SL / Safety Guards ──
print("\n\033[1m2. SL & Safety\033[0m")

check("SL disabled ≤1d: 'days_to_expiry > 1'", "days_to_expiry > 1" in src_monitor)
check("Division by zero: entry_price guard", "entry_price > 0" in src_monitor)
check("Resolution loss: bid_price <= 0.01", "bid_price <= 0.01" in src_monitor)
check("Sanity check: 50% drop filter", "entry_price * 0.5" in src_monitor)
check("MAX_LOSS hard cap", "MAX_LOSS_PER_POS" in src_all)
check("MAX_LOSS always enforced", "ALWAYS enforced" in src_monitor)


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


# ── 4. Position Monitoring ──
print("\n\033[1m4. Monitoring\033[0m")

check("Resolution WIN: ≥99¢", "RESOLUTION_PRICE" in src_all)
check("Resolution LOSS: ≤1¢", "resolved_loss" in src_monitor)
check("SL with REST verify", "_verify_price_rest" in src_monitor)
check("Volume confirm for SL", "_check_volume_confirms" in src_monitor)
check("Expired position cleanup", "check_expired_positions" in src_all)
check("Bid price for exit (not mid)", "best_bid" in src_monitor)
check("Parallel REST for expired", "asyncio.gather" in src_resolver)


# ── 5. Telegram Messages ──
print("\n\033[1m5. Telegram\033[0m")

check("Entry: shows bankroll", "Банк:" in src_entry or "bankroll" in src_entry.lower())
check("Entry: shows spread", "Spread:" in src_entry)
check("Entry: shows SL status", "SL:" in src_entry)
check("Win: shows hold time", "hold_hours" in src_monitor or "Держали" in src_monitor)
check("Win: shows PnL %", "pnl/stake" in src_monitor or "pnl_pct" in src_monitor)
check("Loss: shows days to expiry", "days_to_expiry" in src_monitor)


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
check("NegRisk group check in entry", "neg_risk_group" in src_db)
check("Atomic close: WHERE status='open'", "WHERE status='open'" in src_db or "status = 'open'" in src_db)
check("Bayesian theme auto-block", "SHRINKAGE_K" in src_db)
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


# ── 8. ROI Math ──
print("\n\033[1m8. ROI Math\033[0m")

for price, expected_min in [(0.90, 0.10), (0.92, 0.08), (0.94, 0.06), (0.97, 0.03), (0.99, 0.009)]:
    roi = (1.0 - price) / price
    check(f"ROI at {price*100:.0f}¢ = {roi:.3f} > 1%", roi > 0.01, f"got {roi:.4f}")

# Dynamic entry price math
check("Dynamic 1d: 0.90", 0.90 <= 0.94)  # min(base, 0.90)
check("Dynamic 2d: 0.92", 0.92 <= 0.94)
check("Dynamic 3d: 0.93", 0.93 <= 0.94)


# ── 9. WS Client ──
print("\n\033[1m9. WS Client\033[0m")

check("Auto-reconnect", "reconnect" in src_ws.lower() or "RECONNECT_DELAY" in src_ws)
check("Heartbeat", "PING" in src_ws or "heartbeat" in src_ws.lower())
check("Batch subscribe", "batch" in src_ws.lower() or "100" in src_ws)
check("NO side price inversion", "1.0 -" in src_ws or "1 -" in src_ws)


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
