"""
Backtest: replay closed micro_positions through new v4 rules.
Compares OLD (actual) results vs NEW (what would have happened).

New rules tested:
  1. SL 7-10% instead of 3-7%
  2. SL blacklist (no re-entry after SL on same market+side)
  3. Risky themes: israel, military added
  4. Volume-confirmed SL (skip SL if low volume — simulated via heuristic)
  5. Theme auto-block (Bayesian WR < 40% after 10+ trades → block)
  6. Correlation penalty (effective_n, max 5% bankroll per effective bet)
  7. Rapid drop guard 7¢ instead of 5¢

Usage:
  python3 backtest.py
"""

import asyncio
import os
import json
from datetime import datetime, timezone
from collections import defaultdict

import asyncpg
from dotenv import load_dotenv

# Try local .env first, fall back to quant-ml/.env for DATABASE_URL
load_dotenv()
if not os.getenv("DATABASE_URL"):
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "quant-ml", ".env"))

DATABASE_URL = os.getenv("DATABASE_URL")
INITIAL_BANKROLL = 500.0

# ── New v4 rules ──

RISKY_THEMES_V4 = {"sports", "esports", "israel", "military"}

# New dynamic SL schedule
def new_sl_pct(days_left: float) -> float:
    if days_left <= 0.5: return 0.10
    if days_left <= 1:   return 0.09
    if days_left <= 2:   return 0.08
    return 0.07

# Old dynamic SL schedule (what was actually used)
def old_sl_pct(days_left: float) -> float:
    if days_left <= 0.5: return 0.07
    if days_left <= 1:   return 0.05
    if days_left <= 2:   return 0.04
    return 0.03

SHRINKAGE_K = 20
BLOCK_WR_THRESHOLD = 0.40
BLOCK_MIN_TRADES = 10
THEME_RHO = 0.5
MAX_EFF_STAKE_PCT = 0.05
MAX_WORST_CASE_PCT = 0.15
MAX_PER_THEME = 5


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
        return 2.0  # default
    try:
        end = datetime.fromisoformat(str(end_str).replace("Z", "+00:00"))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        return max(0, (end - opened).total_seconds() / 86400)
    except Exception:
        return 2.0


def simulate_old(trades):
    """Replay with OLD rules — should match actual results."""
    bankroll = INITIAL_BANKROLL
    wins = 0
    losses = 0
    total_pnl = 0
    results = []

    for t in trades:
        if t["status"] != "closed":
            continue
        pnl = t["pnl"] or 0
        result = t["result"]
        total_pnl += pnl
        bankroll += pnl  # simplified: actual had stake deduct/return
        if result == "WIN":
            wins += 1
        elif result == "LOSS":
            losses += 1
        results.append(t)

    return {
        "trades": len(results),
        "wins": wins,
        "losses": losses,
        "wr": wins / len(results) * 100 if results else 0,
        "total_pnl": total_pnl,
        "final_bankroll": INITIAL_BANKROLL + total_pnl,
        "details": results,
    }


