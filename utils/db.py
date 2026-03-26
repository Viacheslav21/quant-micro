import json
import os
import time
import logging
from typing import Optional
import asyncpg

log = logging.getLogger("micro.db")


class Database:
    def __init__(self):
        self.url = os.getenv("DATABASE_URL")
        self.pool: Optional[asyncpg.Pool] = None

    async def init(self):
        self.pool = await asyncpg.create_pool(
            self.url, min_size=2, max_size=10, command_timeout=30
        )
        await self._create_schema()
        log.info("[DB] PostgreSQL connected")

    async def _create_schema(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS micro_watchlist (
                    market_id    TEXT PRIMARY KEY,
                    question     TEXT NOT NULL,
                    theme        TEXT DEFAULT 'other',
                    yes_price    REAL NOT NULL,
                    volume       REAL DEFAULT 0,
                    liquidity    REAL DEFAULT 0,
                    spread       REAL DEFAULT 0,
                    best_ask     REAL DEFAULT 0,
                    days_left    REAL DEFAULT 0,
                    end_date     TEXT,
                    roi          REAL DEFAULT 0,
                    quality      REAL DEFAULT 0,
                    yes_token    TEXT,
                    no_token     TEXT,
                    added_at     TIMESTAMPTZ DEFAULT NOW(),
                    updated_at   TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS micro_positions (
                    id           TEXT PRIMARY KEY,
                    market_id    TEXT NOT NULL,
                    question     TEXT NOT NULL,
                    theme        TEXT DEFAULT 'other',
                    side         TEXT NOT NULL,
                    entry_price  REAL NOT NULL,
                    current_price REAL,
                    stake_amt    REAL NOT NULL,
                    unrealized_pnl REAL DEFAULT 0,
                    pnl          REAL DEFAULT 0,
                    tp_pct       REAL DEFAULT 0.05,
                    sl_pct       REAL DEFAULT 0.05,
                    status       TEXT DEFAULT 'open',
                    result       TEXT,
                    close_reason TEXT,
                    config_tag   TEXT DEFAULT 'micro-v3',
                    end_date     TEXT,
                    opened_at    TIMESTAMPTZ DEFAULT NOW(),
                    closed_at    TIMESTAMPTZ
                );

                CREATE TABLE IF NOT EXISTS micro_stats (
                    id           INTEGER PRIMARY KEY DEFAULT 1,
                    bankroll     REAL NOT NULL DEFAULT 500,
                    total_pnl    REAL DEFAULT 0,
                    wins         INTEGER DEFAULT 0,
                    losses       INTEGER DEFAULT 0,
                    total_trades INTEGER DEFAULT 0,
                    peak_equity  REAL DEFAULT 0,
                    updated_at   TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS micro_log (
                    id           BIGSERIAL PRIMARY KEY,
                    event_type   TEXT NOT NULL,
                    market_id    TEXT,
                    details      JSONB DEFAULT '{}',
                    created_at   TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_micro_pos_status
                    ON micro_positions(status);
                CREATE INDEX IF NOT EXISTS idx_micro_pos_market
                    ON micro_positions(market_id);
                CREATE INDEX IF NOT EXISTS idx_micro_pos_market_side_status
                    ON micro_positions(market_id, side, status);
                CREATE INDEX IF NOT EXISTS idx_micro_log_type
                    ON micro_log(event_type, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_micro_watchlist_price
                    ON micro_watchlist(yes_price DESC);

                INSERT INTO micro_stats (id, bankroll)
                VALUES (1, 500)
                ON CONFLICT (id) DO NOTHING;
            """)
            # Migrations — add new columns
            for col, typ, default in [
                ("days_left", "REAL", "0"),
                ("end_date", "TEXT", "NULL"),
                ("roi", "REAL", "0"),
                ("quality", "REAL", "0"),
            ]:
                await conn.execute(f"""
                    ALTER TABLE micro_watchlist ADD COLUMN IF NOT EXISTS {col} {typ} DEFAULT {default};
                """)
            # Add end_date to positions if missing
            await conn.execute("""
                ALTER TABLE micro_positions ADD COLUMN IF NOT EXISTS end_date TEXT DEFAULT NULL;
            """)
            # Cleanup old unused columns
            for col in ["no_price", "peak_price", "neg_risk"]:
                await conn.execute(f"""
                    ALTER TABLE micro_watchlist DROP COLUMN IF EXISTS {col};
                """)
        log.info("[DB] Schema ready")

    # ── Watchlist ──

    async def upsert_watchlist(self, market: dict):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO micro_watchlist
                    (market_id, question, theme, yes_price,
                     volume, liquidity, spread, best_ask,
                     days_left, end_date, roi, quality, yes_token, no_token)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                ON CONFLICT (market_id) DO UPDATE SET
                    yes_price  = EXCLUDED.yes_price,
                    volume     = EXCLUDED.volume,
                    liquidity  = EXCLUDED.liquidity,
                    spread     = EXCLUDED.spread,
                    best_ask   = EXCLUDED.best_ask,
                    days_left  = EXCLUDED.days_left,
                    roi        = EXCLUDED.roi,
                    quality    = EXCLUDED.quality,
                    updated_at = NOW()
            """,
                market["market_id"], market["question"], market.get("theme", "other"),
                market.get("price", market.get("yes_price", 0)),
                market.get("volume", 0), market.get("liquidity", 0),
                market.get("spread", 0), market.get("best_ask", 0),
                market.get("days_left", 0), market.get("end_date"),
                market.get("roi", 0), market.get("quality", 0),
                market.get("yes_token"), market.get("no_token"),
            )

    async def get_watchlist(self) -> list:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM micro_watchlist ORDER BY yes_price DESC"
            )
            return [dict(r) for r in rows]

    async def remove_from_watchlist(self, market_id: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM micro_watchlist WHERE market_id = $1", market_id
            )

    async def get_watchlist_market(self, market_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM micro_watchlist WHERE market_id = $1", market_id
            )
            return dict(row) if row else None

    # ── Positions ──

    async def save_position(self, pos: dict):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO micro_positions
                    (id, market_id, question, theme, side, entry_price,
                     current_price, stake_amt, tp_pct, sl_pct, config_tag, end_date)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            """,
                pos["id"], pos["market_id"], pos["question"],
                pos.get("theme", "other"), pos["side"], pos["entry_price"],
                pos["entry_price"], pos["stake_amt"],
                pos.get("tp_pct", 0.05), pos.get("sl_pct", 0.05),
                pos.get("config_tag", "micro-v3"),
                pos.get("end_date"),
            )

    async def save_position_and_deduct(self, pos: dict, stake: float):
        """Save position + deduct stake atomically in one transaction."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("""
                    INSERT INTO micro_positions
                        (id, market_id, question, theme, side, entry_price,
                         current_price, stake_amt, tp_pct, sl_pct, config_tag, end_date)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                """,
                    pos["id"], pos["market_id"], pos["question"],
                    pos.get("theme", "other"), pos["side"], pos["entry_price"],
                    pos["entry_price"], pos["stake_amt"],
                    pos.get("tp_pct", 0.05), pos.get("sl_pct", 0.05),
                    pos.get("config_tag", "micro-v3"),
                    pos.get("end_date"),
                )
                await conn.execute(
                    "UPDATE micro_stats SET bankroll = bankroll - $1, updated_at = NOW() WHERE id = 1",
                    stake,
                )

    async def get_open_position_by_market(self, market_id: str, side: str) -> Optional[dict]:
        """Fast single-position lookup for WS callbacks (avoids full table scan)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM micro_positions WHERE market_id = $1 AND side = $2 AND status = 'open'",
                market_id, side,
            )
            return dict(row) if row else None

    async def get_open_positions(self) -> list:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM micro_positions WHERE status = 'open' ORDER BY opened_at DESC"
            )
            return [dict(r) for r in rows]

    async def close_position(self, pos_id: str, pnl: float, result: str, reason: str) -> bool:
        """Atomic close with race protection. Returns stake + pnl to bankroll.
        Wrapped in a transaction so position close + bankroll update are all-or-nothing."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("""
                    UPDATE micro_positions
                    SET status = 'closed', pnl = $2, result = $3, close_reason = $4,
                        closed_at = NOW()
                    WHERE id = $1 AND status = 'open'
                    RETURNING id, stake_amt
                """, pos_id, pnl, result, reason)
                if row:
                    stake = row["stake_amt"]
                    await conn.execute("""
                        UPDATE micro_stats SET
                            bankroll     = bankroll + $1 + $2,
                            total_pnl    = total_pnl + $2,
                            wins         = wins + CASE WHEN $3 = 'WIN' THEN 1 ELSE 0 END,
                            losses       = losses + CASE WHEN $3 = 'LOSS' THEN 1 ELSE 0 END,
                            total_trades = total_trades + 1,
                            updated_at   = NOW()
                        WHERE id = 1
                    """, stake, pnl, result)
                    return True
                return False

    async def update_position_price(self, pos_id: str, price: float, unrealized_pnl: float):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE micro_positions
                SET current_price = $2, unrealized_pnl = $3
                WHERE id = $1 AND status = 'open'
            """, pos_id, price, unrealized_pnl)

    # ── Stats ──

    async def deduct_stake(self, stake: float):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE micro_stats SET bankroll = bankroll - $1, updated_at = NOW() WHERE id = 1",
                stake,
            )

    async def get_stats(self) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM micro_stats WHERE id = 1")
            return dict(row) if row else {"bankroll": 500, "total_pnl": 0, "wins": 0, "losses": 0}

    async def update_peak_equity(self, equity: float):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE micro_stats
                SET peak_equity = GREATEST(peak_equity, $1), updated_at = NOW()
                WHERE id = 1
            """, equity)

    async def set_bankroll(self, bankroll: float):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE micro_stats SET bankroll = $1, updated_at = NOW() WHERE id = 1",
                bankroll,
            )

    async def reset_stats(self, bankroll: float):
        """Full reset: close all positions, clear stats, clear watchlist, clear log."""
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM micro_positions")
            await conn.execute("DELETE FROM micro_watchlist")
            await conn.execute("DELETE FROM micro_log")
            await conn.execute("""
                UPDATE micro_stats SET
                    bankroll = $1, total_pnl = 0, wins = 0, losses = 0,
                    total_trades = 0, peak_equity = $1, updated_at = NOW()
                WHERE id = 1
            """, bankroll)
        log.info(f"[DB] Full reset: bankroll=${bankroll}, all positions/watchlist/log cleared")

    # ── Logging ──

    async def log_event(self, event_type: str, market_id: str = None, details: dict = None):
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO micro_log (event_type, market_id, details) VALUES ($1, $2, $3)",
                    event_type, market_id, json.dumps(details or {}),
                )
        except Exception as e:
            log.warning(f"[DB] log_event failed: {e}")

    # ── Cleanup ──

    async def cleanup_watchlist(self):
        """Remove markets that left the watchlist zone or went stale."""
        async with self.pool.acquire() as conn:
            deleted = await conn.execute("""
                DELETE FROM micro_watchlist
                WHERE yes_price < 0.75 OR yes_price > 0.98
                    OR updated_at < NOW() - INTERVAL '3 days'
            """)
            log.debug(f"[DB] Watchlist cleanup: {deleted}")

    async def has_position_on_market(self, market_id: str, side: str = None) -> bool:
        async with self.pool.acquire() as conn:
            if side:
                row = await conn.fetchrow(
                    "SELECT 1 FROM micro_positions WHERE market_id = $1 AND side = $2 AND status = 'open'",
                    market_id, side,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT 1 FROM micro_positions WHERE market_id = $1 AND status = 'open'",
                    market_id,
                )
            return row is not None

    async def has_recent_close(self, market_id: str, side: str = None, hours: float = 6) -> bool:
        """Check if a position on this market was closed recently (prevents re-entry loops)."""
        async with self.pool.acquire() as conn:
            if side:
                row = await conn.fetchrow(
                    "SELECT 1 FROM micro_positions WHERE market_id = $1 AND side = $2 "
                    "AND status = 'closed' AND closed_at > NOW() - INTERVAL '1 hour' * $3",
                    market_id, side, hours,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT 1 FROM micro_positions WHERE market_id = $1 "
                    "AND status = 'closed' AND closed_at > NOW() - INTERVAL '1 hour' * $3",
                    market_id, hours,
                )
            return row is not None

    async def close(self):
        if self.pool:
            await self.pool.close()
            log.info("[DB] Pool closed")
