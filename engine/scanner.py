import logging
import json as _json
from datetime import datetime, timezone

import httpx

log = logging.getLogger("micro.scanner")

GAMMA_API = "https://gamma-api.polymarket.com"

THEME_KEYWORDS = {
    "crypto":      ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol"],
    "politics":    ["trump", "biden", "election", "president", "congress", "senate", "governor"],
    "fed":         ["fed ", "interest rate", "fomc", "federal reserve", "rate cut", "rate hike"],
    "economy":     ["gdp", "inflation", "recession", "unemployment", "cpi", "jobs"],
    "tech":        ["apple", "google", "meta", "microsoft", "nvidia", "openai", "ai "],
    "sports":      ["nba", "nfl", "mlb", "ufc", "premier league", "champions league", "world cup"],
    "geopolitics": ["russia", "ukraine", "china", "taiwan", "war", "nato", "sanctions"],
    "spacex":      ["spacex", "starship", "launch", "rocket", "nasa"],
    "markets":     ["s&p", "spy", "nasdaq", "dow jones", "gold", "oil", "commodities"],
    "celeb":       ["elon", "musk", "kanye", "celebrity"],
}


def classify_theme(question: str) -> str:
    q = question.lower()
    for theme, keywords in THEME_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return theme
    return "other"


def _parse_token_ids(m: dict) -> tuple:
    token_ids = m.get("clobTokenIds") or []
    if isinstance(token_ids, str):
        token_ids = _json.loads(token_ids)
    yes_token = token_ids[0] if len(token_ids) > 0 else None
    no_token = token_ids[1] if len(token_ids) > 1 else None
    return yes_token, no_token


def _days_until(end_str: str) -> float:
    if not end_str:
        return -1
    try:
        end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        return (end - datetime.now(timezone.utc)).total_seconds() / 86400
    except Exception:
        return -1


class MicroScanner:
    """Scans Polymarket for high-probability markets near resolution.
    Checks BOTH sides: if YES ≥ 90¢ → buy YES, if NO ≥ 90¢ → buy NO."""

    def __init__(self, config: dict):
        self.config = config
        self.client = httpx.AsyncClient(timeout=15.0)

    async def fetch_candidates(self) -> tuple[list, list]:
        """Returns (direct_entries, watchlist).
        Checks both YES and NO sides of each market.
        """
        entry_price = self.config["ENTRY_MIN_PRICE"]
        wl_min = self.config["WATCHLIST_MIN_PRICE"]
        max_days = self.config["MAX_DAYS_LEFT"]
        max_stake = self.config["MAX_STAKE"]
        liq_mult = self.config.get("MIN_LIQUIDITY_MULT", 500)
        min_liquidity = max_stake * liq_mult  # e.g. $5 * 500 = $2500
        max_spread = self.config["MAX_SPREAD"]
        min_roi = self.config.get("MIN_ROI", 0.03)

        direct = []
        watchlist = []
        seen = set()

        try:
            # Fetch all pages in parallel
            import asyncio
            tasks = []
            for offset in range(0, 2000, 100):
                tasks.append(self.client.get(f"{GAMMA_API}/markets", params={
                    "active": "true", "closed": "false",
                    "order": "volume24hr", "ascending": "false",
                    "limit": 100, "offset": offset,
                }))
            responses = await asyncio.gather(*tasks, return_exceptions=True)

            all_markets = []
            for r in responses:
                if isinstance(r, Exception):
                    continue
                batch = r.json() or []
                if not batch:
                    break
                all_markets.extend(batch)
                if len(batch) < 100:
                    break

            for m in all_markets:
                vol = float(m.get("volume") or 0)
                liq = float(m.get("liquidity") or 0)
                if liq < min_liquidity:
                    continue

                raw_prices = m.get("outcomePrices")
                if not raw_prices:
                    continue
                if isinstance(raw_prices, str):
                    raw_prices = _json.loads(raw_prices)
                yes_price = float(raw_prices[0])
                no_price = round(1.0 - yes_price, 4)

                end_str = m.get("endDate") or m.get("endDateIso")
                days_left = _days_until(end_str)
                if days_left < 0 or days_left > max_days:
                    continue

                spread = float(m.get("spread") or 0)
                if spread > max_spread:
                    continue

                yes_token, no_token = _parse_token_ids(m)
                question = m.get("question", "")
                market_id = str(m["id"])

                candidates_for_market = []

                if yes_price >= wl_min:
                    roi = (1.0 - yes_price) / yes_price
                    if roi >= min_roi:
                        candidates_for_market.append({
                            "side": "YES",
                            "price": yes_price,
                            "best_ask": float(m.get("bestAsk") or yes_price),
                            "roi": roi,
                            "ws_token": yes_token,
                            "ws_side": "yes",
                        })

                if no_price >= wl_min:
                    roi = (1.0 - no_price) / no_price
                    if roi >= min_roi:
                        best_bid_yes = float(m.get("bestBid") or yes_price)
                        no_best_ask = round(1.0 - best_bid_yes, 4)
                        candidates_for_market.append({
                            "side": "NO",
                            "price": no_price,
                            "best_ask": no_best_ask,
                            "roi": roi,
                            "ws_token": no_token,
                            "ws_side": "no",
                        })

                for info in candidates_for_market:
                    key = f"{market_id}_{info['side']}"
                    if key in seen:
                        continue
                    seen.add(key)

                    c = {
                        "market_id": market_id,
                        "question":  question,
                        "theme":     classify_theme(question),
                        "side":      info["side"],
                        "price":     round(info["price"], 4),
                        "best_ask":  round(info["best_ask"], 4),
                        "volume":    vol,
                        "liquidity": liq,
                        "spread":    spread,
                        "days_left": round(days_left, 2),
                        "end_date":  end_str,
                        "roi":       round(info["roi"], 4),
                        "yes_token": yes_token,
                        "no_token":  no_token,
                        "ws_token":  info["ws_token"],
                        "ws_side":   info["ws_side"],
                    }

                    if info["price"] >= entry_price:
                        direct.append(c)
                    else:
                        watchlist.append(c)

            direct.sort(key=lambda c: c["days_left"])
            watchlist.sort(key=lambda c: -c["price"])

            log.info(
                f"[Scanner] {len(direct)} direct (≥{entry_price:.0%}) + "
                f"{len(watchlist)} watchlist ({wl_min:.0%}-{entry_price:.0%}), "
                f"≤{max_days}d left"
            )
            return direct, watchlist

        except Exception as e:
            log.error(f"[Scanner] Error: {e}")
            return [], []

    async def close(self):
        await self.client.aclose()
