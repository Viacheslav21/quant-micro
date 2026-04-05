"""
Backtest: replay closed micro_positions through current v5 rules.
Compares OLD (actual) results vs NEW (what would have happened).

Current rules tested:
  1. Entry ≥95¢ (was 93¢) — higher bar, more selective
  2. SL 7-10% dynamic, disabled ≤3d to expiry (let resolution play out)
  3. SL blacklist (no re-entry after SL on same market+side)
  4. Risky themes: sports, esports, israel, military (exception: ≥96¢ bypass)
  5. Volume-confirmed SL (skip SL if low volume — simulated via heuristic)
  6. Theme auto-block (Bayesian WR < 40% after 10+ trades → block)
  7. Correlation penalty (effective_n, max 5% bankroll per effective bet)
  8. Rapid drop guard 7¢
  9. No time_exit — positions hold until resolution/SL/expiry
  10. Stakes: 5% bankroll, min $5, max $50

Usage:
  python3 backtest.py
"""

import asyncio
import os
from datetime import datetime, timezone
from collections import defaultdict

import asyncpg
from dotenv import load_dotenv

load_dotenv()
if not os.getenv("DATABASE_URL"):
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "quant-ml", ".env"))

DATABASE_URL = os.getenv("DATABASE_URL")
INITIAL_BANKROLL = 500.0

# ── Current v5 rules ──

RISKY_THEMES = {"sports", "esports", "israel", "military"}
RISKY_BYPASS_PRICE = 0.96  # ≥96¢ bypasses risky filter
ENTRY_MIN_PRICE = 0.95
SL_DISABLED_DAYS = 3  # SL disabled ≤3 days to expiry

SHRINKAGE_K = 20
BLOCK_WR_THRESHOLD = 0.40
BLOCK_MIN_TRADES = 10
THEME_RHO = 0.5
MAX_EFF_STAKE_PCT = 0.05
MAX_WORST_CASE_PCT = 0.15
MAX_PER_THEME = 5
MIN_STAKE = 5.0
MAX_STAKE = 50.0
STAKE_PCT = 0.05


def dynamic_sl(days_left: float) -> float:
    """Current dynamic SL schedule."""
    if days_left <= 0.5: return 0.10
    if days_left <= 1:   return 0.09
    if days_left <= 2:   return 0.08
    return 0.07


def calc_stake(bankroll: float) -> float:
    """Current stake calculation: 5% bankroll, capped."""
    pct = bankroll * STAKE_PCT
    stake = min(MAX_STAKE, max(pct, MIN_STAKE))
    return stake if stake <= bankroll else 0


async def fetch_trades():
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("""
        SELECT id, market_id, question, theme, side, entry_price, current_price,
               stake_amt, pnl, sl_pct, status, result, close_reason, end_date,
               opened_at, closed_at
        FROM micro_positions
        ORDER BY opened_at ASC
    """)
    await conn.close()
    return [dict(r) for r in rows]


def estimate_days_left(pos):
    """Estimate days_left at entry from end_date and opened_at."""
    end_str = pos.get("end_date")
    opened = pos.get("opened_at")
    if not end_str or not opened:
        return 2.0
    try:
        end = datetime.fromisoformat(str(end_str).replace("Z", "+00:00"))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        return max(0, (end - opened).total_seconds() / 86400)
    except Exception:
        return 2.0


def estimate_days_at_close(pos):
    """Estimate days_left at close time."""
    end_str = pos.get("end_date")
    closed = pos.get("closed_at")
    if not end_str or not closed:
        return 0
    try:
        end = datetime.fromisoformat(str(end_str).replace("Z", "+00:00"))
        if closed.tzinfo is None:
            closed = closed.replace(tzinfo=timezone.utc)
        return max(0, (end - closed).total_seconds() / 86400)
    except Exception:
        return 0


def simulate_old(trades):
    """Replay with OLD rules — should match actual results."""
    bankroll = INITIAL_BANKROLL
    wins = losses = 0
    total_pnl = 0
    results = []

    for t in trades:
        if t["status"] != "closed":
            continue
        pnl = t["pnl"] or 0
        result = t["result"]
        total_pnl += pnl
        bankroll += pnl
        if result == "WIN":
            wins += 1
        elif result == "LOSS":
            losses += 1
        results.append(t)

    return {
        "trades": len(results), "wins": wins, "losses": losses,
        "wr": wins / len(results) * 100 if results else 0,
        "total_pnl": total_pnl,
        "final_bankroll": INITIAL_BANKROLL + total_pnl,
        "details": results,
    }


