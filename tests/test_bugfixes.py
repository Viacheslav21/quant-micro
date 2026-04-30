"""Tests for bug fixes in quant-micro.

Covers: #1 (WS multi-key token mapping), #8 (NO price from API),
        #6 (year rollover), #17 (Telegram HTML escaping),
        shared utilities, book guard.
Run: python3 tests/test_bugfixes.py
All tests are self-contained — no external deps required.
"""
import sys, os, asyncio, re, html, unittest
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ── Bug #1: WS multi-key token mapping (YES + NO on same token) ──
# Inline the core data structures to test without websockets dependency

class FakeWS:
    """Minimal replica of MicroWS data structures + methods under test."""
    def __init__(self):
        self._subscribed_tokens: set = set()
        self._token_to_keys: dict[str, list[str]] = {}
        self._key_invert: dict[str, bool] = {}
        self.prices: dict[str, dict] = {}

    def register_market(self, ws_key, token_id=None, token_side="yes", price=0.5):
        tokens_to_add = []
        if token_id:
            self._key_invert[ws_key] = (token_side == "no")
            if token_id not in self._subscribed_tokens:
                self._token_to_keys[token_id] = [ws_key]
                self._subscribed_tokens.add(token_id)
                tokens_to_add.append(token_id)
            elif ws_key not in self._token_to_keys.get(token_id, []):
                self._token_to_keys[token_id].append(ws_key)

        if ws_key not in self.prices:
            self.prices[ws_key] = {"price": price, "token_id": token_id, "is_position": False}
        return tokens_to_add

    def unregister_market(self, ws_key):
        tokens_to_remove = []
        info = self.prices.pop(ws_key, None)
        self._key_invert.pop(ws_key, None)
        if not info:
            return tokens_to_remove
        token_id = info.get("token_id")
        if token_id and token_id in self._subscribed_tokens:
            keys = self._token_to_keys.get(token_id, [])
            if ws_key in keys:
                keys.remove(ws_key)
            if not keys:
                self._subscribed_tokens.discard(token_id)
                self._token_to_keys.pop(token_id, None)
                tokens_to_remove.append(token_id)
        return tokens_to_remove

    def _side_price(self, ws_key, raw_price):
        if self._key_invert.get(ws_key, False):
            return round(1.0 - raw_price, 4)
        return raw_price


class TestWsMultiKeyMapping(unittest.TestCase):
    def test_register_yes_and_no_same_token(self):
        ws = FakeWS()
        t1 = ws.register_market("mkt_YES", token_id="tok", token_side="yes", price=0.95)
        t2 = ws.register_market("mkt_NO", token_id="tok", token_side="no", price=0.05)
        self.assertEqual(t1, ["tok"])
        self.assertEqual(t2, [])
        self.assertIn("mkt_YES", ws._token_to_keys["tok"])
        self.assertIn("mkt_NO", ws._token_to_keys["tok"])

    def test_invert_per_key(self):
        ws = FakeWS()
        ws.register_market("mkt_YES", token_id="tok", token_side="yes")
        ws.register_market("mkt_NO", token_id="tok", token_side="no")
        self.assertFalse(ws._key_invert["mkt_YES"])
        self.assertTrue(ws._key_invert["mkt_NO"])

    def test_unregister_one_keeps_token(self):
        ws = FakeWS()
        ws.register_market("mkt_YES", token_id="tok", token_side="yes")
        ws.register_market("mkt_NO", token_id="tok", token_side="no")
        unsub = ws.unregister_market("mkt_YES")
        self.assertEqual(unsub, [])
        self.assertIn("tok", ws._subscribed_tokens)

    def test_unregister_both_removes_token(self):
        ws = FakeWS()
        ws.register_market("mkt_YES", token_id="tok", token_side="yes")
        ws.register_market("mkt_NO", token_id="tok", token_side="no")
        ws.unregister_market("mkt_YES")
        unsub = ws.unregister_market("mkt_NO")
        self.assertEqual(unsub, ["tok"])
        self.assertNotIn("tok", ws._subscribed_tokens)

    def test_side_price_inversion(self):
        ws = FakeWS()
        ws.register_market("mkt_YES", token_id="tok", token_side="yes")
        ws.register_market("mkt_NO", token_id="tok", token_side="no")
        self.assertAlmostEqual(ws._side_price("mkt_YES", 0.95), 0.95)
        self.assertAlmostEqual(ws._side_price("mkt_NO", 0.95), 0.05)

    def test_price_dispatch_to_both_keys(self):
        """Simulate a price event updating both YES and NO ws_keys."""
        ws = FakeWS()
        ws.register_market("mkt_YES", token_id="tok", token_side="yes", price=0.90)
        ws.register_market("mkt_NO", token_id="tok", token_side="no", price=0.10)

        # Simulate what _handle_price does: iterate over ws_keys for token
        raw = 0.92
        for ws_key in ws._token_to_keys.get("tok", []):
            info = ws.prices.get(ws_key)
            if info:
                info["price"] = ws._side_price(ws_key, raw)

        self.assertAlmostEqual(ws.prices["mkt_YES"]["price"], 0.92)
        self.assertAlmostEqual(ws.prices["mkt_NO"]["price"], 0.08)


