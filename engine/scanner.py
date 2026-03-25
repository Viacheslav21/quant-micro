import logging
import json as _json
import re
from datetime import datetime, timezone

import httpx

log = logging.getLogger("micro.scanner")

GAMMA_API = "https://gamma-api.polymarket.com"

THEME_KEYWORDS = {
    "crypto":      ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol", "xrp", "dogecoin",
                    "defi", "nft", "token", "blockchain", "megaeth", "polymarket"],
    "politics":    ["trump", "biden", "election", "president", "congress", "senate", "governor",
                    "democrat", "republican", "gop", "parliament", "prime minister", "party win"],
    "iran":        ["iran", "tehran", "khamenei", "hormuz", "persian gulf", "enrichment", "nuclear deal"],
    "israel":      ["israel", "idf", "hamas", "hezbollah", "houthi", "dimona", "gaza", "netanyahu"],
    "military":    ["military", "strike", "ceasefire", "invasion", "troops", "airstrike", "missile",
                    "evacuate", "embassy", "nato", "war ", "peace deal"],
    "fed":         ["fed ", "interest rate", "fomc", "federal reserve", "rate cut", "rate hike"],
    "economy":     ["gdp", "inflation", "recession", "unemployment", "cpi", "jobs"],
    "tech":        ["apple", "google", "meta", "microsoft", "nvidia", "openai", "ai ", "deepseek",
                    "chatgpt", "gemini", "released", "launch"],
    "sports":      ["nba", "nfl", "mlb", "ufc", "premier league", "champions league", "world cup",
                    "ncaa", "tournament", "match", "game ", "score", "winner", "playoff",
                    "real madrid", "barcelona", "lakers", "celtics"],
    "esports":     ["esports", "counter-strike", "dota", "league of legends", "valorant",
                    "semperfi", "fnatic", "navi", "faze", " vs ", "mongolz", "blast open"],
    "geopolitics": ["russia", "ukraine", "china", "taiwan", "sanctions", "pakistan", "afghanistan",
                    "qatar", "kuwait", "uae", "saudi"],
    "spacex":      ["spacex", "starship", "rocket", "nasa"],
    "markets":     ["s&p", "spy", "nasdaq", "dow jones", "gold", "oil", "commodities", "crude"],
    "celeb":       ["elon", "musk", "kanye", "celebrity", "bitboy"],
    "social":      ["tweet", "post", "mention", "x.com", "twitter"],
}

# Themes with ALWAYS-risky gap risk (sports scores, esports matches)
RISKY_THEMES = {"sports", "esports"}

# Question patterns that indicate volatile/unpredictable outcomes
# These are risky regardless of theme — price can gap from 95¢ to 0¢
_RISKY_PATTERNS = [
    # Price bets — any mention of $ amounts or price thresholds
    re.compile(r"\$[\d,]+", re.I),                                          # any dollar amount ($78,000)
    re.compile(r"(above|below|over|under|between|reach|hit|dip|touch)\s", re.I),  # price direction words
    re.compile(r"(up or down|higher or lower|green or red)", re.I),         # binary price direction
    re.compile(r"(price|market cap|fdv|mcap)", re.I),                       # price-related nouns
    # Sports/competition
    re.compile(r"will .+ (beat|defeat|win against|lose to)", re.I),
    re.compile(r" vs\.? ", re.I),
    re.compile(r"(score|goals?|points?) (over|under)", re.I),
    # Counting/mention/range markets — exact number bets gap to 0 on resolution
    re.compile(r"(post|tweet|send|write|publish)\s+\d+[\s-]+\d+", re.I),
    re.compile(r"\d+[\s-]+\d+\s+(tweets?|posts?|times?|mentions?)", re.I),
    re.compile(r"\b\d+-\d+\b.*\b(tweets?|posts?|goals?|points?|runs?|yards?)\b", re.I),
    re.compile(r"(exactly|between)\s+\d+", re.I),
    re.compile(r"(more|fewer|less) than \d+ (tweets?|posts?|times?)", re.I),
    re.compile(r"how many", re.I),
    re.compile(r"(mention|say|use the word)", re.I),
]

# --- Date parsing from question text ---
_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

_DATE_PATTERNS = [
    # "by March 31" / "on March 25" / "before April 15"
    re.compile(r"(?:by|on|before)\s+(\w+)\s+(\d{1,2})", re.I),
    # "March 31, 2026"
    re.compile(r"(\w+)\s+(\d{1,2})\s*,?\s*202[5-7]", re.I),
    # "in March" → end of month
    re.compile(r"in\s+(\w+)\s*\??$", re.I),
]

