"""
quant-micro — Micro-stake sniper for high-probability Polymarket markets.

Strategy:
  1. Scanner finds markets at 85-95¢ (high probability, near resolution)
  2. Markets go into watchlist, WebSocket monitors prices in real-time
  3. On dip (price drops DIP_TRIGGER_PCT from peak) → enter with micro stake ($1-5)
  4. SL: 10% from entry (hard cut — something changed if it drops that much)
  5. TP: resolution to 100¢, or configurable profit target
  6. Many small positions (up to 150) = smooth equity curve
"""

import asyncio
import logging
import os
import signal
import time

from dotenv import load_dotenv

load_dotenv()

from engine.scanner import MicroScanner
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
    "MAX_STAKE":          float(os.getenv("MAX_STAKE", "5.0")),
    "MIN_STAKE":          float(os.getenv("MIN_STAKE", "1.0")),
    "MAX_OPEN":           int(os.getenv("MAX_OPEN", "150")),
    "SL_PCT":             float(os.getenv("SL_PCT", "0.10")),
    "TP_PCT":             float(os.getenv("TP_PCT", "0.05")),
    "WATCHLIST_MIN_PRICE": float(os.getenv("WATCHLIST_MIN_PRICE", "0.85")),
    "WATCHLIST_MAX_PRICE": float(os.getenv("WATCHLIST_MAX_PRICE", "0.95")),
    "DIP_TRIGGER_PCT":    float(os.getenv("DIP_TRIGGER_PCT", "0.02")),
    "MIN_VOLUME":         float(os.getenv("MIN_VOLUME", "50000")),
    "MIN_LIQUIDITY":      float(os.getenv("MIN_LIQUIDITY", "5000")),
    "MAX_SPREAD":         float(os.getenv("MAX_SPREAD", "0.03")),
    "RESOLUTION_PRICE":   float(os.getenv("RESOLUTION_PRICE", "0.99")),
    "MAX_PER_THEME":      int(os.getenv("MAX_PER_THEME", "20")),
    "CONFIG_TAG":         os.getenv("CONFIG_TAG", "micro-v1"),
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
_cooldowns: dict[str, float] = {}  # market_id -> last entry timestamp
COOLDOWN_SEC = 300  # 5 min cooldown per market after entry


def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True
    log.info(f"[MAIN] Shutdown signal received ({sig})")


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ── Core Logic ──

