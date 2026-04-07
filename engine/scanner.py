import logging
import json as _json
import re
from datetime import datetime, timezone

import httpx

log = logging.getLogger("micro.scanner")

GAMMA_API = "https://gamma-api.polymarket.com"

THEME_KEYWORDS = {
    # Sports & esports FIRST — must match before "war" catches "strike" or generic " vs "
    "sports":     ["nba","nfl","mlb","ufc","premier league","champions league","world cup",
                   "ncaa","tournament","match","game ","score","winner","playoff",
                   "masters","pga","golf","tennis","boxing","formula 1","f1 ",
                   "real madrid","barcelona","lakers","celtics","brewers","red sox","yankees",
                   "nations league","fifa","uefa","concacaf","conmebol",
                   "clay court","linz","monza","open:","oxford united","fc win",
                   "o/u 2.5","o/u 3.5","o/u 1.5","games total",
                   "ducks","rockets","pioneers","monkey king","beijing ducks","nanjing"],
    "esports":    ["esports","counter-strike","dota","league of legends","valorant",
                   "fnatic","navi","faze","mongolz","blast open","pgl bucharest","pgl ",
                   "parivision","fut esports","b8 vs","3dmax","fokus vs","wildcard",
                   "astralis","bc.game","voca","map 1 winner","map 2 winner","map 3 winner",
                   "(bo3)","(bo5)","(bo1)"],

    # Geopolitics & conflicts
    "iran":       ["iran","iranian","tehran","nuclear iran","iaea","persian gulf","strait of hormuz",
                   "khamenei","enrichment","nuclear deal"],
    "israel":     ["israel","hamas","gaza","hezbollah","netanyahu","idf","west bank","golan","dimona"],
    "ukraine":    ["ukraine","zelensky","donbas","crimea","kherson","zaporizhzhia"],
    "russia":     ["russia","putin","kremlin","moscow","wagner","navalny"],
    "china":      ["china","taiwan","beijing","xi jinping","south china sea","ccp","uyghur"],
    "war":        ["war ","attack","invasion","missile","nuclear","military","troops","bomb",
                   "drone","ceasefire","peace deal","airstrike","evacuate"],
    "peace":      ["peace","deal","agreement","surrender","truce","negotiations","treaty"],
    "yemen":      ["yemen","houthi","aden","sanaa"],

    # US Politics
    "trump":      ["trump","executive order","tariff","maga","mar-a-lago","trumps"],
    "election":   ["election","vote","president","referendum","governor","mayor","minister","parliament",
                   "primary","caucus","midterm","ballot","polling","swing state","electoral",
                   "presidential","nominee","running mate","party win","fidesz","tisza",
                   "democrat","republican","gop","congress","senate"],
    "usgov":      ["doge","government shutdown","federal budget","pentagon","cia","fbi","doj",
                   "secretary of state","cabinet","impeach","pardon","classified","dhs"],

    # Commodities & markets
    "oil":        ["oil","opec","crude","brent","wti","petroleum","natural gas","lng"],
    "gold":       ["gold","xau","precious metal","silver","platinum"],
    "crypto":     ["bitcoin","btc","crypto","ethereum","eth","solana","sol","dogecoin","doge","xrp",
                   "ripple","cardano","polkadot","avalanche","chainlink","defi","nft","stablecoin",
                   "binance","coinbase","memecoin","altcoin","megaeth","microstrategy"],
    "stocks":     ["s&p","sp500","spx","nasdaq","dow jones","russell","stock market","ipo","earnings",
                   "market cap","fdv","bull market","bear market"],

    # Economy & macro
    "fed":        ["federal reserve","powell","rate cut","rate hike","interest rate","fomc",
                   "monetary policy","fed chair","bank of england","ecb"],
    "economy":    ["gdp","inflation","recession","unemployment","cpi","jobs","nonfarm","payroll",
                   "consumer spending","retail sales","housing","mortgage","trade balance"],

    # Tech & science
    "tech":       ["ai ","artificial intelligence","openai","anthropic","google","apple","nvidia",
                   "tesla","microsoft","meta","amazon","semiconductor","chip","quantum","robotics",
                   "chatgpt","gemini","claude","deepseek"],
    "space":      ["nasa","spacex","rocket","satellite","mars","moon","orbit","launch","starship",
                   "blue origin","artemis"],
    "musk":       ["elon musk","musk","tweet","twitter","x.com"],
    "social":     ["followers","tiktok","instagram","youtube","subscribers","views","downloads",
                   "mrbeast","mr beast","streamer","influencer","viral"],

    # Society
    "health":     ["covid","pandemic","vaccine","fda","who ","disease","outbreak",
                   "bird flu","h5n1","monkeypox","drug","pharma"],
    "climate":    ["climate","hurricane","earthquake","wildfire","flood","weather","tornado",
                   "temperature","emissions","carbon"],
    "legal":      ["court","ruling","lawsuit","indictment","trial","verdict","conviction",
                   "acquittal","sentence","extradition","arrest","charged"],
    "film":       ["box office","movie","film","oscar","opening weekend",
                   "grammy","emmy","golden globe","netflix","disney","streaming"],

    # Regions
    "europe":     ["eu ","european","macron","scholz","starmer","brexit","ecb",
                   "germany","france","uk ","britain","italy","spain","poland","hungarian"],
    "latam":      ["brazil","lula","mexico","argentina","milei","venezuela","maduro",
                   "colombia","peru","chile","bolivia","ecuador","cuba","peruvian"],
    "africa":     ["africa","nigeria","south africa","kenya","ethiopia","egypt","morocco"],
    "mideast":    ["saudi","mbs","qatar","uae","emirates","bahrain","oman","iraq","baghdad"],
}

