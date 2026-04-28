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

# Rapid drop block counter: how many consecutive times REST blocked a rapid drop per ws_key.
# After 3 blocks we trust the WS bid directly — REST midpoint/last-trade lags order book
# during fast moves (e.g. BTC approaching a price target).
_rapid_drop_blocks: dict = {}  # ws_key → int

# Max loss block counter: same logic for MAX_LOSS. REST can lag for 25+ min on thin markets.
# After 5 consecutive REST blocks, trust WS bid directly.
_max_loss_blocks: dict = {}  # ws_key → int

# Price history throttle: record tick only when price moves ≥0.3¢ or ≥30s since last record
_price_last_recorded: dict = {}  # ws_key → (price, timestamp)
_PRICE_RECORD_MIN_CHANGE = 0.003   # 0.3¢
_PRICE_RECORD_MIN_INTERVAL = 30.0  # seconds


def cleanup_stale_cooldowns():
    """Remove expired cooldown entries to prevent unbounded growth."""
    now = time.time()
    stale = [k for k, v in _rest_cooldown.items() if now - v > 3600]
    for k in stale:
        del _rest_cooldown[k]
    # Also clear block counters for markets no longer active (closed > 1h ago)
    # We don't have a timestamp here so just cap at a safe size
    if len(_rapid_drop_blocks) > 200:
        _rapid_drop_blocks.clear()
    if len(_max_loss_blocks) > 200:
        _max_loss_blocks.clear()


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
    _price_last_recorded.pop(ws_key, None)
    _rapid_drop_blocks.pop(ws_key, None)
    _max_loss_blocks.pop(ws_key, None)
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

    # Record price tick for path history (throttled: ≥0.3¢ change or ≥30s interval)
    last_rec = _price_last_recorded.get(ws_key)
    if (last_rec is None
            or abs(bid_price - last_rec[0]) >= _PRICE_RECORD_MIN_CHANGE
            or now_ts - last_rec[1] >= _PRICE_RECORD_MIN_INTERVAL):
        _price_last_recorded[ws_key] = (bid_price, now_ts)
        await db.record_price_tick(market_id, side, bid_price, "ws")

    close_kw = dict(ws_key=ws_key, db=db, ws=ws,
                    pos_cache=pos_cache, pos_last_db_write=pos_last_db_write,
                    exit_price=bid_price)

    days_to_expiry = calc_days_left(pos.get("end_date"))

    # ── Resolution: ≤1¢ (LOSS) ──
    if bid_price <= 0.01:
        # REST-verify before closing — WS can send 0 during platform maintenance/outage
        rest_price, _ = await _verify_price_and_volume(http_client, market_id, side)
        if rest_price is None:
            log.warning(f"[RESOLVED LOSS SKIPPED] REST unavailable for {market_id[:8]} — holding position during outage")
            return
        if rest_price > 0.01:
            log.info(f"[RESOLVED LOSS BLOCKED] {market_id[:8]} WS=0 but REST={rest_price:.4f} — WS noise")
            return
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

    # ── Early Take-Profit ──
    # When price rises to TP threshold with meaningful time remaining, sell now
    # instead of waiting for resolution. Frees capital for faster redeployment.
    # REST-verified to avoid selling on a transient order book spike.
    tp_price = config.get("TAKE_PROFIT_PRICE", 0.98)
    tp_min_days = config.get("TAKE_PROFIT_MIN_DAYS", 1.0)
    # Minimum gain above entry: must exceed fees to be profitable.
    # Break-even: SLIPPAGE + FEE_PCT * entry_price. Add 0.5¢ margin.
    tp_min_gain = config.get("SLIPPAGE", 0) + config.get("FEE_PCT", 0) * entry_price + 0.005
    if (bid_price >= tp_price
            and bid_price >= entry_price + tp_min_gain
            and days_to_expiry > tp_min_days):
        tp_cd_key = f"tp_{market_id}"
        if now_ts - _rest_cooldown.get(tp_cd_key, 0) >= _REST_COOLDOWN_SEC:
            _rest_cooldown[tp_cd_key] = now_ts
            rest_price, _ = await _verify_price_and_volume(http_client, market_id, side)
            if rest_price is not None and rest_price >= tp_price and rest_price >= entry_price + tp_min_gain:
                pnl = ((rest_price - entry_price) / entry_price) * stake - calc_exit_fee(stake, entry_price, config)
                if await _do_close(pos, pnl, "WIN", "take_profit",
                                   **{**close_kw, "exit_price": rest_price}):
                    hold_hours = hours_since(pos, "opened_at")
                    log.info(f"[TP] {side} '{pos['question'][:40]}' PnL: +${pnl:.2f} @ {rest_price:.4f} ({days_to_expiry:.1f}d left)")
                    msg = _tg_base(pos, rest_price, pnl, "💰 <b>TAKE PROFIT</b>")
                    msg += f"\n⏱ До expiry: {days_to_expiry:.1f}d | Держали: {hold_hours:.0f}ч"
                    await tg.send(msg)
                return

    # ── Hard max loss cap — ALWAYS enforced ──
    max_loss = config["MAX_LOSS_PER_POS"]
    if pnl_dollar <= -max_loss:
        ml_blocks = _max_loss_blocks.get(ws_key, 0)
        if ml_blocks >= 5:
            # REST has blocked 5 consecutive times — trust WS bid directly
            log.info(f"[MAX LOSS] Bypassing REST after {ml_blocks} blocks — trusting WS bid={bid_price:.4f}")
            pnl_dollar = ((bid_price - entry_price) / entry_price) * stake
        else:
            rest_price, _ = await _verify_price_and_volume(http_client, market_id, side)
            if rest_price is None:
                # REST unavailable (maintenance/outage) — never close on unverified WS data
                log.warning(f"[MAX LOSS SKIPPED] REST unavailable for {market_id[:8]} — holding position during outage")
                return
            rest_pnl = ((rest_price - entry_price) / entry_price) * stake
            if rest_pnl > -max_loss:
                _max_loss_blocks[ws_key] = ml_blocks + 1
                log.info(f"[MAX LOSS BLOCKED] {market_id[:8]} WS loss=${pnl_dollar:.2f} but REST=${rest_pnl:.2f} — not real (block #{ml_blocks+1})")
                return
            pnl_dollar = rest_pnl
        _max_loss_blocks.pop(ws_key, None)  # reset on close
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
    rapid_drop_abs = config.get("RAPID_DROP_PCT", 0.07)

    if bid_price >= entry_price - rapid_drop_abs:
        _rapid_drop_blocks.pop(ws_key, None)  # price recovered — reset block counter
        return  # no rapid drop

    # REST cooldown
    now_ts = time.time()
    if now_ts - _rest_cooldown.get(market_id, 0) < _REST_COOLDOWN_SEC:
        return
    _rest_cooldown[market_id] = now_ts

    # After 3 consecutive REST blocks, bypass REST verification: Gamma REST
    # returns midpoint/last-trade which lags the live order book during fast moves.
    # At that point the WS bid is more reliable than the stale REST price.
    blocks = _rapid_drop_blocks.get(ws_key, 0)
    if blocks >= 3:
        log.info(f"[RAPID DROP] Bypassing REST after {blocks} blocks — trusting WS bid={bid_price:.4f}")
        rest_price, vol_24h = None, None
        check_price = bid_price
    else:
        # REST verify
        rest_price, vol_24h = await _verify_price_and_volume(http_client, market_id, side)
        if rest_price is None:
            # REST unavailable (maintenance/outage) — skip rapid drop, don't trust WS alone
            log.warning(f"[RAPID DROP SKIPPED] REST unavailable for {market_id[:8]} — holding position during outage")
            return
        check_price = rest_price

        if check_price >= entry_price - rapid_drop_abs:
            if rest_price is not None:
                _rapid_drop_blocks[ws_key] = blocks + 1
                log.info(f"[RAPID DROP BLOCKED] {market_id[:8]} {side} WS={bid_price:.4f} REST={rest_price:.4f} (block #{blocks+1})")
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


