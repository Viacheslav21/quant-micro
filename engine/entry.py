"""Entry logic: try_enter, watchlist callback, stake calculation."""

import time
import logging

from engine.shared import calc_days_left
from engine.scanner import dynamic_entry_price, is_blocked_question
from engine.ws_client import MicroWS
from utils.db import Database
from utils.telegram import TelegramBot

log = logging.getLogger("micro")

# In-memory watchlist cache: ws_key → watchlist row.
# Populated by update_watchlist_cache() after each upsert_watchlist_batch().
# Eliminates 1 DB query per WS price tick (100s of queries/min at scale).
_wl_cache: dict = {}


def update_watchlist_cache(items: list):
    """Refresh in-memory watchlist cache from latest batch upsert."""
    for item in items:
        ws_key = f"{item['market_id']}_{item.get('side', 'YES')}"
        _wl_cache[ws_key] = item


def calc_stake(bankroll: float, config: dict, days_left: float = 99,
               theme: str = "") -> float:
    """Stake = 5% of bankroll, capped by MAX_STAKE.
    Near-expiry markets get a higher cap: ≤6h→MAX_STAKE_6H, ≤1d→MAX_STAKE_1D.
    Rationale: shorter time = lower risk of adverse move = Kelly says bet more.
    Exception: esports — live matches can resolve 95¢→0¢ in minutes regardless
    of time-to-expiry. Dynamic stake uplift does NOT apply; use MAX_STAKE only."""
    pct_stake = bankroll * 0.05

    if theme == "esports":
        max_s = config["MAX_STAKE"]
    elif days_left <= 0.25:
        max_s = config.get("MAX_STAKE_6H", config["MAX_STAKE"] * 2.5)
    elif days_left <= 1.0:
        max_s = config.get("MAX_STAKE_1D", config["MAX_STAKE"] * 1.75)
    else:
        max_s = config["MAX_STAKE"]

    stake = min(max_s, max(pct_stake, config["MIN_STAKE"]))
    if stake > bankroll:
        return 0.0
    return round(stake, 2)


_last_stake_warn = 0.0


