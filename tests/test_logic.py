"""
Unit tests for core micro logic.
Run: python tests/test_logic.py
No external deps — mocks DB/WS/Telegram, tests pure logic.
"""
import sys, os, asyncio, time

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

    async def close_position(self, pos_id, pnl, result, reason, exit_price=None):
        self.closed.append({"id": pos_id, "pnl": pnl, "result": result, "reason": reason, "exit_price": exit_price})
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

    async def record_price_tick(self, market_id, side, price, source="ws"):
        pass  # no-op in tests — price history not verified at DB level


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

# Should BLOCK — crypto price-target markets (flash-crash risk)
# "dip to" was previously allowed; production data showed all 6 BTC dip-to markets
# hit max_loss (-$25+ in 30d) — same flash-move risk profile as "drop to" / "below".
check("Block: dip to $X", is_binary_risk("Will Bitcoin dip to $64,000?"))
check("Block: dips to $X (plural)", is_binary_risk("Will BTC dips to $70k?"))
check("Block: ethereum dip to $X", is_binary_risk("Will Ethereum dip to $2,250?"))
check("Block: crypto above $X", is_binary_risk("Will Bitcoin be above $72,000?"))
check("Block: btc reach $X", is_binary_risk("Will Bitcoin reach $78,000?"))
check("Block: eth above $X", is_binary_risk("Will Ethereum exceed $3,500?"))

# Should ALLOW
check("Block: end in a draw", is_binary_risk("Will the match end in a draw?"))
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
    "MAX_LOSS_PER_POS": 3.0, "MAX_PER_NEG_RISK": 3,
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

import unittest.mock as _mock

def _rest_confirms(price, vol=100000.0):
    """Factory: REST confirms the given price (accepting_orders=True)."""
    async def _mock_fn(*a, **kw):
        return price, vol, True
    return _mock_fn

from engine.monitor import check_position_price

