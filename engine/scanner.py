import logging
import json as _json
import re
from datetime import datetime, timezone

import httpx

log = logging.getLogger("micro.scanner")

GAMMA_API = "https://gamma-api.polymarket.com"

THEME_KEYWORDS = {
    "crypto":      ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol", "xrp", "dogecoin"],
    "politics":    ["trump", "biden", "election", "president", "congress", "senate", "governor"],
    "fed":         ["fed ", "interest rate", "fomc", "federal reserve", "rate cut", "rate hike"],
    "economy":     ["gdp", "inflation", "recession", "unemployment", "cpi", "jobs"],
    "tech":        ["apple", "google", "meta", "microsoft", "nvidia", "openai", "ai "],
    "sports":      ["nba", "nfl", "mlb", "ufc", "premier league", "champions league", "world cup",
                    "match", "game ", "score", "winner", "playoff"],
    "esports":     ["esports", "counter-strike", "dota", "league of legends", "valorant",
                    "semperfi", "fnatic", "navi", "faze", " vs "],
    "geopolitics": ["russia", "ukraine", "china", "taiwan", "war", "nato", "sanctions"],
    "spacex":      ["spacex", "starship", "launch", "rocket", "nasa"],
    "markets":     ["s&p", "spy", "nasdaq", "dow jones", "gold", "oil", "commodities"],
    "celeb":       ["elon", "musk", "kanye", "celebrity"],
}

# Themes with high gap risk — price jumps from 90¢ to 0¢ on resolution
RISKY_THEMES = {"crypto", "sports", "esports", "markets"}

# Question patterns that indicate volatile/unpredictable outcomes
_RISKY_PATTERNS = [
    re.compile(r"price of .+ (above|below|reach|hit|dip)", re.I),
    re.compile(r"will .+ (beat|defeat|win against|lose to)", re.I),
    re.compile(r"(above|below|over|under) \$[\d,]+", re.I),
    re.compile(r" vs\.? ", re.I),
    re.compile(r"(score|goals?|points?) (over|under)", re.I),
]


def classify_theme(question: str) -> str:
    q = question.lower()
    for theme, keywords in THEME_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return theme
    return "other"


def is_risky_market(question: str, theme: str) -> bool:
    """Markets where high price does NOT mean safe resolution."""
    if theme in RISKY_THEMES:
        return True
    for pat in _RISKY_PATTERNS:
        if pat.search(question):
            return True
    return False


def quality_score(price: float, spread: float, days_left: float,
                  volume: float, liquidity: float) -> float:
    """Score 0-100. Higher = better candidate for resolution harvesting."""
    score = 0.0
    # Price confidence: 93¢=30, 95¢=50, 97¢=70
    score += max(0, (price - 0.90) * 1000)  # 0-100 range roughly
    # Tight spread bonus (0-20)
    score += max(0, 20 - spread * 1000)
    # Close to resolution (0-20)
    if days_left <= 0.5:
        score += 20
    elif days_left <= 1:
        score += 15
    elif days_left <= 2:
        score += 10
    # Volume/liquidity bonus (0-10)
    if volume > 500_000:
        score += 10
    elif volume > 100_000:
        score += 5
    return round(score, 1)


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
    Filters out risky markets (crypto price, sports, esports).
    Checks BOTH sides: if YES ≥ entry → buy YES, if NO ≥ entry → buy NO."""

    def __init__(self, config: dict):
        self.config = config
        self.client = httpx.AsyncClient(timeout=15.0)
        self._pages = int(config.get("SCAN_PAGES", 8))  # 800 markets max

    async def fetch_candidates(self) -> tuple[list, list]:
        """Returns (direct_entries, watchlist).
        Checks both YES and NO sides of each market.
        """
        entry_price = self.config["ENTRY_MIN_PRICE"]
        wl_min = self.config["WATCHLIST_MIN_PRICE"]
        max_days = self.config["MAX_DAYS_LEFT"]
        max_stake = self.config["MAX_STAKE"]
        liq_mult = self.config.get("MIN_LIQUIDITY_MULT", 500)
        min_liquidity = max_stake * liq_mult
        max_spread = self.config["MAX_SPREAD"]
        min_roi = self.config.get("MIN_ROI", 0.03)
        min_volume = self.config.get("MIN_VOLUME", 50000)
        min_quality = self.config.get("MIN_QUALITY_SCORE", 30)

        direct = []
        watchlist = []
        seen = set()
        skipped_risky = 0

        try:
            import asyncio
            tasks = []
            for offset in range(0, self._pages * 100, 100):
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
                if vol < min_volume:
                    continue
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

                question = m.get("question", "")
                theme = classify_theme(question)

                # Skip risky markets — these have gap risk
                if is_risky_market(question, theme):
                    skipped_risky += 1
                    continue

                yes_token, no_token = _parse_token_ids(m)
                market_id = str(m["id"])

                candidates_for_market = []

                if yes_price >= wl_min:
                    roi = (1.0 - yes_price) / yes_price
                    if roi >= min_roi:
                        q = quality_score(yes_price, spread, days_left, vol, liq)
                        if q >= min_quality:
                            candidates_for_market.append({
                                "side": "YES",
                                "price": yes_price,
                                "best_ask": float(m.get("bestAsk") or yes_price),
                                "roi": roi,
                                "quality": q,
                                "ws_token": yes_token,
                                "ws_side": "yes",
                            })

                if no_price >= wl_min:
                    roi = (1.0 - no_price) / no_price
                    if roi >= min_roi:
                        q = quality_score(no_price, spread, days_left, vol, liq)
                        if q >= min_quality:
                            best_bid_yes = float(m.get("bestBid") or yes_price)
                            no_best_ask = round(1.0 - best_bid_yes, 4)
                            candidates_for_market.append({
                                "side": "NO",
                                "price": no_price,
                                "best_ask": no_best_ask,
                                "roi": roi,
                                "quality": q,
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
                        "theme":     theme,
                        "side":      info["side"],
                        "price":     round(info["price"], 4),
                        "best_ask":  round(info["best_ask"], 4),
                        "volume":    vol,
                        "liquidity": liq,
                        "spread":    spread,
                        "days_left": round(days_left, 2),
                        "end_date":  end_str,
                        "roi":       round(info["roi"], 4),
                        "quality":   info["quality"],
                        "yes_token": yes_token,
                        "no_token":  no_token,
                        "ws_token":  info["ws_token"],
                        "ws_side":   info["ws_side"],
                    }

                    if info["price"] >= entry_price:
                        direct.append(c)
                    else:
                        watchlist.append(c)

            # Sort by quality (best first), then by days_left
            direct.sort(key=lambda c: (-c["quality"], c["days_left"]))
            watchlist.sort(key=lambda c: (-c["quality"], -c["price"]))

            log.info(
                f"[Scanner] {len(direct)} direct (≥{entry_price:.0%}) + "
                f"{len(watchlist)} watchlist ({wl_min:.0%}-{entry_price:.0%}), "
                f"≤{max_days}d left | skipped {skipped_risky} risky"
            )
            return direct, watchlist

        except Exception as e:
            log.error(f"[Scanner] Error: {e}")
            return [], []

    async def close(self):
        await self.client.aclose()