async def try_enter(candidate: dict, db: Database, ws: MicroWS,
                    tg: TelegramBot, config: dict, pos_cache: dict = None,
                    source: str = "scan", open_positions: list = None):
    """Try to enter a position. Returns True if entered, or reason string if rejected.
    Pass open_positions to avoid a DB round-trip (use the scan-cycle's cached list)."""
    global _last_stake_warn

    market_id = candidate["market_id"]
    side = candidate.get("side", "YES")
    question = candidate.get("question", "")
    theme = candidate.get("theme", "other")
    neg_risk_id = candidate.get("neg_risk_id")

    # Blocked question keywords (city/pattern blacklist)
    if is_blocked_question(question):
        return "blocked_question"

    # Combined entry check: duplicate, theme block, SL blacklist, cooldown, negRisk group
    entry_check = await db.check_entry_allowed(market_id, side, theme, neg_risk_id=neg_risk_id,
                                                   max_per_neg_risk=int(config.get("MAX_PER_NEG_RISK", 3)))
    if not entry_check["allowed"]:
        return entry_check["reason"]

    open_pos = open_positions if open_positions is not None else await db.get_open_positions()
    if len(open_pos) >= config["MAX_OPEN"]:
        return "max_open"

    # negRisk markets have their own limit (MAX_PER_NEG_RISK), skip theme limit for them
    if not neg_risk_id:
        theme_count = sum(1 for p in open_pos if p.get("theme") == theme)
        if theme_count >= config["MAX_PER_THEME"]:
            return "theme_limit"

    days_left = candidate.get("days_left", 99)

    stats = await db.get_stats(config["BANKROLL"])
    bankroll = stats.get("bankroll", config["BANKROLL"])
    stake = calc_stake(bankroll, config, days_left=days_left, theme=theme)

    if stake < config["MIN_STAKE"]:
        now = time.time()
        if now - _last_stake_warn > 300:
            log.warning(f"[ENTRY] Bankroll ${bankroll:.2f} too low for MIN_STAKE ${config['MIN_STAKE']}")
            _last_stake_warn = now
        return "low_bankroll"

    entry_price = candidate.get("best_ask") or candidate["price"]
    if entry_price <= 0:
        entry_price = candidate["price"]
    entry_price = min(entry_price + config.get("SLIPPAGE", 0), 0.99)  # sim: worse fill

    roi = (1.0 - entry_price) / entry_price
    if roi < config["MIN_ROI"]:
        return "low_roi"

    quality = candidate.get("quality", 0)
    # Dynamic quality threshold: far markets need higher quality
    base_q = config["MIN_QUALITY_SCORE"]
    if days_left <= 1:
        min_q = base_q
    elif days_left <= 3:
        min_q = max(base_q, 55)
    elif days_left <= 5:
        min_q = max(base_q, 70)
    else:
        min_q = max(base_q, 80)
    if quality < min_q:
        return "low_quality"

    # Execute
    pos_id = f"mic_{market_id[:8]}_{int(time.time())}"
    pos = {
        "id": pos_id,
        "market_id": market_id,
        "question": question,
        "theme": theme,
        "side": side,
        "entry_price": round(entry_price, 4),
        "stake_amt": stake,
        "config_tag": config["CONFIG_TAG"],
        "end_date": candidate.get("end_date"),
        "neg_risk_id": neg_risk_id,
    }

    await db.save_position_and_deduct(pos, stake)
    await db.upsert_watchlist(candidate)

    # Register in WS for position monitoring
    ws_key = f"{market_id}_{side}"
    ws.mark_as_position(ws_key)
    if pos_cache is not None:
        pos_cache[ws_key] = pos
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

    mode = "SIM" if config["SIMULATION"] else "REAL"
    days = candidate.get("days_left", "?")
    log.info(
        f"[ENTRY] {mode} {source.upper()} {side} '{question[:50]}' "
        f"@ {entry_price:.2f}¢ ${stake:.2f} | ROI {roi:.1%} | Q={quality:.0f} | {days}d left"
    )

    stats_now = await db.get_stats(config["BANKROLL"])
    open_count = len(await db.get_open_positions())
    await tg.send(
        f"🔬 <b>MICRO | {source.upper()}</b> [{mode}]\n\n"
        f"{'✅' if side=='YES' else '❌'} {side} <b>{question[:80]}</b>\n"
        f"📊 Вход: <b>{entry_price*100:.1f}¢</b> | Ставка: <b>${stake:.2f}</b>\n"
        f"💹 ROI: {roi:.1%} | Q={quality:.0f} | {days}d left\n"
        f"📉 Spread: {candidate.get('spread', 0)*100:.1f}¢\n"
        f"💼 Банк: ${stats_now.get('bankroll', 0):.0f} | Открыто: {open_count+1}\n"
        f"🔗 <a href='https://polymarket.com/event/{candidate.get('slug') or candidate.get('market_id', '')}'>Polymarket</a>"
    )
    return True


async def check_watchlist_price(ws_key: str, price: float, info: dict,
                                 db: Database, ws: MicroWS, tg: TelegramBot,
                                 config: dict, pos_cache: dict, shutdown: bool):
    """WS callback: watchlist price updated. Enter if it hit entry zone."""
    if shutdown:
        return

    if price < 0.86:
        return

    parts = ws_key.rsplit("_", 1)
    if len(parts) != 2:
        return
    market_id, side = parts

    spread = ws.get_spread(ws_key)
    if spread > config["MAX_SPREAD"]:
        return

    wl = _wl_cache.get(ws_key) or await db.get_watchlist_market(market_id, side)
    if not wl:
        return

    end_date_str = wl.get("end_date")
    days_left = calc_days_left(end_date_str, fallback=wl.get("days_left", 0))

    dyn_entry = dynamic_entry_price(days_left, config["ENTRY_MIN_PRICE"], config)
    if price < dyn_entry:
        return

    candidate = {
        "market_id": market_id,
        "question": info.get("question", wl.get("question", "")),
        "theme": wl.get("theme", "other"),
        "side": side,
        "price": price,
        "best_ask": info.get("best_ask", price),
        "days_left": days_left,
        "spread": spread,
        "quality": wl.get("quality", wl.get("roi", 0) * 500),
        "yes_token": wl.get("yes_token"),
        "no_token": wl.get("no_token"),
        "ws_token": wl.get("yes_token"),
        "ws_side": "no" if side == "NO" else "yes",
        "end_date": wl.get("end_date"),
        "neg_risk_id": wl.get("neg_risk_id"),
    }

    result = await try_enter(candidate, db, ws, tg, config, pos_cache, source="ws")
    if result is True:
        log.info(f"[WS→ENTRY] {side} hit {price:.2f}¢ on '{info.get('question', '')[:40]}'")
