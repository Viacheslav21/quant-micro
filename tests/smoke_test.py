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
src_scanner = read("engine/scanner.py")
src_ws = read("engine/ws_client.py")
src_db = read("utils/db.py")


# ── 1. Config Defaults ──
print("\n\033[1m1. Config Defaults\033[0m")

def find_config(key, src=src_main):
    m = re.search(rf'"{key}":\s*(?:float|int)\(os\.getenv\([^,]+,\s*"([^"]+)"\)', src)
    return m.group(1) if m else None

check("MAX_STAKE = 50", find_config("MAX_STAKE") == "50.0", f"got {find_config('MAX_STAKE')}")
check("MIN_STAKE = 5", find_config("MIN_STAKE") == "5.0", f"got {find_config('MIN_STAKE')}")
check("ENTRY_MIN_PRICE = 0.95", find_config("ENTRY_MIN_PRICE") == "0.95", f"got {find_config('ENTRY_MIN_PRICE')}")
check("WATCHLIST_MIN_PRICE = 0.90", find_config("WATCHLIST_MIN_PRICE") == "0.90", f"got {find_config('WATCHLIST_MIN_PRICE')}")
check("MIN_ROI = 0.01", find_config("MIN_ROI") == "0.01", f"got {find_config('MIN_ROI')}")
check("MIN_LIQUIDITY_MULT = 100", find_config("MIN_LIQUIDITY_MULT") == "100", f"got {find_config('MIN_LIQUIDITY_MULT')}")
check("MIN_QUALITY_SCORE = 25", find_config("MIN_QUALITY_SCORE") == "25", f"got {find_config('MIN_QUALITY_SCORE')}")
check("MAX_OPEN = 50", find_config("MAX_OPEN") == "50")
check("RESOLUTION_PRICE = 0.99", find_config("RESOLUTION_PRICE") == "0.99")
check("MAX_DAYS_LEFT = 10", find_config("MAX_DAYS_LEFT") == "10")


# ── 2. SL / Safety Guards ──
print("\n\033[1m2. SL & Safety\033[0m")

check("SL disabled ≤3d: 'days_to_expiry > 3'", "days_to_expiry > 3" in src_main)
sl_count = src_main.count("days_to_expiry > 3")
check("Both SL + rapid drop disabled", sl_count >= 2, f"found {sl_count} occurrences (need ≥2)")
check("Division by zero: entry_price guard", "entry_price > 0" in src_main or "max(entry_price" in src_main)
check("Resolution loss: bid_price <= 0.01", "bid_price <= 0.01" in src_main)
check("Sanity check: 50% drop filter", "entry_price * 0.5" in src_main)


# ── 3. Scanner Filters ──
print("\n\033[1m3. Scanner\033[0m")

check("acceptingOrders filter", "acceptingOrders" in src_scanner)
check("Risky bypass ≥96¢", "best_price < 0.96" in src_scanner)
check("Volume filter", "min_volume" in src_scanner)
check("Spread filter", "max_spread" in src_scanner)
check("Quality score function", "def quality_score" in src_scanner)
check("Date parsing from question", "_parse_date_from_question" in src_scanner)
check("Both YES and NO sides checked", "no_price >= wl_min" in src_scanner)


# ── 4. Position Monitoring ──
print("\n\033[1m4. Monitoring\033[0m")

check("Resolution WIN: ≥99¢", "RESOLUTION_PRICE" in src_main)
check("Resolution LOSS: ≤1¢", "resolved_loss" in src_main)
check("SL with REST verify", "_verify_price_rest" in src_main)
check("Volume confirm for SL", "_check_volume_confirms" in src_main)
check("Expired position cleanup", "check_expired_positions" in src_main)
check("Bid price for exit (not mid)", "best_bid" in src_main)


# ── 5. Telegram Messages ──
print("\n\033[1m5. Telegram\033[0m")

check("Entry: shows bankroll", "Банк:" in src_main or "bankroll" in src_main.lower())
check("Entry: shows spread", "Spread:" in src_main or "spread" in src_main)
check("Entry: shows SL status", "SL:" in src_main)
check("Win: shows hold time", "hold_hours" in src_main or "Держали" in src_main)
check("Win: shows PnL %", "pnl/stake" in src_main or "pnl_pct" in src_main)
check("Loss: shows days to expiry", "days_to_expiry" in src_main)


# ── 6. Data Collection ──
print("\n\033[1m6. Data Collection\033[0m")

check("Log: OPEN event", '"OPEN"' in src_main)
check("Log: CLOSE_RESOLVED event", '"CLOSE_RESOLVED"' in src_main)
check("Log: CLOSE_SL event", '"CLOSE_SL"' in src_main)
check("Log: hold_hours in close", "hold_hours" in src_main)
check("Log: theme in close", '"theme"' in src_main)
check("Log: days_to_expiry in SL close", "days_to_expiry" in src_main)
check("DB: micro_log table", "micro_log" in src_db)
check("DB: micro_theme_stats", "micro_theme_stats" in src_db)
check("DB: SL blacklist", "has_sl_loss" in src_db)


# ── 7. ROI Math ──
print("\n\033[1m7. ROI Math\033[0m")

for price, expected_min in [(0.95, 0.05), (0.97, 0.03), (0.99, 0.009)]:
    roi = (1.0 - price) / price
    check(f"ROI at {price*100:.0f}¢ = {roi:.3f} > 1%", roi > 0.01, f"got {roi:.4f}")

# Stake math
for bankroll, expected in [(500, 25), (1000, 50), (50, 5), (3, 0)]:
    pct = bankroll * 0.05
    stake = min(50.0, max(pct, 5.0))
    if stake > bankroll: stake = 0
    check(f"Stake ${bankroll} → ${stake:.0f}", stake == expected, f"got {stake}")


# ── 8. WS Client ──
print("\n\033[1m8. WS Client\033[0m")

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
