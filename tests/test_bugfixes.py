"""Tests for bug fixes in quant-micro.

Covers: #1 (WS multi-key token mapping), #8 (NO price from API),
        #6 (year rollover), #17 (Telegram HTML escaping).
Run: python3 tests/test_bugfixes.py
All tests are self-contained — no external deps required.
"""
import asyncio, re, html, unittest
from datetime import datetime, timezone, timedelta


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
