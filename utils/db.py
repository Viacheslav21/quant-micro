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

    async def init(self, micro_config: dict = None):
        self.pool = await asyncpg.create_pool(
            self.url, min_size=1, max_size=5, command_timeout=30
        )
        await self._create_schema()
        await self._ensure_config_live_tables()
        await self._seed_config_live_micro(micro_config or {})
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
                    status       TEXT DEFAULT 'open',
                    result       TEXT,
                    close_reason TEXT,
                    config_tag   TEXT DEFAULT 'micro-v3',
                    end_date     TEXT,
                    opened_at    TIMESTAMPTZ DEFAULT NOW(),
                    closed_at    TIMESTAMPTZ
                );

                CREATE TABLE IF NOT EXISTS micro_theme_stats (
                    theme        TEXT PRIMARY KEY,
                    blocked      BOOLEAN DEFAULT FALSE,
                    updated_at   TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_micro_pos_status
                    ON micro_positions(status);
                CREATE INDEX IF NOT EXISTS idx_micro_pos_market
                    ON micro_positions(market_id);
                CREATE INDEX IF NOT EXISTS idx_micro_pos_market_side_status
                    ON micro_positions(market_id, side, status);
                CREATE INDEX IF NOT EXISTS idx_micro_watchlist_price
                    ON micro_watchlist(yes_price DESC);

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
            # Add neg_risk_id for correlated position grouping
            await conn.execute("""
                ALTER TABLE micro_positions ADD COLUMN IF NOT EXISTS neg_risk_id TEXT DEFAULT NULL;
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_micro_pos_neg_risk
                    ON micro_positions(neg_risk_id) WHERE neg_risk_id IS NOT NULL;
            """)
            # Add neg_risk_id to watchlist for correlated position grouping
            await conn.execute("""
                ALTER TABLE micro_watchlist ADD COLUMN IF NOT EXISTS neg_risk_id TEXT DEFAULT NULL;
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
            # Migration: drop unused columns from micro_theme_stats
            for col in ["trades", "wins", "losses", "total_pnl", "raw_wr", "adj_wr"]:
                await conn.execute(f"ALTER TABLE micro_theme_stats DROP COLUMN IF EXISTS {col}")
            # Migration: drop dead risk columns from micro_positions.
            # micro never uses % SL or % TP — it relies on MAX_LOSS + RAPID_DROP
            # + resolution detection. These columns were legacy from earlier design.
            for col in ["sl_pct", "tp_pct"]:
                await conn.execute(f"ALTER TABLE micro_positions DROP COLUMN IF EXISTS {col}")
            # No default theme blocking — managed entirely via dashboard
            # Price path history for debugging position exits
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS micro_price_history (
                    id         BIGSERIAL PRIMARY KEY,
                    market_id  TEXT NOT NULL,
                    side       TEXT NOT NULL,
                    price      REAL NOT NULL,
                    source     TEXT NOT NULL DEFAULT 'ws',
                    ts         TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_price_history_lookup
                    ON micro_price_history (market_id, side, ts DESC);
            """)
        log.info("[DB] Schema ready")

    async def _ensure_config_live_tables(self):
        """Create config_live + config_live_history if missing.
        Previously created by quant-engine; micro now self-bootstraps so it can run
        standalone (engine deleted from deployment)."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS config_live (
                    id BIGSERIAL PRIMARY KEY,
                    service TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    value_type TEXT NOT NULL DEFAULT 'str',
                    description TEXT DEFAULT '',
                    min_val REAL,
                    max_val REAL,
                    section TEXT DEFAULT 'general',
                    version INTEGER DEFAULT 1,
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(service, key)
                );
                CREATE TABLE IF NOT EXISTS config_live_history (
                    id BIGSERIAL PRIMARY KEY,
                    service TEXT NOT NULL,
                    key TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    changed_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_config_live_service ON config_live(service);
            """)

    # config_live schema for the 'micro' service (single source of truth).
    # (key, fallback, value_type, description, min_val, max_val, section)
    _MICRO_CONFIG_SCHEMA = [
        # signals
        ("ENTRY_MIN_PRICE",    0.94,  "float", "Base direct-buy threshold — above this → enter immediately (lowered for near-expiry)", 0.80, 0.99, "signals"),
        ("WATCHLIST_MIN_PRICE",0.90,  "float", "Floor to subscribe via WS and wait for price to reach entry (4¢ below ENTRY_MIN_PRICE)", 0.80, 0.99, "signals"),
        ("MIN_ROI",            0.02,  "float", "Min ROI at $1 payout = (1−price)/price (0.02 = 2%, floor after slippage/fees)", 0.005, 0.10, "signals"),
        ("MIN_QUALITY_SCORE",  40,    "float", "Base quality gate 0–100 — stepped up by days_left (1–3d→55, 3–5d→70, >5d→80)", 0, 100, "signals"),
        ("ENTRY_PRICE_1D",     0.90,  "float", "Entry threshold for markets expiring ≤1 day — cheaper = higher ROI at same cert.", 0.80, 0.99, "signals"),
        ("ENTRY_PRICE_2D",     0.92,  "float", "Entry threshold for markets expiring ≤2 days", 0.80, 0.99, "signals"),
        ("ENTRY_PRICE_3D",     0.93,  "float", "Entry threshold for markets expiring ≤3 days (>3d uses ENTRY_MIN_PRICE)", 0.80, 0.99, "signals"),
        # risk
        ("RAPID_DROP_PCT",     0.07,  "float", "Exit if bid drops this many ¢ from entry, absolute (0.07 = 7¢). REST+vol verified", 0.02, 0.15, "risk"),
        ("MAX_LOSS_PER_POS",   3.0,   "float", "Hard $ cap per position — always enforced, REST-verified with retry", 0.5, 20.0, "risk"),
        ("MAX_LOSS_BYPASS_BLOCKS", 2, "int",   "After N consecutive REST blocks of MAX_LOSS, trust WS bid directly. Lower = faster cap enforcement", 1, 10, "risk"),
        # sizing
        ("MAX_STAKE",          20.0,  "float", "Max $ per position (stake = 5% bankroll, capped here)", 1.0, 100.0, "sizing"),
        ("MIN_STAKE",          5.0,   "float", "Min $ per position — skip entry if bankroll too low to meet this", 1.0, 50.0, "sizing"),
        ("MAX_STAKE_Q80_6H",   75.0,  "float", "Cap $ for Q≥80 + ≤6h trades — 100% WR bucket, gets the highest Kelly stake", 5.0, 300.0, "sizing"),
        ("MAX_STAKE_Q80_1D",   50.0,  "float", "Cap $ for Q≥80 + ≤1d trades — middle tier between Q80_6H ($75) and standard MAX_STAKE_1D ($35)", 5.0, 300.0, "sizing"),
        ("PCT_STAKE_Q80",      0.075, "float", "Kelly fraction for Q≥80 tiers (vs 0.05 default) — bumped because WR is near 100%", 0.02, 0.20, "sizing"),
        # capacity
        ("MAX_OPEN",           50,    "int",   "Total open micro positions allowed", 1, 200, "capacity"),
        ("MAX_PER_THEME",      5,     "int",   "Positions per theme — bypassed for negRisk (uses MAX_PER_NEG_RISK)", 1, 50, "capacity"),
        ("MAX_PER_NEG_RISK",   3,     "int",   "Max positions per negRisk event group (correlated, ρ=1.0)", 1, 10, "capacity"),
        # filters
        ("MAX_DAYS_LEFT",      7,     "float", "Skip markets expiring > N days out (resolution harvesting horizon)", 1, 30, "filters"),
        ("MIN_VOLUME",         50000, "float", "Skip markets with total volume below $N (illiquid = noisy)", 1000, 1e7, "filters"),
        # timing
        ("SCAN_INTERVAL",      120,   "int",   "Scanner loop in seconds. Positions monitored live via WS between scans", 30, 600, "timing"),
        # sim
        ("SLIPPAGE",           0.005, "float", "Sim entry slippage per side (0.005 = 0.5¢ added to fill price)", 0.0, 0.02, "sim"),
        ("FEE_PCT",            0.02,  "float", "Sim round-trip fee on early exits as fraction of stake (0.02 = 2%)", 0.0, 0.10, "sim"),
        # general
        ("BANKROLL",           1000,  "float", "Starting bankroll $ — actual bankroll computed live from position PnL", 100, 10000, "general"),
        ("CONFIG_TAG",         "micro-v4", "str", "Label saved on every position for A/B comparison", None, None, "general"),
    ]

    async def _seed_config_live_micro(self, micro_config: dict):
        """Populate config_live with current env values for the 'micro' service.
        ON CONFLICT DO NOTHING — existing DB values (manually edited via dashboard) untouched.
        New rows get inserted with env values; later code reloads pick them up via NOTIFY."""

        def _v(cfg, key, fallback):
            val = cfg.get(key)
            if val is None:
                return str(fallback)
            if isinstance(val, bool):
                return "true" if val else "false"
            return str(val)

        async with self.pool.acquire() as conn:
            inserted = 0
            for key, fallback, vtype, desc, mn, mx, sec in self._MICRO_CONFIG_SCHEMA:
                val = _v(micro_config, key, fallback)
                result = await conn.execute("""
                    INSERT INTO config_live (service, key, value, value_type, description, min_val, max_val, section)
                    VALUES ('micro', $1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (service, key) DO NOTHING
                """, key, val, vtype, desc, mn, mx, sec)
                if result.endswith(" 1"):  # "INSERT 0 1" if a row was actually inserted
                    inserted += 1
            log.info(f"[DB] config_live seeded for micro: {len(self._MICRO_CONFIG_SCHEMA)} schema rows ({inserted} new)")

    async def get_config_overrides(self, service: str) -> dict:
        """Fetch live config overrides from config_live table.
        Schema + seed for the 'micro' service is owned by this module
        (`_ensure_config_live_tables` + `_seed_config_live_micro`)."""
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
                     days_left, end_date, roi, quality, yes_token, no_token, neg_risk_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                ON CONFLICT (market_id, side) DO UPDATE SET
                    yes_price  = EXCLUDED.yes_price,
                    volume     = EXCLUDED.volume,
                    liquidity  = EXCLUDED.liquidity,
                    spread     = EXCLUDED.spread,
                    best_ask   = EXCLUDED.best_ask,
                    days_left  = EXCLUDED.days_left,
                    roi        = EXCLUDED.roi,
                    quality    = EXCLUDED.quality,
                    neg_risk_id = EXCLUDED.neg_risk_id,
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
                market.get("neg_risk_id"),
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
                     days_left, end_date, roi, quality, yes_token, no_token, neg_risk_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                ON CONFLICT (market_id, side) DO UPDATE SET
                    yes_price  = EXCLUDED.yes_price,
                    volume     = EXCLUDED.volume,
                    liquidity  = EXCLUDED.liquidity,
                    spread     = EXCLUDED.spread,
                    best_ask   = EXCLUDED.best_ask,
                    days_left  = EXCLUDED.days_left,
                    roi        = EXCLUDED.roi,
                    quality    = EXCLUDED.quality,
                    neg_risk_id = EXCLUDED.neg_risk_id,
                    updated_at = NOW()
            """, [(m["market_id"], m.get("side", "YES"),
                   m["question"], m.get("theme", "other"),
                   m.get("price", m.get("yes_price", 0)),
                   m.get("volume", 0), m.get("liquidity", 0),
                   m.get("spread", 0), m.get("best_ask", 0),
                   m.get("days_left", 0), m.get("end_date"),
                   m.get("roi", 0), m.get("quality", 0),
                   m.get("yes_token"), m.get("no_token"),
                   m.get("neg_risk_id")) for m in markets])

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

    async def get_watchlist_market(self, market_id: str, side: str = None) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            if side:
                row = await conn.fetchrow(
                    "SELECT * FROM micro_watchlist WHERE market_id = $1 AND side = $2", market_id, side
                )
            else:
                row = await conn.fetchrow(
                    "SELECT * FROM micro_watchlist WHERE market_id = $1", market_id
                )
            return dict(row) if row else None

    # ── Positions ──

    async def save_position_and_deduct(self, pos: dict, stake: float):
        """Save position. Bankroll computed from positions, no separate stats update needed."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO micro_positions
                    (id, market_id, question, theme, side, entry_price,
                     current_price, stake_amt, config_tag, end_date, neg_risk_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """,
                pos["id"], pos["market_id"], pos["question"],
                pos.get("theme", "other"), pos["side"], pos["entry_price"],
                pos["entry_price"], pos["stake_amt"],
                pos.get("config_tag", "micro-v3"),
                pos.get("end_date"),
                pos.get("neg_risk_id"),
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

    async def close_position(self, pos_id: str, pnl: float, result: str, reason: str,
                             exit_price: float = None) -> bool:
        """Atomic close with race protection. Updates current_price to actual exit price."""
        async with self.pool.acquire() as conn:
            if exit_price is not None:
                row = await conn.fetchrow("""
                    UPDATE micro_positions
                    SET status = 'closed', pnl = $2, result = $3, close_reason = $4,
                        current_price = $5, closed_at = NOW()
                    WHERE id = $1 AND status = 'open'
                    RETURNING id
                """, pos_id, pnl, result, reason, exit_price)
            else:
                row = await conn.fetchrow("""
                    UPDATE micro_positions
                    SET status = 'closed', pnl = $2, result = $3, close_reason = $4,
                        closed_at = NOW()
                    WHERE id = $1 AND status = 'open'
                    RETURNING id
                """, pos_id, pnl, result, reason)
            return row is not None

    async def update_position_price(self, pos_id: str, price: float, unrealized_pnl: float):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE micro_positions
                SET current_price = $2, unrealized_pnl = $3
                WHERE id = $1 AND status = 'open'
            """, pos_id, price, unrealized_pnl)

    # ── Stats (computed from positions) ──

    async def get_stats(self, starting_bankroll: float = 1000.0) -> dict:
        """Compute stats live from micro_positions. No separate stats table needed."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT
                    COALESCE(SUM(pnl), 0) as total_pnl,
                    SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) as losses,
                    COUNT(*) as total_trades
                FROM micro_positions WHERE status='closed'
            """)
            open_staked = await conn.fetchval(
                "SELECT COALESCE(SUM(stake_amt), 0) FROM micro_positions WHERE status='open'"
            )
        total_pnl = float(row["total_pnl"])
        return {
            "bankroll": round(starting_bankroll + total_pnl - float(open_staked), 2),
            "total_pnl": round(total_pnl, 2),
            "wins": int(row["wins"]),
            "losses": int(row["losses"]),
            "total_trades": int(row["total_trades"]),
            "peak_equity": 0,  # tracked in memory by main.py
        }

    async def reset_stats(self):
        """Full reset: clear all positions and watchlist."""
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM micro_positions")
            await conn.execute("DELETE FROM micro_watchlist")
        log.info("[DB] Full reset: all positions/watchlist cleared")

    # ── Price History ──

    async def record_price_tick(self, market_id: str, side: str, price: float, source: str = "ws"):
        """Append a price tick to micro_price_history. Called from monitor on significant moves."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO micro_price_history (market_id, side, price, source) VALUES ($1, $2, $3, $4)",
                market_id, side, price, source,
            )

    async def get_price_history(self, market_id: str, side: str):
        """Fetch price path for a position (for debugging / dashboard)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT price, source, ts FROM micro_price_history "
                "WHERE market_id=$1 AND side=$2 ORDER BY ts",
                market_id, side,
            )
        return [{"price": r["price"], "source": r["source"], "ts": r["ts"].isoformat()} for r in rows]

    async def cleanup_price_history(self, days: int = 3):
        """Delete price history older than N days to avoid unbounded growth."""
        async with self.pool.acquire() as conn:
            deleted = await conn.execute(
                "DELETE FROM micro_price_history WHERE ts < NOW() - $1::interval",
                f"{days} days",
            )
            log.debug(f"[DB] Price history cleanup: {deleted}")

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

    async def check_entry_allowed(self, market_id: str, side: str, theme: str,
                                   neg_risk_id: str = None, cooldown_hours: float = 6,
                                   max_per_neg_risk: int = 3) -> dict:
        """Combined entry check — replaces 4 separate queries with 1.
        Returns {allowed: bool, reason: str|None}."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT
                    EXISTS(SELECT 1 FROM micro_positions WHERE market_id=$1 AND side=$2 AND status='open') as has_open,
                    EXISTS(SELECT 1 FROM micro_positions WHERE market_id=$1 AND side=$2
                           AND status='closed' AND close_reason IN ('stop_loss','rapid_drop','max_loss')) as has_sl,
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
            # negRisk group limit: max 1 open position per negRisk event
            if neg_risk_id:
                has_group = await conn.fetchval("""
                    SELECT COUNT(*) FROM micro_positions
                    WHERE neg_risk_id = $1 AND status = 'open'
                """, neg_risk_id)
                if has_group >= max_per_neg_risk:
                    return {"allowed": False, "reason": "neg_risk_group"}
            return {"allowed": True, "reason": None}

    # ── Theme Calibration ──

    SHRINKAGE_K = 20  # Bayesian shrinkage strength toward global mean
    BLOCK_WR_THRESHOLD = 0.40  # block theme if adj WR < 40%
    BLOCK_MIN_TRADES = 5  # need 5+ trades before blocking — fast reaction on toxic themes

    async def recalibrate_theme(self, theme: str):
        """Recalculate theme block status from closed positions using Bayesian shrinkage."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT COUNT(*) as n,
                       SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins
                FROM micro_positions WHERE theme = $1 AND status = 'closed'
            """, theme)
            n = row["n"] or 0
            wins = row["wins"] or 0
            raw_wr = wins / n if n > 0 else 0.5

            g = await conn.fetchrow("""
                SELECT COUNT(*) as n,
                       SUM(CASE WHEN result = 'WIN' THEN 1 ELSE 0 END) as wins
                FROM micro_positions WHERE status = 'closed'
            """)
            global_wr = (g["wins"] or 0) / (g["n"] or 1)

            adj_wr = (n * raw_wr + self.SHRINKAGE_K * global_wr) / (n + self.SHRINKAGE_K)

            # Check if manually blocked (don't auto-unblock manual blocks)
            current = await conn.fetchrow(
                "SELECT blocked FROM micro_theme_stats WHERE theme = $1", theme
            )
            currently_blocked = current["blocked"] if current else False

            # Auto-block if bad WR, but never auto-unblock a manually blocked theme
            should_block = n >= self.BLOCK_MIN_TRADES and adj_wr < self.BLOCK_WR_THRESHOLD
            blocked = should_block or currently_blocked

            await conn.execute("""
                INSERT INTO micro_theme_stats (theme, blocked, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (theme) DO UPDATE SET blocked = $2, updated_at = NOW()
            """, theme, blocked)

            if blocked:
                log.info(f"[THEME] BLOCKED '{theme}': {wins}/{n} WR={adj_wr:.1%} (raw={raw_wr:.1%})")
            return blocked

    async def get_theme_stats(self) -> list:
        """Theme stats computed from positions + blocked flag from micro_theme_stats."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT p.theme,
                    COUNT(*) as trades,
                    SUM(CASE WHEN p.result='WIN' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN p.result='LOSS' THEN 1 ELSE 0 END) as losses,
                    ROUND(SUM(p.pnl)::numeric, 2) as total_pnl,
                    COALESCE(t.blocked, false) as blocked
                FROM micro_positions p
                LEFT JOIN micro_theme_stats t ON p.theme = t.theme
                WHERE p.status = 'closed' AND p.theme IS NOT NULL
                GROUP BY p.theme, t.blocked
                ORDER BY COUNT(*) DESC
            """)
            return [dict(r) for r in rows]

    async def get_theme_adj_wr(self) -> dict:
        """Bayesian adjusted WR per theme (same shrinkage as recalibrate_theme).
        Returns {theme: adj_wr} only for themes with BLOCK_MIN_TRADES+ closed trades."""
        async with self.pool.acquire() as conn:
            g = await conn.fetchrow("""
                SELECT COUNT(*) as n, SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins
                FROM micro_positions WHERE status='closed'
            """)
            global_wr = (g["wins"] or 0) / max(g["n"] or 1, 1)
            rows = await conn.fetch("""
                SELECT theme, COUNT(*) as n,
                       SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins
                FROM micro_positions WHERE status='closed' AND theme IS NOT NULL
                GROUP BY theme
            """)
            result = {}
            for r in rows:
                n = r["n"] or 0
                if n < self.BLOCK_MIN_TRADES:
                    continue
                raw_wr = (r["wins"] or 0) / n
                adj_wr = (n * raw_wr + self.SHRINKAGE_K * global_wr) / (n + self.SHRINKAGE_K)
                result[r["theme"]] = round(adj_wr, 4)
            return result

    async def get_daily_report(self, starting_bankroll: float = 1000.0) -> dict:
        """Gather all data for daily telegram report in a single DB roundtrip."""
        async with self.pool.acquire() as conn:
            # Today's closed
            today = await conn.fetchrow("""
                SELECT COUNT(*) as trades,
                    SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) as losses,
                    COALESCE(SUM(pnl), 0) as pnl,
                    MAX(pnl) as best,
                    MIN(pnl) as worst
                FROM micro_positions WHERE status='closed' AND closed_at >= CURRENT_DATE
            """)
            # Today's by close reason
            reasons = await conn.fetch("""
                SELECT close_reason, COUNT(*) as n, ROUND(SUM(pnl)::numeric, 2) as pnl
                FROM micro_positions WHERE status='closed' AND closed_at >= CURRENT_DATE
                GROUP BY close_reason ORDER BY COUNT(*) DESC
            """)
            # Today's top themes
            themes = await conn.fetch("""
                SELECT theme, COUNT(*) as n,
                    SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins,
                    ROUND(SUM(pnl)::numeric, 2) as pnl
                FROM micro_positions WHERE status='closed' AND closed_at >= CURRENT_DATE
                GROUP BY theme ORDER BY SUM(pnl) DESC LIMIT 5
            """)
            # All-time
            alltime = await conn.fetchrow("""
                SELECT COUNT(*) as trades,
                    SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins,
                    COALESCE(SUM(pnl), 0) as pnl
                FROM micro_positions WHERE status='closed'
            """)
            # Open positions
            open_row = await conn.fetchrow("""
                SELECT COUNT(*) as n,
                    COALESCE(SUM(stake_amt), 0) as staked,
                    COALESCE(SUM(unrealized_pnl), 0) as upnl
                FROM micro_positions WHERE status='open'
            """)
        total_pnl = float(alltime["pnl"])
        open_staked = float(open_row["staked"])
        return {
            "today": dict(today),
            "reasons": [dict(r) for r in reasons],
            "themes": [dict(r) for r in themes],
            "alltime": dict(alltime),
            "open_n": int(open_row["n"]),
            "open_staked": round(open_staked, 2),
            "open_upnl": round(float(open_row["upnl"]), 2),
            "bankroll": round(starting_bankroll + total_pnl - open_staked, 2),
            "equity": round(starting_bankroll + total_pnl - open_staked + float(open_row["upnl"]), 2),
        }

    async def close(self):
        if self.pool:
            await self.pool.close()
            log.info("[DB] Pool closed")
