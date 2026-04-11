"""
Unit tests for core micro logic.
Run: python tests/test_logic.py
No external deps — mocks DB/WS/Telegram, tests pure logic.
"""
import sys, os, asyncio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

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


# ── Mocks ──

class MockDB:
    def __init__(self, open_positions=None, entry_allowed=None, stats=None):
        self._open = open_positions or []
        self._entry = entry_allowed or {"allowed": True, "reason": None}
        self._stats = stats or {"bankroll": 1000, "wins": 10, "losses": 1, "total_pnl": 50, "total_trades": 11, "peak_equity": 0}
        self.saved = []
        self.closed = []
        self.upserted = []

    async def check_entry_allowed(self, market_id, side, theme, neg_risk_id=None, max_per_neg_risk=3):
        return self._entry

    async def get_open_positions(self):
        return self._open

    async def get_stats(self, bankroll=500):
        return self._stats

    async def save_position_and_deduct(self, pos, stake):
        self.saved.append(pos)

    async def upsert_watchlist(self, candidate):
        self.upserted.append(candidate)

    async def close_position(self, pos_id, pnl, result, reason):
        self.closed.append({"id": pos_id, "pnl": pnl, "result": result, "reason": reason})
        return True

    async def recalibrate_theme(self, theme):
        pass

    async def update_position_price(self, pos_id, price, upnl):
        pass

    async def get_open_position_by_market(self, market_id, side):
        for p in self._open:
            if p["market_id"] == market_id and p["side"] == side:
                return p
        return None

    async def get_watchlist_market(self, market_id, side=None):
        return None


class MockWS:
    def __init__(self):
        self.prices = {}
        self.marked = set()
        self.unmarked = set()

    def mark_as_position(self, ws_key):
        self.marked.add(ws_key)

    def unmark_position(self, ws_key):
        self.unmarked.add(ws_key)

    def register_market(self, ws_key, **kwargs):
        self.prices[ws_key] = kwargs.get("price", 0.95)
        return []

    async def subscribe_tokens(self, tokens):
        pass

    def get_spread(self, ws_key):
        return 0.01


class MockTG:
    def __init__(self):
        self.messages = []

    async def send(self, msg):
        self.messages.append(msg)


_loop = asyncio.new_event_loop()
def run(coro):
    return _loop.run_until_complete(coro)


# ══════════════════════════════════════
# 1. dynamic_entry_price
# ══════════════════════════════════════
print("\n\033[1m1. Dynamic Entry Price\033[0m")

from engine.scanner import dynamic_entry_price

check("≤1d → 0.90", dynamic_entry_price(0.5, 0.94) == 0.90)
check("≤1d → 0.90 (exact 1d)", dynamic_entry_price(1.0, 0.94) == 0.90)
check("≤2d → 0.92", dynamic_entry_price(1.5, 0.94) == 0.92)
check("≤2d → 0.92 (exact 2d)", dynamic_entry_price(2.0, 0.94) == 0.92)
check("≤3d → 0.93", dynamic_entry_price(2.5, 0.94) == 0.93)
check("≤3d → 0.93 (exact 3d)", dynamic_entry_price(3.0, 0.94) == 0.93)
check(">3d → base (0.94)", dynamic_entry_price(5.0, 0.94) == 0.94)
check(">3d → base (0.96)", dynamic_entry_price(7.0, 0.96) == 0.96)

# With config override
cfg = {"ENTRY_PRICE_1D": 0.88, "ENTRY_PRICE_2D": 0.91, "ENTRY_PRICE_3D": 0.92}
check("Config override 1d=0.88", dynamic_entry_price(0.5, 0.94, cfg) == 0.88)
check("Config override 2d=0.91", dynamic_entry_price(1.5, 0.94, cfg) == 0.91)
check("Config override 3d=0.92", dynamic_entry_price(2.5, 0.94, cfg) == 0.92)

# min() behavior: if base < configured, use base
check("min(base=0.89, 1d=0.90) → 0.89", dynamic_entry_price(0.5, 0.89) == 0.89)