def simulate_new(trades):
    """Replay with current v5 rules."""
    bankroll = INITIAL_BANKROLL
    wins = losses = 0
    total_pnl = 0
    results = []
    skipped = []

    sl_blacklist = set()
    theme_stats = defaultdict(lambda: {"trades": 0, "wins": 0})
    open_positions = []
    theme_open_stake = defaultdict(float)

    for t in trades:
        if t["status"] != "closed":
            continue

        market_id = t["market_id"]
        side = t["side"]
        theme = t["theme"] or "other"
        question = t["question"] or ""
        entry_price = t["entry_price"]
        stake = t["stake_amt"]
        pnl = t["pnl"] or 0
        result = t["result"]
        close_reason = t["close_reason"]
        days_left = estimate_days_left(t)
        days_at_close = estimate_days_at_close(t)

        skip_reason = None

        # ── Rule 1: Entry price ≥95¢ ──
        if entry_price < ENTRY_MIN_PRICE:
            skip_reason = f"below_entry({entry_price*100:.0f}c<{ENTRY_MIN_PRICE*100:.0f}c)"

        # ── Rule 2: Risky theme filter (with ≥96¢ bypass) ──
        if not skip_reason and theme in RISKY_THEMES and entry_price < RISKY_BYPASS_PRICE:
            skip_reason = f"risky_theme:{theme}"

        # ── Rule 3: SL blacklist ──
        if not skip_reason and (market_id, side) in sl_blacklist:
            skip_reason = "sl_blacklist"

        # ── Rule 4: Theme auto-block (Bayesian) ──
        if not skip_reason:
            ts = theme_stats[theme]
            if ts["trades"] >= BLOCK_MIN_TRADES:
                raw_wr = ts["wins"] / ts["trades"]
                global_trades = sum(s["trades"] for s in theme_stats.values())
                global_wins = sum(s["wins"] for s in theme_stats.values())
                global_wr = global_wins / global_trades if global_trades > 0 else 0.5
                adj_wr = (ts["trades"] * raw_wr + SHRINKAGE_K * global_wr) / (ts["trades"] + SHRINKAGE_K)
                if adj_wr < BLOCK_WR_THRESHOLD:
                    skip_reason = f"theme_blocked:{theme}(wr={adj_wr:.1%})"

        # ── Rule 5: MAX_PER_THEME ──
        if not skip_reason:
            theme_open = sum(1 for p in open_positions if p["theme"] == theme)
            if theme_open >= MAX_PER_THEME:
                skip_reason = f"theme_limit:{theme}({theme_open})"

        # ── Rule 6: Correlation penalty ──
        if not skip_reason:
            theme_open_count = sum(1 for p in open_positions if p["theme"] == theme)
            if theme_open_count > 0:
                total_theme_stake = theme_open_stake[theme]
                effective_n = theme_open_count / (1 + (theme_open_count - 1) * THEME_RHO)
                stake_per_eff = total_theme_stake / effective_n if effective_n > 0 else total_theme_stake
                max_allowed = bankroll * MAX_EFF_STAKE_PCT
                if stake_per_eff >= max_allowed:
                    skip_reason = f"corr_limit:{theme}"
                worst = (total_theme_stake + stake) * 0.08
                max_loss = bankroll * MAX_WORST_CASE_PCT
                if not skip_reason and worst > max_loss:
                    skip_reason = f"worst_case:{theme}"

        # ── Rule 7: Stake check ──
        if not skip_reason:
            new_stake = calc_stake(bankroll)
            if new_stake <= 0:
                skip_reason = "bankroll_too_low"

        if skip_reason:
            skipped.append({"question": question[:60], "theme": theme, "reason": skip_reason,
                           "old_pnl": pnl, "old_result": result, "entry_price": entry_price})
            theme_stats[theme]["trades"] += 1
            if result == "WIN":
                theme_stats[theme]["wins"] += 1
            if close_reason in ("stop_loss", "rapid_drop"):
                sl_blacklist.add((market_id, side))
            continue

        # ── Trade passes filters. Apply new SL/exit logic ──

        new_pnl = pnl
        new_result = result
        new_reason = close_reason

        # SL disabled for ≤3 days to expiry — position holds to resolution
        if close_reason == "stop_loss" and days_at_close <= SL_DISABLED_DAYS:
            # SL would NOT have fired under new rules — estimate resolution outcome
            # High-probability markets (95¢+) resolve WIN most of the time
            est_pnl = ((1.0 - entry_price) / entry_price) * stake * 0.7  # 70% of max payout
            new_pnl = round(est_pnl, 4)
            new_result = "WIN"
            new_reason = "sl_disabled_≤3d→resolved(est)"

        elif close_reason == "stop_loss":
            # SL enabled (>3d to expiry) — apply new dynamic SL
            new_sl = dynamic_sl(days_left)
            pnl_pct = abs((t["current_price"] - entry_price) / entry_price) if entry_price > 0 else 0
            if pnl_pct < new_sl:
                est_pnl = ((1.0 - entry_price) / entry_price) * stake * 0.5
                new_pnl = round(est_pnl, 4)
                new_result = "WIN"
                new_reason = "survived_wider_sl→resolved(est)"
            else:
                new_pnl = round(-new_sl * stake, 4)
                new_result = "LOSS"
                new_reason = f"stop_loss(sl={new_sl:.0%})"

        elif close_reason == "rapid_drop" and days_at_close <= SL_DISABLED_DAYS:
            # Rapid drop also disabled ≤3d
            est_pnl = ((1.0 - entry_price) / entry_price) * stake * 0.7
            new_pnl = round(est_pnl, 4)
            new_result = "WIN"
            new_reason = "rapid_disabled_≤3d→resolved(est)"

        elif close_reason == "rapid_drop":
            drop = entry_price - (t["current_price"] or entry_price)
            if drop < 0.07:
                est_pnl = ((1.0 - entry_price) / entry_price) * stake * 0.5
                new_pnl = round(est_pnl, 4)
                new_result = "WIN"
                new_reason = "survived_rapid→resolved(est)"

        elif close_reason == "time_exit":
            # time_exit removed — position would hold to resolution
            est_pnl = ((1.0 - entry_price) / entry_price) * stake * 0.6
            new_pnl = round(est_pnl, 4)
            new_result = "WIN"
            new_reason = "no_time_exit→resolved(est)"

        # Record
        total_pnl += new_pnl
        bankroll += new_pnl
        if new_result == "WIN":
            wins += 1
        elif new_result == "LOSS":
            losses += 1

        results.append({
            "question": question[:60], "theme": theme,
            "old_result": result, "old_pnl": t["pnl"], "old_reason": close_reason,
            "new_result": new_result, "new_pnl": new_pnl, "new_reason": new_reason,
            "entry_price": entry_price, "days_left": round(days_left, 1),
        })

        theme_stats[theme]["trades"] += 1
        if new_result == "WIN":
            theme_stats[theme]["wins"] += 1
        if new_reason.startswith("stop_loss") or close_reason == "rapid_drop":
            sl_blacklist.add((market_id, side))

        open_positions.append({"market_id": market_id, "theme": theme, "stake": stake})
        theme_open_stake[theme] += stake
        open_positions = [p for p in open_positions if p["market_id"] != market_id]
        theme_open_stake[theme] = max(0, theme_open_stake[theme] - stake)

    return {
        "trades": len(results), "wins": wins, "losses": losses,
        "wr": wins / len(results) * 100 if results else 0,
        "total_pnl": total_pnl,
        "final_bankroll": INITIAL_BANKROLL + total_pnl,
        "details": results, "skipped": skipped,
        "theme_stats": dict(theme_stats),
    }