POS = {
    "id": "pos1", "market_id": "mkt1", "side": "YES", "question": "Test?",
    "entry_price": 0.95, "stake_amt": 20, "theme": "other",
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
with _mock.patch("engine.monitor._verify_price_and_volume", _rest_confirms(0.01)):
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
with _mock.patch("engine.monitor._verify_price_and_volume", _rest_confirms(0.40)):
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
with _mock.patch("engine.monitor._verify_price_and_volume", _rest_confirms(0.80)):
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

# Sweet spot 93-96¢ should score highest
q_sweet = quality_score(0.94, 0.005, 0.5, 1000000, 500000)
q_high = quality_score(0.99, 0.005, 0.5, 1000000, 500000)
q_low = quality_score(0.90, 0.005, 0.5, 1000000, 500000)
check(f"Sweet spot 94¢ ({q_sweet:.0f}) > 99¢ ({q_high:.0f})", q_sweet > q_high)
check(f"Sweet spot 94¢ ({q_sweet:.0f}) > 90¢ ({q_low:.0f})", q_sweet > q_low)

# High price (98¢+) should be penalized — thin ROI after fees
q_98 = quality_score(0.98, 0.005, 0.5, 1000000, 500000)
q_95 = quality_score(0.95, 0.005, 0.5, 1000000, 500000)
check(f"98¢ ({q_98:.0f}) penalized vs 95¢ ({q_95:.0f})", q_98 < q_95)

# 99¢ should score particularly low (no ROI after fees)
check(f"99¢ scores low ({q_high:.0f} < 60)", q_high < 60)

# Near expiry (≤0.5d) gets max bonus
q_near = quality_score(0.94, 0.01, 0.3, 500000, 100000)
q_far = quality_score(0.94, 0.01, 5.0, 500000, 100000)
check(f"≤0.5d ({q_near:.0f}) > 5d ({q_far:.0f})", q_near > q_far)


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
with _mock.patch("engine.monitor._verify_price_and_volume", _rest_confirms(0.80)):
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
# 10. Rapid Drop (SL disabled — only MAX_LOSS + rapid drop)
# ══════════════════════════════════════
print("\n\033[1m10. Rapid Drop\033[0m")

from engine.monitor import _rest_cooldown
from datetime import datetime, timezone, timedelta

far_end = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
near_end = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()

# -- No SL: moderate drop without rapid drop threshold → no close --
POS_MOD = {**POS, "end_date": far_end}
db = MockDB(open_positions=[POS_MOD])
pos_cache = {"mkt1_YES": POS_MOD.copy()}
_rest_cooldown.clear()
# bid=0.90 → drop 5¢ < 7¢ rapid_drop threshold, pnl=-5.3% > SL=7% (but SL disabled)
run(check_position_price("mkt1_YES", 0.90, {"best_bid": 0.90},
    db, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache, {}, False))
check("No SL: 5¢ drop → no close (below rapid_drop threshold)", len(db.closed) == 0)

# -- Rapid drop triggers (>7¢ default) --
POS_RD = {**POS, "end_date": far_end}
db = MockDB(open_positions=[POS_RD])
pos_cache = {"mkt1_YES": POS_RD.copy()}
_rest_cooldown.clear()
# bid=0.87 → drop = 0.95 - 0.87 = 8¢ > 7¢
with _mock.patch("engine.monitor._verify_price_and_volume", _rest_confirms(0.87)):
    run(check_position_price("mkt1_YES", 0.87, {"best_bid": 0.87},
        db, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache, {}, False))
check("Rapid drop fires (8¢ drop)", len(db.closed) == 1 and db.closed[0]["reason"] == "rapid_drop")

# -- Rapid drop works near expiry too --
POS_RD_NEAR = {**POS, "end_date": near_end}
db = MockDB(open_positions=[POS_RD_NEAR])
pos_cache = {"mkt1_YES": POS_RD_NEAR.copy()}
_rest_cooldown.clear()
with _mock.patch("engine.monitor._verify_price_and_volume", _rest_confirms(0.87)):
    run(check_position_price("mkt1_YES", 0.87, {"best_bid": 0.87},
        db, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache, {}, False))
check("Rapid drop works ≤1d (no SL disable)", len(db.closed) == 1 and db.closed[0]["reason"] == "rapid_drop")

# -- RAPID_DROP_PCT from config --
CUSTOM_RD = {**BASE_CONFIG, "RAPID_DROP_PCT": 0.10}  # 10¢ instead of 7¢
db = MockDB(open_positions=[{**POS, "end_date": far_end}])
pos_cache = {"mkt1_YES": {**POS, "end_date": far_end}}
_rest_cooldown.clear()
# 8¢ drop < 10¢ threshold → no trigger
run(check_position_price("mkt1_YES", 0.87, {"best_bid": 0.87},
    db, MockWS(), MockTG(), CUSTOM_RD, None, pos_cache, {}, False))
check("RAPID_DROP_PCT=10¢: 8¢ drop → no close", len(db.closed) == 0)

# 11¢ drop > 10¢ threshold → triggers
db = MockDB(open_positions=[{**POS, "end_date": far_end}])
pos_cache = {"mkt1_YES": {**POS, "end_date": far_end}}
_rest_cooldown.clear()
with _mock.patch("engine.monitor._verify_price_and_volume", _rest_confirms(0.84)):
    run(check_position_price("mkt1_YES", 0.84, {"best_bid": 0.84},
        db, MockWS(), MockTG(), CUSTOM_RD, None, pos_cache, {}, False))
check("RAPID_DROP_PCT=10¢: 11¢ drop → triggers", len(db.closed) == 1 and db.closed[0]["reason"] == "rapid_drop")

# -- Rapid drop with fees --
POS_RD_SIM = {**POS, "end_date": far_end, "entry_price": 0.945}
db = MockDB(open_positions=[POS_RD_SIM])
pos_cache = {"mkt1_YES": POS_RD_SIM.copy()}
_rest_cooldown.clear()
SIM_C = {**BASE_CONFIG, "SLIPPAGE": 0.005, "FEE_PCT": 0.02}
with _mock.patch("engine.monitor._verify_price_and_volume", _rest_confirms(0.85)):
    run(check_position_price("mkt1_YES", 0.85, {"best_bid": 0.85},
        db, MockWS(), MockTG(), SIM_C, None, pos_cache, {}, False))
if db.closed:
    rd_pnl = db.closed[0]["pnl"]
    raw_pnl = (0.85 - 0.945) / 0.945 * 20
    check(f"Rapid drop with fees: PnL={rd_pnl:.2f} < raw {raw_pnl:.2f}", rd_pnl < raw_pnl)
else:
    check("Rapid drop with fees: position closed", False, "not closed")


# ══════════════════════════════════════
# 11. Shutdown stops callbacks
# ══════════════════════════════════════
print("\n\033[1m11. Shutdown Guard\033[0m")

db = MockDB(open_positions=[POS])
pos_cache = {"mkt1_YES": POS.copy()}
run(check_position_price("mkt1_YES", 0.99, {"best_bid": 0.99},
    db, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache, pos_writes, True))  # shutdown=True
check("Shutdown: no action on position", len(db.closed) == 0)


# ══════════════════════════════════════
# 12. Binary risk: game/map winner markets
# ══════════════════════════════════════
print("\n\033[1m12. Binary Risk: Game/Map Winner\033[0m")

from engine.scanner import is_binary_risk

check("Block: Game 1 Winner", is_binary_risk("LoL: Sentinels vs Cloud9 - Game 1 Winner"))
check("Block: Game 2 Winner", is_binary_risk("LoL: Sentinels vs Cloud9 - Game 2 Winner"))
check("Block: Game 3 Winner", is_binary_risk("Counter-Strike: Astralis vs FUT - Game 3 Winner"))
check("Block: Map 1 Winner",  is_binary_risk("Counter-Strike: Astralis vs FUT Esports - Map 1 Winner"))
check("Block: Map 2 Winner",  is_binary_risk("Valorant: FURIA vs NRG - Map 2 Winner"))
check("Block: Map 3 Winner",  is_binary_risk("Valorant: FURIA vs NRG - Map 3 Winner"))
check("Block: case-insensitive", is_binary_risk("LoL: T1 vs GEN - game 2 winner"))
check("Allow: BO3 series",    not is_binary_risk("LoL: Sentinels vs Cloud9 (BO3) - LCS Regular Season"))
check("Allow: BO5 series",    not is_binary_risk("LoL: T1 vs GEN (BO5) - Worlds Final"))
check("Allow: Map Handicap",  not is_binary_risk("Map Handicap: AUR (-1.5) vs HOTU (+1.5)"))
check("Allow: Map winner (no number)", not is_binary_risk("Who wins the map vote?"))


# ══════════════════════════════════════
# 13. Rapid drop block counter
# ══════════════════════════════════════
print("\n\033[1m13. Rapid Drop Block Counter\033[0m")

import engine.monitor as _mon
from engine.monitor import check_position_price, _rest_cooldown, _rapid_drop_blocks, _max_loss_blocks

far_end_13 = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
POS_13 = {
    "id": "pos13", "market_id": "mkt13", "side": "YES", "question": "BTC $74k?",
    "entry_price": 0.945, "stake_amt": 20, "theme": "crypto",
    "end_date": far_end_13,
}


class MockDBWithRestPrice(MockDB):
    """DB mock that tracks price ticks and supports configurable REST price."""
    def __init__(self, open_positions=None, rest_price=None):
        super().__init__(open_positions=open_positions)
        self.rest_price = rest_price  # what _verify_price_and_volume will return
        self.price_ticks = []

    async def record_price_tick(self, market_id, side, price, source="ws"):
        self.price_ticks.append({"market_id": market_id, "side": side, "price": price, "source": source})


# Patch _verify_price_and_volume to return controlled REST price
import unittest.mock as mock

async def _rest_above_threshold(*a, **kw):
    return 0.90, 100000.0, True  # above threshold (entry=0.945, threshold=0.875)

async def _rest_none(*a, **kw):
    return None, None, True

# Test 1: REST blocks rapid drop → block counter increments
_rest_cooldown.clear()
_rapid_drop_blocks.clear()
with mock.patch("engine.monitor._verify_price_and_volume", _rest_above_threshold):
    db = MockDBWithRestPrice(open_positions=[POS_13])
    pos_cache = {"mkt13_YES": POS_13.copy()}
    # bid=0.86 → drop 8.5¢ > 7¢ threshold → triggers rapid drop check
    run(check_position_price("mkt13_YES", 0.86, {"best_bid": 0.86},
        db, MockWS(), MockTG(), BASE_CONFIG, object(), pos_cache, {}, False))
    check("Block #1: REST above threshold → no close", len(db.closed) == 0)
    check("Block #1: counter = 1", _rapid_drop_blocks.get("mkt13_YES", 0) == 1)

# Test 2: second block → counter = 2
_rest_cooldown.clear()
with mock.patch("engine.monitor._verify_price_and_volume", _rest_above_threshold):
    db = MockDBWithRestPrice(open_positions=[POS_13])
    pos_cache = {"mkt13_YES": POS_13.copy()}
    run(check_position_price("mkt13_YES", 0.86, {"best_bid": 0.86},
        db, MockWS(), MockTG(), BASE_CONFIG, object(), pos_cache, {}, False))
    check("Block #2: counter = 2", _rapid_drop_blocks.get("mkt13_YES", 0) == 2)

# Test 3: third block → counter = 3
_rest_cooldown.clear()
with mock.patch("engine.monitor._verify_price_and_volume", _rest_above_threshold):
    db = MockDBWithRestPrice(open_positions=[POS_13])
    pos_cache = {"mkt13_YES": POS_13.copy()}
    run(check_position_price("mkt13_YES", 0.86, {"best_bid": 0.86},
        db, MockWS(), MockTG(), BASE_CONFIG, object(), pos_cache, {}, False))
    check("Block #3: counter = 3", _rapid_drop_blocks.get("mkt13_YES", 0) == 3)

# Test 4: 4th attempt → bypass REST (blocks≥3), use WS bid → closes
_rest_cooldown.clear()
with mock.patch("engine.monitor._verify_price_and_volume", _rest_above_threshold):
    db = MockDBWithRestPrice(open_positions=[POS_13])
    pos_cache = {"mkt13_YES": POS_13.copy()}
    run(check_position_price("mkt13_YES", 0.86, {"best_bid": 0.86},
        db, MockWS(), MockTG(), BASE_CONFIG, object(), pos_cache, {}, False))
    check("Bypass: closes after 3 blocks", len(db.closed) == 1)
    check("Bypass: reason=rapid_drop", db.closed[0]["reason"] == "rapid_drop")

# Test 5: price recovery resets block counter
_rest_cooldown.clear()
_rapid_drop_blocks["mkt13_YES"] = 2  # simulate 2 prior blocks
db = MockDBWithRestPrice(open_positions=[POS_13])
pos_cache = {"mkt13_YES": POS_13.copy()}
# price recovers above threshold (entry=0.945, threshold=0.875, bid=0.90 > 0.875)
run(check_position_price("mkt13_YES", 0.90, {"best_bid": 0.90},
    db, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache, {}, False))
check("Recovery: counter reset to 0", _rapid_drop_blocks.get("mkt13_YES", 0) == 0)
check("Recovery: no close", len(db.closed) == 0)

# Test 6: counter cleared on position close
_rest_cooldown.clear()
_rapid_drop_blocks.clear()
_rapid_drop_blocks["mkt13_YES"] = 3
db = MockDBWithRestPrice(open_positions=[POS_13])
pos_cache = {"mkt13_YES": POS_13.copy()}
# WIN resolution clears block counter
run(check_position_price("mkt13_YES", 0.995, {"best_bid": 0.995},
    db, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache, {}, False))
check("Close (WIN): block counter cleared", "mkt13_YES" not in _rapid_drop_blocks)

# Test 7: accepting_orders=False → MAX_LOSS paused (maintenance protection)
_rest_cooldown.clear()
_rapid_drop_blocks.clear()
_max_loss_blocks.clear()
async def _rest_paused(*a, **kw):
    return 0.80, 100000.0, False  # price confirms loss BUT market is paused
db = MockDBWithRestPrice(open_positions=[POS_13])
pos_cache = {"mkt13_YES": POS_13.copy()}
# bid=0.80 → max_loss triggers, but accepting_orders=False → must NOT close
with mock.patch("engine.monitor._verify_price_and_volume", _rest_paused):
    run(check_position_price("mkt13_YES", 0.80, {"best_bid": 0.80},
        db, MockWS(), MockTG(), BASE_CONFIG, object(), pos_cache, {}, False))
check("Maintenance: MAX_LOSS not fired when accepting_orders=False", len(db.closed) == 0)

# Test 8: resolved_loss paused when accepting_orders=False
_rest_cooldown.clear()
async def _rest_zero_paused(*a, **kw):
    return 0.01, 100000.0, False  # price=1¢ AND market paused
db = MockDBWithRestPrice(open_positions=[{**POS_13, "entry_price": 0.02}])
pos_cache = {"mkt13_YES": {**POS_13, "entry_price": 0.02}}
with mock.patch("engine.monitor._verify_price_and_volume", _rest_zero_paused):
    run(check_position_price("mkt13_YES", 0.01, {"best_bid": 0.01},
        db, MockWS(), MockTG(), BASE_CONFIG, object(), pos_cache, {}, False))
check("Maintenance: resolved_loss not fired when accepting_orders=False", len(db.closed) == 0)


# ══════════════════════════════════════
# 14. rest_poll_stale_positions
# ══════════════════════════════════════
print("\n\033[1m14. REST Poll Stale Positions\033[0m")

from engine.monitor import rest_poll_stale_positions

OPEN_POS_14 = [
    {"id": "p1", "market_id": "mkt14a", "side": "NO", "question": "BTC $74k?",
     "entry_price": 0.945, "stake_amt": 20, "theme": "crypto",
     "end_date": far_end_13},
    {"id": "p2", "market_id": "mkt14b", "side": "YES", "question": "ETH $2k?",
     "entry_price": 0.92, "stake_amt": 20, "theme": "crypto",
     "end_date": far_end_13},
]

# Fresh positions (last_update recent) → NOT polled
ws_fresh = MockWS()
ws_fresh.prices = {
    "mkt14a_NO":  {"last_update": time.time(), "price": 0.93, "best_bid": 0.93},
    "mkt14b_YES": {"last_update": time.time(), "price": 0.93, "best_bid": 0.93},
}
db = MockDBWithRestPrice(open_positions=OPEN_POS_14)
_rest_cooldown.clear()
async def _rest_fresh(*a, **kw): return 0.93, 100000.0, True
with mock.patch("engine.monitor._verify_price_and_volume", _rest_fresh):
    run(rest_poll_stale_positions(OPEN_POS_14, db=db, ws=ws_fresh, tg=MockTG(),
        config=BASE_CONFIG, http_client=object(),
        pos_cache={}, pos_last_db_write={}, shutdown=False))
check("Fresh positions: not polled (no close)", len(db.closed) == 0)

# Stale position (last_update old) → polled → resolution WIN
ws_stale = MockWS()
ws_stale.prices = {
    "mkt14a_NO":  {"last_update": time.time() - 400, "price": 0.93, "best_bid": 0.93},
    "mkt14b_YES": {"last_update": time.time(), "price": 0.93, "best_bid": 0.93},
}
db = MockDBWithRestPrice(open_positions=OPEN_POS_14)
_rest_cooldown.clear()
async def _rest_win(*a, **kw): return 0.995, 100000.0, True  # WIN price
with mock.patch("engine.monitor._verify_price_and_volume", _rest_win):
    run(rest_poll_stale_positions(OPEN_POS_14, db=db, ws=ws_stale, tg=MockTG(),
        config=BASE_CONFIG, http_client=object(),
        pos_cache={}, pos_last_db_write={}, shutdown=False))
check("Stale position: polled and WIN-resolved", len(db.closed) == 1)
check("Stale position: price tick recorded (rest_poll)", any(
    t["source"] == "rest_poll" for t in db.price_ticks))

# Shutdown flag → nothing happens
ws_stale2 = MockWS()
ws_stale2.prices = {"mkt14a_NO": {"last_update": time.time() - 400}}
db = MockDBWithRestPrice(open_positions=OPEN_POS_14)
run(rest_poll_stale_positions(OPEN_POS_14, db=db, ws=ws_stale2, tg=MockTG(),
    config=BASE_CONFIG, http_client=object(),
    pos_cache={}, pos_last_db_write={}, shutdown=True))
check("Shutdown: stale position not polled", len(db.closed) == 0)

# REST returns None → no action
ws_stale3 = MockWS()
ws_stale3.prices = {"mkt14a_NO": {"last_update": time.time() - 400, "price": 0.93, "best_bid": 0.93}}
db = MockDBWithRestPrice(open_positions=OPEN_POS_14)
_rest_cooldown.clear()
with mock.patch("engine.monitor._verify_price_and_volume", _rest_none):
    run(rest_poll_stale_positions(OPEN_POS_14, db=db, ws=ws_stale3, tg=MockTG(),
        config=BASE_CONFIG, http_client=object(),
        pos_cache={}, pos_last_db_write={}, shutdown=False))
check("REST returns None: no close", len(db.closed) == 0)


# ══════════════════════════════════════
# 15. Price history recording throttle
# ══════════════════════════════════════
print("\n\033[1m15. Price History Recording\033[0m")

import engine.monitor as _mon_mod

_rest_cooldown.clear()
_rapid_drop_blocks.clear()
_mon_mod._price_last_recorded.clear()

class MockDBTicks(MockDB):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.ticks = []
    async def record_price_tick(self, market_id, side, price, source="ws"):
        self.ticks.append((price, source))

POS_15 = {
    "id": "pos15", "market_id": "mkt15", "side": "YES", "question": "Test?",
    "entry_price": 0.95, "stake_amt": 20, "theme": "other",
    "end_date": far_end_13,
}

# First call → always recorded
db = MockDBTicks(open_positions=[POS_15])
pos_cache = {"mkt15_YES": POS_15.copy()}
run(check_position_price("mkt15_YES", 0.95, {"best_bid": 0.95},
    db, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache, {}, False))
check("First tick: recorded", len(db.ticks) == 1)

# Same price immediately after → NOT recorded (< 0.3¢ change, < 30s)
db2 = MockDBTicks(open_positions=[POS_15])
pos_cache2 = {"mkt15_YES": POS_15.copy()}
run(check_position_price("mkt15_YES", 0.951, {"best_bid": 0.951},
    db2, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache2, {}, False))
check("Tiny move (0.1¢): NOT recorded", len(db2.ticks) == 0)

# Price moves ≥0.3¢ → recorded regardless of time
db3 = MockDBTicks(open_positions=[POS_15])
pos_cache3 = {"mkt15_YES": POS_15.copy()}
run(check_position_price("mkt15_YES", 0.947, {"best_bid": 0.947},
    db3, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache3, {}, False))
check("Move ≥0.3¢ (0.3¢): recorded", len(db3.ticks) == 1)

# Simulate ≥30s elapsed → recorded even for tiny move
_mon_mod._price_last_recorded["mkt15_YES"] = (0.95, time.time() - 35)
db4 = MockDBTicks(open_positions=[POS_15])
pos_cache4 = {"mkt15_YES": POS_15.copy()}
run(check_position_price("mkt15_YES", 0.951, {"best_bid": 0.951},
    db4, MockWS(), MockTG(), BASE_CONFIG, None, pos_cache4, {}, False))
check("≥30s elapsed: recorded despite tiny move", len(db4.ticks) == 1)

_mon_mod._price_last_recorded.clear()


# ══════════════════════════════════════
# 16. Early Take-Profit
# ══════════════════════════════════════
print("\n\033[1m16. Early Take-Profit\033[0m")

import unittest.mock as mock16
from engine.monitor import check_position_price as _cpp
from engine.monitor import _rest_cooldown as _rc16, _rapid_drop_blocks as _rdb16

_rc16.clear()
_rdb16.clear()
_mon_mod._price_last_recorded.clear()

# end_date far in future (3 days out) for TP tests
import datetime as _dt
_far_end_16 = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=3)).isoformat()
_near_end_16 = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=12)).isoformat()