# ══════════════════════════════════════
# 1b. Binary Risk Filter
# ══════════════════════════════════════
print("\n\033[1m1b. Binary Risk Filter\033[0m")

from engine.scanner import is_binary_risk

# Should BLOCK
check("Block: Up or Down", is_binary_risk("S&P 500 Up or Down on April 9?"))
check("Block: Opens Up or Down", is_binary_risk("S&P Opens Up or Down on April 9?"))
check("Block: Green or Red", is_binary_risk("Bitcoin Green or Red on April 7?"))
check("Block: Higher or Lower", is_binary_risk("Higher or Lower: Nasdaq?"))
check("Block: between $X and $Y", is_binary_risk("Will BTC be between $70,000 and $72,000?"))
check("Block: between temp range", is_binary_risk("Will temp be between 20°C and 25°C?"))
check("Block: between % range", is_binary_risk("Will inflation be between 2.5% and 3.0%?"))

# Should ALLOW
check("Allow: above $X", not is_binary_risk("Will Bitcoin be above $72,000?"))
check("Allow: dip to $X", not is_binary_risk("Will Bitcoin dip to $64,000?"))
check("Allow: reach $X", not is_binary_risk("Will Bitcoin reach $78,000?"))
check("Allow: more than X°C", not is_binary_risk("Will temp increase by more than 1.29°C?"))
check("Allow: highest temp be X°C", not is_binary_risk("Will highest temp in Tokyo be 20°C?"))
check("Allow: Musk tweets", not is_binary_risk("Will Elon Musk post 280-299 tweets?"))
check("Allow: increase by X%", not is_binary_risk("Will annual inflation increase by 3.1%?"))


# ══════════════════════════════════════
# 2. calc_stake
# ══════════════════════════════════════
print("\n\033[1m2. Stake Calculation\033[0m")

from engine.entry import calc_stake

cfg = {"MAX_STAKE": 20, "MIN_STAKE": 5}
check("$1000 → $20 (5%=50, capped)", calc_stake(1000, cfg) == 20)
check("$200 → $10 (5%=10)", calc_stake(200, cfg) == 10)
check("$100 → $5 (5%=5=min)", calc_stake(100, cfg) == 5)
check("$80 → $5 (5%=4 < min, use min)", calc_stake(80, cfg) == 5)
check("$3 → $0 (stake > bankroll)", calc_stake(3, cfg) == 0)

cfg2 = {"MAX_STAKE": 50, "MIN_STAKE": 10}
check("$1000/50max → $50", calc_stake(1000, cfg2) == 50)
check("$150/10min → $10", calc_stake(150, cfg2) == 10)
check("$5/10min → $0 (can't afford)", calc_stake(5, cfg2) == 0)


# ══════════════════════════════════════
# 3. try_enter rejections
# ══════════════════════════════════════
print("\n\033[1m3. Entry Rejections\033[0m")

from engine.entry import try_enter

BASE_CONFIG = {
    "BANKROLL": 1000, "MAX_OPEN": 50, "MAX_PER_THEME": 5,
    "MAX_STAKE": 20, "MIN_STAKE": 5, "MIN_ROI": 0.02,
    "MIN_QUALITY_SCORE": 40, "SIMULATION": True, "CONFIG_TAG": "test",
    "SL_PCT": 0.07, "MAX_LOSS_PER_POS": 3.0, "MAX_PER_NEG_RISK": 3,
    "RESOLUTION_PRICE": 0.99, "MAX_SPREAD": 0.02,
}

BASE_CANDIDATE = {
    "market_id": "test123", "question": "Will X happen?", "theme": "other",
    "side": "YES", "price": 0.95, "best_ask": 0.95, "days_left": 2,
    "spread": 0.01, "quality": 80, "slug": "test", "volume": 100000,
    "liquidity": 50000, "end_date": "2026-04-15T00:00:00Z",
    "yes_token": "tok1", "no_token": "tok2", "ws_token": "tok1", "ws_side": "yes",
}

