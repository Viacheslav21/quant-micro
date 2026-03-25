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
        self._token_to_key: dict[str, str] = {}      # token_id -> ws_key
        self._token_invert: dict[str, bool] = {}      # token_id -> True if NO side (invert price)
        self.prices: dict[str, dict] = {}              # ws_key -> {price, best_bid, best_ask, ...}
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

        if token_id and token_id not in self._subscribed_tokens:
            self._token_to_key[token_id] = ws_key
            self._token_invert[token_id] = (token_side == "no")
            self._subscribed_tokens.add(token_id)
            tokens_to_add.append(token_id)

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
        if not info:
            return tokens_to_remove
        token_id = info.get("token_id")
        if token_id and token_id in self._subscribed_tokens:
            self._subscribed_tokens.discard(token_id)
            self._token_to_key.pop(token_id, None)
            self._token_invert.pop(token_id, None)
            tokens_to_remove.append(token_id)
        return tokens_to_remove

    def mark_as_position(self, ws_key: str):
        if ws_key in self.prices:
            self.prices[ws_key]["is_position"] = True

    def unmark_position(self, ws_key: str):
        if ws_key in self.prices:
            self.prices[ws_key]["is_position"] = False

    def get_spread(self, ws_key: str) -> float:
        info = self.prices.get(ws_key, {})
        return info.get("best_ask", 0) - info.get("best_bid", 0)

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
        while self._running:
            try:
                async with websockets.connect(WS_URL, ping_interval=None) as ws:
                    self.ws = ws
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
                log.warning(f"[WS] Disconnected: {e}, reconnecting in {RECONNECT_DELAY}s")
                self.ws = None
                await asyncio.sleep(RECONNECT_DELAY)
            except Exception as e:
                log.error(f"[WS] Unexpected error: {e}", exc_info=True)
                self.ws = None
                await asyncio.sleep(RECONNECT_DELAY)

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
            self._token_to_key.pop(tid, None)
            self._token_invert.pop(tid, None)

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

    def _to_side_price(self, token_id: str, raw_price: float) -> float:
        """Convert raw token price to our side's price (invert if NO)."""
        if self._token_invert.get(token_id, False):
            return round(1.0 - raw_price, 4)
        return raw_price

    async def _handle_price(self, data):
        token_id = data.get("asset_id", "")
        ws_key = self._token_to_key.get(token_id)
        if not ws_key or ws_key not in self.prices:
            return

        raw = float(data.get("price", 0))
        if raw <= 0 or raw > 1.0:
            return

        info = self.prices[ws_key]
        new_price = self._to_side_price(token_id, raw)

        # Sanity: reject wild price jumps (>50% from current) — likely bad tick
        old_price = info.get("price", 0.5)
        if old_price > 0.1 and abs(new_price - old_price) > old_price * 0.5:
            log.warning(f"[WS] Wild tick rejected: {ws_key} {old_price:.4f}→{new_price:.4f} (raw={raw:.4f})")
            return

        info["price"] = new_price
        info["last_update"] = time.time()
        await self._dispatch(ws_key, info)

    async def _handle_book(self, data):
        token_id = data.get("asset_id", "")
        ws_key = self._token_to_key.get(token_id)
        if not ws_key or ws_key not in self.prices:
            return

        info = self.prices[ws_key]
        invert = self._token_invert.get(token_id, False)

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

        if not invert:
            if raw_best_bid > 0:
                info["best_bid"] = raw_best_bid
            if raw_best_ask > 0:
                info["best_ask"] = raw_best_ask
        else:
            # NO side: subscribed to YES token, invert to get NO prices
            # NO bid = 1 - YES ask (what we'd get selling NO)
            # NO ask = 1 - YES bid (what we'd pay buying NO)
            if raw_best_ask > 0:
                info["best_bid"] = round(1.0 - raw_best_ask, 4)
            if raw_best_bid > 0:
                info["best_bid_from_book"] = True  # mark that we got a real book update
                info["best_ask"] = round(1.0 - raw_best_bid, 4)

        # Sanity check: bid/ask must be in valid range and bid < ask
        bid = info.get("best_bid", 0)
        ask = info.get("best_ask", 0)
        if bid <= 0.001 or bid >= 1.0:
            info["best_bid"] = info["price"]  # fallback to last price
        if ask <= 0.001 or ask >= 1.0:
            info["best_ask"] = info["price"]

        info["last_update"] = time.time()
        await self._dispatch(ws_key, info)
