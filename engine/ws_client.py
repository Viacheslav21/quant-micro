import asyncio
import json
import time
import logging
from typing import Optional, Callable

import websockets

log = logging.getLogger("micro.ws")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
HEARTBEAT_INTERVAL = 10
RECONNECT_DELAY = 5
BATCH_SIZE = 100
WATCHLIST_CHECK_INTERVAL = 2.0


class MicroWS:
    """WebSocket client: watchlist price-up detection + position SL/resolution.

    Each tracked entry is keyed by ws_key (e.g. "market_id_YES" or "market_id_NO").
    ALWAYS subscribes to the YES token. For NO side, prices are inverted
    (price = 1 - yes_token_price) so all callbacks see the correct side price.
    """

    def __init__(self):
        self.ws = None
        self._running = False
        self._subscribed_tokens: set = set()
        self._token_to_keys: dict[str, list[str]] = {}  # token_id -> [ws_key, ...] (one token can serve YES + NO)
        self._key_invert: dict[str, bool] = {}           # ws_key -> True if NO side (invert price)
        self.prices: dict[str, dict] = {}                # ws_key -> {price, best_bid, best_ask, ...}
        self._last_watchlist_check: dict[str, float] = {}

        self._on_watchlist_price: Optional[Callable] = None
        self._on_position_price: Optional[Callable] = None

    def set_callbacks(self, on_watchlist_price=None, on_position_price=None, **_):
        self._on_watchlist_price = on_watchlist_price
        self._on_position_price = on_position_price

    def register_market(self, ws_key: str, token_id: str = None,
                        token_side: str = "yes", price: float = 0.5,
                        question: str = "", is_position: bool = False) -> list:
        """Register a single side for tracking. Returns tokens to subscribe."""
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
            self.prices[ws_key] = {
                "price": price,
                "best_bid": price,
                "best_ask": price,
                "question": question,
                "token_id": token_id,
                "is_position": is_position,
                "last_update": time.time(),
            }
        elif is_position:
            self.prices[ws_key]["is_position"] = True

        return tokens_to_add

    def unregister_market(self, ws_key: str) -> list:
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
                # No more ws_keys using this token — unsubscribe
                self._subscribed_tokens.discard(token_id)
                self._token_to_keys.pop(token_id, None)
                tokens_to_remove.append(token_id)
        return tokens_to_remove

    def mark_as_position(self, ws_key: str):
        if ws_key in self.prices:
            self.prices[ws_key]["is_position"] = True

    def unmark_position(self, ws_key: str):
        if ws_key in self.prices:
            self.prices[ws_key]["is_position"] = False

    def update_last_seen(self, ws_key: str):
        """Mark ws_key as recently seen (prevents stale re-fires without direct mutation)."""
        info = self.prices.get(ws_key)
        if info:
            info["last_update"] = time.time()

    def get_spread(self, ws_key: str) -> float:
        info = self.prices.get(ws_key, {})
        return info.get("best_ask", 0) - info.get("best_bid", 0)

    def cleanup_stale_checks(self):
        """Remove expired watchlist check timestamps to prevent unbounded growth."""
        now = time.time()
        stale = [k for k, v in self._last_watchlist_check.items() if now - v > 3600]
        for k in stale:
            del self._last_watchlist_check[k]

    # ── Dispatch ──

    async def _dispatch(self, ws_key: str, info: dict):
        price = info["price"]

        if info.get("is_position") and self._on_position_price:
            try:
                await self._on_position_price(ws_key, price, info)
            except Exception as e:
                log.error(f"[WS] position callback error: {e}")
        elif not info.get("is_position") and self._on_watchlist_price:
            now = time.time()
            last = self._last_watchlist_check.get(ws_key, 0)
            if now - last < WATCHLIST_CHECK_INTERVAL:
                return
            self._last_watchlist_check[ws_key] = now
            try:
                await self._on_watchlist_price(ws_key, price, info)
            except Exception as e:
                log.error(f"[WS] watchlist callback error: {e}")

    # ── Connection ──

    async def connect(self):
        self._running = True
        reconnect_attempts = 0
        while self._running:
            try:
                async with websockets.connect(WS_URL, ping_interval=None) as ws:
                    self.ws = ws
                    reconnect_attempts = 0
                    log.info(f"[WS] Connected, {len(self._subscribed_tokens)} tokens tracked")
                    await self._subscribe_all(ws)
                    heartbeat_task = asyncio.create_task(self._heartbeat(ws))
                    try:
                        async for message in ws:
                            if message == "PONG":
                                continue
                            try:
                                data = json.loads(message)
                                await self._handle_message(data)
                            except json.JSONDecodeError:
                                continue
                    finally:
                        heartbeat_task.cancel()
            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                delay = min(RECONNECT_DELAY * (2 ** min(reconnect_attempts, 4)), 60)
                reconnect_attempts += 1
                log.warning(f"[WS] Disconnected: {e}, reconnecting in {delay}s")
                self.ws = None
                await asyncio.sleep(delay)
            except Exception as e:
                delay = min(RECONNECT_DELAY * (2 ** min(reconnect_attempts, 4)), 60)
                reconnect_attempts += 1
                log.error(f"[WS] Unexpected error: {e}", exc_info=True)
                self.ws = None
                await asyncio.sleep(delay)

    async def stop(self):
        self._running = False
        if self.ws:
            await self.ws.close()
            self.ws = None

    # ── Subscribe ──

    async def subscribe_tokens(self, token_ids: list):
        if not token_ids or not self.ws:
            return
        for i in range(0, len(token_ids), BATCH_SIZE):
            batch = token_ids[i:i + BATCH_SIZE]
            msg = {"assets_ids": batch, "type": "market", "custom_feature_enabled": True}
            try:
                await self.ws.send(json.dumps(msg))
                log.debug(f"[WS] Subscribed {len(batch)} tokens")
            except Exception as e:
                log.warning(f"[WS] Subscribe failed: {e}")

    async def unsubscribe_tokens(self, token_ids: list):
        if not token_ids or not self.ws:
            return
        for tid in token_ids:
            self._subscribed_tokens.discard(tid)
            self._token_to_keys.pop(tid, None)
        # Send unsubscribe to server (bug #10 fix)
        for i in range(0, len(token_ids), BATCH_SIZE):
            batch = token_ids[i:i + BATCH_SIZE]
            try:
                msg = {"assets_ids": batch, "type": "market", "action": "unsubscribe"}
                await self.ws.send(json.dumps(msg))
                log.debug(f"[WS] Unsubscribed {len(batch)} tokens")
            except Exception as e:
                log.warning(f"[WS] Unsubscribe send failed: {e}")

    async def _subscribe_all(self, ws):
        if not self._subscribed_tokens:
            return
        tokens = list(self._subscribed_tokens)
        for i in range(0, len(tokens), BATCH_SIZE):
            batch = tokens[i:i + BATCH_SIZE]
            msg = {"assets_ids": batch, "type": "market", "custom_feature_enabled": True}
            await ws.send(json.dumps(msg))
            log.info(f"[WS] Subscribed batch: {len(batch)} tokens")

    async def _heartbeat(self, ws):
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await ws.send("PING")
            except Exception:
                break

    # ── Message Handling ──

    async def _handle_message(self, data):
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    await self._handle_message(item)
            return
        if not isinstance(data, dict):
            return

        event_type = data.get("event_type")
        if event_type in ("price_change", "last_trade_price"):
            await self._handle_price(data)
        elif event_type == "book":
            await self._handle_book(data)

    def _side_price(self, ws_key: str, raw_price: float) -> float:
        """Convert raw token price to the side's price (invert if NO)."""
        if self._key_invert.get(ws_key, False):
            return round(1.0 - raw_price, 4)
        return raw_price

    async def _handle_price(self, data):
        token_id = data.get("asset_id", "")
        ws_keys = self._token_to_keys.get(token_id)
        if not ws_keys:
            return

        raw = float(data.get("price", 0))
        if raw <= 0 or raw > 1.0:
            return

        for ws_key in ws_keys:
            info = self.prices.get(ws_key)
            if not info:
                continue

            new_price = self._side_price(ws_key, raw)

            # Sanity: reject wild price jumps (>50% from current) — likely bad tick
            # But allow if multiple consecutive events confirm the new level
            old_price = info.get("price", 0.5)
            if old_price > 0.1 and abs(new_price - old_price) > old_price * 0.5:
                last_rejected = info.get("_rejected_price")
                if last_rejected is not None and abs(new_price - last_rejected) < 0.05:
                    # Second event at same level — real move, not a glitch
                    log.info(f"[WS] Large move confirmed: {ws_key} {old_price:.4f}→{new_price:.4f} (2nd event)")
                else:
                    info["_rejected_price"] = new_price
                    log.warning(f"[WS] Wild tick rejected: {ws_key} {old_price:.4f}→{new_price:.4f} (raw={raw:.4f})")
                    continue
            info.pop("_rejected_price", None)

            info["price"] = new_price
            # Sync best_bid from authoritative price — book events are incremental
            # and can set stale lower bids from non-top levels
            info["best_bid"] = new_price
            info["last_update"] = time.time()
            await self._dispatch(ws_key, info)

    async def _handle_book(self, data):
        token_id = data.get("asset_id", "")
        ws_keys = self._token_to_keys.get(token_id)
        if not ws_keys:
            return

        bids = data.get("bids", [])
        asks = data.get("asks", [])

        # Polymarket book events can be incremental — bids[0] might NOT be the best bid.
        # Find actual best bid (highest) and best ask (lowest > 0) from all levels.
        raw_best_bid = 0.0
        raw_best_ask = 0.0
        for b in bids:
            p = float(b.get("price", 0))
            s = float(b.get("size", 0))
            if p > 0 and s > 0 and p > raw_best_bid:
                raw_best_bid = p
        for a in asks:
            p = float(a.get("price", 0))
            s = float(a.get("size", 0))
            if p > 0 and s > 0 and (raw_best_ask == 0 or p < raw_best_ask):
                raw_best_ask = p

        for ws_key in ws_keys:
            info = self.prices.get(ws_key)
            if not info:
                continue

            invert = self._key_invert.get(ws_key, False)

            # Guard: incremental book events may only contain deeper levels,
            # not the top of book. Don't let best_bid drop below the authoritative
            # price from the last price_change event (prevents phantom SL triggers).
            # Real drops arrive via price_change first, then book follows.
            auth_price = info.get("price", 0)

            if not invert:
                if raw_best_bid > 0:
                    info["best_bid"] = max(raw_best_bid, auth_price)
                if raw_best_ask > 0:
                    info["best_ask"] = raw_best_ask
            else:
                # NO side: subscribed to YES token, invert to get NO prices
                # NO bid = 1 - YES ask (what we'd get selling NO)
                # NO ask = 1 - YES bid (what we'd pay buying NO)
                if raw_best_ask > 0:
                    no_bid = round(1.0 - raw_best_ask, 4)
                    info["best_bid"] = max(no_bid, auth_price)
                if raw_best_bid > 0:
                    info["best_ask"] = round(1.0 - raw_best_bid, 4)

            # Sanity check: bid/ask must be in valid range
            bid = info.get("best_bid", 0)
            ask = info.get("best_ask", 0)
            if bid <= 0.001 or bid >= 1.0:
                info["best_bid"] = auth_price
            if ask <= 0.001 or ask >= 1.0:
                info["best_ask"] = auth_price

            info["last_update"] = time.time()
            await self._dispatch(ws_key, info)