# Duplicate
db = MockDB(entry_allowed={"allowed": False, "reason": "duplicate"})
result = run(try_enter(BASE_CANDIDATE, db, MockWS(), MockTG(), BASE_CONFIG))
check("Reject: duplicate", result == "duplicate")

# Theme blocked
db = MockDB(entry_allowed={"allowed": False, "reason": "theme_blocked"})
result = run(try_enter(BASE_CANDIDATE, db, MockWS(), MockTG(), BASE_CONFIG))
check("Reject: theme_blocked", result == "theme_blocked")

# SL blacklist
db = MockDB(entry_allowed={"allowed": False, "reason": "sl_blacklist"})
result = run(try_enter(BASE_CANDIDATE, db, MockWS(), MockTG(), BASE_CONFIG))
check("Reject: sl_blacklist", result == "sl_blacklist")

# NegRisk group
db = MockDB(entry_allowed={"allowed": False, "reason": "neg_risk_group"})
result = run(try_enter(BASE_CANDIDATE, db, MockWS(), MockTG(), BASE_CONFIG))
check("Reject: neg_risk_group", result == "neg_risk_group")

# Max open
db = MockDB(open_positions=[{"theme": "x"}] * 50)
result = run(try_enter(BASE_CANDIDATE, db, MockWS(), MockTG(), BASE_CONFIG))
check("Reject: max_open", result == "max_open")

# Theme limit (non-negRisk)
db = MockDB(open_positions=[{"theme": "other"}] * 5)
result = run(try_enter(BASE_CANDIDATE, db, MockWS(), MockTG(), BASE_CONFIG))
check("Reject: theme_limit", result == "theme_limit")

# Theme limit skipped for negRisk
candidate_neg = {**BASE_CANDIDATE, "neg_risk_id": "neg123"}
db = MockDB(open_positions=[{"theme": "other"}] * 5)
result = run(try_enter(candidate_neg, db, MockWS(), MockTG(), BASE_CONFIG))
check("NegRisk skips theme_limit", result is True, f"got {result}")

# Low bankroll
db = MockDB(stats={"bankroll": 3, "wins": 0, "losses": 0, "total_pnl": 0, "total_trades": 0, "peak_equity": 0})
result = run(try_enter(BASE_CANDIDATE, db, MockWS(), MockTG(), BASE_CONFIG))
check("Reject: low_bankroll", result == "low_bankroll")

# Low ROI
candidate_low_roi = {**BASE_CANDIDATE, "best_ask": 0.995}  # ROI = 0.5%
result = run(try_enter(candidate_low_roi, MockDB(), MockWS(), MockTG(), BASE_CONFIG))
check("Reject: low_roi", result == "low_roi")

# Low quality
candidate_low_q = {**BASE_CANDIDATE, "quality": 20}
result = run(try_enter(candidate_low_q, MockDB(), MockWS(), MockTG(), BASE_CONFIG))
check("Reject: low_quality", result == "low_quality")

# Successful entry
db = MockDB()
ws = MockWS()
tg = MockTG()
result = run(try_enter(BASE_CANDIDATE, db, ws, tg, BASE_CONFIG))
check("Accept: good candidate → True", result is True)
check("Accept: position saved to DB", len(db.saved) == 1)
check("Accept: WS marked as position", len(ws.marked) == 1)
check("Accept: Telegram sent", len(tg.messages) == 1)
check("Accept: correct stake", db.saved[0]["stake_amt"] == 20)  # 5% of 1000 = 50, capped at 20
check("Accept: SL set (2d → 8%)", db.saved[0]["sl_pct"] == 0.08)


# ══════════════════════════════════════
# 4. Dynamic SL by days_left
# ══════════════════════════════════════
print("\n\033[1m4. Dynamic SL\033[0m")

