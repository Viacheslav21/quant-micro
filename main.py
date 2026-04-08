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
import json
import time
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

from engine.scanner import MicroScanner, classify_theme
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
    "MAX_STAKE":          float(os.getenv("MAX_STAKE", "20.0")),      # $20 max (was $50 — reduce risk until 50+ trades)
    "MIN_STAKE":          float(os.getenv("MIN_STAKE", "5.0")),
    "MAX_OPEN":           int(os.getenv("MAX_OPEN", "50")),
    "SL_PCT":             float(os.getenv("SL_PCT", "0.05")),        # 5% default
    "ENTRY_MIN_PRICE":    float(os.getenv("ENTRY_MIN_PRICE", "0.94")), # 94¢ direct entry (was 95¢)
    "WATCHLIST_MIN_PRICE": float(os.getenv("WATCHLIST_MIN_PRICE", "0.90")), # 90¢ watchlist (was 88¢)
    "MAX_DAYS_LEFT":      float(os.getenv("MAX_DAYS_LEFT", "7")),    # 7 days max (was 10 — too much volatility)
    "MIN_ROI":            float(os.getenv("MIN_ROI", "0.02")),       # 2% min ROI (1% too thin — $0 PnL on 99¢ entries)
    "MIN_LIQUIDITY_MULT": float(os.getenv("MIN_LIQUIDITY_MULT", "100")),  # was 500 — too strict for $50 stakes
    "MAX_SPREAD":         float(os.getenv("MAX_SPREAD", "0.02")),    # 2¢ tight spread
    "RESOLUTION_PRICE":   float(os.getenv("RESOLUTION_PRICE", "0.99")),
    "MAX_LOSS_PER_POS":   float(os.getenv("MAX_LOSS_PER_POS", "3.0")),  # hard cap $3 loss per position, always enforced
    "MAX_PER_THEME":      int(os.getenv("MAX_PER_THEME", "5")),     # limit correlated risk
    "CONFIG_TAG":         os.getenv("CONFIG_TAG", "micro-v4"),
    "SCAN_PAGES":         int(os.getenv("SCAN_PAGES", "16")),        # 1600 markets
    "MIN_VOLUME":         float(os.getenv("MIN_VOLUME", "50000")),   # 50k volume
    "MIN_QUALITY_SCORE":  float(os.getenv("MIN_QUALITY_SCORE", "40")), # quality gate (was 25 — Q<40 had 0% WR, -$9.28)
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
_http_client: httpx.AsyncClient | None = None
_pos_cache: dict = {}  # ws_key -> position dict (in-memory cache to avoid DB reads on every WS tick)
_pos_last_db_write: dict = {}  # pos_id -> timestamp of last DB price write
_POS_DB_WRITE_INTERVAL = 30  # seconds between DB price writes per position
_sl_rest_cooldown: dict = {}  # market_id -> timestamp of last REST SL check
_SL_REST_COOLDOWN = 60  # seconds between REST verifications per market for SL
_last_scan_at = 0.0  # timestamp of last successful scan
_scan_count_global = 0
_peak_equity = 0.0  # tracked in memory (was in micro_stats)
_WATCHDOG_STALE_SECONDS = 900  # 15 min