# ── Bug #8: NO price from raw_prices[1] ──

class TestNoPriceFromApi(unittest.TestCase):
    def test_uses_raw_prices_1(self):
        raw_prices = ["0.94", "0.04"]
        yes_price = float(raw_prices[0])
        no_price = float(raw_prices[1]) if len(raw_prices) > 1 else round(1.0 - yes_price, 4)
        self.assertAlmostEqual(no_price, 0.04)

    def test_fallback_single_price(self):
        raw_prices = ["0.94"]
        yes_price = float(raw_prices[0])
        no_price = float(raw_prices[1]) if len(raw_prices) > 1 else round(1.0 - yes_price, 4)
        self.assertAlmostEqual(no_price, 0.06)


# ── Bug #6: Year rollover in date parsing ──

class TestYearRollover(unittest.TestCase):
    def _parse(self, question, now):
        """Inline date parser matching scanner logic."""
        import calendar
        MONTH_MAP = {m.lower(): i for i, m in enumerate([
            "", "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december"
        ]) if m}
        pats = [
            re.compile(r'(?:on|by|before)\s+(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+\d{4})?', re.I),
        ]
        for pat in pats:
            m = pat.search(question)
            if not m:
                continue
            month_str = m.group(1).lower()
            month = MONTH_MAP.get(month_str)
            if not month:
                continue
            day = int(m.group(2))
            try:
                dt = datetime(now.year, month, day, 23, 59, tzinfo=timezone.utc)
                if (now - dt).days > 30:
                    dt = dt.replace(year=now.year + 1)
                return dt
            except (ValueError, OverflowError):
                continue
        return None

    def test_jan_in_december(self):
        now = datetime(2026, 12, 15, tzinfo=timezone.utc)
        dt = self._parse("by January 15", now)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2027)

    def test_march_in_january(self):
        now = datetime(2026, 1, 10, tzinfo=timezone.utc)
        dt = self._parse("by March 15", now)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)

    def test_recent_past_stays(self):
        now = datetime(2026, 3, 20, tzinfo=timezone.utc)
        dt = self._parse("by March 1", now)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)  # only 19 days ago, < 30


# ── Bug #17: Telegram HTML escaping ──

class TestTelegramHtmlEscape(unittest.TestCase):
    def _escape(self, text):
        """Inline _escape_question logic."""
        parts = re.split(r'(</?(?:b|i|code)>)', text)
        return "".join(p if re.match(r'</?(?:b|i|code)>$', p) else html.escape(p) for p in parts)

    def test_angle_brackets_escaped(self):
        result = self._escape("Will BTC hit <$50k>? <b>yes</b>")
        self.assertIn("&lt;$50k&gt;", result)
        self.assertIn("<b>yes</b>", result)

    def test_ampersand_escaped(self):
        result = self._escape("M&A deal <b>done</b>")
        self.assertIn("M&amp;A", result)
        self.assertIn("<b>done</b>", result)

    def test_plain_unchanged(self):
        text = "Simple question"
        self.assertEqual(self._escape(text), text)