for days, expected_sl in [(0.3, 0.10), (0.5, 0.10), (1.0, 0.09), (1.5, 0.08), (2.0, 0.08), (3.0, 0.07), (5.0, 0.07)]:
    candidate = {**BASE_CANDIDATE, "days_left": days}
    db = MockDB()
    run(try_enter(candidate, db, MockWS(), MockTG(), BASE_CONFIG))
    if db.saved:
        actual = db.saved[0]["sl_pct"]
        check(f"SL at {days}d = {expected_sl:.0%}", actual == expected_sl, f"got {actual}")
    else:
        check(f"SL at {days}d = {expected_sl:.0%}", False, "no position saved")


# ══════════════════════════════════════
# 4b. Dynamic Quality Threshold by days_left
# ══════════════════════════════════════
print("\n\033[1m4b. Dynamic Quality Threshold\033[0m")

# ≤1d: base Q (40) — near resolution, low bar
candidate_q50_1d = {**BASE_CANDIDATE, "days_left": 0.5, "quality": 45}
result = run(try_enter(candidate_q50_1d, MockDB(), MockWS(), MockTG(), BASE_CONFIG))
check("≤1d Q=45 → accepted (base=40)", result is True)

# 1-3d: need Q≥55
candidate_q50_2d = {**BASE_CANDIDATE, "days_left": 2.0, "quality": 50}
result = run(try_enter(candidate_q50_2d, MockDB(), MockWS(), MockTG(), BASE_CONFIG))
check("2d Q=50 → rejected (need 55)", result == "low_quality", f"got {result}")

candidate_q55_2d = {**BASE_CANDIDATE, "days_left": 2.0, "quality": 55}
result = run(try_enter(candidate_q55_2d, MockDB(), MockWS(), MockTG(), BASE_CONFIG))
check("2d Q=55 → accepted", result is True)

# 3-5d: need Q≥70
candidate_q60_4d = {**BASE_CANDIDATE, "days_left": 4.0, "quality": 60}
result = run(try_enter(candidate_q60_4d, MockDB(), MockWS(), MockTG(), BASE_CONFIG))
check("4d Q=60 → rejected (need 70)", result == "low_quality", f"got {result}")

candidate_q70_4d = {**BASE_CANDIDATE, "days_left": 4.0, "quality": 70}
result = run(try_enter(candidate_q70_4d, MockDB(), MockWS(), MockTG(), BASE_CONFIG))
check("4d Q=70 → accepted", result is True)

# 5d+: need Q≥80
candidate_q75_6d = {**BASE_CANDIDATE, "days_left": 6.0, "quality": 75}
result = run(try_enter(candidate_q75_6d, MockDB(), MockWS(), MockTG(), BASE_CONFIG))
check("6d Q=75 → rejected (need 80)", result == "low_quality", f"got {result}")

candidate_q80_6d = {**BASE_CANDIDATE, "days_left": 6.0, "quality": 80}
result = run(try_enter(candidate_q80_6d, MockDB(), MockWS(), MockTG(), BASE_CONFIG))
check("6d Q=80 → accepted", result is True)

# Config override: if MIN_QUALITY_SCORE > default tier, use config value
high_q_config = {**BASE_CONFIG, "MIN_QUALITY_SCORE": 60}
candidate_q55_05d = {**BASE_CANDIDATE, "days_left": 0.5, "quality": 55}
result = run(try_enter(candidate_q55_05d, MockDB(), MockWS(), MockTG(), high_q_config))
check("≤1d config Q=60, actual Q=55 → rejected", result == "low_quality", f"got {result}")


# ══════════════════════════════════════
# 5. Monitor: resolution detection
# ══════════════════════════════════════
print("\n\033[1m5. Resolution Detection\033[0m")

from engine.monitor import check_position_price

POS = {
    "id": "pos1", "market_id": "mkt1", "side": "YES", "question": "Test?",
    "entry_price": 0.95, "stake_amt": 20, "sl_pct": 0.07, "theme": "other",
    "end_date": "2026-04-15T00:00:00Z",
}