POS_16 = {
    "id": "pos16a", "market_id": "mkt16a", "side": "YES", "question": "TP test?",
    "entry_price": 0.94, "stake_amt": 20, "theme": "other",
    "end_date": _far_end_16,
}

TP_CONFIG = {**BASE_CONFIG, "TAKE_PROFIT_PRICE": 0.98, "TAKE_PROFIT_MIN_DAYS": 1.0,
             "SLIPPAGE": 0.005, "FEE_PCT": 0.02}

async def _rest_tp_confirms(*a, **kw):
    return 0.981, 100000.0, True

async def _rest_tp_below(*a, **kw):
    return 0.972, 100000.0, True  # below TP threshold → spike, don't sell

async def _rest_tp_none(*a, **kw):
    return None, None, True

# 1. Price at 98.1¢, 3 days left, REST confirms → TP triggered
_rc16.clear()
db = MockDB(open_positions=[POS_16])
pos_cache = {"mkt16a_YES": POS_16.copy()}
with mock16.patch("engine.monitor._verify_price_and_volume", _rest_tp_confirms):
    run(_cpp("mkt16a_YES", 0.981, {"best_bid": 0.981},
             db, MockWS(), MockTG(), TP_CONFIG, object(), pos_cache, {}, False))
check("TP: triggered at 98.1¢ with 3d left", len(db.closed) == 1)
check("TP: reason is take_profit", db.closed[0]["reason"] == "take_profit")
check("TP: result is WIN", db.closed[0]["result"] == "WIN")
check("TP: PnL positive", db.closed[0]["pnl"] > 0)