async def check_dip_entry(market_id: str, price: float, info: dict,
                          db: Database, ws: MicroWS, tg: TelegramBot):
    """Called by WS when a watchlist market price updates. Check for dip entry."""
    if _shutdown:
        return

    peak = info.get("peak_price", price)
    if peak <= 0:
        return

    dip_pct = (peak - price) / peak
    if dip_pct < CONFIG["DIP_TRIGGER_PCT"]:
        return  # no dip yet

    log.info(f"[DIP] Detected {dip_pct:.1%} dip on {market_id[:8]} "
             f"peak={peak:.3f} now={price:.3f} q='{info.get('question', '')[:40]}'")

    # Price must still be in a reasonable range (not crashing)
    if price < 0.80:
        return

    # Cooldown check
    last_entry = _cooldowns.get(market_id, 0)
    if time.time() - last_entry < COOLDOWN_SEC:
        return

    # Spread check from live WS data
    spread = ws.get_spread(market_id)
    if spread > CONFIG["MAX_SPREAD"]:
        log.debug(f"[DIP] Skip {market_id[:8]}: spread {spread:.3f} > {CONFIG['MAX_SPREAD']}")
        return

    # Already have position?
    if await db.has_position_on_market(market_id):
        return

    # Max open check
    open_pos = await db.get_open_positions()
    if len(open_pos) >= CONFIG["MAX_OPEN"]:
        return

    # Theme limit check
    question = info.get("question", "")
    theme = "other"
    wl = await db.get_watchlist_market(market_id)
    if wl:
        theme = wl.get("theme", "other")

    theme_count = sum(1 for p in open_pos if p.get("theme") == theme)
    if theme_count >= CONFIG["MAX_PER_THEME"]:
        log.debug(f"[DIP] Skip: theme '{theme}' full ({theme_count})")
        return

    # Compute stake
    stats = await db.get_stats()
    bankroll = stats.get("bankroll", CONFIG["BANKROLL"])
    stake = min(CONFIG["MAX_STAKE"], bankroll * 0.01)  # 1% of bankroll, capped at MAX_STAKE
    stake = round(stake, 2)

    if stake < CONFIG["MIN_STAKE"]:
        log.warning(f"[DIP] Stake ${stake:.2f} too small, skipping")
        return

    # Entry price = best ask (what we'd actually pay)
    entry_price = info.get("best_ask", price)
    if entry_price <= 0:
        entry_price = price

    # Expected profit at resolution
    profit_at_resolve = (1.0 - entry_price) * stake
    roi_at_resolve = (1.0 - entry_price) / entry_price

    # Only enter if ROI at resolution > 3% (very conservative floor)
    if roi_at_resolve < 0.03:
        return

    # ── Execute ──

    pos_id = f"mic_{market_id[:8]}_{int(time.time())}"
    pos = {
        "id": pos_id,
        "market_id": market_id,
        "question": question or info.get("question", ""),
        "theme": theme,
        "side": "YES",
        "entry_price": round(entry_price, 4),
        "stake_amt": stake,
        "tp_pct": CONFIG["TP_PCT"],
        "sl_pct": CONFIG["SL_PCT"],
        "config_tag": CONFIG["CONFIG_TAG"],
    }

    await db.save_position(pos)
    _cooldowns[market_id] = time.time()

    # Mark in WS as position (triggers position monitoring instead of dip detection)
    ws.mark_as_position(market_id)

    mode = "SIM" if CONFIG["SIMULATION"] else "REAL"
    log.info(
        f"[ENTRY] {mode} YES '{question[:50]}' @ {entry_price:.2f}¢ "
        f"${stake:.2f} | dip {dip_pct:.1%} from peak {peak:.2f}¢ | "
        f"ROI@resolve {roi_at_resolve:.1%}"
    )

    await db.log_event("OPEN", market_id, {
        "entry_price": entry_price, "stake": stake, "dip_pct": round(dip_pct, 4),
        "peak": peak, "spread": round(spread, 4), "roi_at_resolve": round(roi_at_resolve, 4),
        "mode": mode,
    })

    await tg.send(
        f"<b>MICRO</b> {'SIM' if CONFIG['SIMULATION'] else 'REAL'}\n"
        f"YES <b>{question[:60]}</b>\n"
        f"Entry: {entry_price:.2f}¢ | Stake: ${stake:.2f}\n"
        f"Dip: {dip_pct:.1%} | ROI@resolve: {roi_at_resolve:.1%}"
    )