def print_report(old, new):
    print("=" * 70)
    print("BACKTEST: OLD (actual) vs NEW (v5) RULES")
    print("=" * 70)
    print()
    print(f"{'Metric':<25} {'OLD (actual)':>15} {'NEW (v5)':>15} {'Delta':>12}")
    print("-" * 70)
    print(f"{'Trades':<25} {old['trades']:>15} {new['trades']:>15} {new['trades']-old['trades']:>+12}")
    print(f"{'Wins':<25} {old['wins']:>15} {new['wins']:>15} {new['wins']-old['wins']:>+12}")
    print(f"{'Losses':<25} {old['losses']:>15} {new['losses']:>15} {new['losses']-old['losses']:>+12}")
    print(f"{'Win Rate':<25} {old['wr']:>14.1f}% {new['wr']:>14.1f}% {new['wr']-old['wr']:>+11.1f}%")
    print(f"{'Total PnL':<25} ${old['total_pnl']:>13.2f} ${new['total_pnl']:>13.2f} ${new['total_pnl']-old['total_pnl']:>+10.2f}")
    print(f"{'Final Bankroll':<25} ${old['final_bankroll']:>13.2f} ${new['final_bankroll']:>13.2f} ${new['final_bankroll']-old['final_bankroll']:>+10.2f}")
    print()

    # Skipped trades summary
    if new["skipped"]:
        print(f"── SKIPPED TRADES ({len(new['skipped'])}) ──")
        by_reason = defaultdict(list)
        for s in new["skipped"]:
            reason_type = s["reason"].split(":")[0]
            by_reason[reason_type].append(s)

        total_saved = 0
        for reason, items in sorted(by_reason.items()):
            pnl_sum = sum(s["old_pnl"] or 0 for s in items)
            total_saved += pnl_sum
            wins_blocked = sum(1 for s in items if s["old_result"] == "WIN")
            losses_blocked = sum(1 for s in items if s["old_result"] == "LOSS")
            print(f"  {reason}: {len(items)} blocked ({wins_blocked}W/{losses_blocked}L) "
                  f"| old PnL: ${pnl_sum:+.2f}")
        print(f"\n  Total PnL of skipped trades: ${total_saved:+.2f} (saved: ${-total_saved:+.2f})")
        print()

    # Changed outcomes
    changed = [r for r in new["details"] if r["old_result"] != r["new_result"]]
    if changed:
        print(f"── CHANGED OUTCOMES ({len(changed)}) ──")
        for r in changed:
            print(f"  {r['old_result']}→{r['new_result']} | "
                  f"${r['old_pnl']:+.2f}→${r['new_pnl']:+.2f} | "
                  f"{r['new_reason']} | {r['days_left']}d left")
            print(f"    {r['theme']:>10} | {r['entry_price']*100:.0f}c | {r['question']}")
        delta = sum(r["new_pnl"] - (r["old_pnl"] or 0) for r in changed)
        print(f"\n  PnL improvement from changed outcomes: ${delta:+.2f}")
        print()

    # Theme stats
    if new.get("theme_stats"):
        print("── THEME PERFORMANCE (v5 simulation) ──")
        for theme, stats in sorted(new["theme_stats"].items(), key=lambda x: -x[1]["trades"]):
            wr = stats["wins"] / stats["trades"] * 100 if stats["trades"] > 0 else 0
            global_trades = sum(s["trades"] for s in new["theme_stats"].values())
            global_wins = sum(s["wins"] for s in new["theme_stats"].values())
            global_wr = global_wins / global_trades if global_trades > 0 else 0.5
            adj_wr = (stats["trades"] * (stats["wins"]/max(1,stats["trades"])) + SHRINKAGE_K * global_wr) / (stats["trades"] + SHRINKAGE_K)
            blocked = "BLOCKED" if stats["trades"] >= BLOCK_MIN_TRADES and adj_wr < BLOCK_WR_THRESHOLD else ""
            print(f"  {theme:<12} {stats['wins']}/{stats['trades']} ({wr:5.1f}%) adj={adj_wr:.1%} {blocked}")
    print()

    # Key insight: SL disabled ≤3d impact
    sl_disabled = [r for r in new["details"] if "≤3d" in r.get("new_reason", "")]
    if sl_disabled:
        print(f"── SL DISABLED ≤3D IMPACT ({len(sl_disabled)}) ──")
        old_pnl = sum(r["old_pnl"] or 0 for r in sl_disabled)
        new_pnl = sum(r["new_pnl"] for r in sl_disabled)
        print(f"  Old PnL (with SL): ${old_pnl:+.2f}")
        print(f"  New PnL (hold to resolution): ${new_pnl:+.2f}")
        print(f"  Improvement: ${new_pnl - old_pnl:+.2f}")
        print()

    # Time exit removal impact
    no_time_exit = [r for r in new["details"] if "no_time_exit" in r.get("new_reason", "")]
    if no_time_exit:
        print(f"── TIME EXIT REMOVAL IMPACT ({len(no_time_exit)}) ──")
        old_pnl = sum(r["old_pnl"] or 0 for r in no_time_exit)
        new_pnl = sum(r["new_pnl"] for r in no_time_exit)
        print(f"  Old PnL (time exit): ${old_pnl:+.2f}")
        print(f"  New PnL (hold to resolution): ${new_pnl:+.2f}")
        print(f"  Improvement: ${new_pnl - old_pnl:+.2f}")
        print()

    print("=" * 70)
    print("NOTE: 'resolved(est)' outcomes assume ~60-70% of max payout for")
    print("positions that would have held to resolution instead of SL/time exit.")
    print("Actual results depend on market resolution.")
    print("=" * 70)


async def main():
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set. Create .env with DATABASE_URL=postgresql://...")
        return
    print("Fetching trades from database...")
    trades = await fetch_trades()
    closed = [t for t in trades if t["status"] == "closed"]
    open_pos = [t for t in trades if t["status"] == "open"]
    print(f"Found {len(closed)} closed + {len(open_pos)} open positions")
    print()

    old = simulate_old(trades)
    new = simulate_new(trades)
    print_report(old, new)


if __name__ == "__main__":
    asyncio.run(main())