# 2. Price at 98¢, only 12h left → NOT triggered (too close, just wait for resolution)
_rc16.clear()
_mon_mod._price_last_recorded.clear()
POS_16b = {**POS_16, "id": "pos16b", "market_id": "mkt16b", "end_date": _near_end_16}
db = MockDB(open_positions=[POS_16b])
pos_cache = {"mkt16b_YES": POS_16b.copy()}
with mock16.patch("engine.monitor._verify_price_and_volume", _rest_tp_confirms):
    run(_cpp("mkt16b_YES", 0.981, {"best_bid": 0.981},
             db, MockWS(), MockTG(), TP_CONFIG, object(), pos_cache, {}, False))
check("TP: NOT triggered with only 12h left", len(db.closed) == 0)

# 3. Price at 98¢, REST returns 97.2¢ (spike) → NOT triggered
_rc16.clear()
_mon_mod._price_last_recorded.clear()
db = MockDB(open_positions=[POS_16])
pos_cache = {"mkt16a_YES": POS_16.copy()}
with mock16.patch("engine.monitor._verify_price_and_volume", _rest_tp_below):
    run(_cpp("mkt16a_YES", 0.981, {"best_bid": 0.981},
             db, MockWS(), MockTG(), TP_CONFIG, object(), pos_cache, {}, False))