async def check_position_price(market_id: str, price: float, info: dict,
                                db: Database, ws: MicroWS, tg: TelegramBot):
    """Called by WS when a position market price updates. Check SL/TP/resolution."""
    if _shutdown:
        return

    open_pos = await db.get_open_positions()
    pos = None
    for p in open_pos:
        if p["market_id"] == market_id:
            pos = p
            break

    if not pos:
        ws.unmark_position(market_id)
        return

    entry_price = pos["entry_price"]
    stake = pos["stake_amt"]
    sl_pct = pos.get("sl_pct", CONFIG["SL_PCT"])
    tp_pct = pos.get("tp_pct", CONFIG["TP_PCT"])

    # Use bid price for realistic exit
    bid_price = info.get("best_bid", price)
    if bid_price <= 0:
        bid_price = price

    pnl_pct = (bid_price - entry_price) / entry_price
    pnl_dollar = pnl_pct * stake

    # Update position price in DB (throttled — only if changed significantly)
    await db.update_position_price(pos["id"], bid_price, round(pnl_dollar, 4))

    # ── Resolution detection ──
    if price >= CONFIG["RESOLUTION_PRICE"]:
        pnl = (1.0 - entry_price) * stake  # full resolution payout
        closed = await db.close_position(pos["id"], round(pnl, 4), "WIN", "resolved")
        if closed:
            ws.unmark_position(market_id)
            log.info(f"[RESOLVED] WIN '{pos['question'][:40]}' PnL: +${pnl:.2f}")
            await db.log_event("CLOSE_RESOLVED", market_id, {
                "pnl": round(pnl, 4), "entry": entry_price, "exit": 1.0,
            })
            await tg.send(
                f"<b>RESOLVED WIN</b>\n"
                f"{pos['question'][:60]}\n"
                f"PnL: +${pnl:.2f}"
            )
        return

    # ── Stop Loss ──
    if pnl_pct <= -sl_pct:
        pnl = pnl_pct * stake
        closed = await db.close_position(pos["id"], round(pnl, 4), "LOSS", "stop_loss")
        if closed:
            ws.unmark_position(market_id)
            log.info(
                f"[SL] LOSS '{pos['question'][:40]}' @ {bid_price:.2f}¢ "
                f"PnL: ${pnl:.2f} ({pnl_pct:+.1%})"
            )
            await db.log_event("CLOSE_SL", market_id, {
                "pnl": round(pnl, 4), "entry": entry_price, "exit": bid_price,
                "pnl_pct": round(pnl_pct, 4),
            })
            await tg.send(
                f"<b>SL LOSS</b>\n"
                f"{pos['question'][:60]}\n"
                f"Entry: {entry_price:.2f}¢ → Exit: {bid_price:.2f}¢\n"
                f"PnL: ${pnl:.2f} ({pnl_pct:+.1%})"
            )
        return

    # ── Take Profit ──
    if pnl_pct >= tp_pct:
        pnl = pnl_pct * stake
        closed = await db.close_position(pos["id"], round(pnl, 4), "WIN", "take_profit")
        if closed:
            ws.unmark_position(market_id)
            log.info(
                f"[TP] WIN '{pos['question'][:40]}' @ {bid_price:.2f}¢ "
                f"PnL: +${pnl:.2f} ({pnl_pct:+.1%})"
            )
            await db.log_event("CLOSE_TP", market_id, {
                "pnl": round(pnl, 4), "entry": entry_price, "exit": bid_price,
                "pnl_pct": round(pnl_pct, 4),
            })
            await tg.send(
                f"<b>TP WIN</b>\n"
                f"{pos['question'][:60]}\n"
                f"Entry: {entry_price:.2f}¢ → Exit: {bid_price:.2f}¢\n"
                f"PnL: +${pnl:.2f} ({pnl_pct:+.1%})"
            )
        return


# ── Main Loop ──

