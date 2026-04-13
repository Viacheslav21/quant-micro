"""
quant-micro — Resolution harvester for Polymarket.

Orchestrator: startup, config, scan loop, shutdown.
Logic split into engine/entry.py, engine/monitor.py, engine/resolver.py.
"""

import asyncio
import logging
import os
import signal
import json
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

from engine.scanner import MicroScanner, classify_theme
from engine.ws_client import MicroWS
from engine.entry import try_enter, check_watchlist_price
from engine.monitor import check_position_price, cleanup_stale_cooldowns
from engine.resolver import check_expired_positions, check_event_cascade
from utils.db import Database
from utils.telegram import TelegramBot

# ── Config ──

CONFIG = {
    "TELEGRAM_TOKEN":     os.getenv("TELEGRAM_BOT_TOKEN"),
    "TELEGRAM_CHAT_ID":   os.getenv("TELEGRAM_CHAT_ID"),
    "BANKROLL":           float(os.getenv("BANKROLL", "500")),
    "SIMULATION":         os.getenv("SIMULATION", "true").lower() == "true",
    "SCAN_INTERVAL":      int(os.getenv("SCAN_INTERVAL", "120")),
    "MAX_STAKE":          float(os.getenv("MAX_STAKE", "20.0")),
    "MIN_STAKE":          float(os.getenv("MIN_STAKE", "5.0")),
    "MAX_OPEN":           int(os.getenv("MAX_OPEN", "50")),
    "SL_PCT":             float(os.getenv("SL_PCT", "0.05")),
    "ENTRY_MIN_PRICE":    float(os.getenv("ENTRY_MIN_PRICE", "0.94")),
    "WATCHLIST_MIN_PRICE": float(os.getenv("WATCHLIST_MIN_PRICE", "0.90")),
    "MAX_DAYS_LEFT":      float(os.getenv("MAX_DAYS_LEFT", "7")),
    "MIN_ROI":            float(os.getenv("MIN_ROI", "0.02")),
    "MIN_LIQUIDITY_MULT": float(os.getenv("MIN_LIQUIDITY_MULT", "100")),
    "MAX_SPREAD":         float(os.getenv("MAX_SPREAD", "0.02")),
    "RESOLUTION_PRICE":   float(os.getenv("RESOLUTION_PRICE", "0.995")),
    "MAX_LOSS_PER_POS":   float(os.getenv("MAX_LOSS_PER_POS", "3.0")),
    "MAX_PER_THEME":      int(os.getenv("MAX_PER_THEME", "5")),
    "MAX_PER_NEG_RISK":   int(os.getenv("MAX_PER_NEG_RISK", "3")),
    "CONFIG_TAG":         os.getenv("CONFIG_TAG", "micro-v4"),
    "SCAN_PAGES":         int(os.getenv("SCAN_PAGES", "16")),
    "MIN_VOLUME":         float(os.getenv("MIN_VOLUME", "50000")),
    "MIN_QUALITY_SCORE":  float(os.getenv("MIN_QUALITY_SCORE", "40")),
    "SLIPPAGE":           float(os.getenv("SLIPPAGE", "0.005")),    # 0.5¢ per side
    "FEE_PCT":            float(os.getenv("FEE_PCT", "0.02")),      # 2% round-trip fee
}

# ── Logging ──

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-18s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("micro")

# ── Shared State ──

_shutdown = False
_pos_cache: dict = {}         # ws_key -> position dict (in-memory cache)
_pos_last_db_write: dict = {} # pos_id -> timestamp of last DB price write
_last_scan_at = 0.0
_scan_count_global = 0
_peak_equity = 0.0
_WATCHDOG_STALE_SECONDS = 900
_last_daily_report_date: str = ""

_SAFE_CONFIG_KEYS = {
    "ENTRY_MIN_PRICE", "WATCHLIST_MIN_PRICE", "MIN_ROI", "MIN_QUALITY_SCORE",
    "ENTRY_PRICE_1D", "ENTRY_PRICE_2D", "ENTRY_PRICE_3D",
    "SL_PCT", "RAPID_DROP_PCT", "MAX_LOSS_PER_POS",
    "MAX_STAKE", "MIN_STAKE", "MAX_OPEN", "MAX_PER_THEME",
    "MAX_DAYS_LEFT", "MIN_VOLUME", "SCAN_INTERVAL", "CONFIG_TAG", "MAX_PER_NEG_RISK",
    "BANKROLL", "SLIPPAGE", "FEE_PCT",
}


