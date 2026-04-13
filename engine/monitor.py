"""Position monitoring: MAX_LOSS, rapid drop, resolution detection via WS."""

import time
import logging

import httpx
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type

from engine.shared import calc_days_left, calc_exit_fee, hours_since, parse_outcome_prices
from engine.ws_client import MicroWS
from utils.db import Database
from utils.telegram import TelegramBot

log = logging.getLogger("micro")

# REST cooldown per market (avoids hammering API on stale WS prices)
_rest_cooldown: dict = {}
_REST_COOLDOWN_SEC = 60


def cleanup_stale_cooldowns():
    """Remove expired cooldown entries to prevent unbounded growth."""
    now = time.time()
    stale = [k for k, v in _rest_cooldown.items() if now - v > 3600]
    for k in stale:
        del _rest_cooldown[k]


@retry(stop=stop_after_attempt(2), wait=wait_fixed(1),
       retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError, OSError)),
       reraise=True)
async def _fetch_market_data(http_client: httpx.AsyncClient, market_id: str):
    """Fetch market data from Gamma API with 1 retry on network errors."""
    r = await http_client.get(
        f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=5,
    )
    r.raise_for_status()
    return r.json()


async def _verify_price_and_volume(http_client: httpx.AsyncClient, market_id: str, side: str):
    """Single REST call: fetch price + 24h volume. Returns (side_price, vol_24h) or (None, None).
    Retries once on timeout/network error (critical for SL verification)."""
    if not http_client:
        return None, None
    try:
        m = await _fetch_market_data(http_client, market_id)
        yes_p, no_p = parse_outcome_prices(m)
        price = no_p if side == "NO" else yes_p
        vol_24h = float(m.get("volume24hr") or 0)
        return round(price, 4) if price > 0 else None, vol_24h
    except Exception as e:
        log.warning(f"[REST] Verify failed for {market_id[:8]}: {e}")
        return None, None


async def _do_close(pos, pnl, result, reason, ws_key,
                    db: Database, ws: MicroWS,
                    pos_cache: dict, pos_last_db_write: dict,
                    exit_price: float = None):
    """Atomic close + WS cleanup + theme recalibration. Returns True if closed."""
    closed = await db.close_position(pos["id"], round(pnl, 4), result, reason, exit_price=exit_price)
    if not closed:
        return False
    ws.unmark_position(ws_key)
    p = pos_cache.pop(ws_key, None)
    if p:
        pos_last_db_write.pop(p.get("id"), None)
    await db.recalibrate_theme(pos.get("theme", "other"))
    return True


def _tg_base(pos, bid_price, pnl, label):
    """Build common telegram message prefix."""
    side = pos.get("side", "YES")
    entry_price = pos["entry_price"]
    stake = pos["stake_amt"]
    pnl_pct = pnl / stake if stake else 0
    return (
        f"🔬 <b>MICRO</b> | {label}\n\n"
        f"{'✅' if side=='YES' else '❌'} {side} <b>{pos.get('question', '')[:80]}</b>\n"
        f"📊 Вход: {entry_price*100:.1f}¢ → <b>{bid_price*100:.1f}¢</b>\n"
        f"💰 PnL: <b>${pnl:.2f}</b> ({pnl_pct:+.1%})"
    )