# WIN: bid ≥ 99¢
db = MockDB(open_positions=[POS])
pos_cache = {"mkt1_YES": POS.copy()}
pos_writes = {}
run(check_position_price("mkt1_YES", 0.99, {"best_bid": 0.99},
    db, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache, pos_writes, False))
check("Resolution WIN at 99¢", len(db.closed) == 1 and db.closed[0]["result"] == "WIN")

# LOSS: bid ≤ 1¢ — need entry price low enough to pass sanity (50% check)
POS_LOW = {**POS, "entry_price": 0.02}  # entered at 2¢, resolved to 1¢ LOSS side
db = MockDB(open_positions=[POS_LOW])
pos_cache = {"mkt1_YES": POS_LOW.copy()}
run(check_position_price("mkt1_YES", 0.01, {"best_bid": 0.01},
    db, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache, pos_writes, False))
check("Resolution LOSS at 1¢", len(db.closed) == 1 and db.closed[0]["result"] == "LOSS")

# No action: normal price
db = MockDB(open_positions=[POS])
pos_cache = {"mkt1_YES": POS.copy()}
run(check_position_price("mkt1_YES", 0.94, {"best_bid": 0.94},
    db, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache, pos_writes, False))
check("Normal price: no close", len(db.closed) == 0)

# Large drop: MAX_LOSS catches it (no sanity filter blocking)
db = MockDB(open_positions=[POS])
pos_cache = {"mkt1_YES": POS.copy()}
run(check_position_price("mkt1_YES", 0.40, {"best_bid": 0.40},
    db, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache, pos_writes, False))
check("Large drop: MAX_LOSS triggers", len(db.closed) == 1 and db.closed[0]["reason"] == "max_loss")


# ══════════════════════════════════════
# 6. Monitor: MAX_LOSS cap
# ══════════════════════════════════════
print("\n\033[1m6. MAX_LOSS Cap\033[0m")

# MAX_LOSS triggers at -$3 (entry 0.95, bid 0.80 → pnl_pct=-15.8%, $20 stake → -$3.16)
db = MockDB(open_positions=[POS])
pos_cache = {"mkt1_YES": POS.copy()}
run(check_position_price("mkt1_YES", 0.80, {"best_bid": 0.80},
    db, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache, pos_writes, False))
check("MAX_LOSS triggers at -$3+", len(db.closed) == 1 and db.closed[0]["reason"] == "max_loss")

# Just under MAX_LOSS: entry 0.95, bid 0.82 → pnl=-13.7%, -$2.74 < $3 cap → no trigger
db = MockDB(open_positions=[POS])
pos_cache = {"mkt1_YES": POS.copy()}
run(check_position_price("mkt1_YES", 0.82, {"best_bid": 0.82},
    db, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache, pos_writes, False))
# SL may trigger here (pnl=-13.7% > sl=7%) but SL needs days_to_expiry > 1 and REST cooldown
# Just verify MAX_LOSS didn't trigger (close_reason != "max_loss")
max_loss_closes = [c for c in db.closed if c.get("reason") == "max_loss"]
check("Under MAX_LOSS: no max_loss trigger", len(max_loss_closes) == 0)


# ══════════════════════════════════════
# 7. Quality score
# ══════════════════════════════════════
print("\n\033[1m7. Quality Score\033[0m")

from engine.scanner import quality_score

q1 = quality_score(0.95, 0.01, 1.0, 500000, 100000)
check(f"Q(95¢, 1d, 500k) = {q1:.0f} > 40", q1 > 40)

q2 = quality_score(0.90, 0.015, 5.0, 50000, 50000)
check(f"Q(90¢, 5d, 50k) = {q2:.0f} < Q1", q2 < q1)

q3 = quality_score(0.97, 0.005, 0.5, 1000000, 500000)
check(f"Q(97¢, 0.5d, 1M) = {q3:.0f} > 60", q3 > 60)

q4 = quality_score(0.91, 0.02, 7.0, 20000, 10000)
check(f"Q(91¢, 7d, 20k) = {q4:.0f} low", q4 < 30)