async def _reload_config(db):
    """Fetch live config overrides from DB and merge into CONFIG."""
    try:
        overrides = await db.get_config_overrides("micro")
        changed = []
        for key, val in overrides.items():
            if key in _SAFE_CONFIG_KEYS and key in CONFIG and CONFIG[key] != val:
                changed.append(f"{key}: {CONFIG[key]}→{val}")
                CONFIG[key] = val
        if changed:
            log.info(f"[CONFIG] Live reload: {', '.join(changed)}")
    except Exception as e:
        log.warning(f"[CONFIG] Failed to reload: {e}")


def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True
    log.info(f"[MAIN] Shutdown signal received ({sig})")


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


async def _send_daily_report(db, tg, config):
    """Send daily summary to Telegram. Called once per day at first scan after midnight UTC."""
    global _last_daily_report_date
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if now.hour < 9:
        return  # wait until 9:00 UTC
    today = now.strftime("%Y-%m-%d")
    if today == _last_daily_report_date:
        return
    _last_daily_report_date = today

    try:
        d = await db.get_daily_report(config["BANKROLL"])
        t = d["today"]
        trades = int(t["trades"])

        # Skip if no trades yesterday (report covers previous day's final state)
        # But still send on days with 0 trades for awareness

        wr = int(t["wins"]) / trades * 100 if trades > 0 else 0
        at = d["alltime"]
        at_trades = int(at["trades"])
        at_wr = int(at["wins"]) / at_trades * 100 if at_trades > 0 else 0

        # Themes
        theme_lines = []
        for th in d["themes"][:4]:
            theme_lines.append(f"  {th['theme']}: {th['wins']}/{th['n']} ${th['pnl']:+.2f}")
        themes_str = "\n".join(theme_lines) if theme_lines else "  —"

        # Close reasons
        reason_parts = []
        for r in d["reasons"]:
            reason_parts.append(f"{r['close_reason']}={r['n']}")
        reasons_str = ", ".join(reason_parts) if reason_parts else "—"

        # Best/worst
        best = float(t["best"] or 0)
        worst = float(t["worst"] or 0)

        msg = (
            f"🔬 <b>MICRO DAILY</b> | {today}\n\n"
            f"📊 <b>Сегодня:</b> {trades} сделок | WR {wr:.0f}% | <b>${float(t['pnl']):+.2f}</b>\n"
            f"🏷 {reasons_str}\n"
            f"🏆 Best: ${best:+.2f} | Worst: ${worst:+.2f}\n\n"
            f"📈 <b>Темы:</b>\n{themes_str}\n\n"
            f"💼 <b>Портфель:</b>\n"
            f"  Банк: ${d['bankroll']:.0f} | Equity: ${d['equity']:.0f}\n"
            f"  Открыто: {d['open_n']} поз (${d['open_staked']:.0f}) uPnL: ${d['open_upnl']:+.2f}\n\n"
            f"📉 <b>All-time:</b> {at_trades} сделок | WR {at_wr:.0f}% | ${float(at['pnl']):+.2f}"
        )
        await tg.send(msg)
        log.info(f"[DAILY] Report sent: {trades} trades, ${float(t['pnl']):+.2f}")
    except Exception as e:
        log.error(f"[DAILY] Report failed: {e}")


# ── Main ──