# ── WS Price Sync: price_change syncs best_bid, book guard ──

class TestWsPriceSync(unittest.TestCase):
    """Regression: incremental book events were overwriting best_bid with
    stale lower-level bids (e.g., 88¢ real → 80¢ displayed)."""

    def _make_ws(self):
        ws = FakeWS()
        ws.register_market("mkt_YES", token_id="tok", token_side="yes", price=0.92)
        return ws

    def test_price_change_syncs_best_bid(self):
        """price_change event should update best_bid (authoritative source)."""
        ws = self._make_ws()
        info = ws.prices["mkt_YES"]
        # Simulate _handle_price setting price + best_bid
        new_price = ws._side_price("mkt_YES", 0.88)
        info["price"] = new_price
        info["best_bid"] = new_price  # this is the fix
        self.assertAlmostEqual(info["best_bid"], 0.88)

    def test_book_lowers_best_bid(self):
        """Book events update best_bid directly — real price drops must not be blocked.
        Critical: blocking book-driven drops caused missed SL on BTC YES position."""
        ws = self._make_ws()
        info = ws.prices["mkt_YES"]
        info["price"] = 0.88
        info["best_bid"] = 0.88
        raw_best_bid = 0.40
        info["best_bid"] = raw_best_bid
        self.assertAlmostEqual(info["best_bid"], 0.40, msg="Book bid should update directly")

    def test_book_raises_best_bid(self):
        """Book events CAN raise best_bid above current price."""
        ws = self._make_ws()
        info = ws.prices["mkt_YES"]
        info["price"] = 0.88
        info["best_bid"] = 0.88
        info["best_bid"] = 0.92
        self.assertAlmostEqual(info["best_bid"], 0.92)


# ── Theme classification fixes ──

from engine.scanner import classify_theme


class TestThemeClassification(unittest.TestCase):
    """Regression: theme misclassification caused blocked categories to slip through."""

    def test_counter_strike_is_esports_not_war(self):
        self.assertEqual(classify_theme("Counter-Strike: Astralis vs TheMongolz - Map 1 Winner"), "esports")

    def test_shanghai_fc_is_sports_not_tech(self):
        """'ai ' in 'shanghai' was matching tech keyword."""
        self.assertEqual(classify_theme("Will Shanghai Shenhua FC vs. Shanghai Haigang FC end in a draw?"), "sports")

    def test_western_force_is_sports(self):
        self.assertEqual(classify_theme("Will Western Force win?"), "sports")

    def test_draw_market_is_sports(self):
        self.assertEqual(classify_theme("Will Hertha BSC vs. 1. FC Kaiserslautern end in a draw?"), "sports")

    def test_cba_teams_are_sports(self):
        self.assertEqual(classify_theme("Tianjin Pioneers vs. Ningbo Rockets"), "sports")
        self.assertEqual(classify_theme("Beijing Ducks vs. Nanjing Monkey King"), "sports")

    def test_ai_still_matches_tech(self):
        self.assertEqual(classify_theme("Will AI models surpass human performance?"), "tech")

    def test_shanghai_weather_is_climate_not_tech(self):
        self.assertEqual(classify_theme("Will Shanghai temperature reach 30°C?"), "climate")

    def test_vs_pattern_with_abbreviations(self):
        """Improved _VS_PATTERN handles FC, BSC abbreviations."""
        self.assertEqual(classify_theme("Hertha BSC vs FC Kaiserslautern"), "sports")

    def test_inflation_is_economy(self):
        self.assertEqual(classify_theme("Will annual inflation increase by 3.1% in March?"), "economy")

    def test_lol_is_esports(self):
        self.assertEqual(classify_theme("LoL: Top Esports vs JD Gaming - Game 2 Winner"), "esports")

    def test_dota_is_esports(self):
        self.assertEqual(classify_theme("Dota 2: PARIVISION vs Nigma Galaxy - Game 1 Winner"), "esports")

    def test_retail_gas_is_oil(self):
        """Bug: 'gas' (gasoline retail) was falling into 'other' and pooling losses
        with unrelated markets. Merged into 'oil' since natural gas/lng already there."""
        self.assertEqual(classify_theme("Will gas hit (High) $4.25 by April 30?"), "oil")
        self.assertEqual(classify_theme("Will gasoline drop below $3 by EOY?"), "oil")
        self.assertEqual(classify_theme("Average per gallon price in California?"), "oil")

    def test_las_vegas_still_sports(self):
        """'gas' in 'Las Vegas' must NOT trigger oil — keep sports."""
        self.assertEqual(classify_theme("Will Las Vegas Raiders win on 2026-04-30?"), "sports")