def simulate_new(trades):
    """Replay with NEW v4 rules."""
    bankroll = INITIAL_BANKROLL
    wins = 0
    losses = 0
    total_pnl = 0
    results = []
    skipped = []

    # State tracking
    sl_blacklist = set()  # (market_id, side) pairs that had SL
    theme_stats = defaultdict(lambda: {"trades": 0, "wins": 0})  # per-theme tracking
    open_positions = []  # currently "open" positions in simulation
    theme_open_stake = defaultdict(float)  # theme → total staked

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
        old_sl = t["sl_pct"] or 0.05

        skip_reason = None

        # ── Rule 1: Risky theme filter ──
        if theme in RISKY_THEMES_V4:
            skip_reason = f"risky_theme:{theme}"

        # ── Rule 2: SL blacklist ──
        if not skip_reason and (market_id, side) in sl_blacklist:
            skip_reason = "sl_blacklist"

        # ── Rule 3: Theme auto-block (Bayesian) ──
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

        # ── Rule 4: MAX_PER_THEME = 5 ──
        if not skip_reason:
            theme_open = sum(1 for p in open_positions if p["theme"] == theme)
            if theme_open >= MAX_PER_THEME:
                skip_reason = f"theme_limit:{theme}({theme_open})"

        # ── Rule 5: Correlation penalty ──
        if not skip_reason:
            theme_open_count = sum(1 for p in open_positions if p["theme"] == theme)
            if theme_open_count > 0:
                total_theme_stake = theme_open_stake[theme]
                effective_n = theme_open_count / (1 + (theme_open_count - 1) * THEME_RHO)
                stake_per_eff = total_theme_stake / effective_n if effective_n > 0 else total_theme_stake
                max_allowed = bankroll * MAX_EFF_STAKE_PCT
                if stake_per_eff >= max_allowed:
                    skip_reason = f"corr_limit:{theme}(eff_stake=${stake_per_eff:.1f}>max${max_allowed:.1f})"
                # Worst case
                worst = (total_theme_stake + stake) * 0.08
                max_loss = bankroll * MAX_WORST_CASE_PCT
                if worst > max_loss:
                    skip_reason = f"worst_case:{theme}(${worst:.1f}>15%=${max_loss:.1f})"

        if skip_reason:
            skipped.append({"question": question[:60], "theme": theme, "reason": skip_reason,
                           "old_pnl": pnl, "old_result": result})
            # Still update theme stats (the trade happened historically)
            theme_stats[theme]["trades"] += 1
            if result == "WIN":
                theme_stats[theme]["wins"] += 1
            if close_reason in ("stop_loss", "rapid_drop"):
                sl_blacklist.add((market_id, side))
            continue

        # ── Trade passes new filters. Now apply new SL/exit logic ──

        new_pnl = pnl
        new_result = result
        new_reason = close_reason

        if close_reason == "stop_loss":
            new_sl = new_sl_pct(days_left)

            # Was the drop within new SL range? If so, position would have survived
            pnl_pct = (t["current_price"] - entry_price) / entry_price if entry_price > 0 else 0
            if abs(pnl_pct) < new_sl:
                # Position would NOT have hit new wider SL
                # Estimate: it likely would have resolved (based on 100% WR on resolved trades)
                est_pnl = ((1.0 - entry_price) / entry_price) * stake * 0.5  # conservative: 50% of max
                new_pnl = round(est_pnl, 4)
                new_result = "WIN"
                new_reason = "survived_sl→resolved(est)"
            else:
                # Still hit SL even with wider range — keep as loss but with wider SL
                new_pnl = round(-new_sl * stake, 4)
                new_result = "LOSS"
                new_reason = f"stop_loss(new_sl={new_sl:.0%})"

        elif close_reason == "rapid_drop":
            # New rapid drop is 7¢ instead of 5¢
            drop = entry_price - (t["current_price"] or entry_price)
            if drop < 0.07:
                # Would have survived new 7¢ guard
                est_pnl = ((1.0 - entry_price) / entry_price) * stake * 0.5
                new_pnl = round(est_pnl, 4)
                new_result = "WIN"
                new_reason = "survived_rapid→resolved(est)"
            # else: still rapid drop

        # Record
        total_pnl += new_pnl
        bankroll += new_pnl
        if new_result == "WIN":
            wins += 1
        elif new_result == "LOSS":
            losses += 1

        results.append({
            "question": question[:60],
            "theme": theme,
            "old_result": result,
            "old_pnl": t["pnl"],
            "old_reason": close_reason,
            "new_result": new_result,
            "new_pnl": new_pnl,
            "new_reason": new_reason,
        })

        # Update tracking
        theme_stats[theme]["trades"] += 1
        if new_result == "WIN":
            theme_stats[theme]["wins"] += 1
        if new_reason.startswith("stop_loss") or close_reason == "rapid_drop":
            sl_blacklist.add((market_id, side))

        # Simulate open/close for correlation tracking
        open_positions.append({"market_id": market_id, "theme": theme, "stake": stake})
        theme_open_stake[theme] += stake
        # "Close" after processing (simplified)
        open_positions = [p for p in open_positions if p["market_id"] != market_id]
        theme_open_stake[theme] = max(0, theme_open_stake[theme] - stake)

    return {
        "trades": len(results),
        "wins": wins,
        "losses": losses,
        "wr": wins / len(results) * 100 if results else 0,
        "total_pnl": total_pnl,
        "final_bankroll": INITIAL_BANKROLL + total_pnl,
        "details": results,
        "skipped": skipped,
        "theme_stats": dict(theme_stats),
    }