async def main():
    global _shutdown, _last_scan_at, _scan_count_global, _peak_equity

    log.info("[MAIN] quant-micro v3 starting — loading config from DB…")

    db = Database()
    await db.init()

    # Fresh start option
    if os.getenv("RESET_ON_START", "false").lower() == "true":
        await db.reset_stats()
        log.info(f"[MAIN] RESET: clean slate, bankroll=${CONFIG['BANKROLL']}")

    tg = TelegramBot(CONFIG["TELEGRAM_TOKEN"], CONFIG["TELEGRAM_CHAT_ID"])
    scanner = MicroScanner(CONFIG)
    http_client = scanner.client
    ws = MicroWS()

    # WS callbacks
    async def on_watchlist(ws_key, price, info):
        await check_watchlist_price(ws_key, price, info, db, ws, tg, CONFIG, _pos_cache, _shutdown)

    async def on_position(ws_key, price, info):
        await check_position_price(ws_key, price, info, db, ws, tg, CONFIG,
                                    http_client, _pos_cache, _pos_last_db_write, _shutdown)

    ws.set_callbacks(on_watchlist_price=on_watchlist, on_position_price=on_position)

    # Restore open positions into WS
    open_pos = await db.get_open_positions()
    restored = 0
    wl_all = await db.get_watchlist()
    wl_tokens = {w["market_id"]: w.get("yes_token") for w in wl_all if w.get("yes_token")}

    needs_fetch = []
    pos_tokens = {}
    for pos in open_pos:
        token_id = wl_tokens.get(pos["market_id"])
        if token_id:
            pos_tokens[pos["market_id"]] = token_id
        else:
            needs_fetch.append(pos["market_id"])

    if needs_fetch:
        async def _fetch_token(mid):
            try:
                r = await scanner.client.get(f"https://gamma-api.polymarket.com/markets/{mid}")
                if r.status_code == 200:
                    tids = r.json().get("clobTokenIds") or []
                    if isinstance(tids, str):
                        tids = json.loads(tids)
                    return mid, tids[0] if tids else None
            except Exception:
                pass
            return mid, None

        results = await asyncio.gather(*[_fetch_token(mid) for mid in needs_fetch])
        for mid, token_id in results:
            if token_id:
                pos_tokens[mid] = token_id
                log.info(f"[RESTORE] Fetched YES token for {mid[:8]} from API")

    for pos in open_pos:
        side = pos.get("side", "YES")
        ws_key = f"{pos['market_id']}_{side}"
        token_id = pos_tokens.get(pos["market_id"])
        if token_id:
            ws.register_market(ws_key, token_id=token_id, token_side=side.lower(),
                               price=pos.get("entry_price", 0.9), question=pos.get("question", ""),
                               is_position=True)
            restored += 1
        else:
            log.warning(f"[RESTORE] No token for {pos['market_id'][:8]} {side}")
    if open_pos:
        log.info(f"[MAIN] Restored {restored}/{len(open_pos)} positions to WS")

    ws_task = asyncio.create_task(ws.connect())
    await _reload_config(db)

    # Effective config banner — printed AFTER DB overrides are merged
    log.info("=" * 60)
    log.info("[MAIN] quant-micro v3 (resolution harvester)")
    log.info(f"[MAIN] Simulation: {CONFIG['SIMULATION']}")
    log.info(f"[MAIN] Direct entry: ≥{CONFIG['ENTRY_MIN_PRICE']:.0%}")
    log.info(f"[MAIN] Watchlist: {CONFIG['WATCHLIST_MIN_PRICE']:.0%}-{CONFIG['ENTRY_MIN_PRICE']:.0%}")
    log.info(f"[MAIN] Max days: {CONFIG['MAX_DAYS_LEFT']}, ROI≥{CONFIG['MIN_ROI']:.1%}")
    log.info(f"[MAIN] Max stake: ${CONFIG['MAX_STAKE']}, MAX_LOSS: ${CONFIG['MAX_LOSS_PER_POS']}, Rapid drop: {CONFIG.get('RAPID_DROP_PCT', 0.07)*100:.0f}¢")
    log.info(f"[MAIN] Max open: {CONFIG['MAX_OPEN']}, per theme: {CONFIG['MAX_PER_THEME']}, per negRisk: {CONFIG.get('MAX_PER_NEG_RISK', 3)}")
    log.info(f"[MAIN] Spread: <{CONFIG['MAX_SPREAD']:.0%}, Quality≥{CONFIG['MIN_QUALITY_SCORE']}, Bankroll: ${CONFIG['BANKROLL']:.0f}, Tag: {CONFIG['CONFIG_TAG']}")
    log.info("=" * 60)

    await tg.send(
        f"<b>quant-micro v3 started</b>\n"
        f"Mode: {'SIM' if CONFIG['SIMULATION'] else 'REAL'}\n"
        f"Entry: ≥{CONFIG['ENTRY_MIN_PRICE']:.0%} (1d→{CONFIG.get('ENTRY_PRICE_1D', 0.90):.0%}, 2d→{CONFIG.get('ENTRY_PRICE_2D', 0.92):.0%})\n"
        f"Bankroll: ${CONFIG['BANKROLL']:.0f} | Tag: {CONFIG['CONFIG_TAG']}\n"
        f"Open: {len(open_pos)} positions"
    )

    # ── Watchdog ──
    async def _watchdog():
        global _last_scan_at
        _last_scan_at = time.time()
        log.info(f"[WATCHDOG] Started, _last_scan_at={_last_scan_at:.0f}")
        while not _shutdown:
            await asyncio.sleep(60)
            if _last_scan_at and time.time() - _last_scan_at > _WATCHDOG_STALE_SECONDS:
                stale_min = int((time.time() - _last_scan_at) / 60)
                log.error(f"[WATCHDOG] Scan loop stale! Last scan {stale_min}m ago")
                await tg.send(
                    f"<b>MICRO WATCHDOG</b>\n"
                    f"Scan loop stale — last run {stale_min}m ago\n"
                    f"Scan #{_scan_count_global} | WS={'connected' if ws.ws else 'DISCONNECTED'}"
                )
                _last_scan_at = time.time()
    asyncio.create_task(_watchdog())

    # ── Health endpoint (lightweight asyncio, no aiohttp) ──
    async def _health_handler(reader, writer):
        try:
            await reader.read(4096)  # consume request
            stale = time.time() - _last_scan_at if _last_scan_at else 9999
            healthy = stale < _WATCHDOG_STALE_SECONDS and not _shutdown
            body = json.dumps({"status": "ok" if healthy else "stale",
                               "scan_count": _scan_count_global,
                               "last_scan_age_s": int(stale),
                               "ws_connected": ws.ws is not None,
                               "positions_cached": len(_pos_cache),
                               "shutdown": _shutdown})
            status = "200 OK" if healthy else "503 Service Unavailable"
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}".encode())
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
    try:
        health_port = int(os.getenv("HEALTH_PORT", "8082"))
        await asyncio.start_server(_health_handler, "0.0.0.0", health_port)
        log.info(f"[HEALTH] Listening on :{health_port}")
    except OSError as e:
        log.warning(f"[HEALTH] Could not start health server: {e}")

    # ── LISTEN for config_reload ──
    async def _listen_config():
        import asyncpg as _apg
        while not _shutdown:
            try:
                conn = await _apg.connect(db.url)
                await conn.add_listener("config_reload",
                    lambda c, pid, ch, payload: asyncio.create_task(_reload_config(db)))
                await conn.execute("LISTEN config_reload")
                log.info("[CONFIG] LISTEN config_reload active")
                while not _shutdown:
                    await asyncio.sleep(60)
                await conn.close()
            except Exception as e:
                log.warning(f"[CONFIG] LISTEN failed: {e}, reconnecting in 10s")
                await asyncio.sleep(10)
    asyncio.create_task(_listen_config())

    # ── Scan Loop ──
    _SCAN_TIMEOUT = 600
    scan_count = 0

    async def _scan_cycle():
        nonlocal scan_count
        global _peak_equity

        # 1. Fetch candidates (builds active market IDs)
        direct, watchlist = await scanner.fetch_candidates()

        # 2. Check expired + disappeared positions
        await check_expired_positions(db, ws, tg, http_client, CONFIG,
                                       _pos_cache, _pos_last_db_write,
                                       active_market_ids=getattr(scanner, '_scanned_market_ids', None))

        # 3. Direct entries
        entered = 0
        _skip = {}
        _skip_themes = {}  # {reason: {theme: count}}
        for c in direct:
            if _shutdown:
                break
            result = await try_enter(c, db, ws, tg, CONFIG, _pos_cache, source="scan")
            if result is True:
                entered += 1
            elif isinstance(result, str):
                _skip[result] = _skip.get(result, 0) + 1
                theme = c.get("theme", "other")
                _skip_themes.setdefault(result, {})[theme] = _skip_themes.get(result, {}).get(theme, 0) + 1

        # 4. Event cascade
        cascade_entered = await check_event_cascade(scanner, db, ws, tg, CONFIG, _pos_cache)
        entered += cascade_entered

        # 5. Watchlist → WS
        if watchlist:
            await db.upsert_watchlist_batch(watchlist)
        new_ws = 0
        for c in watchlist:
            ws_key = f"{c['market_id']}_{c['side']}"
            if ws_key not in ws.prices:
                tokens = ws.register_market(ws_key, token_id=c.get("ws_token"),
                                             token_side=c.get("ws_side", c["side"].lower()),
                                             price=c["price"], question=c["question"],
                                             is_position=False)
                if tokens:
                    await ws.subscribe_tokens(tokens)
                    new_ws += 1

        # 6. Status
        open_pos = await db.get_open_positions()
        stats = await db.get_stats(CONFIG["BANKROLL"])
        bankroll = stats.get("bankroll", CONFIG["BANKROLL"])
        total_unrealized = sum(p.get("unrealized_pnl", 0) for p in open_pos)
        equity = bankroll + total_unrealized
        if equity > _peak_equity:
            _peak_equity = equity

        skip_str = " | Skip: " + ", ".join(f"{k}={v}" for k, v in sorted(_skip.items())) if _skip else ""
        log.info(
            f"[SCAN #{scan_count}] Direct: {len(direct)} ({entered} entered, {cascade_entered} cascade) | "
            f"WL: {len(watchlist)} ({new_ws} new WS) | Events: {len(scanner.event_siblings)} | Open: {len(open_pos)} | "
            f"Bankroll: ${bankroll:.2f} | Equity: ${equity:.2f} | "
            f"PnL: ${stats.get('total_pnl', 0):.2f} | "
            f"W/L: {stats.get('wins', 0)}/{stats.get('losses', 0)}{skip_str}"
        )
        # Breakdown by theme for skipped (helps diagnose blocking)
        for reason, themes in _skip_themes.items():
            theme_str = ", ".join(f"{t}={n}" for t, n in sorted(themes.items(), key=lambda x: -x[1]))
            log.info(f"[SCAN #{scan_count}] Skip/{reason}: {theme_str}")

        # 7. Daily report (once per day, first scan after midnight UTC)
        await _send_daily_report(db, tg, CONFIG)

        # 8. Cleanup stale WS (every 30 scans)
        if scan_count > 0 and scan_count % 30 == 0:
            await db.cleanup_watchlist()
            wl_data = await db.get_watchlist()
            wl_ids = {w["market_id"] for w in wl_data}
            pos_keys = {f"{p['market_id']}_{p['side']}" for p in open_pos}
            to_remove = []
            for ws_key in list(ws.prices.keys()):
                mid = ws_key.rsplit("_", 1)[0] if "_" in ws_key else ws_key
                if mid not in wl_ids and ws_key not in pos_keys:
                    to_remove.append(ws_key)
            for ws_key in to_remove:
                tokens = ws.unregister_market(ws_key)
                await ws.unsubscribe_tokens(tokens)
            if to_remove:
                log.info(f"[CLEANUP] Removed {len(to_remove)} stale WS entries")
            cleanup_stale_cooldowns()
            ws.cleanup_stale_checks()

    while not _shutdown:
        try:
            scan_count += 1
            log.info(f"[SCAN #{scan_count}] Starting...")
            await asyncio.wait_for(_scan_cycle(), timeout=_SCAN_TIMEOUT)
            _last_scan_at = time.time()
            _scan_count_global = scan_count
            await asyncio.sleep(CONFIG["SCAN_INTERVAL"])
        except asyncio.TimeoutError:
            log.error(f"[MAIN] Scan #{scan_count} timed out after {_SCAN_TIMEOUT}s!")
            await tg.send(f"⏰ <b>MICRO SCAN TIMEOUT</b>\nScan #{scan_count} exceeded {_SCAN_TIMEOUT}s")
            await asyncio.sleep(5)
        except Exception as e:
            log.error(f"[MAIN] Loop error: {e}", exc_info=True)
            await asyncio.sleep(5)
        finally:
            _last_scan_at = time.time()
            _scan_count_global = scan_count

    # ── Shutdown ──
    log.info("[MAIN] Shutting down...")
    await ws.stop()
    ws_task.cancel()
    await scanner.close()
    await tg.send("<b>quant-micro stopped</b>")
    await tg.close()
    await db.close()
    log.info("[MAIN] Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