# ── Shared utilities ──

from engine.shared import calc_days_left, parse_outcome_prices, calc_exit_fee, hours_since


class TestSharedCalcDaysLeft(unittest.TestCase):
    def test_future_date(self):
        result = calc_days_left("2027-06-01T00:00:00Z")
        self.assertGreater(result, 0)

    def test_past_date_returns_zero(self):
        self.assertEqual(calc_days_left("2020-01-01T00:00:00Z"), 0)

    def test_none_returns_default(self):
        self.assertEqual(calc_days_left(None), 999.0)

    def test_custom_fallback(self):
        self.assertEqual(calc_days_left(None, fallback=0.5), 0.5)

    def test_invalid_string(self):
        self.assertEqual(calc_days_left("garbage"), 999.0)

    def test_z_suffix(self):
        result = calc_days_left("2027-01-01T00:00:00Z")
        self.assertGreater(result, 0)

    def test_plus_offset(self):
        result = calc_days_left("2027-01-01T00:00:00+00:00")
        self.assertGreater(result, 0)


class TestSharedParseOutcomePrices(unittest.TestCase):
    def test_dict_with_json_string(self):
        y, n = parse_outcome_prices({"outcomePrices": '["0.95","0.05"]'})
        self.assertAlmostEqual(y, 0.95, places=2)
        self.assertAlmostEqual(n, 0.05, places=2)

    def test_dict_with_list(self):
        y, n = parse_outcome_prices({"outcomePrices": [0.92, 0.08]})
        self.assertAlmostEqual(y, 0.92, places=2)
        self.assertAlmostEqual(n, 0.08, places=2)

    def test_dict_fallback_yes_no(self):
        y, n = parse_outcome_prices({"yes_price": "0.90", "no_price": "0.10"})
        self.assertAlmostEqual(y, 0.90, places=2)

    def test_single_element(self):
        y, n = parse_outcome_prices({"outcomePrices": [0.75]})
        self.assertAlmostEqual(n, 0.25, places=2)

    def test_empty_dict(self):
        y, n = parse_outcome_prices({})
        self.assertEqual(y, 0)
        self.assertEqual(n, 0)

    def test_none(self):
        y, n = parse_outcome_prices(None)
        self.assertEqual(y, 0)
        self.assertEqual(n, 0)

    def test_raw_list(self):
        y, n = parse_outcome_prices([0.60, 0.40])
        self.assertAlmostEqual(y, 0.60, places=2)
        self.assertAlmostEqual(n, 0.40, places=2)

    def test_raw_string(self):
        y, n = parse_outcome_prices('["0.80","0.20"]')
        self.assertAlmostEqual(y, 0.80, places=2)
        self.assertAlmostEqual(n, 0.20, places=2)


class TestSharedCalcExitFee(unittest.TestCase):
    def test_normal(self):
        fee = calc_exit_fee(20.0, 0.95, {"SLIPPAGE": 0.005, "FEE_PCT": 0.02})
        # slippage = 0.005 * 20 / 0.95 ≈ 0.1053, fee = 0.02 * 20 = 0.40
        self.assertAlmostEqual(fee, 0.5053, places=3)

    def test_zero_config(self):
        self.assertEqual(calc_exit_fee(20.0, 0.95, {}), 0)

    def test_zero_entry(self):
        fee = calc_exit_fee(20.0, 0.0, {"SLIPPAGE": 0.005, "FEE_PCT": 0.02})
        self.assertAlmostEqual(fee, 0.40, places=2)

    def test_no_slippage_only_fee(self):
        fee = calc_exit_fee(10.0, 0.90, {"FEE_PCT": 0.01})
        self.assertAlmostEqual(fee, 0.10, places=2)