check("TP: NOT triggered when REST < TP threshold (spike)", len(db.closed) == 0)

# 4. Price at 97¢ (below TP threshold) → NOT triggered
_rc16.clear()
_mon_mod._price_last_recorded.clear()
db = MockDB(open_positions=[POS_16])
pos_cache = {"mkt16a_YES": POS_16.copy()}
with mock16.patch("engine.monitor._verify_price_and_volume", _rest_tp_confirms):
    run(_cpp("mkt16a_YES", 0.97, {"best_bid": 0.97},
             db, MockWS(), MockTG(), TP_CONFIG, object(), pos_cache, {}, False))
check("TP: NOT triggered at 97¢ (below threshold)", len(db.closed) == 0)

# 5. REST cooldown: second call within cooldown window → REST not called again
_rc16.clear()
_mon_mod._price_last_recorded.clear()
rest_call_count = 0
async def _rest_tp_counting(*a, **kw):
    global rest_call_count
    rest_call_count += 1
    return 0.982, 100000.0, True

POS_16c = {**POS_16, "id": "pos16c", "market_id": "mkt16c", "end_date": _far_end_16}
db = MockDB(open_positions=[POS_16c])
pos_cache = {"mkt16c_YES": POS_16c.copy()}
# First call → REST called, TP fires
rest_call_count = 0
with mock16.patch("engine.monitor._verify_price_and_volume", _rest_tp_counting):
    run(_cpp("mkt16c_YES", 0.982, {"best_bid": 0.982},
             db, MockWS(), MockTG(), TP_CONFIG, object(), pos_cache, {}, False))