# Themes with ALWAYS-risky gap risk (sports scores, esports matches)
RISKY_THEMES = {"sports", "esports", "israel", "war"}

# Question patterns that indicate volatile/unpredictable outcomes
# These are risky regardless of theme — price can gap from 95¢ to 0¢
_RISKY_PATTERNS = [
    # Price bets — any mention of $ amounts or price thresholds
    re.compile(r"\$[\d,]+", re.I),                                          # any dollar amount ($78,000)
    re.compile(r"(above|below|over|under|reach|hit|dip|touch)\s+\$?[\d,]+", re.I),  # price direction + number
    re.compile(r"(up or down|higher or lower|green or red)", re.I),         # binary price direction
    re.compile(r"(price|market cap|fdv|mcap)", re.I),                       # price-related nouns
    # Sports/competition
    re.compile(r"will .+ (beat|defeat|win against|lose to)", re.I),
    re.compile(r"will .+ win\s+(on|in|at|against|the|their)\b", re.I),  # "will X win on [date]", "will X win the match"
    re.compile(r" vs\.? ", re.I),
    re.compile(r"(score|goals?|points?) (over|under)", re.I),
    # Counting/threshold/range — "90 million or more", "80 ships", "be 7°C"
    re.compile(r"(post|tweet|send|write|publish)\s+\d+[\s-]+\d+", re.I),
    re.compile(r"\d+[\s-]+\d+\s+(tweets?|posts?|times?|mentions?)", re.I),
    re.compile(r"\b\d+-\d+\b.*\b(tweets?|posts?|goals?|points?|runs?|yards?)\b", re.I),
    re.compile(r"(exactly|between)\s+\d+", re.I),
    re.compile(r"(more|fewer|less) than \d+", re.I),                        # "more than 5"
    re.compile(r"\d+[\w\s]*(or more|or less|or fewer|\+)", re.I),           # "90 million or more", "80+"
    re.compile(r"(get|have|receive|reach)\s+\d{2,}", re.I),                  # "get 90 million views" (2+ digit numbers)
    re.compile(r"how many", re.I),
    re.compile(r"(mention|say|use the word)", re.I),
    # Weather / temperature / measurement bets — unpredictable
    re.compile(r"(temperature|°[CF]|degrees|celsius|fahrenheit|weather|rainfall|wind speed)", re.I),
    re.compile(r"(highest|lowest|average|max|min)\s+(temperature|temp|wind|rain)", re.I),
    # Exact number bets — "be 7°C", "be exactly", "end at"
    re.compile(r"\bbe\s+\d+", re.I),                                        # "be 7°C", "be 100"
    # Ships/transit/counting physical events
    re.compile(r"\d+\s+(ships?|vessels?|flights?|trains?|trucks?)\s+(transit|cross|pass)", re.I),
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
    re.compile(r"(\w+)\s+(\d{1,2})\s*,?\s*20\d{2}", re.I),
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
            day = calendar.monthrange(datetime.now(timezone.utc).year, month)[1]
        else:
            day = int(m.group(2))
        now = datetime.now(timezone.utc)
        try:
            dt = datetime(now.year, month, day, 23, 59, tzinfo=timezone.utc)
            # If no explicit year and date is >30 days in the past, assume next year
            if (now - dt).days > 30:
                dt = dt.replace(year=now.year + 1)
            return dt
        except (ValueError, OverflowError):
            continue

    # Range pattern: "March 23-29" → use end of range
    m = _RANGE_PATTERN.search(question)
    if m:
        month_str = m.group(1).lower()
        month = _MONTH_MAP.get(month_str)
        day = int(m.group(2))
        if month:
            now = datetime.now(timezone.utc)
            try:
                dt = datetime(now.year, month, day, 23, 59, tzinfo=timezone.utc)
                if (now - dt).days > 30:
                    dt = dt.replace(year=now.year + 1)
                return dt
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
        # Event cascade: negRiskMarketID → list of sibling market info
        self.event_siblings: dict[str, list[dict]] = {}

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
                all_markets.extend(batch)

            # Build event siblings map (negRisk events: one YES → all others NO)
            _event_map: dict[str, list[dict]] = {}
            for m in all_markets:
                neg_risk_id = m.get("negRiskMarketID") or ""
                if neg_risk_id:
                    raw_p = m.get("outcomePrices")
                    if raw_p:
                        if isinstance(raw_p, str):
                            raw_p = _json.loads(raw_p)
                        _event_map.setdefault(neg_risk_id, []).append({
                            "market_id": str(m["id"]),
                            "question": m.get("question", ""),
                            "yes_price": float(raw_p[0]),
                            "no_price": float(raw_p[1]) if len(raw_p) > 1 else round(1.0 - float(raw_p[0]), 4),
                            "yes_token": None,  # filled below if needed
                            "no_token": None,
                            "spread": float(m.get("spread") or 0),
                            "volume": float(m.get("volume") or 0),
                            "theme": classify_theme(m.get("question", "")),
                            "slug": (m.get("events") or [{}])[0].get("slug", "") if m.get("events") else m.get("slug", ""),
                            "end_date": m.get("endDate") or m.get("endDateIso"),
                            "neg_risk_id": neg_risk_id,
                            "_raw_m": m,  # keep for token parsing
                        })
            # Parse tokens for event siblings
            for siblings in _event_map.values():
                for s in siblings:
                    raw_m = s.pop("_raw_m")
                    yt, nt = _parse_token_ids(raw_m)
                    s["yes_token"] = yt
                    s["no_token"] = nt
            self.event_siblings = _event_map

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
                no_price = float(raw_prices[1]) if len(raw_prices) > 1 else round(1.0 - yes_price, 4)

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

                # Skip markets not accepting orders (in review / paused)
                if not m.get("acceptingOrders", True):
                    continue

                theme = classify_theme(question)

                # Skip risky markets — these have gap risk (except ≥96¢ which are near-certain)
                best_price = max(yes_price, no_price)
                if is_risky_market(question, theme) and best_price < 0.96:
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

                    events = m.get("events") or []
                    event_slug = events[0].get("slug", "") if events else ""
                    url_slug = event_slug or m.get("slug", "")

                    c = {
                        "market_id": market_id,
                        "slug":      url_slug,
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