async def check_position_price(ws_key: str, price: float, info: dict,
                                db: Database, ws: MicroWS, tg: TelegramBot,
                                config: dict, http_client: httpx.AsyncClient,
                                pos_cache: dict, pos_last_db_write: dict,
                                shutdown: bool):
    """WS callback: position price updated. Check SL/resolution."""
    if shutdown:
        return

    parts = ws_key.rsplit("_", 1)
    if len(parts) != 2:
        return
    market_id, side = parts

    # Use in-memory cache to avoid DB read on every WS tick
    pos = pos_cache.get(ws_key)
    if not pos:
        pos = await db.get_open_position_by_market(market_id, side)
        if not pos:
            ws.unmark_position(ws_key)
            pos_cache.pop(ws_key, None)
            return
        pos_cache[ws_key] = pos

    entry_price = pos["entry_price"]
    stake = pos["stake_amt"]

    bid_price = info.get("best_bid", price)
    if bid_price <= 0:
        bid_price = price

    pnl_pct = (bid_price - entry_price) / entry_price if entry_price > 0 else 0
    pnl_dollar = pnl_pct * stake
    DB_WRITE_INTERVAL = 30

    # Throttle DB writes
    now_ts = time.time()
    last_write = pos_last_db_write.get(pos["id"], 0)
    if now_ts - last_write >= DB_WRITE_INTERVAL:
        await db.update_position_price(pos["id"], bid_price, round(pnl_dollar, 4))
        pos_last_db_write[pos["id"]] = now_ts

    close_kw = dict(ws_key=ws_key, db=db, ws=ws,
                    pos_cache=pos_cache, pos_last_db_write=pos_last_db_write,
                    exit_price=bid_price)

    # ── Resolution: ≤1¢ (LOSS) ──
    if bid_price <= 0.01:
        pnl = -stake
        if await _do_close(pos, pnl, "LOSS", "resolved_loss", **close_kw):
            log.info(f"[RESOLVED] LOSS {side} '{pos['question'][:40]}' PnL: ${pnl:.2f}")
            await tg.send(_tg_base(pos, bid_price, pnl, "🏁 <b>RESOLVED LOSS</b>"))
        return

    # ── Resolution: ≥99¢ (WIN) — payout is $1.00, not bid_price ──
    if bid_price >= config["RESOLUTION_PRICE"]:
        pnl = ((1.0 - entry_price) / entry_price) * stake
        if await _do_close(pos, pnl, "WIN", "resolved", **close_kw):
            hold_hours = hours_since(pos, "opened_at")
            stats_now = await db.get_stats(config["BANKROLL"])
            log.info(f"[RESOLVED] WIN {side} '{pos['question'][:40]}' PnL: +${pnl:.2f}")
            msg = _tg_base(pos, bid_price, pnl, "🏁 <b>RESOLVED WIN</b> ✅")
            msg += f"\n⏱ Держали: {hold_hours:.0f}ч | 💼 Банк: ${stats_now.get('bankroll', 0):.0f}"
            await tg.send(msg)
        return

    # ── Hard max loss cap — ALWAYS enforced ──
    max_loss = config["MAX_LOSS_PER_POS"]
    if pnl_dollar <= -max_loss:
        rest_price, _ = await _verify_price_and_volume(http_client, market_id, side)
        if rest_price is not None:
            rest_pnl = ((rest_price - entry_price) / entry_price) * stake
            if rest_pnl > -max_loss:
                log.info(f"[MAX LOSS BLOCKED] {market_id[:8]} WS loss=${pnl_dollar:.2f} but REST=${rest_pnl:.2f} — not real")
                return
            pnl_dollar = rest_pnl
        pnl = pnl_dollar - calc_exit_fee(stake, entry_price, config)
        if await _do_close(pos, pnl, "LOSS", "max_loss", **close_kw):
            log.info(f"[MAX LOSS] {side} '{pos['question'][:40]}' loss=${pnl:.2f} > cap ${max_loss}")
            msg = _tg_base(pos, bid_price, pnl, "🚨 <b>MAX LOSS CAP</b>")
            msg += f"\n📉 Cap: ${max_loss}"
            await tg.send(msg)
        return

    # ── Rapid Drop (only protection besides MAX_LOSS) ──
    # Percentage SL disabled — fights the resolution harvesting strategy.
    # MAX_LOSS ($3 hard cap) + rapid drop (absolute ¢ threshold) are sufficient.
    days_to_expiry = calc_days_left(pos.get("end_date"))
    rapid_drop_abs = config.get("RAPID_DROP_PCT", 0.07)

    if bid_price >= entry_price - rapid_drop_abs:
        return  # no rapid drop

    # REST cooldown
    now_ts = time.time()
    if now_ts - _rest_cooldown.get(market_id, 0) < _REST_COOLDOWN_SEC:
        return
    _rest_cooldown[market_id] = now_ts

    # REST verify
    rest_price, vol_24h = await _verify_price_and_volume(http_client, market_id, side)
    check_price = rest_price if rest_price is not None else bid_price

    if check_price >= entry_price - rapid_drop_abs:
        if rest_price is not None:
            log.info(f"[RAPID DROP BLOCKED] {market_id[:8]} {side} WS={bid_price:.4f} but REST={rest_price:.4f}")
        return

    # Volume confirmation
    if vol_24h is not None and vol_24h < 5000:
        log.info(f"[RAPID DROP VOL BLOCKED] {market_id[:8]} {side} vol_24h=${vol_24h:.0f} — skipping")
        return

    check_pnl_pct = (check_price - entry_price) / entry_price if entry_price > 0 else 0
    pnl = check_pnl_pct * stake - calc_exit_fee(stake, entry_price, config)
    if await _do_close(pos, pnl, "LOSS", "rapid_drop", **close_kw):
        log.info(f"[RAPID DROP] LOSS {side} '{pos['question'][:40]}' PnL: ${pnl:.2f} ({check_pnl_pct:+.1%})")
        msg = _tg_base(pos, bid_price, pnl, "⚡ <b>RAPID DROP</b>")
        msg += f"\n⏱ До expiry: {days_to_expiry:.1f}d"
        await tg.send(msg)