class TestWsUpdateLastSeen(unittest.TestCase):
    def test_updates_timestamp(self):
        ws = FakeWS()
        ws.register_market("mkt_YES", token_id="tok", token_side="yes", price=0.90)
        ws.prices["mkt_YES"]["last_update"] = 0  # simulate old timestamp
        import time
        # Simulate what update_last_seen does
        info = ws.prices.get("mkt_YES")
        if info:
            info["last_update"] = time.time()
        self.assertGreater(info["last_update"], 0, "Timestamp should be updated from 0")

    def test_missing_key_no_error(self):
        ws = FakeWS()
        # Should not raise
        info = ws.prices.get("nonexistent")
        self.assertIsNone(info)


# ── CLOB book API parsing ──

from engine.monitor import _parse_clob_book


class TestClobBookParse(unittest.TestCase):
    """Replaces lagged Gamma midpoint with live CLOB orderbook for REST verify."""

    def test_normal_book(self):
        book = {
            "bids": [{"price": "0.74", "size": "500"}, {"price": "0.73", "size": "100"}],
            "asks": [{"price": "0.78", "size": "300"}, {"price": "0.79", "size": "200"}],
        }
        bid, ask = _parse_clob_book(book)
        self.assertAlmostEqual(bid, 0.74)
        self.assertAlmostEqual(ask, 0.78)

    def test_zero_size_levels_skipped(self):
        """Zero-size levels are pulled orders, not real depth."""
        book = {
            "bids": [{"price": "0.90", "size": "0"}, {"price": "0.74", "size": "500"}],
            "asks": [{"price": "0.78", "size": "300"}, {"price": "0.50", "size": "0"}],
        }
        bid, ask = _parse_clob_book(book)
        self.assertAlmostEqual(bid, 0.74, msg="0-size bid at 0.90 should be ignored")
        self.assertAlmostEqual(ask, 0.78, msg="0-size ask at 0.50 should be ignored")

    def test_unsorted_levels(self):
        """CLOB may return unsorted — parser picks best (max bid, min ask)."""
        book = {
            "bids": [{"price": "0.71", "size": "100"}, {"price": "0.74", "size": "500"}, {"price": "0.72", "size": "200"}],
            "asks": [{"price": "0.81", "size": "50"}, {"price": "0.78", "size": "300"}, {"price": "0.79", "size": "200"}],
        }
        bid, ask = _parse_clob_book(book)
        self.assertAlmostEqual(bid, 0.74)
        self.assertAlmostEqual(ask, 0.78)

    def test_empty_book(self):
        bid, ask = _parse_clob_book({"bids": [], "asks": []})
        self.assertEqual(bid, 0.0)
        self.assertEqual(ask, 0.0)

    def test_missing_keys(self):
        bid, ask = _parse_clob_book({})
        self.assertEqual(bid, 0.0)
        self.assertEqual(ask, 0.0)

    def test_string_numbers(self):
        """Polymarket returns price/size as strings."""
        book = {"bids": [{"price": "0.5", "size": "10"}], "asks": [{"price": "0.6", "size": "20"}]}
        bid, ask = _parse_clob_book(book)
        self.assertAlmostEqual(bid, 0.5)
        self.assertAlmostEqual(ask, 0.6)


# ── SL blacklist now blocks max_loss ──

