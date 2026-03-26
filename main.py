"""
quant-micro — Resolution harvester for Polymarket.

Strategy:
  1. Scanner finds NON-RISKY markets expiring within 5 days:
     a) ≥93¢ → enter immediately (high probability, near resolution)
     b) 88-93¢ → watchlist, WS monitors. When price hits 93¢ → enter
  2. WS monitors positions: dynamic SL (3-7%), resolution at ≥99¢
  3. Micro stakes ($1-5), quality-scored, risky patterns excluded
  4. Time-based exit: close if price < entry with <4h to resolution
  5. Risky filter: sports/esports always blocked; crypto/markets only blocked
     if question is a PRICE BET (will X be above $Y). Non-price crypto OK.
"""

import asyncio
import logging
import os
import signal
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from engine.scanner import MicroScanner, is_risky_market, classify_theme
from engine.ws_client import MicroWS
from utils.db import Database
from utils.telegram import TelegramBot

# ── Config ──

CONFIG = {
    "TELEGRAM_TOKEN":     os.getenv("TELEGRAM_BOT_TOKEN"),
    "TELEGRAM_CHAT_ID":   os.getenv("TELEGRAM_CHAT_ID"),
    "BANKROLL":           float(os.getenv("BANKROLL", "500")),
    "SIMULATION":         os.getenv("SIMULATION", "true").lower() == "true",
    "SCAN_INTERVAL":      int(os.getenv("SCAN_INTERVAL", "120")),
    "MAX_STAKE":          float(os.getenv("MAX_STAKE", "5.0")),       # $5 max
    "MIN_STAKE":          float(os.getenv("MIN_STAKE", "1.0")),
    "MAX_OPEN":           int(os.getenv("MAX_OPEN", "50")),
    "SL_PCT":             float(os.getenv("SL_PCT", "0.05")),        # 5% default
    "ENTRY_MIN_PRICE":    float(os.getenv("ENTRY_MIN_PRICE", "0.93")), # 93¢ direct entry
    "WATCHLIST_MIN_PRICE": float(os.getenv("WATCHLIST_MIN_PRICE", "0.88")), # 88¢ watchlist
    "MAX_DAYS_LEFT":      float(os.getenv("MAX_DAYS_LEFT", "10")),   # 10 days — parse dates from questions
    "MIN_ROI":            float(os.getenv("MIN_ROI", "0.03")),
    "MIN_LIQUIDITY_MULT": float(os.getenv("MIN_LIQUIDITY_MULT", "500")),
    "MAX_SPREAD":         float(os.getenv("MAX_SPREAD", "0.02")),    # 2¢ tight spread
    "RESOLUTION_PRICE":   float(os.getenv("RESOLUTION_PRICE", "0.99")),
    "MAX_PER_THEME":      int(os.getenv("MAX_PER_THEME", "5")),     # limit correlated risk
    "CONFIG_TAG":         os.getenv("CONFIG_TAG", "micro-v4"),
    "SCAN_PAGES":         int(os.getenv("SCAN_PAGES", "16")),        # 1600 markets
    "MIN_VOLUME":         float(os.getenv("MIN_VOLUME", "50000")),   # 50k volume
    "MIN_QUALITY_SCORE":  float(os.getenv("MIN_QUALITY_SCORE", "35")), # quality gate
    "TIME_EXIT_HOURS":    float(os.getenv("TIME_EXIT_HOURS", "4")),  # exit if losing <4h before expiry
}

