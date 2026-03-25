import logging
import json as _json
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


class MicroScanner:
    """Scans Polymarket for high-probability markets (85-95 cent range)."""

    def __init__(self, config: dict):
        self.config = config
        self.client = httpx.AsyncClient(timeout=15.0)

    async def fetch_watchlist_candidates(self) -> list:
        min_price = self.config["WATCHLIST_MIN_PRICE"]
        max_price = self.config["WATCHLIST_MAX_PRICE"]
        min_volume = self.config["MIN_VOLUME"]
        min_liquidity = self.config["MIN_LIQUIDITY"]
        max_spread = self.config["MAX_SPREAD"]

        candidates = []
        offset = 0

        try:
            while len(candidates) < 300 and offset < 600:
                r = await self.client.get(f"{GAMMA_API}/markets", params={
                    "active": "true", "closed": "false",
                    "order": "volume24hr", "ascending": "false",
                    "limit": 100, "offset": offset,
                })
                batch = r.json() or []
                if not batch:
                    break

                for m in batch:
                    vol = float(m.get("volume") or 0)
                    liq = float(m.get("liquidity") or 0)
                    if vol < min_volume or liq < min_liquidity:
                        continue

                    # Parse price like arbitrage does
                    raw_prices = m.get("outcomePrices")
                    if not raw_prices:
                        continue
                    if isinstance(raw_prices, str):
                        raw_prices = _json.loads(raw_prices)
                    yes_price = float(raw_prices[0])

                    # Watchlist zone filter (YES side only — we buy YES tokens)
                    if not (min_price <= yes_price <= max_price):
                        continue

                    spread = float(m.get("spread") or 0)
                    if spread > max_spread:
                        continue

                    yes_token, no_token = _parse_token_ids(m)
                    question = m.get("question", "")

                    candidates.append({
                        "market_id": str(m["id"]),
                        "question":  question,
                        "theme":     classify_theme(question),
                        "yes_price": round(yes_price, 4),
                        "no_price":  round(1.0 - yes_price, 4),
                        "volume":    vol,
                        "liquidity": liq,
                        "spread":    spread,
                        "best_ask":  float(m.get("bestAsk") or yes_price),
                        "yes_token": yes_token,
                        "no_token":  no_token,
                        "neg_risk":  bool(m.get("negRisk")),
                    })

                offset += 100
                if len(batch) < 100:
                    break

            log.info(f"[Scanner] Found {len(candidates)} watchlist candidates "
                     f"in [{min_price:.0%}-{max_price:.0%}] range")
            return candidates

        except Exception as e:
            log.error(f"[Scanner] Error: {e}")
            return []

    async def close(self):
        await self.client.aclose()
