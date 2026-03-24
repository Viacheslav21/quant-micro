import logging
import httpx

log = logging.getLogger("micro.scanner")

GAMMA_API = "https://gamma-api.polymarket.com"

THEME_KEYWORDS = {
    "crypto":    ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol"],
    "politics":  ["trump", "biden", "election", "president", "congress", "senate", "governor"],
    "fed":       ["fed ", "interest rate", "fomc", "federal reserve", "rate cut", "rate hike"],
    "economy":   ["gdp", "inflation", "recession", "unemployment", "cpi", "jobs"],
    "tech":      ["apple", "google", "meta", "microsoft", "nvidia", "openai", "ai "],
    "sports":    ["nba", "nfl", "mlb", "ufc", "premier league", "champions league", "world cup"],
    "geopolitics": ["russia", "ukraine", "china", "taiwan", "war", "nato", "sanctions"],
    "spacex":    ["spacex", "starship", "launch", "rocket", "nasa"],
    "markets":   ["s&p", "spy", "nasdaq", "dow jones", "gold", "oil", "commodities"],
    "celeb":     ["elon", "musk", "kanye", "celebrity"],
}


def classify_theme(question: str) -> str:
    q = question.lower()
    for theme, keywords in THEME_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return theme
    return "other"


class MicroScanner:
    """Scans Polymarket for high-probability markets (85-95 cent range)."""

    def __init__(self, config: dict):
        self.config = config
        self.client = httpx.AsyncClient(timeout=15.0)

    async def fetch_watchlist_candidates(self) -> list:
        """Fetch markets in the watchlist price zone."""
        min_price = self.config["WATCHLIST_MIN_PRICE"]
        max_price = self.config["WATCHLIST_MAX_PRICE"]
        min_volume = self.config["MIN_VOLUME"]
        min_liquidity = self.config["MIN_LIQUIDITY"]
        max_spread = self.config["MAX_SPREAD"]

        candidates = []
        offset = 0

        try:
            while len(candidates) < 300:
                r = await self.client.get(f"{GAMMA_API}/markets", params={
                    "active": "true",
                    "closed": "false",
                    "order": "volume24hr",
                    "ascending": "false",
                    "limit": 100,
                    "offset": offset,
                })
                batch = r.json() or []
                if not batch:
                    break

                for m in batch:
                    parsed = self._parse_market(m, min_price, max_price,
                                                 min_volume, min_liquidity, max_spread)
                    if parsed:
                        candidates.append(parsed)

                offset += 100
                if offset >= 600:
                    break

            log.info(f"[Scanner] Found {len(candidates)} watchlist candidates "
                     f"in [{min_price:.0%}-{max_price:.0%}] range")
            return candidates

        except Exception as e:
            log.error(f"[Scanner] Error: {e}")
            return []

    def _parse_market(self, m: dict,
                      min_price: float, max_price: float,
                      min_volume: float, min_liquidity: float,
                      max_spread: float) -> dict | None:
        """Parse and filter a single market. Returns dict or None."""
        try:
            volume = float(m.get("volume") or 0)
            liquidity = float(m.get("liquidity") or 0)

            if volume < min_volume or liquidity < min_liquidity:
                return None

            # Get best prices
            yes_price = float(m.get("bestAsk") or m.get("outcomePrices", "[0.5]").strip("[]").split(",")[0])
            best_bid = float(m.get("bestBid") or 0)
            spread = yes_price - best_bid if best_bid > 0 else 0.05

            if spread > max_spread:
                return None

            # Check YES side in watchlist zone
            yes_in_zone = min_price <= yes_price <= max_price

            # Check NO side (1 - yes_price) in watchlist zone
            no_price = 1.0 - yes_price
            no_in_zone = min_price <= (1.0 - no_price) <= max_price  # same as yes_in_zone for the "high" side

            if not yes_in_zone:
                return None

            # Extract token IDs
            tokens = m.get("clobTokenIds", "")
            if isinstance(tokens, str):
                tokens = tokens.strip("[]").replace('"', "").split(",")
            yes_token = tokens[0].strip() if len(tokens) > 0 else None
            no_token = tokens[1].strip() if len(tokens) > 1 else None

            question = m.get("question", "")
            theme = classify_theme(question)

            return {
                "market_id": m.get("id", ""),
                "question": question,
                "theme": theme,
                "yes_price": round(yes_price, 4),
                "no_price": round(no_price, 4),
                "volume": volume,
                "liquidity": liquidity,
                "spread": round(spread, 4),
                "best_ask": round(yes_price, 4),
                "yes_token": yes_token,
                "no_token": no_token,
                "neg_risk": bool(m.get("negRisk")),
                "end_date": m.get("endDate"),
            }

        except (ValueError, TypeError, IndexError):
            return None

    async def close(self):
        await self.client.aclose()