_SAFE_CONFIG_KEYS = {
    "ENTRY_MIN_PRICE", "WATCHLIST_MIN_PRICE", "MIN_ROI", "MIN_QUALITY_SCORE",
    "SL_PCT", "RAPID_DROP_PCT", "MAX_LOSS_PER_POS",
    "MAX_STAKE", "MIN_STAKE", "MAX_OPEN", "MAX_PER_THEME",
    "MAX_DAYS_LEFT", "MIN_VOLUME", "SCAN_INTERVAL", "CONFIG_TAG",
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


# ── Helpers ──

def _days_to_expiry_from_pos(pos: dict, date_field: str = "end_date") -> float:
    """Calculate hours since a date field (for hold time tracking)."""
    val = pos.get(date_field)
    if not val:
        return 0
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return abs((datetime.now(timezone.utc) - dt).total_seconds() / 3600)
    except Exception:
        return 0


def _days_to_expiry(pos: dict) -> float:
    """Calculate days until position's market expires. Returns 999 if unknown."""
    end_date_str = pos.get("end_date")
    if not end_date_str:
        return 999
    try:
        end = datetime.fromisoformat(str(end_date_str).replace("Z", "+00:00"))
        return max(0, (end - datetime.now(timezone.utc)).total_seconds() / 86400)
    except Exception:
        return 999


# ── REST Price Verification ──

async def _verify_price_rest(market_id: str, side: str) -> float | None:
    """Fetch current mid-price from Gamma REST API to confirm WS price.
    Returns the side's price or None on failure."""
    if not _http_client:
        return None
    try:
        r = await _http_client.get(
            f"https://gamma-api.polymarket.com/markets/{market_id}",
            timeout=5,
        )
        if r.status_code != 200:
            return None
        m = r.json()
        raw = m.get("outcomePrices")
        if not raw:
            return None
        if isinstance(raw, str):
            raw = json.loads(raw)
        yes_p = float(raw[0])
        return round(1.0 - yes_p, 4) if side == "NO" else round(yes_p, 4)
    except Exception as e:
        log.warning(f"[REST] Price verify failed for {market_id[:8]}: {e}")
        return None


async def _check_volume_confirms(market_id: str) -> bool:
    """Check if recent volume supports a real price move (not noise).
    Returns True if volume is elevated (move is real), False if low volume (likely noise)."""
    if not _http_client:
        return True  # can't check, assume real
    try:
        r = await _http_client.get(
            f"https://gamma-api.polymarket.com/markets/{market_id}",
            timeout=5,
        )
        if r.status_code != 200:
            return True
        m = r.json()
        vol_24h = float(m.get("volume24hr") or 0)
        vol_total = float(m.get("volume") or 0)

        # Estimate average daily volume (total / age, rough)
        # If 24h volume is < 30% of what we'd expect, it's a low-volume drop
        if vol_total > 0 and vol_24h > 0:
            # Simple heuristic: if 24h volume < $5k, it's thin/noise
            if vol_24h < 5000:
                log.info(f"[VOL CHECK] {market_id[:8]} vol_24h=${vol_24h:.0f} — low volume, likely noise")
                return False
        return True
    except Exception:
        return True  # can't check, assume real


# ── Stake Calculation ──

def calc_stake(bankroll: float) -> float:
    """Stake = min(MAX_STAKE, 5% of bankroll), but at least MIN_STAKE if bankroll allows."""
    pct_stake = bankroll * 0.05
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
    question = candidate.get("question", "")
    theme = candidate.get("theme", "other")
    neg_risk_id = candidate.get("neg_risk_id")

    # Combined entry check: duplicate, theme block, SL blacklist, cooldown, negRisk group
    entry_check = await db.check_entry_allowed(market_id, side, theme, neg_risk_id=neg_risk_id)
    if not entry_check["allowed"]:
        return False

    open_pos = await db.get_open_positions()
    if len(open_pos) >= CONFIG["MAX_OPEN"]:
        return False

    theme_count = sum(1 for p in open_pos if p.get("theme") == theme)
    if theme_count >= CONFIG["MAX_PER_THEME"]:
        return False

    stats = await db.get_stats(CONFIG["BANKROLL"])
    bankroll = stats.get("bankroll", CONFIG["BANKROLL"])
    stake = calc_stake(bankroll)

    # Correlation penalty removed for micro — negRisk event buckets are anti-correlated
    # (max 1 loses out of N). Risk is managed by MAX_PER_THEME + MAX_OPEN + SL.

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

    # ── Dynamic SL: wide enough to survive normal fluctuations ──
    # Audit showed 0% WR on SL exits — old 3% was too tight, killed positions before resolution
    days_left = candidate.get("days_left", 0)
    if days_left <= 0.5:
        sl_pct = 0.10  # 10% — resolves very soon, let it ride
    elif days_left <= 1:
        sl_pct = 0.09  # 9%
    elif days_left <= 2:
        sl_pct = 0.08  # 8%
    else:
        sl_pct = 0.07  # 7% — 2+ days, still give room

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
        "neg_risk_id": neg_risk_id,
    }

    await db.save_position_and_deduct(pos, stake)

    await db.upsert_watchlist(candidate)

    # Register in WS for position monitoring
    ws_key = f"{market_id}_{side}"
    ws.mark_as_position(ws_key)
    _pos_cache[ws_key] = pos  # populate in-memory cache
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

    stats_now = await db.get_stats(CONFIG["BANKROLL"])
    open_count = len(await db.get_open_positions())
    await tg.send(
        f"🔬 <b>MICRO | {source.upper()}</b> [{mode}]\n\n"
        f"{'✅' if side=='YES' else '❌'} {side} <b>{question[:80]}</b>\n"
        f"📊 Вход: <b>{entry_price*100:.1f}¢</b> | Ставка: <b>${stake:.2f}</b>\n"
        f"💹 ROI: {roi:.1%} | Q={quality:.0f} | {days}d left\n"
        f"📉 Spread: {candidate.get('spread', 0)*100:.1f}¢ | SL: {'OFF' if (isinstance(days, (int, float)) and days <= 3) else f'{sl_pct:.0%}'}\n"
        f"💼 Банк: ${stats_now.get('bankroll', 0):.0f} | Открыто: {open_count+1}\n"
        f"🔗 <a href='https://polymarket.com/event/{candidate.get('slug') or candidate.get('market_id', '')}'>Polymarket</a>"
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

    # Recalculate days_left from end_date (watchlist value may be stale)
    end_date_str = wl.get("end_date")
    if end_date_str:
        try:
            end_dt = datetime.fromisoformat(str(end_date_str).replace("Z", "+00:00"))
            days_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400
        except Exception:
            days_left = wl.get("days_left", 0)
    else:
        days_left = wl.get("days_left", 0)

    candidate = {
        "market_id": market_id,
        "question": info.get("question", wl.get("question", "")),
        "theme": wl.get("theme", "other"),
        "side": side,
        "price": price,
        "best_ask": info.get("best_ask", price),
        "days_left": days_left,
        "spread": spread,
        "quality": wl.get("quality", wl.get("roi", 0) * 500),  # estimate if not stored
        "yes_token": wl.get("yes_token"),
        "no_token": wl.get("no_token"),
        # Always use YES token for WS; ws_side tells client to invert for NO
        "ws_token": wl.get("yes_token"),
        "ws_side": "no" if side == "NO" else "yes",
        "end_date": wl.get("end_date"),
        "neg_risk_id": wl.get("neg_risk_id"),
    }

    entered = await try_enter(candidate, db, ws, tg, source="ws")
    if entered:
        log.info(f"[WS→ENTRY] {side} hit {price:.2f}¢ on '{info.get('question', '')[:40]}'")


# ── Position Monitoring ──

def _invalidate_pos_cache(ws_key: str):
    """Remove position from in-memory cache on close."""
    pos = _pos_cache.pop(ws_key, None)
    if pos:
        _pos_last_db_write.pop(pos.get("id"), None)

async def check_position_price(ws_key: str, price: float, info: dict,
                                db: Database, ws: MicroWS, tg: TelegramBot):
    """WS callback: position price updated. Check SL/resolution."""
    if _shutdown:
        return

    parts = ws_key.rsplit("_", 1)
    if len(parts) != 2:
        return
    market_id, side = parts

    # Use in-memory cache to avoid DB read on every WS tick
    pos = _pos_cache.get(ws_key)
    if not pos:
        pos = await db.get_open_position_by_market(market_id, side)
        if not pos:
            ws.unmark_position(ws_key)
            _invalidate_pos_cache(ws_key)
            return
        _pos_cache[ws_key] = pos

    entry_price = pos["entry_price"]
    stake = pos["stake_amt"]
    sl_pct = pos.get("sl_pct", CONFIG["SL_PCT"])

    bid_price = info.get("best_bid", price)
    if bid_price <= 0:
        bid_price = price

    # Sanity: for 90%+ entries, bid can't realistically drop >50% without resolution
    if bid_price < entry_price * 0.5:
        log.warning(
            f"[SANITY] {market_id[:8]} {side} bid={bid_price:.4f} << entry={entry_price:.4f} — "
            f"ignoring bad tick"
        )
        return

    pnl_pct = (bid_price - entry_price) / entry_price if entry_price > 0 else 0
    pnl_dollar = pnl_pct * stake

    # Throttle DB writes: only update every _POS_DB_WRITE_INTERVAL seconds per position
    import time as _time
    now_ts = _time.time()
    last_write = _pos_last_db_write.get(pos["id"], 0)
    if now_ts - last_write >= _POS_DB_WRITE_INTERVAL:
        await db.update_position_price(pos["id"], bid_price, round(pnl_dollar, 4))
        _pos_last_db_write[pos["id"]] = now_ts

    # ── Resolution: our side price → 99¢+ (WIN) or ≤1¢ (LOSS) ──
    if bid_price <= 0.01:
        pnl = -stake  # total loss
        closed = await db.close_position(pos["id"], round(pnl, 4), "LOSS", "resolved_loss")
        if closed:
            ws.unmark_position(ws_key)
            _invalidate_pos_cache(ws_key)
            await db.recalibrate_theme(pos.get("theme", "other"))
            log.info(f"[RESOLVED] LOSS {side} '{pos['question'][:40]}' PnL: ${pnl:.2f}")
            await tg.send(
                f"🔬 <b>MICRO</b> | 🏁 <b>RESOLVED LOSS</b>\n"
                f"{'✅' if side=='YES' else '❌'} {side} <b>{pos['question'][:80]}</b>\n"
                f"📊 Вход: {entry_price*100:.1f}¢ → {bid_price*100:.1f}¢\n"
                f"💰 PnL: <b>${pnl:.2f}</b>"
            )
        return

    if bid_price >= CONFIG["RESOLUTION_PRICE"]:
        pnl = ((bid_price - entry_price) / entry_price) * stake
        closed = await db.close_position(pos["id"], round(pnl, 4), "WIN", "resolved")
        if closed:
            ws.unmark_position(ws_key)
            _invalidate_pos_cache(ws_key)
            await db.recalibrate_theme(pos.get("theme", "other"))
            log.info(f"[RESOLVED] WIN {side} '{pos['question'][:40]}' PnL: +${pnl:.2f}")
            hold_hours = _days_to_expiry_from_pos(pos, "opened_at")
            stats_now = await db.get_stats(CONFIG["BANKROLL"])
            await tg.send(
                f"🔬 <b>MICRO</b> | 🏁 <b>RESOLVED WIN</b> ✅\n\n"
                f"{'✅' if side=='YES' else '❌'} {side} <b>{pos['question'][:80]}</b>\n"
                f"📊 Вход: {entry_price*100:.1f}¢ → <b>{bid_price*100:.1f}¢</b>\n"
                f"💰 PnL: <b>+${pnl:.2f}</b> ({pnl/stake*100:+.1f}%)\n"
                f"⏱ Держали: {hold_hours:.0f}ч | 💼 Банк: ${stats_now.get('bankroll', 0):.0f}"
            )
        return

    # ── Hard max loss cap — ALWAYS enforced, even when SL is disabled ──
    # No REST cooldown — this is an emergency stop, must fire immediately
    dollar_loss = pnl_pct * stake
    max_loss = CONFIG["MAX_LOSS_PER_POS"]
    if dollar_loss <= -max_loss:
        # Quick REST verify (no cooldown for max loss)
        rest_price = await _verify_price_rest(market_id, side)
        if rest_price is not None:
            rest_pnl = ((rest_price - entry_price) / entry_price) * stake
            if rest_pnl > -max_loss:
                log.info(f"[MAX LOSS BLOCKED] {market_id[:8]} WS loss=${dollar_loss:.2f} but REST=${rest_pnl:.2f} — not real")
                return
            dollar_loss = rest_pnl  # use REST-confirmed loss
        pnl = dollar_loss
        closed = await db.close_position(pos["id"], round(pnl, 4), "LOSS", "max_loss")
        if closed:
            ws.unmark_position(ws_key)
            _invalidate_pos_cache(ws_key)
            await db.recalibrate_theme(pos.get("theme", "other"))
            log.info(f"[MAX LOSS] {side} '{pos['question'][:40]}' loss=${pnl:.2f} > cap ${max_loss}")
            await tg.send(
                f"🔬 <b>MICRO</b> | 🚨 <b>MAX LOSS CAP</b>\n\n"
                f"{'✅' if side=='YES' else '❌'} {side} <b>{pos['question'][:80]}</b>\n"
                f"📊 Вход: {entry_price*100:.1f}¢ → <b>{bid_price*100:.1f}¢</b>\n"
                f"💰 PnL: <b>${pnl:.2f}</b> (cap: ${max_loss})"
            )
        return

    # ── Stop Loss (disabled for markets ≤1 day to expiry — let them resolve) ──
    # Was ≤3d but iran loss happened in that window. Narrowed to ≤1d.
    days_to_expiry = _days_to_expiry(pos)
    if pnl_pct <= -sl_pct and days_to_expiry > 1:
        # Only apply SL for markets >3 days out — near-expiry markets should ride to resolution
        # Verify via REST before closing — WS book can be stale/incomplete
        # Rate limit: max 1 REST check per market per 60s (WS can fire dozens of ticks/sec)
        now_ts = time.time()
        last_check = _sl_rest_cooldown.get(market_id, 0)
        if now_ts - last_check < _SL_REST_COOLDOWN:
            return  # already checked recently, WS price likely stale — skip
        _sl_rest_cooldown[market_id] = now_ts
        rest_price = await _verify_price_rest(market_id, side)
        if rest_price is not None:
            rest_pnl_pct = (rest_price - entry_price) / entry_price
            if rest_pnl_pct > -sl_pct:
                log.info(
                    f"[SL BLOCKED] {market_id[:8]} {side} WS bid={bid_price:.4f} but REST={rest_price:.4f} "
                    f"(WS pnl={pnl_pct:+.1%}, REST pnl={rest_pnl_pct:+.1%}) — not a real SL"
                )
                return
        # Volume confirmation: skip SL if drop is on low volume (likely noise)
        vol_confirms = await _check_volume_confirms(market_id)
        if not vol_confirms:
            log.info(
                f"[SL VOL BLOCKED] {market_id[:8]} {side} pnl={pnl_pct:+.1%} but low volume — skipping SL"
            )
            return
        pnl = pnl_pct * stake
        closed = await db.close_position(pos["id"], round(pnl, 4), "LOSS", "stop_loss")
        if closed:
            ws.unmark_position(ws_key)
            _invalidate_pos_cache(ws_key)
            await db.recalibrate_theme(pos.get("theme", "other"))
            log.info(
                f"[SL] LOSS {side} '{pos['question'][:40]}' @ {bid_price:.2f}¢ "
                f"PnL: ${pnl:.2f} ({pnl_pct:+.1%})"
                f"{f' (REST confirmed: {rest_price:.4f})' if rest_price else ' (REST unavailable)'}"
            )
            await tg.send(
                f"🔬 <b>MICRO</b> | 🛑 <b>STOP LOSS</b>\n\n"
                f"{'✅' if side=='YES' else '❌'} {side} <b>{pos['question'][:80]}</b>\n"
                f"📊 Вход: {entry_price*100:.1f}¢ → <b>{bid_price*100:.1f}¢</b>\n"
                f"💰 PnL: <b>${pnl:.2f}</b> ({pnl_pct:+.1%})\n"
                f"⏱ До expiry: {days_to_expiry:.1f}d"
            )
        return

    # ── Rapid Drop Guard (7¢ — disabled for ≤1 day to expiry, let it resolve) ──
    if bid_price < entry_price - 0.07 and days_to_expiry > 1:
        # Verify via REST before closing (reuse SL cooldown to prevent spam)
        now_ts = time.time()
        last_check = _sl_rest_cooldown.get(market_id, 0)
        if now_ts - last_check < _SL_REST_COOLDOWN:
            return
        _sl_rest_cooldown[market_id] = now_ts
        rest_price = await _verify_price_rest(market_id, side)
        if rest_price is not None and rest_price >= entry_price - 0.07:
            log.info(
                f"[RAPID DROP BLOCKED] {market_id[:8]} {side} WS bid={bid_price:.4f} but REST={rest_price:.4f} — not a real drop"
            )
            return
        pnl = pnl_pct * stake
        closed = await db.close_position(pos["id"], round(pnl, 4), "LOSS", "rapid_drop")
        if closed:
            ws.unmark_position(ws_key)
            _invalidate_pos_cache(ws_key)
            await db.recalibrate_theme(pos.get("theme", "other"))
            log.info(
                f"[RAPID DROP] LOSS {side} '{pos['question'][:40]}' @ {bid_price:.2f}¢ "
                f"PnL: ${pnl:.2f} ({pnl_pct:+.1%})"
            )
            await tg.send(
                f"🔬 <b>MICRO</b> | <b>RAPID DROP</b> {side}\n{pos['question'][:60]}\n"
                f"Entry: {entry_price:.2f}¢ → {bid_price:.2f}¢\n"
                f"PnL: ${pnl:.2f} ({pnl_pct:+.1%})"
            )
        return


# ── Time-based Exit Check ──

async def check_expired_positions(db: Database, ws: MicroWS, tg: TelegramBot):
    """Check expired positions via REST: resolved → proper payout, 72h past expiry → force close."""
    open_pos = await db.get_open_positions()
    now = datetime.now(timezone.utc)

    # Filter to expired positions only
    expired = []
    for pos in open_pos:
        end_date_str = pos.get("end_date")
        if not end_date_str:
            continue
        try:
            end = datetime.fromisoformat(str(end_date_str).replace("Z", "+00:00"))
        except Exception:
            continue
        if now < end:
            continue
        pos["_hours_past"] = (now - end).total_seconds() / 3600
        expired.append(pos)

    if not expired:
        return

    # Parallel REST fetch for all expired positions
    async def _fetch_market(mid):
        try:
            resp = await _http_client.get(f"https://gamma-api.polymarket.com/markets/{mid}", timeout=10)
            return resp.json() if resp.status_code == 200 else None
        except Exception:
            return None

    market_data = await asyncio.gather(*[_fetch_market(p["market_id"]) for p in expired])

    for pos, mdata in zip(expired, market_data):
        market_id = pos["market_id"]
        side = pos["side"]
        entry_price = pos["entry_price"]
        stake = pos["stake_amt"]
        ws_key = f"{market_id}_{side}"
        hours_past = pos["_hours_past"]

        # 1. Check resolution
        if mdata and (mdata.get("closed") or mdata.get("resolved")):
            yes_p, no_p = 0, 0
            try:
                op = mdata.get("outcomePrices")
                if isinstance(op, str):
                    prices = json.loads(op)
                    yes_p = float(prices[0]) if prices else 0
                    no_p = float(prices[1]) if len(prices) > 1 else 1 - yes_p
                elif isinstance(op, list):
                    yes_p = float(op[0]) if op else 0
                    no_p = float(op[1]) if len(op) > 1 else 1 - yes_p
                else:
                    yes_p = float(mdata.get("yes_price", 0) or 0)
                    no_p = float(mdata.get("no_price", 0) or 0)
            except Exception:
                yes_p = float(mdata.get("yes_price", 0) or 0)
                no_p = float(mdata.get("no_price", 0) or 0)
            won = (side == "YES" and yes_p > 0.9) or (side == "NO" and no_p > 0.9)
            pnl = ((1.0 - entry_price) / entry_price) * stake if won else -stake
            result = "WIN" if won else "LOSS"
            closed = await db.close_position(pos["id"], round(pnl, 4), result, "resolved")
            if closed:
                ws.unmark_position(ws_key)
                _invalidate_pos_cache(ws_key)
                await db.recalibrate_theme(pos.get("theme", "other"))
                log.info(f"[RESOLVED] {result} {side} '{pos['question'][:40]}' yes_p={yes_p:.2f} no_p={no_p:.2f} PnL: ${pnl:.2f}")
                await tg.send(
                    f"🔬 <b>MICRO</b> | 🏁 <b>RESOLVED {result}</b> {'✅' if won else '❌'}\n"
                    f"{pos['question'][:60]}\n"
                    f"PnL: <b>${pnl:.2f}</b>"
                )
            continue

        # 2. Force-close if 72h+ past expiry
        if hours_past < 72:
            continue

        current_price = pos.get("current_price", entry_price)
        pnl = ((current_price - entry_price) / entry_price) * stake
        result = "WIN" if pnl >= 0 else "LOSS"
        closed = await db.close_position(pos["id"], round(pnl, 4), result, "expired")
        if closed:
            ws.unmark_position(ws_key)
            _invalidate_pos_cache(ws_key)
            await db.recalibrate_theme(pos.get("theme", "other"))
            log.info(f"[EXPIRED] {result} {side} '{pos['question'][:40]}' PnL: ${pnl:.2f} | {hours_past:.0f}h past expiry")
            await tg.send(
                f"🔬 <b>MICRO</b> | <b>EXPIRED {result}</b> {side}\n{pos['question'][:60]}\n"
                f"PnL: ${pnl:.2f} | {hours_past:.0f}h past expiry"
            )


async def check_event_cascade(scanner, db: Database, ws, tg, source="cascade"):
    """Event cascade: when a market in a negRisk event resolves YES (price ≥99¢),
    all sibling markets resolve NO. Enter NO on siblings at good prices.
    This is nearly risk-free: the event outcome is already determined."""
    if not scanner.event_siblings:
        return 0

    entered = 0
    for neg_risk_id, siblings in scanner.event_siblings.items():
        # Check if any sibling resolved YES (yes_price ≥ 99¢)
        resolved_yes = [s for s in siblings if s["yes_price"] >= 0.99]
        if not resolved_yes:
            continue

        # Found a resolution — enter NO on all others that haven't resolved yet
        for s in siblings:
            if s["yes_price"] >= 0.99 or s["yes_price"] <= 0.01:
                continue  # already resolved
            no_price = s["no_price"]
            if no_price < 0.93 or no_price >= 0.995:
                continue  # too cheap or already at resolution

            days_left = 0.5  # resolving now, estimate
            end_str = s.get("end_date")
            if end_str:
                try:
                    end = datetime.fromisoformat(str(end_str).replace("Z", "+00:00"))
                    days_left = max(0, (end - datetime.now(timezone.utc)).total_seconds() / 86400)
                except Exception:
                    pass

            candidate = {
                "market_id": s["market_id"],
                "slug": s.get("slug", ""),
                "question": s["question"],
                "theme": s["theme"],
                "side": "NO",
                "price": no_price,
                "best_ask": no_price,
                "volume": s["volume"],
                "liquidity": s["volume"],  # approximation
                "spread": s["spread"],
                "days_left": days_left,
                "end_date": end_str,
                "roi": round((1.0 - no_price) / no_price, 4) if no_price > 0 else 0,
                "quality": 95,  # high quality — event is resolving
                "yes_token": s["yes_token"],
                "no_token": s["no_token"],
                "ws_token": s["yes_token"],  # always subscribe to YES token
                "ws_side": "no",
                "neg_risk_id": neg_risk_id,
            }

            if await try_enter(candidate, db, ws, tg, source=source):
                entered += 1
                log.info(
                    f"[CASCADE] Entered NO {s['question'][:50]} @ {no_price*100:.1f}¢ "
                    f"(sibling resolved YES in event {neg_risk_id[:8]})"
                )

    return entered


# ── Main Loop ──

async def main():
    global _shutdown, _http_client, _last_scan_at, _scan_count_global

    log.info("=" * 60)
    log.info("[MAIN] quant-micro v3 (resolution harvester)")
    log.info(f"[MAIN] Simulation: {CONFIG['SIMULATION']}")
    log.info(f"[MAIN] Direct entry: ≥{CONFIG['ENTRY_MIN_PRICE']:.0%}")
    log.info(f"[MAIN] Watchlist: {CONFIG['WATCHLIST_MIN_PRICE']:.0%}-{CONFIG['ENTRY_MIN_PRICE']:.0%}")
    log.info(f"[MAIN] Max days: {CONFIG['MAX_DAYS_LEFT']}, ROI≥{CONFIG['MIN_ROI']:.0%}")
    log.info(f"[MAIN] Max stake: ${CONFIG['MAX_STAKE']}, SL: 7-10% (dynamic)")
    log.info(f"[MAIN] Max open: {CONFIG['MAX_OPEN']}, per theme: {CONFIG['MAX_PER_THEME']}")
    log.info(f"[MAIN] Spread: <{CONFIG['MAX_SPREAD']:.0%}, Quality≥{CONFIG['MIN_QUALITY_SCORE']}")
    log.info(f"[MAIN] Risky themes excluded")
    log.info("=" * 60)

    db = Database()
    await db.init()

    # ── Fresh start: reset all stats, positions, watchlist ──
    RESET_ON_START = os.getenv("RESET_ON_START", "false").lower() == "true"
    if RESET_ON_START:
        await db.reset_stats()
        log.info(f"[MAIN] RESET: clean slate, bankroll=${CONFIG['BANKROLL']}")

    tg = TelegramBot(CONFIG["TELEGRAM_TOKEN"], CONFIG["TELEGRAM_CHAT_ID"])
    scanner = MicroScanner(CONFIG)
    _http_client = scanner.client
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

    # Restore open positions into WS (parallel token fetch)
    open_pos = await db.get_open_positions()
    restored = 0

    # Single DB query for all watchlist tokens
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

    # Parallel API fetch for missing tokens
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
            ws.register_market(
                ws_key,
                token_id=token_id,
                token_side=side.lower(),
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

    # Load config overrides from DB at startup
    await _reload_config(db)

    await tg.send(
        f"<b>quant-micro v3 started</b>\n"
        f"Mode: {'SIM' if CONFIG['SIMULATION'] else 'REAL'}\n"
        f"Entry: ≥{CONFIG['ENTRY_MIN_PRICE']:.0%} | WL: {CONFIG['WATCHLIST_MIN_PRICE']:.0%}+\n"
        f"Quality≥{CONFIG['MIN_QUALITY_SCORE']} | Risky themes excluded\n"
        f"Open: {len(open_pos)} positions"
    )

    # ── Watchdog: alert if scan loop stalls ──

    async def _watchdog():
        global _last_scan_at
        _last_scan_at = time.time()
        log.info(f"[WATCHDOG] Started, _last_scan_at={_last_scan_at:.0f}")
        while not _shutdown:
            await asyncio.sleep(60)
            age = int(time.time() - _last_scan_at)
            log.debug(f"[WATCHDOG] Check: scan #{_scan_count_global}, age={age}s")
            if _last_scan_at and time.time() - _last_scan_at > _WATCHDOG_STALE_SECONDS:
                stale_min = int((time.time() - _last_scan_at) / 60)
                log.error(f"[WATCHDOG] Scan loop stale! Last scan {stale_min}m ago")
                await tg.send(
                    f"<b>MICRO WATCHDOG</b>\n"
                    f"Scan loop stale — last run {stale_min}m ago\n"
                    f"Scan #{_scan_count_global} | WS={'connected' if ws.ws else 'DISCONNECTED'}"
                )
                _last_scan_at = time.time()  # reset to avoid spam (re-alert in 15 min if still stuck)
    watchdog_task = asyncio.create_task(_watchdog())

    # ── Health endpoint ──

    async def _health_server():
        from aiohttp import web
        async def _health_handler(request):
            stale = time.time() - _last_scan_at if _last_scan_at else 9999
            healthy = stale < _WATCHDOG_STALE_SECONDS and not _shutdown
            import json
            data = {
                "status": "ok" if healthy else "stale",
                "scan_count": _scan_count_global,
                "last_scan_age_s": int(stale),
                "ws_connected": ws.ws is not None,
                "positions_cached": len(_pos_cache),
                "shutdown": _shutdown,
            }
            return web.Response(text=json.dumps(data), content_type="application/json",
                                status=200 if healthy else 503)
        app_h = web.Application()
        app_h.router.add_get("/health", _health_handler)
        runner = web.AppRunner(app_h)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("HEALTH_PORT", "8082")))
        try:
            await site.start()
            log.info(f"[HEALTH] Listening on :{os.getenv('HEALTH_PORT', '8082')}")
        except OSError as e:
            log.warning(f"[HEALTH] Could not start health server: {e}")
    try:
        asyncio.create_task(_health_server())
    except Exception:
        pass

    # ── Scan Loop ──

    _SCAN_TIMEOUT = 600  # 10 min max per scan cycle

    async def _scan_cycle(scan_count, db, ws, tg, scanner):
        """One full scan cycle — wrapped in wait_for for timeout."""
        # 1. Expired positions (check every scan)
        await check_expired_positions(db, ws, tg)

        # 2. Fetch candidates
        direct, watchlist = await scanner.fetch_candidates()

        # 3. Direct entries (≥93¢)
        entered = 0
        for c in direct:
            if _shutdown:
                break
            if await try_enter(c, db, ws, tg, source="scan"):
                entered += 1

        # 3b. Event cascade: enter NO on siblings of resolved markets
        cascade_entered = await check_event_cascade(scanner, db, ws, tg)
        entered += cascade_entered

        # 4. Watchlist (90-95¢) → register in WS for price monitoring
        if watchlist:
            await db.upsert_watchlist_batch(watchlist)
        new_ws = 0
        for c in watchlist:
            ws_key = f"{c['market_id']}_{c['side']}"
            if ws_key not in ws.prices:
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
        global _peak_equity
        stats = await db.get_stats(CONFIG["BANKROLL"])
        bankroll = stats.get("bankroll", CONFIG["BANKROLL"])
        total_unrealized = sum(p.get("unrealized_pnl", 0) for p in open_pos)
        equity = bankroll + total_unrealized
        if equity > _peak_equity:
            _peak_equity = equity

        log.info(
            f"[SCAN #{scan_count}] Direct: {len(direct)} ({entered} entered, {cascade_entered} cascade) | "
            f"WL: {len(watchlist)} ({new_ws} new WS) | Events: {len(scanner.event_siblings)} | Open: {len(open_pos)} | "
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

    scan_count = 0

    # LISTEN for config_reload NOTIFY — instant config updates from dashboard
    async def _listen_config():
        try:
            import asyncpg as _apg
            _listen_conn = await _apg.connect(db.url)
            await _listen_conn.add_listener("config_reload",
                lambda conn, pid, channel, payload:
                    asyncio.create_task(_reload_config(db)))
            await _listen_conn.execute("LISTEN config_reload")
            log.info("[CONFIG] LISTEN config_reload active")
            while not _shutdown:
                await asyncio.sleep(60)
            await _listen_conn.close()
        except Exception as e:
            log.warning(f"[CONFIG] LISTEN setup failed: {e}")
    asyncio.create_task(_listen_config())

    while not _shutdown:
        try:
            scan_count += 1

            log.info(f"[SCAN #{scan_count}] Starting...")

            await asyncio.wait_for(
                _scan_cycle(scan_count, db, ws, tg, scanner),
                timeout=_SCAN_TIMEOUT,
            )

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
            log.debug(f"[MAIN] Scan #{scan_count} done, watchdog reset (age=0s)")

    # ── Shutdown ──

    log.info("[MAIN] Shutting down...")
    await ws.stop()
    ws_task.cancel()
    _http_client = None
    await scanner.close()
    await tg.send("<b>quant-micro stopped</b>")
    await tg.close()
    await db.close()
    log.info("[MAIN] Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