# ══════════════════════════════════════
# 8. Realistic Sim Costs (slippage + fees)
# ══════════════════════════════════════
print("\n\033[1m8. Sim Costs\033[0m")

# Entry slippage: entry_price should be best_ask + SLIPPAGE
SIM_CONFIG = {**BASE_CONFIG, "SLIPPAGE": 0.005, "FEE_PCT": 0.02}
candidate_sim = {**BASE_CANDIDATE, "best_ask": 0.94}
db = MockDB()
run(try_enter(candidate_sim, db, MockWS(), MockTG(), SIM_CONFIG))
check("Entry slippage: 94¢ + 0.5¢ = 94.5¢",
      db.saved and abs(db.saved[0]["entry_price"] - 0.945) < 0.001,
      f"got {db.saved[0]['entry_price'] if db.saved else 'none'}")

# Slippage can push ROI below MIN_ROI → rejected
candidate_edge = {**BASE_CANDIDATE, "best_ask": 0.98}  # ROI without slip: 2.04%, with slip: 1.52%
result = run(try_enter(candidate_edge, MockDB(), MockWS(), MockTG(), SIM_CONFIG))
check("Entry slippage: 98¢+0.5¢ → ROI too low → rejected", result == "low_roi", f"got {result}")

# Zero slippage = backwards compatible
ZERO_CONFIG = {**BASE_CONFIG, "SLIPPAGE": 0, "FEE_PCT": 0}
candidate_zero = {**BASE_CANDIDATE, "best_ask": 0.94}
db = MockDB()
run(try_enter(candidate_zero, db, MockWS(), MockTG(), ZERO_CONFIG))
check("Zero slippage: entry_price = best_ask",
      db.saved and db.saved[0]["entry_price"] == 0.94,
      f"got {db.saved[0]['entry_price'] if db.saved else 'none'}")

# MAX_LOSS with fee: PnL should include fee deduction
POS_SIM = {**POS, "entry_price": 0.945}  # after slippage
db = MockDB(open_positions=[POS_SIM])
pos_cache = {"mkt1_YES": POS_SIM.copy()}
run(check_position_price("mkt1_YES", 0.80, {"best_bid": 0.80},
    db, MockWS(), MockTG(), SIM_CONFIG, None, pos_cache, {}, False))
if db.closed:
    # PnL without costs: (0.80 - 0.945) / 0.945 * 20 = -3.07
    # Costs: slippage_exit = 0.005 * 20 / 0.945 = 0.106 + fee = 0.02 * 20 = 0.40
    # Total PnL ≈ -3.07 - 0.51 = -3.58
    pnl = db.closed[0]["pnl"]
    check(f"MAX_LOSS with fees: PnL={pnl:.2f} < -3.0 (includes costs)", pnl < -3.0, f"pnl={pnl:.2f}")
    # Without fees it would be -3.07, with fees it should be worse
    check(f"MAX_LOSS fee impact: PnL={pnl:.2f} < -3.5", pnl < -3.5, f"pnl={pnl:.2f}")
else:
    check("MAX_LOSS with fees: position closed", False, "not closed")


# ══════════════════════════════════════
# 9. Shared Utilities
# ══════════════════════════════════════
print("\n\033[1m9. Shared Utilities\033[0m")

from engine.shared import calc_days_left, parse_outcome_prices, calc_exit_fee

# calc_days_left
check("calc_days_left: future → >0", calc_days_left("2027-01-01T00:00:00Z") > 0)
check("calc_days_left: past → 0", calc_days_left("2020-01-01T00:00:00Z") == 0)
check("calc_days_left: None → 999", calc_days_left(None) == 999)
check("calc_days_left: None → custom fallback", calc_days_left(None, fallback=5.0) == 5.0)
check("calc_days_left: invalid → fallback", calc_days_left("not-a-date") == 999)
check("calc_days_left: Z suffix handled", calc_days_left("2027-06-01T00:00:00Z") > 0)