# WS silence threshold: if no tick received for this many seconds, trigger REST poll
_WS_STALE_SEC = 300  # 5 minutes


async def rest_poll_stale_positions(open_positions: list,
                                    db, ws: MicroWS, tg,
                                    config: dict, http_client,
                                    pos_cache: dict, pos_last_db_write: dict,
                                    shutdown: bool):
    """REST-poll positions where WS has been silent for ≥5 minutes.
    Safety net for markets where Polymarket WS sends no ticks (low liquidity,
    inactive order book) — price can move significantly without monitor being called."""
    if shutdown:
        return
    now = time.time()
    stale = []
    for pos in open_positions:
        ws_key = f"{pos['market_id']}_{pos['side']}"
        info = ws.prices.get(ws_key, {})
        last_update = info.get("last_update", 0)
        if now - last_update >= _WS_STALE_SEC:
            stale.append((ws_key, pos))

    if not stale:
        return

    log.info(f"[REST POLL] {len(stale)} stale positions (WS silent ≥{_WS_STALE_SEC}s)")

    # Fetch all stale prices in parallel — then process sequentially to avoid
    # race conditions on shared pos_cache / ws.prices state.
    import asyncio as _asyncio
    prices = await _asyncio.gather(*[
        _verify_price_and_volume(http_client, pos["market_id"], pos["side"])
        for _, pos in stale
    ], return_exceptions=True)

    for (ws_key, pos), result in zip(stale, prices):
        if isinstance(result, Exception) or result[0] is None:
            continue
        rest_price, _ = result
        market_id, side = pos["market_id"], pos["side"]
        # Record to price history so we can see the gap
        await db.record_price_tick(market_id, side, rest_price, "rest_poll")
        log.debug(f"[REST POLL] {ws_key} price={rest_price:.4f}")
        # Inject into WS state so check_position_price uses REST price
        if ws_key not in ws.prices:
            ws.prices[ws_key] = {}
        ws.prices[ws_key]["best_bid"] = rest_price
        ws.prices[ws_key]["price"] = rest_price
        ws.prices[ws_key]["last_update"] = now
        # Run full exit logic with the REST price
        await check_position_price(
            ws_key=ws_key, price=rest_price,
            info={"best_bid": rest_price},
            db=db, ws=ws, tg=tg, config=config,
            http_client=http_client,
            pos_cache=pos_cache, pos_last_db_write=pos_last_db_write,
            shutdown=shutdown,
        )
