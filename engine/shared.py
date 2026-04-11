"""Shared utilities for quant-micro engine modules."""

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger("micro")


def calc_days_left(end_date_str, fallback: float = 999.0) -> float:
    """Calculate days until market expires. Returns fallback if unknown."""
    if not end_date_str:
        return fallback
    try:
        end = datetime.fromisoformat(str(end_date_str).replace("Z", "+00:00"))
        return max(0, (end - datetime.now(timezone.utc)).total_seconds() / 86400)
    except Exception:
        return fallback


def parse_outcome_prices(data) -> tuple:
    """Parse outcomePrices from Gamma API response or raw value.
    Accepts dict (full API response), str (JSON string), or list.
    Returns (yes_price, no_price)."""
    if isinstance(data, dict):
        raw = data.get("outcomePrices")
        if not raw:
            yes = float(data.get("yes_price", 0) or 0)
            no = float(data.get("no_price", 0) or 0)
            return yes, no
    else:
        raw = data

    if isinstance(raw, str):
        raw = json.loads(raw)

    if not raw:
        return 0.0, 0.0

    yes = float(raw[0])
    no = float(raw[1]) if len(raw) > 1 else round(1.0 - yes, 4)
    return yes, no


def calc_exit_fee(stake: float, entry_price: float, config: dict) -> float:
    """Calculate sim exit costs: slippage + fee.
    Uses SLIPPAGE and FEE_PCT from config."""
    slippage_cost = config.get("SLIPPAGE", 0) * stake / entry_price if entry_price > 0 else 0
    fee_cost = stake * config.get("FEE_PCT", 0)
    return slippage_cost + fee_cost


def hours_since(pos: dict, date_field: str = "opened_at") -> float:
    """Calculate hours since a date field."""
    val = pos.get(date_field)
    if not val:
        return 0
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return abs((datetime.now(timezone.utc) - dt).total_seconds() / 3600)
    except Exception:
        return 0
