import json
import os
import time
import logging
from typing import Optional
import asyncpg

log = logging.getLogger("micro.db")


def _cast_config_value(value: str, value_type: str):
    """Cast config string value to its proper Python type."""
    if value_type == "float":
        return float(value)
    elif value_type == "int":
        return int(float(value))
    elif value_type == "bool":
        return value.lower() in ("true", "1", "yes")
    return value


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
                    market_id    TEXT NOT NULL,
                    side         TEXT NOT NULL DEFAULT 'YES',
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
                    updated_at   TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (market_id, side)
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

                CREATE TABLE IF NOT EXISTS micro_theme_stats (
                    theme        TEXT PRIMARY KEY,
                    trades       INTEGER DEFAULT 0,
                    wins         INTEGER DEFAULT 0,
                    losses       INTEGER DEFAULT 0,
                    total_pnl    REAL DEFAULT 0,
                    raw_wr       REAL DEFAULT 0.5,
                    adj_wr       REAL DEFAULT 0.5,
                    blocked      BOOLEAN DEFAULT FALSE,
                    updated_at   TIMESTAMPTZ DEFAULT NOW()
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
            # Migration: add side column + change PK to (market_id, side) for dual-side support
            try:
                has_side = await conn.fetchval("""
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='micro_watchlist' AND column_name='side'
                """)
                if not has_side:
                    await conn.execute("ALTER TABLE micro_watchlist ADD COLUMN side TEXT NOT NULL DEFAULT 'YES'")
                    await conn.execute("ALTER TABLE micro_watchlist DROP CONSTRAINT IF EXISTS micro_watchlist_pkey")
                    await conn.execute("ALTER TABLE micro_watchlist ADD PRIMARY KEY (market_id, side)")
                    log.info("[DB] Migrated micro_watchlist: added side column + composite PK")
            except Exception as e:
                log.warning(f"[DB] Watchlist side migration: {e}")
        log.info("[DB] Schema ready")

    async def get_config_overrides(self, service: str) -> dict:
        """Fetch live config overrides from config_live table (owned by engine)."""
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT key, value, value_type FROM config_live WHERE service = $1", service
                )
            return {r["key"]: _cast_config_value(r["value"], r["value_type"]) for r in rows}
        except Exception:
            return {}  # table may not exist yet if engine hasn't started

    # ── Watchlist ──

    async def upsert_watchlist(self, market: dict):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO micro_watchlist
                    (market_id, side, question, theme, yes_price,
                     volume, liquidity, spread, best_ask,
                     days_left, end_date, roi, quality, yes_token, no_token)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                ON CONFLICT (market_id, side) DO UPDATE SET
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
                market["market_id"], market.get("side", "YES"),
                market["question"], market.get("theme", "other"),
                market.get("price", market.get("yes_price", 0)),
                market.get("volume", 0), market.get("liquidity", 0),
                market.get("spread", 0), market.get("best_ask", 0),
                market.get("days_left", 0), market.get("end_date"),
                market.get("roi", 0), market.get("quality", 0),
                market.get("yes_token"), market.get("no_token"),
            )

    async def upsert_watchlist_batch(self, markets: list):
        """Batch upsert watchlist items in a single transaction."""
        if not markets:
            return
        async with self.pool.acquire() as conn:
            await conn.executemany("""
                INSERT INTO micro_watchlist
                    (market_id, side, question, theme, yes_price,
                     volume, liquidity, spread, best_ask,
                     days_left, end_date, roi, quality, yes_token, no_token)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                ON CONFLICT (market_id, side) DO UPDATE SET
                    yes_price  = EXCLUDED.yes_price,
                    volume     = EXCLUDED.volume,
                    liquidity  = EXCLUDED.liquidity,
                    spread     = EXCLUDED.spread,
                    best_ask   = EXCLUDED.best_ask,
                    days_left  = EXCLUDED.days_left,
                    roi        = EXCLUDED.roi,
                    quality    = EXCLUDED.quality,
                    updated_at = NOW()
            """, [(m["market_id"], m.get("side", "YES"),
                   m["question"], m.get("theme", "other"),
                   m.get("price", m.get("yes_price", 0)),
                   m.get("volume", 0), m.get("liquidity", 0),
                   m.get("spread", 0), m.get("best_ask", 0),
                   m.get("days_left", 0), m.get("end_date"),
                   m.get("roi", 0), m.get("quality", 0),
                   m.get("yes_token"), m.get("no_token")) for m in markets])

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

    async def check_entry_allowed(self, market_id: str, side: str, theme: str, cooldown_hours: float = 6) -> dict:
        """Combined entry check — replaces 4 separate queries with 1.
        Returns {allowed: bool, reason: str|None}."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT
                    EXISTS(SELECT 1 FROM micro_positions WHERE market_id=$1 AND side=$2 AND status='open') as has_open,
                    EXISTS(SELECT 1 FROM micro_positions WHERE market_id=$1 AND side=$2
                           AND status='closed' AND close_reason IN ('stop_loss','rapid_drop')) as has_sl,
                    EXISTS(SELECT 1 FROM micro_positions WHERE market_id=$1 AND side=$2
                           AND status='closed' AND closed_at > NOW() - INTERVAL '1 hour' * $3) as has_recent,
                    COALESCE((SELECT blocked FROM micro_theme_stats WHERE theme=$4), false) as theme_blocked
            """, market_id, side, cooldown_hours, theme)
            if row["has_open"]:
                return {"allowed": False, "reason": "duplicate"}
            if row["theme_blocked"]:
                return {"allowed": False, "reason": "theme_blocked"}
            if row["has_sl"]:
                return {"allowed": False, "reason": "sl_blacklist"}
            if row["has_recent"]:
                return {"allowed": False, "reason": "recent_close"}
            return {"allowed": True, "reason": None}

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

    # ── Theme Calibration ──

    SHRINKAGE_K = 20  # Bayesian shrinkage strength toward global mean
    BLOCK_WR_THRESHOLD = 0.40  # block theme if adj WR < 40%
    BLOCK_MIN_TRADES = 10  # need 10+ trades before blocking

    async def recalibrate_theme(self, theme: str):
        """Recalculate theme stats from closed positions using Bayesian shrinkage."""
        async with self.pool.acquire() as conn:
            # Theme stats
            row = await conn.fetchrow("""
                SELECT COUNT(*) as n,
                       SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN result = 'LOSS' THEN 1 ELSE 0 END) as losses,
                       COALESCE(SUM(pnl), 0) as total_pnl
                FROM micro_positions WHERE theme = $1 AND status = 'closed'
            """, theme)
            n = row["n"] or 0
            wins = row["wins"] or 0
            losses = row["losses"] or 0
            total_pnl = row["total_pnl"] or 0
            raw_wr = wins / n if n > 0 else 0.5

            # Global stats for shrinkage
            g = await conn.fetchrow("""
                SELECT COUNT(*) as n,
                       SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins
                FROM micro_positions WHERE status = 'closed'
            """)
            global_wr = (g["wins"] or 0) / (g["n"] or 1)

            # Bayesian shrinkage: pull toward global mean
            adj_wr = (n * raw_wr + self.SHRINKAGE_K * global_wr) / (n + self.SHRINKAGE_K)
            blocked = n >= self.BLOCK_MIN_TRADES and adj_wr < self.BLOCK_WR_THRESHOLD

            await conn.execute("""
                INSERT INTO micro_theme_stats (theme, trades, wins, losses, total_pnl, raw_wr, adj_wr, blocked, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                ON CONFLICT (theme) DO UPDATE SET
                    trades = $2, wins = $3, losses = $4, total_pnl = $5,
                    raw_wr = $6, adj_wr = $7, blocked = $8, updated_at = NOW()
            """, theme, n, wins, losses, total_pnl, round(raw_wr, 4), round(adj_wr, 4), blocked)

            if blocked:
                log.info(f"[THEME] BLOCKED '{theme}': {wins}/{n} WR={adj_wr:.1%} (raw={raw_wr:.1%})")
            return blocked

    async def is_theme_blocked(self, theme: str) -> bool:
        """Check if theme is blocked due to poor performance."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT blocked FROM micro_theme_stats WHERE theme = $1", theme
            )
            return row["blocked"] if row else False

    async def get_theme_stats(self) -> list:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM micro_theme_stats ORDER BY adj_wr DESC")
            return [dict(r) for r in rows]

    async def has_sl_loss(self, market_id: str, side: str = None) -> bool:
        """Check if this market ever had a SL/rapid_drop loss — blacklist from re-entry."""
        async with self.pool.acquire() as conn:
            if side:
                row = await conn.fetchrow(
                    "SELECT 1 FROM micro_positions WHERE market_id = $1 AND side = $2 "
                    "AND status = 'closed' AND close_reason IN ('stop_loss', 'rapid_drop')",
                    market_id, side,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT 1 FROM micro_positions WHERE market_id = $1 "
                    "AND status = 'closed' AND close_reason IN ('stop_loss', 'rapid_drop')",
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
