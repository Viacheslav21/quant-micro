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


class MicroWS:
    """WebSocket client for watchlist dip detection + position monitoring."""

    def __init__(self):
        self.ws = None
        self._running = False
        self._subscribed_tokens: set = set()
        self._token_to_market: dict[str, str] = {}
        self._token_side: dict[str, str] = {}  # token_id -> "yes" / "no"
        self.prices: dict[str, dict] = {}  # market_id -> {yes_price, best_bid, best_ask, ...}

        # Callbacks
        self._on_watchlist_dip: Optional[Callable] = None
        self._on_position_price: Optional[Callable] = None
        self._on_disconnect: Optional[Callable] = None
        self._on_reconnect: Optional[Callable] = None

    def set_callbacks(self, on_watchlist_dip=None, on_position_price=None,
                      on_disconnect=None, on_reconnect=None):
        self._on_watchlist_dip = on_watchlist_dip
        self._on_position_price = on_position_price
        self._on_disconnect = on_disconnect
        self._on_reconnect = on_reconnect

    def register_market(self, market_id: str, yes_token: str = None,
                        no_token: str = None, yes_price: float = 0.5,
                        question: str = "", is_position: bool = False) -> list:
        """Register a market for WS tracking. Returns tokens to subscribe."""
        tokens_to_add = []

        if yes_token and yes_token not in self._subscribed_tokens:
            self._token_to_market[yes_token] = market_id
            self._token_side[yes_token] = "yes"
            self._subscribed_tokens.add(yes_token)
            tokens_to_add.append(yes_token)

        if no_token and no_token not in self._subscribed_tokens:
            self._token_to_market[no_token] = market_id
            self._token_side[no_token] = "no"
            self._subscribed_tokens.add(no_token)
            tokens_to_add.append(no_token)

        if market_id not in self.prices:
            self.prices[market_id] = {
                "yes_price": yes_price,
                "best_bid": 0,
                "best_ask": yes_price,
                "question": question,
                "yes_token": yes_token,
                "no_token": no_token,
                "is_position": is_position,
                "peak_price": yes_price,
                "last_update": time.time(),
            }
        elif is_position:
            self.prices[market_id]["is_position"] = True

        return tokens_to_add

    def unregister_market(self, market_id: str) -> list:
        """Unregister a market. Returns tokens to unsubscribe."""
        tokens_to_remove = []
        info = self.prices.pop(market_id, None)
        if not info:
            return tokens_to_remove

        for token_id in [info.get("yes_token"), info.get("no_token")]:
            if token_id and token_id in self._subscribed_tokens:
                self._subscribed_tokens.discard(token_id)
                self._token_to_market.pop(token_id, None)
                self._token_side.pop(token_id, None)
                tokens_to_remove.append(token_id)

        return tokens_to_remove

    def mark_as_position(self, market_id: str):
        if market_id in self.prices:
            self.prices[market_id]["is_position"] = True

    def unmark_position(self, market_id: str):
        if market_id in self.prices:
            self.prices[market_id]["is_position"] = False

    def get_price(self, market_id: str) -> float:
        return self.prices.get(market_id, {}).get("yes_price", 0)

    def get_bid(self, market_id: str) -> float:
        return self.prices.get(market_id, {}).get("best_bid", 0)

    def get_spread(self, market_id: str) -> float:
        info = self.prices.get(market_id, {})
        return info.get("best_ask", 0) - info.get("best_bid", 0)

    # ── Connection ──

    async def connect(self):
        self._running = True
        while self._running:
            try:
                async with websockets.connect(WS_URL, ping_interval=None) as ws:
                    self.ws = ws
                    log.info(f"[WS] Connected, {len(self._subscribed_tokens)} tokens tracked")

                    if self._on_reconnect:
                        try:
                            await self._on_reconnect()
                        except Exception:
                            pass

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
                if self._on_disconnect:
                    try:
                        await self._on_disconnect()
                    except Exception:
                        pass
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

    # ── Subscribe / Unsubscribe ──

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
        # Polymarket WS doesn't have explicit unsubscribe,
        # but we stop tracking internally
        for tid in token_ids:
            self._subscribed_tokens.discard(tid)
            self._token_to_market.pop(tid, None)
            self._token_side.pop(tid, None)

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
        if event_type == "price_change":
            await self._handle_price_change(data)
        elif event_type == "book":
            await self._handle_book(data)
        elif event_type == "last_trade_price":
            await self._handle_trade(data)

    async def _handle_price_change(self, data):
        token_id = data.get("asset_id", "")
        market_id = self._token_to_market.get(token_id)
        if not market_id or market_id not in self.prices:
            return

        info = self.prices[market_id]
        side = self._token_side.get(token_id, "yes")

        price = float(data.get("price", 0))
        if price <= 0:
            return

        if side == "yes":
            info["yes_price"] = price
        else:
            info["yes_price"] = round(1.0 - price, 4)

        info["last_update"] = time.time()
        now_price = info["yes_price"]

        # Track peak for dip detection
        if now_price > info.get("peak_price", 0):
            info["peak_price"] = now_price

        # Dispatch to appropriate callback
        if info.get("is_position") and self._on_position_price:
            try:
                await self._on_position_price(market_id, now_price, info)
            except Exception as e:
                log.error(f"[WS] position callback error: {e}")
        elif not info.get("is_position") and self._on_watchlist_dip:
            try:
                await self._on_watchlist_dip(market_id, now_price, info)
            except Exception as e:
                log.error(f"[WS] watchlist callback error: {e}")

    async def _handle_book(self, data):
        token_id = data.get("asset_id", "")
        market_id = self._token_to_market.get(token_id)
        if not market_id or market_id not in self.prices:
            return

        info = self.prices[market_id]
        side = self._token_side.get(token_id, "yes")

        bids = data.get("bids", [])
        asks = data.get("asks", [])

        if side == "yes":
            if bids:
                info["best_bid"] = float(bids[0].get("price", 0))
            if asks:
                info["best_ask"] = float(asks[0].get("price", 0))
        # For NO token, invert
        else:
            if asks:
                info["best_bid"] = round(1.0 - float(asks[0].get("price", 0)), 4)
            if bids:
                info["best_ask"] = round(1.0 - float(bids[0].get("price", 0)), 4)

        info["last_update"] = time.time()

        # Also trigger position callback on book updates (for SL/TP on bid price)
        if info.get("is_position") and self._on_position_price:
            now_price = info["yes_price"]
            try:
                await self._on_position_price(market_id, now_price, info)
            except Exception as e:
                log.error(f"[WS] position callback error: {e}")

    async def _handle_trade(self, data):
        token_id = data.get("asset_id", "")
        market_id = self._token_to_market.get(token_id)
        if not market_id or market_id not in self.prices:
            return

        info = self.prices[market_id]
        side = self._token_side.get(token_id, "yes")

        price = float(data.get("price", 0))
        if price <= 0:
            return

        if side == "yes":
            info["yes_price"] = price
        else:
            info["yes_price"] = round(1.0 - price, 4)

        info["last_update"] = time.time()
        now_price = info["yes_price"]

        if now_price > info.get("peak_price", 0):
            info["peak_price"] = now_price

        if info.get("is_position") and self._on_position_price:
            try:
                await self._on_position_price(market_id, now_price, info)
            except Exception as e:
                log.error(f"[WS] position callback error: {e}")
        elif not info.get("is_position") and self._on_watchlist_dip:
            try:
                await self._on_watchlist_dip(market_id, now_price, info)
            except Exception as e:
                log.error(f"[WS] watchlist callback error: {e}")
