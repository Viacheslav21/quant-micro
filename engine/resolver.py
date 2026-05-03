"""Resolution detection: expired positions, REST resolution check, event cascade."""

import asyncio
import logging
import time as _time
from datetime import datetime, timezone

import httpx

from engine.shared import calc_days_left, parse_outcome_prices
from engine.ws_client import MicroWS
from utils.db import Database
from utils.telegram import TelegramBot

log = logging.getLogger("micro")


async def check_expired_positions(db: Database, ws: MicroWS, tg: TelegramBot,
                                   http_client: httpx.AsyncClient, config: dict,
                                   pos_cache: dict, pos_last_db_write: dict,
                                   active_market_ids: set = None,
                                   open_positions: list = None):
    """Check positions via REST: resolved → proper payout, 72h past expiry → force close.
    Also checks positions whose market disappeared from active scan (closed/resolved early).
    Pass open_positions to reuse the scan-cycle's cached list and skip the DB query."""
    open_pos = open_positions if open_positions is not None else await db.get_open_positions()
    now = datetime.now(timezone.utc)

    to_check = []
    for pos in open_pos:
        end_date_str = pos.get("end_date")
        hours_past = 0
        is_expired = False

        if end_date_str:
            try:
                end = datetime.fromisoformat(str(end_date_str).replace("Z", "+00:00"))
                if now >= end:
                    hours_past = (now - end).total_seconds() / 3600
                    is_expired = True
            except Exception:
                pass

        gone_from_scan = (active_market_ids is not None
                          and pos["market_id"] not in active_market_ids)

        # Stale WS: no price update in 5+ min — market may have resolved
        # (WS stops sending events when trading halts on resolution)
        ws_key = f"{pos['market_id']}_{pos.get('side', 'YES')}"
        ws_info = ws.prices.get(ws_key)
        ws_stale = (ws_info is not None
                    and _time.time() - ws_info.get("last_update", 0) > 300)

        pos["_hours_past"] = hours_past
        pos["_is_expired"] = is_expired

        if is_expired or gone_from_scan or ws_stale:
            to_check.append(pos)

    if not to_check:
        return

    stale_count = sum(1 for p in to_check if not p["_is_expired"]
                      and (active_market_ids is None or p["market_id"] in (active_market_ids or set())))
    if stale_count:
        log.info(f"[RESOLVER] Checking {len(to_check)} positions ({stale_count} stale WS)")

    async def _fetch_market(mid):
        try:
            resp = await http_client.get(f"https://gamma-api.polymarket.com/markets/{mid}", timeout=10)
            return resp.json() if resp.status_code == 200 else None
        except Exception:
            return None

    market_data = await asyncio.gather(*[_fetch_market(p["market_id"]) for p in to_check])

    def _invalidate(ws_key):
        p = pos_cache.pop(ws_key, None)
        if p:
            pos_last_db_write.pop(p.get("id"), None)

    for pos, mdata in zip(to_check, market_data):
        market_id = pos["market_id"]
        side = pos["side"]
        entry_price = pos["entry_price"]
        stake = pos["stake_amt"]
        ws_key = f"{market_id}_{side}"
        hours_past = pos["_hours_past"]

        # 1. Check resolution via closed/resolved flag
        if mdata and (mdata.get("closed") or mdata.get("resolved")):
            yes_p, no_p = parse_outcome_prices(mdata)
            won = (side == "YES" and yes_p > 0.9) or (side == "NO" and no_p > 0.9)
            side_p = yes_p if side == "YES" else no_p
            pnl = ((1.0 - entry_price) / entry_price) * stake if won else -stake
            result = "WIN" if won else "LOSS"
            closed = await db.close_position(pos["id"], round(pnl, 4), result, "resolved",
                                              exit_price=round(side_p, 4))
            if closed:
                ws.unmark_position(ws_key)
                _invalidate(ws_key)
                await db.recalibrate_theme(pos.get("theme", "other"))
                log.info(f"[RESOLVED] {result} {side} '{pos['question'][:40]}' yes_p={yes_p:.2f} no_p={no_p:.2f} PnL: ${pnl:.2f}")
                await tg.send(
                    f"🔬 <b>MICRO</b> | 🏁 <b>RESOLVED {result}</b> {'✅' if won else '❌'}\n\n"
                    f"{'✅' if side=='YES' else '❌'} {side} <b>{pos['question'][:80]}</b>\n"
                    f"📊 Вход: {entry_price*100:.1f}¢\n"
                    f"💰 PnL: <b>${pnl:.2f}</b>"
                )
            continue

        # 2. Price-based resolution — API may lag with closed=false for hours
        #    If REST price ≥99¢ our side → resolve (same threshold as monitor)
        if mdata:
            try:
                yes_p, no_p = parse_outcome_prices(mdata)
                if yes_p > 0 or no_p > 0:
                    side_p = yes_p if side == "YES" else no_p
                    if side_p >= 0.99 or side_p <= 0.01:
                        won = side_p >= 0.99
                        pnl = ((1.0 - entry_price) / entry_price) * stake if won else -stake
                        result = "WIN" if won else "LOSS"
                        closed = await db.close_position(pos["id"], round(pnl, 4), result, "resolved",
                                                          exit_price=round(side_p, 4))
                        if closed:
                            ws.unmark_position(ws_key)
                            _invalidate(ws_key)
                            await db.recalibrate_theme(pos.get("theme", "other"))
                            log.info(f"[RESOLVED BY PRICE] {result} {side} '{pos['question'][:40]}' side_p={side_p:.4f} PnL: ${pnl:.2f}")
                            await tg.send(
                                f"🔬 <b>MICRO</b> | 🏁 <b>RESOLVED {result}</b> {'✅' if won else '❌'}\n\n"
                                f"{'✅' if side=='YES' else '❌'} {side} <b>{pos['question'][:80]}</b>\n"
                                f"📊 Вход: {entry_price*100:.1f}¢ | Цена: {side_p*100:.1f}¢\n"
                                f"💰 PnL: <b>${pnl:.2f}</b>"
                            )
                        continue
            except Exception:
                pass

        # Not resolved — update WS timestamp via proper API to prevent stale re-fires
        ws.update_last_seen(ws_key)

        # 3. Force-close if 72h+ past expiry (only for truly expired)
        if not pos.get("_is_expired") or hours_past < 72:
            continue

        current_price = pos.get("current_price", entry_price)
        pnl = ((current_price - entry_price) / entry_price) * stake
        result = "WIN" if pnl >= 0 else "LOSS"
        closed = await db.close_position(pos["id"], round(pnl, 4), result, "expired",
                                         exit_price=round(current_price, 4))
        if closed:
            ws.unmark_position(ws_key)
            _invalidate(ws_key)
            await db.recalibrate_theme(pos.get("theme", "other"))
            log.info(f"[EXPIRED] {result} {side} '{pos['question'][:40]}' PnL: ${pnl:.2f} | {hours_past:.0f}h past expiry")
            await tg.send(
                f"🔬 <b>MICRO</b> | ⏰ <b>EXPIRED {result}</b>\n\n"
                f"{'✅' if side=='YES' else '❌'} {side} <b>{pos['question'][:80]}</b>\n"
                f"📊 Вход: {entry_price*100:.1f}¢ → <b>{current_price*100:.1f}¢</b>\n"
                f"💰 PnL: <b>${pnl:.2f}</b> | {hours_past:.0f}ч после expiry"
            )