async def main():
    global _shutdown

    log.info("=" * 60)
    log.info("[MAIN] quant-micro starting")
    log.info(f"[MAIN] Simulation: {CONFIG['SIMULATION']}")
    log.info(f"[MAIN] Watchlist zone: {CONFIG['WATCHLIST_MIN_PRICE']:.0%}-{CONFIG['WATCHLIST_MAX_PRICE']:.0%}")
    log.info(f"[MAIN] Max stake: ${CONFIG['MAX_STAKE']}, SL: {CONFIG['SL_PCT']:.0%}, TP: {CONFIG['TP_PCT']:.0%}")
    log.info(f"[MAIN] Dip trigger: {CONFIG['DIP_TRIGGER_PCT']:.0%}")
    log.info(f"[MAIN] Max open: {CONFIG['MAX_OPEN']}")
    log.info("=" * 60)

    # Init services
    db = Database()
    await db.init()

    tg = TelegramBot(CONFIG["TELEGRAM_TOKEN"], CONFIG["TELEGRAM_CHAT_ID"])
    scanner = MicroScanner(CONFIG)
    ws = MicroWS()

    # WS callbacks — closures over db, ws, tg
    async def on_watchlist_dip(market_id, price, info):
        await check_dip_entry(market_id, price, info, db, ws, tg)

    async def on_position_price(market_id, price, info):
        await check_position_price(market_id, price, info, db, ws, tg)

    ws.set_callbacks(
        on_watchlist_dip=on_watchlist_dip,
        on_position_price=on_position_price,
    )

    # Restore open positions into WS
    open_pos = await db.get_open_positions()
    for pos in open_pos:
        wl = await db.get_watchlist_market(pos["market_id"])
        if wl and wl.get("yes_token"):
            ws.register_market(
                pos["market_id"],
                yes_token=wl.get("yes_token"),
                no_token=wl.get("no_token"),
                yes_price=pos.get("entry_price", 0.9),
                question=pos.get("question", ""),
                is_position=True,
            )
    if open_pos:
        log.info(f"[MAIN] Restored {len(open_pos)} open positions to WS")

    # Start WS in background
    ws_task = asyncio.create_task(ws.connect())

    await db.log_event("STARTUP", details={
        "config": {k: v for k, v in CONFIG.items() if k not in ("TELEGRAM_TOKEN",)},
        "open_positions": len(open_pos),
    })

    await tg.send(
        f"<b>quant-micro started</b>\n"
        f"Mode: {'SIM' if CONFIG['SIMULATION'] else 'REAL'}\n"
        f"Zone: {CONFIG['WATCHLIST_MIN_PRICE']:.0%}-{CONFIG['WATCHLIST_MAX_PRICE']:.0%}\n"
        f"Open: {len(open_pos)} positions"
    )

    # ── Scan Loop ──

    scan_count = 0

    while not _shutdown:
        try:
            scan_count += 1
            log.info(f"[SCAN #{scan_count}] Starting market scan...")

            # 1. Fetch watchlist candidates
            candidates = await scanner.fetch_watchlist_candidates()

            # 2. Update watchlist in DB + register in WS
            new_count = 0
            for c in candidates:
                await db.upsert_watchlist(c)

                # Register in WS if not already tracked
                if c["market_id"] not in ws.prices:
                    tokens = ws.register_market(
                        c["market_id"],
                        yes_token=c.get("yes_token"),
                        no_token=c.get("no_token"),
                        yes_price=c["yes_price"],
                        question=c["question"],
                        is_position=False,
                    )
                    if tokens:
                        await ws.subscribe_tokens(tokens)
                        new_count += 1

            # 3. REST fallback: check open positions
            open_pos = await db.get_open_positions()
            stats = await db.get_stats()
            bankroll = stats.get("bankroll", CONFIG["BANKROLL"])

            # Calculate equity
            total_unrealized = sum(p.get("unrealized_pnl", 0) for p in open_pos)
            equity = bankroll + total_unrealized
            await db.update_peak_equity(equity)

            log.info(
                f"[SCAN #{scan_count}] Watchlist: {len(candidates)} candidates, "
                f"{new_count} new WS | Open: {len(open_pos)} | "
                f"Bankroll: ${bankroll:.2f} | Equity: ${equity:.2f} | "
                f"PnL: ${stats.get('total_pnl', 0):.2f} | "
                f"W/L: {stats.get('wins', 0)}/{stats.get('losses', 0)}"
            )

            # 4. Cleanup stale watchlist entries
            if scan_count % 30 == 0:
                await db.cleanup_watchlist()

                # Unregister WS markets that are no longer in watchlist and not positions
                watchlist = await db.get_watchlist()
                wl_ids = {w["market_id"] for w in watchlist}
                pos_ids = {p["market_id"] for p in open_pos}
                to_remove = []
                for mid in list(ws.prices.keys()):
                    if mid not in wl_ids and mid not in pos_ids:
                        to_remove.append(mid)
                for mid in to_remove:
                    tokens = ws.unregister_market(mid)
                    await ws.unsubscribe_tokens(tokens)
                if to_remove:
                    log.info(f"[CLEANUP] Removed {len(to_remove)} stale WS markets")

            # 5. Log scan event
            await db.log_event("SCAN", details={
                "scan": scan_count,
                "watchlist": len(candidates),
                "new_ws": new_count,
                "open": len(open_pos),
                "bankroll": round(bankroll, 2),
                "equity": round(equity, 2),
                "wins": stats.get("wins", 0),
                "losses": stats.get("losses", 0),
            })

            # Sleep until next scan
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