# "March 23-29" or "March 20 to March 27"
_RANGE_PATTERN = re.compile(r"(\w+)\s+\d+\s*(?:-|to)\s*(?:\w+\s+)?(\d{1,2})", re.I)


def _parse_date_from_question(question: str):
    """Extract resolution date from question text when API endDate is missing."""
    for pat in _DATE_PATTERNS:
        m = pat.search(question)
        if not m:
            continue
        month_str = m.group(1).lower()
        month = _MONTH_MAP.get(month_str)
        if not month:
            continue
        if pat == _DATE_PATTERNS[2]:
            # "in March" → last day of month
            import calendar
            day = calendar.monthrange(2026, month)[1]
        else:
            day = int(m.group(2))
        try:
            return datetime(2026, month, day, 23, 59, tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            continue

    # Range pattern: "March 23-29" → use end of range
    m = _RANGE_PATTERN.search(question)
    if m:
        month_str = m.group(1).lower()
        month = _MONTH_MAP.get(month_str)
        day = int(m.group(2))
        if month:
            try:
                return datetime(2026, month, day, 23, 59, tzinfo=timezone.utc)
            except (ValueError, OverflowError):
                pass

    return None


def classify_theme(question: str) -> str:
    q = question.lower()
    for theme, keywords in THEME_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return theme
    return "other"


def is_risky_market(question: str, theme: str) -> bool:
    """Markets where high price does NOT mean safe resolution.
    Sports/esports: always risky (score-based, unpredictable).
    Price bets, counting bets, mention bets: gap risk.
    """
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
    # Price confidence: 90¢=0, 93¢=30, 95¢=50, 97¢=70
    score += max(0, (price - 0.90) * 1000)
    # Tight spread bonus (0-20)
    score += max(0, 20 - spread * 1000)
    # Close to resolution (0-20)
    if days_left <= 0.5:
        score += 20
    elif days_left <= 1:
        score += 15
    elif days_left <= 2:
        score += 12
    elif days_left <= 3:
        score += 10
    elif days_left <= 5:
        score += 7
    elif days_left <= 7:
        score += 5
    elif days_left <= 10:
        score += 3
    # Volume/liquidity bonus (0-10)
    if volume > 1_000_000:
        score += 10
    elif volume > 500_000:
        score += 8
    elif volume > 100_000:
        score += 5
    elif volume > 20_000:
        score += 3
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
    Filters out risky markets (price bets, sports, counting).
    Parses dates from question text when API endDate is missing.
    Checks BOTH sides: if YES ≥ entry → buy YES, if NO ≥ entry → buy NO."""

    def __init__(self, config: dict):
        self.config = config
        self.client = httpx.AsyncClient(timeout=15.0)
        self._pages = int(config.get("SCAN_PAGES", 16))  # 1600 markets max

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
        min_volume = self.config.get("MIN_VOLUME", 20000)
        min_quality = self.config.get("MIN_QUALITY_SCORE", 25)

        direct = []
        watchlist = []
        seen = set()
        skipped_risky = 0
        skipped_no_date = 0

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

                # Neither side in our target zone — skip early
                if yes_price < wl_min and no_price < wl_min:
                    continue

                # Get days_left: API endDate first, then parse from question
                end_str = m.get("endDate") or m.get("endDateIso")
                days_left = _days_until(end_str)

                question = m.get("question", "")

                if days_left < 0:
                    # Try to parse date from question text
                    parsed_end = _parse_date_from_question(question)
                    if parsed_end:
                        days_left = (parsed_end - datetime.now(timezone.utc)).total_seconds() / 86400
                        end_str = parsed_end.isoformat()

                if days_left < 0:
                    skipped_no_date += 1
                    continue
                if days_left > max_days:
                    continue

                spread = float(m.get("spread") or 0)
                if spread > max_spread:
                    continue

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
                            # Always subscribe to YES token; ws_client inverts for NO
                            candidates_for_market.append({
                                "side": "NO",
                                "price": no_price,
                                "best_ask": no_best_ask,
                                "roi": roi,
                                "quality": q,
                                "ws_token": yes_token,
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
                f"≤{max_days:.0f}d | skipped {skipped_risky} risky, {skipped_no_date} no-date"
            )
            return direct, watchlist

        except Exception as e:
            log.error(f"[Scanner] Error: {e}")
            return [], []

    async def close(self):
        await self.client.aclose()