# ── Logging ──

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-18s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("micro.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("micro")

# ── Globals ──

_shutdown = False
_last_stake_warn = 0.0  # throttle "stake too small" warnings


def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True
    log.info(f"[MAIN] Shutdown signal received ({sig})")


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ── Stake Calculation ──

def calc_stake(bankroll: float) -> float:
    """Stake = min(MAX_STAKE, 1% of bankroll), but at least MIN_STAKE if bankroll allows."""
    pct_stake = bankroll * 0.01
    stake = min(CONFIG["MAX_STAKE"], max(pct_stake, CONFIG["MIN_STAKE"]))
    # Don't stake if bankroll can't afford it
    if stake > bankroll:
        return 0.0
    return round(stake, 2)


# ── Entry Logic ──

async def try_enter(candidate: dict, db: Database, ws: MicroWS,
                    tg: TelegramBot, source: str = "scan") -> bool:
    """Try to enter a position. Returns True if entered."""
    global _last_stake_warn

    market_id = candidate["market_id"]
    side = candidate.get("side", "YES")

    # Double-check: reject risky markets even if scanner passed them
    question = candidate.get("question", "")
    theme = candidate.get("theme", "other")
    if is_risky_market(question, theme):
        return False

    if await db.has_position_on_market(market_id, side):
        return False

    open_pos = await db.get_open_positions()
    if len(open_pos) >= CONFIG["MAX_OPEN"]:
        return False

    theme_count = sum(1 for p in open_pos if p.get("theme") == theme)
    if theme_count >= CONFIG["MAX_PER_THEME"]:
        return False

    stats = await db.get_stats()
    bankroll = stats.get("bankroll", CONFIG["BANKROLL"])
    stake = calc_stake(bankroll)

    if stake < CONFIG["MIN_STAKE"]:
        now = time.time()
        if now - _last_stake_warn > 300:  # warn at most every 5 min
            log.warning(f"[ENTRY] Bankroll ${bankroll:.2f} too low for MIN_STAKE ${CONFIG['MIN_STAKE']}")
            _last_stake_warn = now
        return False

    entry_price = candidate.get("best_ask") or candidate["price"]
    if entry_price <= 0:
        entry_price = candidate["price"]

    roi = (1.0 - entry_price) / entry_price
    if roi < CONFIG["MIN_ROI"]:
        return False

    # Quality gate
    quality = candidate.get("quality", 0)
    if quality < CONFIG["MIN_QUALITY_SCORE"]:
        return False

    # ── Dynamic SL: tighter for longer-dated, very tight overall ──
    days_left = candidate.get("days_left", 0)
    if days_left <= 0.5:
        sl_pct = 0.07  # 7% — resolves very soon, hold tighter
    elif days_left <= 1:
        sl_pct = 0.05  # 5%
    elif days_left <= 2:
        sl_pct = 0.04  # 4% — 1-2 days
    else:
        sl_pct = 0.03  # 3% — 2-5 days, cut losses fast

    # ── Execute ──
    pos_id = f"mic_{market_id[:8]}_{int(time.time())}"
    pos = {
        "id": pos_id,
        "market_id": market_id,
        "question": question,
        "theme": theme,
        "side": side,
        "entry_price": round(entry_price, 4),
        "stake_amt": stake,
        "sl_pct": sl_pct,
        "config_tag": CONFIG["CONFIG_TAG"],
        "end_date": candidate.get("end_date"),
    }

    await db.save_position(pos)
    await db.deduct_stake(stake)

    await db.upsert_watchlist(candidate)

    # Register in WS for position monitoring
    ws_key = f"{market_id}_{side}"
    ws.mark_as_position(ws_key)
    if ws_key not in ws.prices:
        ws_token = candidate.get("ws_token")
        ws_side = candidate.get("ws_side", side.lower())
        tokens = ws.register_market(
            ws_key,
            token_id=ws_token,
            token_side=ws_side,
            price=entry_price,
            question=question,
            is_position=True,
        )
        if tokens:
            await ws.subscribe_tokens(tokens)

    mode = "SIM" if CONFIG["SIMULATION"] else "REAL"
    days = candidate.get("days_left", "?")
    log.info(
        f"[ENTRY] {mode} {source.upper()} {side} '{question[:50]}' "
        f"@ {entry_price:.2f}¢ ${stake:.2f} | ROI {roi:.1%} | SL {sl_pct:.0%} | "
        f"Q={quality:.0f} | {days}d left"
    )

    await db.log_event("OPEN", market_id, {
        "side": side, "entry_price": entry_price, "stake": stake,
        "roi": round(roi, 4), "days_left": days, "quality": quality,
        "spread": candidate.get("spread", 0), "source": source, "mode": mode,
    })

    await tg.send(
        f"<b>MICRO {source.upper()}</b> {mode}\n"
        f"{side} <b>{question[:60]}</b>\n"
        f"Entry: {entry_price:.2f}¢ | Stake: ${stake:.2f}\n"
        f"ROI: {roi:.1%} | Q={quality:.0f} | {days}d left"
    )
    return True


# ── WS Watchlist Callback ──

async def check_watchlist_price(ws_key: str, price: float, info: dict,
                                 db: Database, ws: MicroWS, tg: TelegramBot):
    """WS callback: watchlist price updated. Enter if it hit entry zone."""
    if _shutdown or price < CONFIG["ENTRY_MIN_PRICE"]:
        return

    parts = ws_key.rsplit("_", 1)
    if len(parts) != 2:
        return
    market_id, side = parts

    spread = ws.get_spread(ws_key)
    if spread > CONFIG["MAX_SPREAD"]:
        return

    wl = await db.get_watchlist_market(market_id)
    if not wl:
        return

    candidate = {
        "market_id": market_id,
        "question": info.get("question", wl.get("question", "")),
        "theme": wl.get("theme", "other"),
        "side": side,
        "price": price,
        "best_ask": info.get("best_ask", price),
        "days_left": wl.get("days_left", "?"),
        "spread": spread,
        "quality": wl.get("quality", wl.get("roi", 0) * 500),  # estimate if not stored
        "yes_token": wl.get("yes_token"),
        "no_token": wl.get("no_token"),
        # Always use YES token for WS; ws_side tells client to invert for NO
        "ws_token": wl.get("yes_token"),
        "ws_side": "no" if side == "NO" else "yes",
        "end_date": wl.get("end_date"),
    }

    entered = await try_enter(candidate, db, ws, tg, source="ws")
    if entered:
        log.info(f"[WS→ENTRY] {side} hit {price:.2f}¢ on '{info.get('question', '')[:40]}'")


# ── Position Monitoring ──

async def check_position_price(ws_key: str, price: float, info: dict,
                                db: Database, ws: MicroWS, tg: TelegramBot):
    """WS callback: position price updated. Check SL/resolution/time-exit."""
    if _shutdown:
        return

    parts = ws_key.rsplit("_", 1)
    if len(parts) != 2:
        return
    market_id, side = parts

    open_pos = await db.get_open_positions()
    pos = None
    for p in open_pos:
        if p["market_id"] == market_id and p["side"] == side:
            pos = p
            break

    if not pos:
        ws.unmark_position(ws_key)
        return

    entry_price = pos["entry_price"]
    stake = pos["stake_amt"]
    sl_pct = pos.get("sl_pct", CONFIG["SL_PCT"])

    bid_price = info.get("best_bid", price)
    if bid_price <= 0:
        bid_price = price

    # Sanity: for 90%+ entries, bid can't realistically drop >50% without resolution
    # If bid_price is absurdly low, it's a bad WS tick — use last known good price or entry
    if bid_price < entry_price * 0.5:
        log.warning(
            f"[SANITY] {market_id[:8]} {side} bid={bid_price:.4f} << entry={entry_price:.4f} — "
            f"ignoring bad tick (price={price:.4f}, best_bid={info.get('best_bid')}, best_ask={info.get('best_ask')})"
        )
        return  # skip this tick entirely, don't update DB with garbage

    pnl_pct = (bid_price - entry_price) / entry_price
    pnl_dollar = pnl_pct * stake

    await db.update_position_price(pos["id"], bid_price, round(pnl_dollar, 4))

    # ── Resolution WIN: our side price → 99¢+ ──
    if price >= CONFIG["RESOLUTION_PRICE"]:
        pnl = ((1.0 - entry_price) / entry_price) * stake
        closed = await db.close_position(pos["id"], round(pnl, 4), "WIN", "resolved")
        if closed:
            ws.unmark_position(ws_key)
            log.info(f"[RESOLVED] WIN {side} '{pos['question'][:40]}' PnL: +${pnl:.2f}")
            await db.log_event("CLOSE_RESOLVED", market_id, {
                "side": side, "pnl": round(pnl, 4), "entry": entry_price, "exit": 1.0,
            })
            await tg.send(
                f"<b>RESOLVED WIN</b> {side}\n{pos['question'][:60]}\nPnL: +${pnl:.2f}"
            )
        return

    # ── Stop Loss ──
    if pnl_pct <= -sl_pct:
        pnl = pnl_pct * stake
        closed = await db.close_position(pos["id"], round(pnl, 4), "LOSS", "stop_loss")
        if closed:
            ws.unmark_position(ws_key)
            log.info(
                f"[SL] LOSS {side} '{pos['question'][:40]}' @ {bid_price:.2f}¢ "
                f"PnL: ${pnl:.2f} ({pnl_pct:+.1%})"
            )
            await db.log_event("CLOSE_SL", market_id, {
                "side": side, "pnl": round(pnl, 4), "entry": entry_price, "exit": bid_price,
            })
            await tg.send(
                f"<b>SL LOSS</b> {side}\n{pos['question'][:60]}\n"
                f"Entry: {entry_price:.2f}¢ → {bid_price:.2f}¢\n"
                f"PnL: ${pnl:.2f} ({pnl_pct:+.1%})"
            )
        return

    # ── Rapid Drop Guard ──
    # If price dropped >5¢ from entry, exit immediately (something went wrong)
    if bid_price < entry_price - 0.05:
        pnl = pnl_pct * stake
        closed = await db.close_position(pos["id"], round(pnl, 4), "LOSS", "rapid_drop")
        if closed:
            ws.unmark_position(ws_key)
            log.info(
                f"[RAPID DROP] LOSS {side} '{pos['question'][:40]}' @ {bid_price:.2f}¢ "
                f"PnL: ${pnl:.2f} ({pnl_pct:+.1%})"
            )
            await db.log_event("CLOSE_RAPID", market_id, {
                "side": side, "pnl": round(pnl, 4), "entry": entry_price, "exit": bid_price,
            })
            await tg.send(
                f"<b>RAPID DROP</b> {side}\n{pos['question'][:60]}\n"
                f"Entry: {entry_price:.2f}¢ → {bid_price:.2f}¢\n"
                f"PnL: ${pnl:.2f} ({pnl_pct:+.1%})"
            )
        return


# ── Time-based Exit Check ──

async def check_time_exits(db: Database, ws: MicroWS, tg: TelegramBot):
    """Close positions that are losing with <TIME_EXIT_HOURS hours until resolution."""
    open_pos = await db.get_open_positions()
    now = datetime.now(timezone.utc)
    exit_hours = CONFIG["TIME_EXIT_HOURS"]

    for pos in open_pos:
        end_date_str = pos.get("end_date")
        if not end_date_str:
            continue
        try:
            end = datetime.fromisoformat(str(end_date_str).replace("Z", "+00:00"))
        except Exception:
            continue

        hours_left = (end - now).total_seconds() / 3600
        if hours_left > exit_hours:
            continue

        # Only exit if position is in the red
        entry_price = pos["entry_price"]
        current_price = pos.get("current_price", entry_price)
        if current_price >= entry_price:
            continue  # in profit or flat, let it ride to resolution

        pnl_pct = (current_price - entry_price) / entry_price
        pnl = pnl_pct * pos["stake_amt"]

        closed = await db.close_position(pos["id"], round(pnl, 4), "LOSS", "time_exit")
        if closed:
            side = pos["side"]
            ws_key = f"{pos['market_id']}_{side}"
            ws.unmark_position(ws_key)
            log.info(
                f"[TIME EXIT] LOSS {side} '{pos['question'][:40]}' @ {current_price:.2f}¢ "
                f"PnL: ${pnl:.2f} ({pnl_pct:+.1%}) | {hours_left:.1f}h left"
            )
            await db.log_event("CLOSE_TIME", pos["market_id"], {
                "side": side, "pnl": round(pnl, 4), "entry": entry_price,
                "exit": current_price, "hours_left": round(hours_left, 1),
            })
            await tg.send(
                f"<b>TIME EXIT</b> {side}\n{pos['question'][:60]}\n"
                f"Entry: {entry_price:.2f}¢ → {current_price:.2f}¢\n"
                f"PnL: ${pnl:.2f} | {hours_left:.1f}h to expiry"
            )


# ── Main Loop ──

async def main():
    global _shutdown

    log.info("=" * 60)
    log.info("[MAIN] quant-micro v3 (resolution harvester)")
    log.info(f"[MAIN] Simulation: {CONFIG['SIMULATION']}")
    log.info(f"[MAIN] Direct entry: ≥{CONFIG['ENTRY_MIN_PRICE']:.0%}")
    log.info(f"[MAIN] Watchlist: {CONFIG['WATCHLIST_MIN_PRICE']:.0%}-{CONFIG['ENTRY_MIN_PRICE']:.0%}")
    log.info(f"[MAIN] Max days: {CONFIG['MAX_DAYS_LEFT']}, ROI≥{CONFIG['MIN_ROI']:.0%}")
    log.info(f"[MAIN] Max stake: ${CONFIG['MAX_STAKE']}, SL: 3-7% (dynamic)")
    log.info(f"[MAIN] Max open: {CONFIG['MAX_OPEN']}, per theme: {CONFIG['MAX_PER_THEME']}")
    log.info(f"[MAIN] Spread: <{CONFIG['MAX_SPREAD']:.0%}, Quality≥{CONFIG['MIN_QUALITY_SCORE']}")
    log.info(f"[MAIN] Risky themes excluded, time exit <{CONFIG['TIME_EXIT_HOURS']}h")
    log.info("=" * 60)

    db = Database()
    await db.init()

    # ── Fresh start: reset all stats, positions, watchlist ──
    RESET_ON_START = os.getenv("RESET_ON_START", "false").lower() == "true"
    if RESET_ON_START:
        await db.reset_stats(CONFIG["BANKROLL"])
        log.info(f"[MAIN] RESET: clean slate, bankroll=${CONFIG['BANKROLL']}")

    tg = TelegramBot(CONFIG["TELEGRAM_TOKEN"], CONFIG["TELEGRAM_CHAT_ID"])
    scanner = MicroScanner(CONFIG)
    ws = MicroWS()

    # WS callbacks
    async def on_watchlist_price(ws_key, price, info):
        await check_watchlist_price(ws_key, price, info, db, ws, tg)

    async def on_position_price(ws_key, price, info):
        await check_position_price(ws_key, price, info, db, ws, tg)

    ws.set_callbacks(
        on_watchlist_price=on_watchlist_price,
        on_position_price=on_position_price,
    )

    # Restore open positions into WS
    # Fetch fresh token IDs from Polymarket API for each open position
    open_pos = await db.get_open_positions()
    restored = 0
    for pos in open_pos:
        side = pos.get("side", "YES")
        ws_key = f"{pos['market_id']}_{side}"

        # Try watchlist first (fast, local)
        wl = await db.get_watchlist_market(pos["market_id"])
        token_id = wl.get("yes_token") if wl else None

        # If no token in watchlist, fetch from API
        if not token_id:
            try:
                import json as _json
                r = await scanner.client.get(f"https://gamma-api.polymarket.com/markets/{pos['market_id']}")
                if r.status_code == 200:
                    mdata = r.json()
                    tids = mdata.get("clobTokenIds") or []
                    if isinstance(tids, str):
                        tids = _json.loads(tids)
                    token_id = tids[0] if tids else None  # YES token
                    if token_id:
                        log.info(f"[RESTORE] Fetched YES token for {pos['market_id'][:8]} from API")
            except Exception as e:
                log.warning(f"[RESTORE] Failed to fetch token for {pos['market_id'][:8]}: {e}")

        if token_id:
            ws.register_market(
                ws_key,
                token_id=token_id,
                token_side=side.lower(),  # "no" → ws_client will invert
                price=pos.get("entry_price", 0.9),
                question=pos.get("question", ""),
                is_position=True,
            )
            restored += 1
        else:
            log.warning(f"[RESTORE] No token for {pos['market_id'][:8]} {side} — will not monitor")
    if open_pos:
        log.info(f"[MAIN] Restored {restored}/{len(open_pos)} positions to WS")

    ws_task = asyncio.create_task(ws.connect())

    await db.log_event("STARTUP", details={
        "config": {k: v for k, v in CONFIG.items() if k not in ("TELEGRAM_TOKEN",)},
        "open_positions": len(open_pos),
    })

    await tg.send(
        f"<b>quant-micro v3 started</b>\n"
        f"Mode: {'SIM' if CONFIG['SIMULATION'] else 'REAL'}\n"
        f"Entry: ≥{CONFIG['ENTRY_MIN_PRICE']:.0%} | WL: {CONFIG['WATCHLIST_MIN_PRICE']:.0%}+\n"
        f"Quality≥{CONFIG['MIN_QUALITY_SCORE']} | Risky themes excluded\n"
        f"Open: {len(open_pos)} positions"
    )

    # ── Scan Loop ──

    scan_count = 0

    while not _shutdown:
        try:
            scan_count += 1
            log.info(f"[SCAN #{scan_count}] Starting...")

            # 1. Time-based exits (check every scan)
            await check_time_exits(db, ws, tg)

            # 2. Fetch candidates
            direct, watchlist = await scanner.fetch_candidates()

            # 3. Direct entries (≥93¢)
            entered = 0
            for c in direct:
                if _shutdown:
                    break
                if await try_enter(c, db, ws, tg, source="scan"):
                    entered += 1
                    await asyncio.sleep(0.5)

            # 4. Watchlist (88-93¢) → register in WS for price monitoring
            new_ws = 0
            for c in watchlist:
                await db.upsert_watchlist(c)
                ws_key = f"{c['market_id']}_{c['side']}"
                if ws_key not in ws.prices:
                    # ws_token already points to YES token (scanner fixed)
                    # ws_side tells ws_client whether to invert
                    tokens = ws.register_market(
                        ws_key,
                        token_id=c.get("ws_token"),
                        token_side=c.get("ws_side", c["side"].lower()),
                        price=c["price"],
                        question=c["question"],
                        is_position=False,
                    )
                    if tokens:
                        await ws.subscribe_tokens(tokens)
                        new_ws += 1

            # 5. Status
            open_pos = await db.get_open_positions()
            stats = await db.get_stats()
            bankroll = stats.get("bankroll", CONFIG["BANKROLL"])
            total_unrealized = sum(p.get("unrealized_pnl", 0) for p in open_pos)
            equity = bankroll + total_unrealized
            await db.update_peak_equity(equity)

            log.info(
                f"[SCAN #{scan_count}] Direct: {len(direct)} ({entered} entered) | "
                f"WL: {len(watchlist)} ({new_ws} new WS) | Open: {len(open_pos)} | "
                f"Bankroll: ${bankroll:.2f} | Equity: ${equity:.2f} | "
                f"PnL: ${stats.get('total_pnl', 0):.2f} | "
                f"W/L: {stats.get('wins', 0)}/{stats.get('losses', 0)}"
            )

            # 6. Cleanup stale WS (every 30 scans)
            if scan_count % 30 == 0:
                await db.cleanup_watchlist()
                wl_data = await db.get_watchlist()
                wl_ids = {w["market_id"] for w in wl_data}
                pos_keys = {f"{p['market_id']}_{p['side']}" for p in open_pos}
                to_remove = []
                for ws_key in list(ws.prices.keys()):
                    market_id = ws_key.rsplit("_", 1)[0] if "_" in ws_key else ws_key
                    if market_id not in wl_ids and ws_key not in pos_keys:
                        to_remove.append(ws_key)
                for ws_key in to_remove:
                    tokens = ws.unregister_market(ws_key)
                    await ws.unsubscribe_tokens(tokens)
                if to_remove:
                    log.info(f"[CLEANUP] Removed {len(to_remove)} stale WS entries")

            await db.log_event("SCAN", details={
                "scan": scan_count, "direct": len(direct), "entered": entered,
                "watchlist": len(watchlist), "new_ws": new_ws,
                "open": len(open_pos), "bankroll": round(bankroll, 2),
            })

            await asyncio.sleep(CONFIG["SCAN_INTERVAL"])

        except Exception as e:
            log.error(f"[MAIN] Loop error: {e}", exc_info=True)
            await asyncio.sleep(5)

    # ── Shutdown ──

    log.info("[MAIN] Shutting down...")
    await db.log_event("SHUTDOWN")
    await ws.stop()
    ws_task.cancel()
    await scanner.close()
    await tg.send("<b>quant-micro stopped</b>")
    await tg.close()
    await db.close()
    log.info("[MAIN] Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