first_calls = rest_call_count
# Second call within cooldown → REST NOT called (position already closed anyway)
with mock16.patch("engine.monitor._verify_price_and_volume", _rest_tp_counting):
    run(_cpp("mkt16c_YES", 0.982, {"best_bid": 0.982},
             db, MockWS(), MockTG(), TP_CONFIG, object(), pos_cache, {}, False))
check("TP: REST called exactly once (cooldown prevents repeat)", rest_call_count == first_calls)

# 5b. Entered at 98¢, TP=98¢ → NOT triggered (no gain, would lose fees)
_rc16.clear()
_mon_mod._price_last_recorded.clear()
POS_16e = {**POS_16, "id": "pos16e", "market_id": "mkt16e",
           "entry_price": 0.98, "end_date": _far_end_16}
db = MockDB(open_positions=[POS_16e])
pos_cache = {"mkt16e_YES": POS_16e.copy()}
with mock16.patch("engine.monitor._verify_price_and_volume", _rest_tp_confirms):
    run(_cpp("mkt16e_YES", 0.981, {"best_bid": 0.981},
             db, MockWS(), MockTG(), TP_CONFIG, object(), pos_cache, {}, False))
check("TP: NOT triggered when entry=98¢ (< 1¢ gain)", len(db.closed) == 0)

# 6. TP respects exit fees (PnL < gross)
_rc16.clear()
_mon_mod._price_last_recorded.clear()
POS_16d = {**POS_16, "id": "pos16d", "market_id": "mkt16d", "end_date": _far_end_16}
db = MockDB(open_positions=[POS_16d])
pos_cache = {"mkt16d_YES": POS_16d.copy()}
with mock16.patch("engine.monitor._verify_price_and_volume", _rest_tp_confirms):
    run(_cpp("mkt16d_YES", 0.981, {"best_bid": 0.981},
             db, MockWS(), MockTG(), TP_CONFIG, object(), pos_cache, {}, False))
gross_pnl = ((0.981 - 0.94) / 0.94) * 20
check("TP: PnL less than gross (fees deducted)", db.closed[0]["pnl"] < gross_pnl)


# ══════════════════════════════════════
# 17. Dynamic Stake by Days Left
# ══════════════════════════════════════
print("\n\033[1m17. Dynamic Stake by Days Left\033[0m")

from engine.entry import calc_stake

cfg_dyn = {"MAX_STAKE": 20.0, "MIN_STAKE": 5.0, "MAX_STAKE_6H": 50.0, "MAX_STAKE_1D": 35.0}
cfg_no_dyn = {"MAX_STAKE": 20.0, "MIN_STAKE": 5.0}  # no dynamic keys → uses fallback