# parse_outcome_prices
y, n = parse_outcome_prices({"outcomePrices": '["0.95","0.05"]'})
check("parse_prices: dict/str", abs(y - 0.95) < 0.01 and abs(n - 0.05) < 0.01)
y, n = parse_outcome_prices({"outcomePrices": [0.92, 0.08]})
check("parse_prices: dict/list", abs(y - 0.92) < 0.01 and abs(n - 0.08) < 0.01)
y, n = parse_outcome_prices({"yes_price": 0.90, "no_price": 0.10})
check("parse_prices: fallback yes/no", abs(y - 0.90) < 0.01 and abs(n - 0.10) < 0.01)
y, n = parse_outcome_prices({"outcomePrices": [0.75]})
check("parse_prices: single → no=1-yes", abs(n - 0.25) < 0.01)
y, n = parse_outcome_prices({})
check("parse_prices: empty → 0,0", y == 0 and n == 0)
y, n = parse_outcome_prices(None)
check("parse_prices: None → 0,0", y == 0 and n == 0)

# calc_exit_fee
fee = calc_exit_fee(20.0, 0.95, {"SLIPPAGE": 0.005, "FEE_PCT": 0.02})
check(f"calc_exit_fee: ~0.51 (got {fee:.3f})", 0.50 < fee < 0.52)
check("calc_exit_fee: zero config → 0", calc_exit_fee(20.0, 0.95, {}) == 0)
fee_z = calc_exit_fee(20.0, 0.0, {"SLIPPAGE": 0.005, "FEE_PCT": 0.02})
check("calc_exit_fee: zero entry → fee only", abs(fee_z - 0.40) < 0.01)


# ══════════════════════════════════════
# 10. SL / Rapid Drop Behavior
# ══════════════════════════════════════
print("\n\033[1m10. SL & Rapid Drop\033[0m")

from engine.monitor import _rest_cooldown
from datetime import datetime, timezone, timedelta

# -- SL disabled near expiry (≤1 day) --
near_end = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
POS_NEAR = {**POS, "end_date": near_end, "sl_pct": 0.07}
db = MockDB(open_positions=[POS_NEAR])
pos_cache = {"mkt1_YES": POS_NEAR.copy()}
_rest_cooldown.clear()
# bid=0.85 → pnl=-10.5% > SL=7%, but ≤1 day → disabled. MAX_LOSS: -$2.11 < $3 cap → no trigger
run(check_position_price("mkt1_YES", 0.85, {"best_bid": 0.85},
    db, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache, {}, False))
check("SL disabled ≤1d: no close", len(db.closed) == 0)

# -- SL fires when >1 day to expiry --
far_end = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
POS_FAR = {**POS, "end_date": far_end, "sl_pct": 0.07}
db = MockDB(open_positions=[POS_FAR])
pos_cache = {"mkt1_YES": POS_FAR.copy()}
_rest_cooldown.clear()
# bid=0.85 → pnl=-10.5% > SL=7%, >1 day → fires
run(check_position_price("mkt1_YES", 0.85, {"best_bid": 0.85},
    db, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache, {}, False))
check("SL fires >1d", len(db.closed) == 1 and db.closed[0]["reason"] == "stop_loss")

# -- SL with fees --
POS_FAR_SIM = {**POS, "end_date": far_end, "sl_pct": 0.07, "entry_price": 0.945}
db = MockDB(open_positions=[POS_FAR_SIM])
pos_cache = {"mkt1_YES": POS_FAR_SIM.copy()}
_rest_cooldown.clear()
SIM_C = {**BASE_CONFIG, "SLIPPAGE": 0.005, "FEE_PCT": 0.02}
run(check_position_price("mkt1_YES", 0.85, {"best_bid": 0.85},
    db, MockWS(), MockTG(), SIM_C, None, pos_cache, {}, False))
