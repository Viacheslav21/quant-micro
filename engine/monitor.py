"""Position monitoring: SL, rapid drop, MAX_LOSS, resolution detection via WS."""

import time
import json
import logging

import httpx

from engine.ws_client import MicroWS
from utils.db import Database
from utils.telegram import TelegramBot

log = logging.getLogger("micro")

# Cooldown for REST SL checks (per market)
_sl_rest_cooldown: dict = {}
_SL_REST_COOLDOWN = 60  # seconds


def cleanup_stale_cooldowns():
    """Remove expired cooldown entries to prevent unbounded growth."""
    now = time.time()
    stale = [k for k, v in _sl_rest_cooldown.items() if now - v > 3600]
    for k in stale:
        del _sl_rest_cooldown[k]


def _days_to_expiry(pos: dict) -> float:
    """Calculate days until position's market expires. Returns 999 if unknown."""
    from datetime import datetime, timezone
    end_date_str = pos.get("end_date")
    if not end_date_str:
        return 999
    try:
        end = datetime.fromisoformat(str(end_date_str).replace("Z", "+00:00"))
        return max(0, (end - datetime.now(timezone.utc)).total_seconds() / 86400)
    except Exception:
        return 999


def _hours_since(pos: dict, date_field: str = "opened_at") -> float:
    """Calculate hours since a date field."""
    from datetime import datetime, timezone
    val = pos.get(date_field)
    if not val:
        return 0
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return abs((datetime.now(timezone.utc) - dt).total_seconds() / 3600)
    except Exception:
        return 0


async def _verify_price_rest(http_client: httpx.AsyncClient, market_id: str, side: str):
    """Fetch current price from Gamma REST API. Returns side price or None."""
    if not http_client:
        return None
    try:
        r = await http_client.get(
            f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=5,
        )
        if r.status_code != 200:
            return None
        m = r.json()
        raw = m.get("outcomePrices")
        if not raw:
            return None
        if isinstance(raw, str):
            raw = json.loads(raw)
        if side == "NO" and len(raw) > 1:
            return round(float(raw[1]), 4)
        return round(float(raw[0]), 4)
    except Exception as e:
        log.warning(f"[REST] Price verify failed for {market_id[:8]}: {e}")
        return None