# ≤6h (0.25 days) → MAX_STAKE_6H
check("6h: stake capped at $50", calc_stake(1000, cfg_dyn, days_left=0.1) == 50.0)
check("6h: stake capped at $50 (exact 0.25d)", calc_stake(1000, cfg_dyn, days_left=0.25) == 50.0)

# ≤1d → MAX_STAKE_1D
check("1d: stake capped at $35", calc_stake(1000, cfg_dyn, days_left=0.5) == 35.0)
check("1d: stake capped at $35 (exact 1d)", calc_stake(1000, cfg_dyn, days_left=1.0) == 35.0)

# >1d → regular MAX_STAKE
check(">1d: stake capped at $20", calc_stake(1000, cfg_dyn, days_left=2.0) == 20.0)
check(">1d: stake capped at $20 (5d)", calc_stake(1000, cfg_dyn, days_left=5.0) == 20.0)

# No-config fallback (uses MAX_STAKE * 2.5 for 6h, * 1.75 for 1d)
check("6h fallback: MAX_STAKE*2.5=50", calc_stake(1000, cfg_no_dyn, days_left=0.1) == 50.0)
check("1d fallback: MAX_STAKE*1.75=35", calc_stake(1000, cfg_no_dyn, days_left=0.5) == 35.0)

# Small bankroll — still capped at bankroll
check("6h: bankroll $30 → stake $0 (can't afford $50 min)", calc_stake(3, cfg_dyn, days_left=0.1) == 0.0)

# MIN_STAKE still respected
cfg_dyn2 = {"MAX_STAKE": 20.0, "MIN_STAKE": 5.0, "MAX_STAKE_6H": 50.0, "MAX_STAKE_1D": 35.0}
check("6h: small bankroll $60 → 5% = $3, but MIN_STAKE=$5", calc_stake(60, cfg_dyn2, days_left=0.1) == 5.0)

# Backward compat: no days_left argument → defaults to 99 (regular MAX_STAKE)
check("No days_left: regular MAX_STAKE", calc_stake(1000, cfg_dyn) == 20.0)

# Esports exception: dynamic uplift does NOT apply — live BO3 matches can resolve
# 95c→0c in minutes, so "shorter time = higher certainty" logic doesn't hold.
check("esports 6h: capped at MAX_STAKE not MAX_STAKE_6H", calc_stake(1000, cfg_dyn, days_left=0.1, theme="esports") == 20.0)
check("esports 1d: capped at MAX_STAKE not MAX_STAKE_1D", calc_stake(1000, cfg_dyn, days_left=0.5, theme="esports") == 20.0)
check("esports >1d: still MAX_STAKE", calc_stake(1000, cfg_dyn, days_left=3.0, theme="esports") == 20.0)
check("non-esports 6h: still gets MAX_STAKE_6H", calc_stake(1000, cfg_dyn, days_left=0.1, theme="crypto") == 50.0)

# Q≥80 + ≤6h tier: bumped Kelly fraction (7.5%) and higher cap (MAX_STAKE_Q80_6H)
# Production data: Q80+ is 100% WR (15/15) — undercapitalized at base 5%.
cfg_q80 = {"MAX_STAKE": 20.0, "MIN_STAKE": 5.0, "MAX_STAKE_6H": 50.0, "MAX_STAKE_1D": 35.0,
           "MAX_STAKE_Q80_6H": 75.0, "MAX_STAKE_Q80_1D": 50.0, "PCT_STAKE_Q80": 0.075}
# Bankroll $1239 (~real prod): pct=7.5% → $93, capped at MAX_STAKE_Q80_6H=$75
check("Q80+6h: bankroll $1239, 7.5%=$93 capped at $75", calc_stake(1239, cfg_q80, days_left=0.1, quality=80) == 75.0)
check("Q80+6h: exact Q=80 boundary (≥80)", calc_stake(1239, cfg_q80, days_left=0.1, quality=80) == 75.0)
check("Q90+6h: same uplift", calc_stake(1239, cfg_q80, days_left=0.1, quality=95) == 75.0)
# Q79 must NOT trigger uplift — falls back to MAX_STAKE_6H
check("Q79+6h: NOT uplifted, uses MAX_STAKE_6H=$50", calc_stake(1239, cfg_q80, days_left=0.1, quality=79) == 50.0)
# Q80 but >6h: uses ≤1d/≥1d rules (not Q80 path)
check("Q80 + 0.5d: uses MAX_STAKE_Q80_1D ($50, pct binds at $93→$50)", calc_stake(1239, cfg_q80, days_left=0.5, quality=85) == 50.0)
check("Q80 + 1.0d (boundary): uses MAX_STAKE_Q80_1D", calc_stake(1239, cfg_q80, days_left=1.0, quality=85) == 50.0)
check("Q80 + 2d: uses MAX_STAKE", calc_stake(1239, cfg_q80, days_left=2.0, quality=90) == 20.0)
check("Q79 + 0.5d: NO Q80 uplift, uses MAX_STAKE_1D=$35", calc_stake(1239, cfg_q80, days_left=0.5, quality=79) == 35.0)
# Q80 + 1d tier — small bankroll: pct=7.5% binds before cap
check("Q80 + 0.5d: bankroll $400, 7.5%=$30 (pct binds, > MIN)", calc_stake(400, cfg_q80, days_left=0.5, quality=85) == 30.0)
# Q80+esports: esports rule wins (uplift disabled for esports regardless of quality)
check("esports Q80+6h: still capped at MAX_STAKE", calc_stake(1239, cfg_q80, days_left=0.1, theme="esports", quality=90) == 20.0)
# Lower bankroll: pct binds before cap
check("Q80+6h: bankroll $500, 7.5%=$37.5 (pct binds)", calc_stake(500, cfg_q80, days_left=0.1, quality=85) == 37.5)
# Defaults: missing config keys → fallback to defaults
cfg_no_q80 = {"MAX_STAKE": 20.0, "MIN_STAKE": 5.0, "MAX_STAKE_6H": 50.0}
check("Q80+6h fallback: default $75 cap, default 7.5% pct", calc_stake(1239, cfg_no_q80, days_left=0.1, quality=85) == 75.0)
# Backward compat: no quality argument → defaults to 0 (no uplift)
check("No quality arg: no Q80 uplift", calc_stake(1239, cfg_q80, days_left=0.1) == 50.0)