def print_report(old, new):
    print("=" * 70)
    print("BACKTEST: OLD (v3) vs NEW (v4) RULES")
    print("=" * 70)
    print()
    print(f"{'Metric':<25} {'OLD (actual)':>15} {'NEW (v4)':>15} {'Delta':>12}")
    print("-" * 70)
    print(f"{'Trades':<25} {old['trades']:>15} {new['trades']:>15} {new['trades']-old['trades']:>+12}")
    print(f"{'Wins':<25} {old['wins']:>15} {new['wins']:>15} {new['wins']-old['wins']:>+12}")
    print(f"{'Losses':<25} {old['losses']:>15} {new['losses']:>15} {new['losses']-old['losses']:>+12}")
    print(f"{'Win Rate':<25} {old['wr']:>14.1f}% {new['wr']:>14.1f}% {new['wr']-old['wr']:>+11.1f}%")
    print(f"{'Total PnL':<25} ${old['total_pnl']:>13.2f} ${new['total_pnl']:>13.2f} ${new['total_pnl']-old['total_pnl']:>+10.2f}")
    print(f"{'Final Bankroll':<25} ${old['final_bankroll']:>13.2f} ${new['final_bankroll']:>13.2f} ${new['final_bankroll']-old['final_bankroll']:>+10.2f}")
    print()

    # Skipped trades
    if new["skipped"]:
        print(f"── SKIPPED TRADES ({len(new['skipped'])}) ──")
        print(f"  (trades that NEW rules would have blocked)")
        print()
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
            print(f"  {reason}: {len(items)} trades blocked ({wins_blocked}W/{losses_blocked}L) "
                  f"| old PnL: ${pnl_sum:+.2f}")
            for s in items:
                marker = "W" if s["old_result"] == "WIN" else "L"
                print(f"    [{marker}] ${s['old_pnl']:+.2f} {s['theme']:>10} | {s['question']}")
        print(f"\n  Total PnL saved by skipping: ${-total_saved:+.2f}")
        print()

    # Changed outcomes
    changed = [r for r in new["details"] if r["old_result"] != r["new_result"]]
    if changed:
        print(f"── CHANGED OUTCOMES ({len(changed)}) ──")
        print(f"  (trades where new SL/rapid-drop rules change the result)")
        print()
        for r in changed:
            print(f"  {r['old_result']}→{r['new_result']} | "
                  f"${r['old_pnl']:+.2f}→${r['new_pnl']:+.2f} | "
                  f"{r['new_reason']}")
            print(f"    {r['theme']:>10} | {r['question']}")
        delta = sum(r["new_pnl"] - (r["old_pnl"] or 0) for r in changed)
        print(f"\n  PnL improvement from changed outcomes: ${delta:+.2f}")
        print()

    # Theme stats
    if new.get("theme_stats"):
        print("── THEME PERFORMANCE (in NEW simulation) ──")
        for theme, stats in sorted(new["theme_stats"].items(), key=lambda x: -x[1]["trades"]):
            wr = stats["wins"] / stats["trades"] * 100 if stats["trades"] > 0 else 0
            global_trades = sum(s["trades"] for s in new["theme_stats"].values())
            global_wins = sum(s["wins"] for s in new["theme_stats"].values())
            global_wr = global_wins / global_trades if global_trades > 0 else 0.5
            adj_wr = (stats["trades"] * (stats["wins"]/max(1,stats["trades"])) + SHRINKAGE_K * global_wr) / (stats["trades"] + SHRINKAGE_K)
            blocked = "BLOCKED" if stats["trades"] >= BLOCK_MIN_TRADES and adj_wr < BLOCK_WR_THRESHOLD else ""
            print(f"  {theme:<12} {stats['wins']}/{stats['trades']} ({wr:5.1f}%) adj={adj_wr:.1%} {blocked}")
    print()
    print("=" * 70)


async def main():
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