async def _check_volume_confirms(http_client: httpx.AsyncClient, market_id: str) -> bool:
    """Check if recent volume supports a real price move (not noise)."""
    if not http_client:
        return True
    try:
        r = await http_client.get(
            f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=5,
        )
        if r.status_code != 200:
            return True
        m = r.json()
        vol_24h = float(m.get("volume24hr") or 0)
        if vol_24h < 5000:
            log.info(f"[VOL CHECK] {market_id[:8]} vol_24h=${vol_24h:.0f} — low volume, likely noise")
            return False
        return True
    except Exception:
        return True


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
    sl_pct = pos.get("sl_pct", config["SL_PCT"])

    bid_price = info.get("best_bid", price)
    if bid_price <= 0:
        bid_price = price

    # Sanity: for 90%+ entries, bid can't realistically drop >50% without resolution
    if bid_price < entry_price * 0.5:
        log.warning(f"[SANITY] {market_id[:8]} {side} bid={bid_price:.4f} << entry={entry_price:.4f} — ignoring bad tick")
        return

    pnl_pct = (bid_price - entry_price) / entry_price if entry_price > 0 else 0
    pnl_dollar = pnl_pct * stake
    DB_WRITE_INTERVAL = 30

    # Throttle DB writes
    now_ts = time.time()
    last_write = pos_last_db_write.get(pos["id"], 0)
    if now_ts - last_write >= DB_WRITE_INTERVAL:
        await db.update_position_price(pos["id"], bid_price, round(pnl_dollar, 4))
        pos_last_db_write[pos["id"]] = now_ts

    def _invalidate():
        p = pos_cache.pop(ws_key, None)
        if p:
            pos_last_db_write.pop(p.get("id"), None)

    # ── Resolution: ≤1¢ (LOSS) ──
    if bid_price <= 0.01:
        pnl = -stake
        closed = await db.close_position(pos["id"], round(pnl, 4), "LOSS", "resolved_loss")
        if closed:
            ws.unmark_position(ws_key)
            _invalidate()
            await db.recalibrate_theme(pos.get("theme", "other"))
            log.info(f"[RESOLVED] LOSS {side} '{pos['question'][:40]}' PnL: ${pnl:.2f}")
            await tg.send(
                f"🔬 <b>MICRO</b> | 🏁 <b>RESOLVED LOSS</b>\n"
                f"{'✅' if side=='YES' else '❌'} {side} <b>{pos['question'][:80]}</b>\n"
                f"📊 Вход: {entry_price*100:.1f}¢ → {bid_price*100:.1f}¢\n"
                f"💰 PnL: <b>${pnl:.2f}</b>"
            )
        return

    # ── Resolution: ≥99¢ (WIN) — payout is $1.00, not bid_price ──
    if bid_price >= config["RESOLUTION_PRICE"]:
        pnl = ((1.0 - entry_price) / entry_price) * stake
        closed = await db.close_position(pos["id"], round(pnl, 4), "WIN", "resolved")
        if closed:
            ws.unmark_position(ws_key)
            _invalidate()
            await db.recalibrate_theme(pos.get("theme", "other"))
            log.info(f"[RESOLVED] WIN {side} '{pos['question'][:40]}' PnL: +${pnl:.2f}")
            hold_hours = _hours_since(pos, "opened_at")
            stats_now = await db.get_stats(config["BANKROLL"])
            await tg.send(
                f"🔬 <b>MICRO</b> | 🏁 <b>RESOLVED WIN</b> ✅\n\n"
                f"{'✅' if side=='YES' else '❌'} {side} <b>{pos['question'][:80]}</b>\n"
                f"📊 Вход: {entry_price*100:.1f}¢ → <b>{bid_price*100:.1f}¢</b>\n"
                f"💰 PnL: <b>+${pnl:.2f}</b> ({pnl/stake*100:+.1f}%)\n"
                f"⏱ Держали: {hold_hours:.0f}ч | 💼 Банк: ${stats_now.get('bankroll', 0):.0f}"
            )
        return

    # ── Hard max loss cap — ALWAYS enforced ──
    max_loss = config["MAX_LOSS_PER_POS"]
    if pnl_dollar <= -max_loss:
        rest_price = await _verify_price_rest(http_client, market_id, side)
        if rest_price is not None:
            rest_pnl = ((rest_price - entry_price) / entry_price) * stake
            if rest_pnl > -max_loss:
                log.info(f"[MAX LOSS BLOCKED] {market_id[:8]} WS loss=${pnl_dollar:.2f} but REST=${rest_pnl:.2f} — not real")
                return
            pnl_dollar = rest_pnl
        pnl = pnl_dollar
        # Sim: exit slippage + fee
        pnl -= config.get("SLIPPAGE", 0) * stake / entry_price + stake * config.get("FEE_PCT", 0)
        closed = await db.close_position(pos["id"], round(pnl, 4), "LOSS", "max_loss")
        if closed:
            ws.unmark_position(ws_key)
            _invalidate()
            await db.recalibrate_theme(pos.get("theme", "other"))
            log.info(f"[MAX LOSS] {side} '{pos['question'][:40]}' loss=${pnl:.2f} > cap ${max_loss}")
            await tg.send(
                f"🔬 <b>MICRO</b> | 🚨 <b>MAX LOSS CAP</b>\n\n"
                f"{'✅' if side=='YES' else '❌'} {side} <b>{pos['question'][:80]}</b>\n"
                f"📊 Вход: {entry_price*100:.1f}¢ → <b>{bid_price*100:.1f}¢</b>\n"
                f"💰 PnL: <b>${pnl:.2f}</b> (cap: ${max_loss})"
            )
        return

    # ── Stop Loss (disabled ≤1 day to expiry) ──
    days_to_expiry = _days_to_expiry(pos)
    if pnl_pct <= -sl_pct and days_to_expiry > 1:
        now_ts = time.time()
        last_check = _sl_rest_cooldown.get(market_id, 0)
        if now_ts - last_check < _SL_REST_COOLDOWN:
            return
        _sl_rest_cooldown[market_id] = now_ts
        rest_price = await _verify_price_rest(http_client, market_id, side)
        if rest_price is not None:
            rest_pnl_pct = (rest_price - entry_price) / entry_price
            if rest_pnl_pct > -sl_pct:
                log.info(
                    f"[SL BLOCKED] {market_id[:8]} {side} WS bid={bid_price:.4f} but REST={rest_price:.4f} "
                    f"(WS pnl={pnl_pct:+.1%}, REST pnl={rest_pnl_pct:+.1%}) — not a real SL"
                )
                return
        vol_confirms = await _check_volume_confirms(http_client, market_id)
        if not vol_confirms:
            log.info(f"[SL VOL BLOCKED] {market_id[:8]} {side} pnl={pnl_pct:+.1%} but low volume — skipping SL")
            return
        # Use REST price for PnL when available (more accurate than WS bid)
        if rest_price is not None:
            pnl = ((rest_price - entry_price) / entry_price) * stake
        else:
            pnl = pnl_pct * stake
        # Sim: exit slippage + fee (real sell would be worse)
        pnl -= config.get("SLIPPAGE", 0) * stake / entry_price + stake * config.get("FEE_PCT", 0)
        closed = await db.close_position(pos["id"], round(pnl, 4), "LOSS", "stop_loss")
        if closed:
            ws.unmark_position(ws_key)
            _invalidate()
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

    # ── Rapid Drop Guard (7¢ — disabled ≤1 day) ──
    if bid_price < entry_price - 0.07 and days_to_expiry > 1:
        now_ts = time.time()
        last_check = _sl_rest_cooldown.get(market_id, 0)
        if now_ts - last_check < _SL_REST_COOLDOWN:
            return
        _sl_rest_cooldown[market_id] = now_ts
        rest_price = await _verify_price_rest(http_client, market_id, side)
        if rest_price is not None and rest_price >= entry_price - 0.07:
            log.info(f"[RAPID DROP BLOCKED] {market_id[:8]} {side} WS bid={bid_price:.4f} but REST={rest_price:.4f} — not a real drop")
            return
        # Use REST price for PnL when available (more accurate than WS bid)
        if rest_price is not None:
            pnl = ((rest_price - entry_price) / entry_price) * stake
        else:
            pnl = pnl_pct * stake
        # Sim: exit slippage + fee
        pnl -= config.get("SLIPPAGE", 0) * stake / entry_price + stake * config.get("FEE_PCT", 0)
        closed = await db.close_position(pos["id"], round(pnl, 4), "LOSS", "rapid_drop")
        if closed:
            ws.unmark_position(ws_key)
            _invalidate()
            await db.recalibrate_theme(pos.get("theme", "other"))
            log.info(
                f"[RAPID DROP] LOSS {side} '{pos['question'][:40]}' @ {bid_price:.2f}¢ "
                f"PnL: ${pnl:.2f} ({pnl_pct:+.1%})"
            )
            await tg.send(
                f"🔬 <b>MICRO</b> | ⚡ <b>RAPID DROP</b>\n\n"
                f"{'✅' if side=='YES' else '❌'} {side} <b>{pos['question'][:80]}</b>\n"
                f"📊 Вход: {entry_price*100:.1f}¢ → <b>{bid_price*100:.1f}¢</b>\n"
                f"💰 PnL: <b>${pnl:.2f}</b> ({pnl_pct:+.1%})"
            )
        return