# try_enter passes days_left: 6h market gets bigger stake
print("  (try_enter dynamic stake integration via calc_stake directly ↑)")


# ══════════════════════════════════════
# 18. Theme Quality Adjustment
# ══════════════════════════════════════
print("\n\033[1m18. Theme Quality Adjustment\033[0m")

from engine.scanner import theme_quality_factor, quality_score, _TARGET_WR, _ADJ_FACTOR_MIN, _ADJ_FACTOR_MAX

# No data → neutral
check("No theme_wr data → factor 1.0",       theme_quality_factor("crypto", {}) == 1.0)
check("Unknown theme in dict → factor 1.0",  theme_quality_factor("unknown", {"crypto": 0.81}) == 1.0)

# Formula: adj_wr / TARGET_WR, clamped
crypto_factor = theme_quality_factor("crypto", {"crypto": 0.81})
expected_crypto = max(_ADJ_FACTOR_MIN, min(_ADJ_FACTOR_MAX, 0.81 / _TARGET_WR))
check("crypto 0.81 → factor = 0.81/TARGET_WR", abs(crypto_factor - expected_crypto) < 0.001)
check("crypto factor < 1.0 (penalty)",         crypto_factor < 1.0)

climate_factor = theme_quality_factor("climate", {"climate": 0.94})
expected_climate = max(_ADJ_FACTOR_MIN, min(_ADJ_FACTOR_MAX, 0.94 / _TARGET_WR))
check("climate 0.94 → factor ≈ 1.01",          abs(climate_factor - expected_climate) < 0.001)
check("climate factor >= 1.0 (slight boost)",   climate_factor >= 1.0)

# Floor and ceiling
check("adj_wr=0.50 → clamped to MIN floor",    theme_quality_factor("bad", {"bad": 0.50}) == _ADJ_FACTOR_MIN)
check("adj_wr=1.00 → clamped to MAX ceiling",  theme_quality_factor("top", {"top": 1.00}) == _ADJ_FACTOR_MAX)
check("election 0.70 → 0.753 (above floor)",    abs(theme_quality_factor("election", {"election": 0.70}) - round(0.70 / _TARGET_WR, 3)) < 0.001)
check("election 0.60 → hits floor 0.75",       theme_quality_factor("election", {"election": 0.60}) == _ADJ_FACTOR_MIN)

# Applied: crypto Q70 goes down, climate Q70 stays up
base_q = quality_score(0.95, 0.005, 0.5, 200_000, 10_000)
crypto_adj  = round(base_q * theme_quality_factor("crypto",  {"crypto": 0.81}), 1)
climate_adj = round(base_q * theme_quality_factor("climate", {"climate": 0.94}), 1)
check("crypto adj_q < base_q",                 crypto_adj < base_q)
check("climate adj_q >= base_q",               climate_adj >= base_q)

# Key scenario: crypto Q70 at WR=0.81 → ~61, not blocked at MIN_Q=40 but lower priority
crypto_adj_70 = round(70.0 * theme_quality_factor("crypto", {"crypto": 0.81}), 1)
check("crypto Q70 → ~61 (above Q40 floor)",    40 < crypto_adj_70 < 70)

# election at floor: Q70 → 70 * 0.75 = 52.5
election_adj_70 = round(70.0 * _ADJ_FACTOR_MIN, 1)
check("election Q70 @ floor → 52.5",           abs(election_adj_70 - 52.5) < 0.2)

# Bayesian adj_wr math (mirrors db.get_theme_adj_wr formula)
SHRINKAGE_K = 20
n, wins, global_wr = 150, 122, 0.87   # crypto-like
raw_wr = wins / n
adj_wr = (n * raw_wr + SHRINKAGE_K * global_wr) / (n + SHRINKAGE_K)
check("Bayesian adj_wr shrinks toward global", min(raw_wr, global_wr) <= adj_wr <= max(raw_wr, global_wr))

n2, wins2 = 5, 5   # tiny theme, 100% WR
raw_wr2 = wins2 / n2
adj_wr2 = (n2 * raw_wr2 + SHRINKAGE_K * global_wr) / (n2 + SHRINKAGE_K)
check("Small sample shrinks heavily toward global", adj_wr2 < raw_wr2)


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