if db.closed:
    sl_pnl = db.closed[0]["pnl"]
    raw_pnl = (0.85 - 0.945) / 0.945 * 20  # -2.01
    check(f"SL with fees: PnL={sl_pnl:.2f} < raw {raw_pnl:.2f} (fees deducted)", sl_pnl < raw_pnl)
else:
    check("SL with fees: position closed", False, "not closed")

# -- Rapid drop triggers when >1 day --
POS_RD = {**POS, "end_date": far_end, "sl_pct": 0.20}  # high SL so only rapid_drop triggers
db = MockDB(open_positions=[POS_RD])
pos_cache = {"mkt1_YES": POS_RD.copy()}
_rest_cooldown.clear()
# bid=0.87 → drop = 0.95 - 0.87 = 8¢ > 7¢ default, pnl=-8.4% < SL=20%
run(check_position_price("mkt1_YES", 0.87, {"best_bid": 0.87},
    db, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache, {}, False))
check("Rapid drop fires >1d (8¢ drop)", len(db.closed) == 1 and db.closed[0]["reason"] == "rapid_drop")

# -- Rapid drop disabled near expiry --
POS_RD_NEAR = {**POS, "end_date": near_end, "sl_pct": 0.20}
db = MockDB(open_positions=[POS_RD_NEAR])
pos_cache = {"mkt1_YES": POS_RD_NEAR.copy()}
_rest_cooldown.clear()
run(check_position_price("mkt1_YES", 0.87, {"best_bid": 0.87},
    db, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache, {}, False))
check("Rapid drop disabled ≤1d: no close", len(db.closed) == 0)

# -- RAPID_DROP_PCT from config --
CUSTOM_RD = {**BASE_CONFIG, "RAPID_DROP_PCT": 0.10}  # 10¢ instead of 7¢
POS_RD2 = {**POS, "end_date": far_end, "sl_pct": 0.20}
db = MockDB(open_positions=[POS_RD2])
pos_cache = {"mkt1_YES": POS_RD2.copy()}
_rest_cooldown.clear()
# 8¢ drop < 10¢ threshold → no trigger
run(check_position_price("mkt1_YES", 0.87, {"best_bid": 0.87},
    db, MockWS(), MockTG(), CUSTOM_RD, None, pos_cache, {}, False))
check("RAPID_DROP_PCT=10¢: 8¢ drop → no close", len(db.closed) == 0)

# 11¢ drop > 10¢ threshold → triggers
db = MockDB(open_positions=[POS_RD2])
pos_cache = {"mkt1_YES": POS_RD2.copy()}
_rest_cooldown.clear()
run(check_position_price("mkt1_YES", 0.84, {"best_bid": 0.84},
    db, MockWS(), MockTG(), CUSTOM_RD, None, pos_cache, {}, False))
check("RAPID_DROP_PCT=10¢: 11¢ drop → triggers", len(db.closed) == 1 and db.closed[0]["reason"] == "rapid_drop")

# -- SL priority over rapid drop: both triggered → SL fires --
POS_BOTH = {**POS, "end_date": far_end, "sl_pct": 0.07}
db = MockDB(open_positions=[POS_BOTH])
pos_cache = {"mkt1_YES": POS_BOTH.copy()}
_rest_cooldown.clear()
# bid=0.85 → pnl=-10.5%>SL=7%, drop=10¢>7¢ — both triggered, SL wins
run(check_position_price("mkt1_YES", 0.85, {"best_bid": 0.85},
    db, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache, {}, False))
check("SL priority: both triggered → stop_loss",
      len(db.closed) == 1 and db.closed[0]["reason"] == "stop_loss")


# ══════════════════════════════════════
# 11. Shutdown stops callbacks
# ══════════════════════════════════════
print("\n\033[1m11. Shutdown Guard\033[0m")

db = MockDB(open_positions=[POS])
pos_cache = {"mkt1_YES": POS.copy()}
run(check_position_price("mkt1_YES", 0.99, {"best_bid": 0.99},
    db, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache, pos_writes, True))  # shutdown=True
check("Shutdown: no action on position", len(db.closed) == 0)


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