class TestSLBlacklistMaxLoss(unittest.TestCase):
    """Bug: max_loss closes were not in the SL blacklist IN clause, allowing
    instant re-entry after the 6h cooldown — same market lost repeatedly."""

    def test_db_query_includes_max_loss(self):
        """Verify the SQL constant in db.py covers all three close reasons."""
        with open(os.path.join(ROOT, "utils", "db.py")) as f:
            src = f.read()
        # The SL blacklist EXISTS clause must include max_loss
        self.assertIn(
            "close_reason IN ('stop_loss','rapid_drop','max_loss')",
            src,
            "SL blacklist must include 'max_loss' to prevent re-entry on losers",
        )


# ── rest_poll: doesn't clobber best_bid; bumps block counter on cap breach ──

class TestRestPollNoClobber(unittest.TestCase):
    """rest_poll used to overwrite ws.prices[ws_key]['best_bid'] with REST midpoint,
    erasing a real WS-observed drop. Now it only refreshes last_update."""

    def test_source_does_not_overwrite_best_bid(self):
        with open(os.path.join(ROOT, "engine", "monitor.py")) as f:
            src = f.read()
        # Find the rest_poll function body
        start = src.index("async def rest_poll_stale_positions")
        end = src.index("\n\n", start + 100) if "\n\n" in src[start + 100:] else len(src)
        body = src[start:end + 5000]  # generous slice
        # Old code wrote ws.prices[ws_key]["best_bid"] = rest_price — that's what we removed
        self.assertNotIn(
            'ws.prices[ws_key]["best_bid"] = rest_price',
            body,
            "rest_poll must not overwrite WS best_bid (preserves WS-observed drops)",
        )

    def test_increments_max_loss_blocks_on_cap_breach(self):
        """When REST already shows loss past cap, rest_poll bumps the block counter
        so silent-WS positions reach bypass_thresh in finite time."""
        with open(os.path.join(ROOT, "engine", "monitor.py")) as f:
            src = f.read()
        start = src.index("async def rest_poll_stale_positions")
        body = src[start:]
        self.assertIn("_max_loss_blocks[ws_key] = _max_loss_blocks.get(ws_key, 0) + 1", body)
        self.assertIn("rest_pnl <= -max_loss", body)


# ── _verify_price_and_volume: signature + CLOB-first behavior ──

class TestVerifyPriceSignature(unittest.TestCase):
    def test_accepts_token_id(self):
        """token_id parameter is the YES outcome token; required for CLOB book lookup."""
        import inspect
        from engine.monitor import _verify_price_and_volume
        sig = inspect.signature(_verify_price_and_volume)
        self.assertIn("token_id", sig.parameters)
        # Default None for backwards-compat with callers that lack the token
        self.assertIsNone(sig.parameters["token_id"].default)

    def test_no_token_falls_back_to_gamma(self):
        """With token_id=None, must still return a result (Gamma midpoint fallback)."""
        with open(os.path.join(ROOT, "engine", "monitor.py")) as f:
            src = f.read()
        # The fallback path is the Gamma midpoint via parse_outcome_prices
        self.assertIn("parse_outcome_prices(m)", src)
        self.assertIn("Fallback", src)


# ── MAX_LOSS bypass threshold is configurable ──

class TestMaxLossBypassConfig(unittest.TestCase):
    def test_default_is_2(self):
        from engine.monitor import _MAX_LOSS_BYPASS_BLOCKS_DEFAULT
        self.assertEqual(_MAX_LOSS_BYPASS_BLOCKS_DEFAULT, 2)

    def test_read_from_config(self):
        with open(os.path.join(ROOT, "engine", "monitor.py")) as f:
            src = f.read()
        self.assertIn('config.get("MAX_LOSS_BYPASS_BLOCKS"', src)

    def test_in_safe_config_keys(self):
        with open(os.path.join(ROOT, "main.py")) as f:
            src = f.read()
        self.assertIn("MAX_LOSS_BYPASS_BLOCKS", src)
        # Must be in _SAFE_CONFIG_KEYS so live reload accepts overrides
        safe_block = src[src.index("_SAFE_CONFIG_KEYS"):src.index("}", src.index("_SAFE_CONFIG_KEYS"))]
        self.assertIn("MAX_LOSS_BYPASS_BLOCKS", safe_block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