async def check_event_cascade(scanner, db: Database, ws: MicroWS, tg: TelegramBot,
                                config: dict, pos_cache: dict = None, source="cascade"):
    """Event cascade: when a negRisk market resolves YES (price ≥99¢),
    enter NO on siblings at good prices."""
    if not scanner.event_siblings:
        return 0

    from engine.entry import try_enter

    entered = 0
    for neg_risk_id, siblings in scanner.event_siblings.items():
        resolved_yes = [s for s in siblings if s["yes_price"] >= 0.99]
        if not resolved_yes:
            continue

        for s in siblings:
            if s["yes_price"] >= 0.99 or s["yes_price"] <= 0.01:
                continue
            no_price = s["no_price"]
            if no_price < 0.93 or no_price >= 0.995:
                continue

            days_left = calc_days_left(s.get("end_date"), fallback=0.5)

            candidate = {
                "market_id": s["market_id"],
                "slug": s.get("slug", ""),
                "question": s["question"],
                "theme": s["theme"],
                "side": "NO",
                "price": no_price,
                "best_ask": no_price,
                "volume": s["volume"],
                "liquidity": s["volume"],
                "spread": s["spread"],
                "days_left": days_left,
                "end_date": s.get("end_date"),
                "roi": round((1.0 - no_price) / no_price, 4) if no_price > 0 else 0,
                "quality": 95,
                "yes_token": s["yes_token"],
                "no_token": s["no_token"],
                "ws_token": s["no_token"],
                "ws_side": "no",
                "neg_risk_id": neg_risk_id,
            }

            if (await try_enter(candidate, db, ws, tg, config, pos_cache, source=source)) is True:
                entered += 1
                log.info(
                    f"[CASCADE] Entered NO {s['question'][:50]} @ {no_price*100:.1f}¢ "
                    f"(sibling resolved YES in event {neg_risk_id[:8]})"
                )

    return entered
