"""CoinMarketCap for intake: the exchanges CMC lists a coin on that CoinGecko doesn't, and CMC coin links.

CoinGecko misses markets (a token's $1.3M-a-day KuCoin pair absent from its tickers, 2026-11-03), so intake
reads CMC's public market-pairs list too. It is the endpoint CMC's own website uses (no key, unofficial), so
every failure here is silent: CoinGecko alone still works. CMC's exchange slugs equal our venue ids.

CMC links (2026-11-18): some coins have no CoinGecko page at all yet trade on Bitget, MEXC and BingX, so a rep
may send coinmarketcap.com/currencies/<slug>. detail() reads the coin from the same keyless API.
"""
import json
import re
import threading
import time
import urllib.parse
import urllib.request

BASE = "https://api.coinmarketcap.com/data-api/v3/cryptocurrency"
URL = BASE + "/market-pairs/latest"
DETAIL_URL = BASE + "/detail"
LOGO = "https://s2.coinmarketcap.com/static/img/coins/200x200/{}.png"
LINK = re.compile(r"coinmarketcap\.com/(?:[a-z]{2}(?:-[a-z]{2,4})?/)?currencies/([a-z0-9][a-z0-9-]*)", re.I)
UA = "Mozilla/5.0 (mm-audit)"
MIN_GAP_S = 1.2
PAGES = 3  # 100 pairs a page; a big coin (213 pairs) is fully read in 3
_lock = threading.Lock()
_next_at = 0.0
_cache = {}  # coin_id -> (fetched_at, pairs)
CACHE_S = 600


def _get(params, url=URL):
    global _next_at
    with _lock:
        wait = _next_at - time.time()
        _next_at = max(time.time(), _next_at) + MIN_GAP_S
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def detail(slug):
    """{id, slug, name, symbol, platforms: [{chain_id, platform, address}], logo} for a CMC slug, or None when CMC
    has no such coin. An unknown slug is not a 404: CMC answers 200 with error_code "500" and no data (2026-11-18).
    Network errors raise (the rep is told to try again), unlike pairs()."""
    d = (_get({"slug": slug}, DETAIL_URL) or {}).get("data")
    if not d or not d.get("id") or not d.get("symbol"):
        return None
    plats = [{"chain_id": p.get("contractChainId"), "platform": p.get("contractPlatform") or "",
              "address": (p.get("contractAddress") or "").strip()}
             for p in d.get("platforms") or [] if (p.get("contractAddress") or "").strip()]
    return {"id": d["id"], "slug": d.get("slug") or slug, "name": d.get("name") or slug,
            "symbol": d["symbol"].upper(), "platforms": plats, "logo": LOGO.format(d["id"])}


def slugs(coin):
    """CMC slugs to try: the coin's own CMC slug when it came in by a CMC link (it is the only one then); else
    CoinGecko's id, which is usually CMC's slug too; else the name, hyphenated, then run together
    (an apostrophe in the name: CoinGecko's id x-s-coin is CMC's xscoin, 2026-11-06)."""
    if coin.get("cmc_slug"):
        return [coin["cmc_slug"]]
    name = (coin.get("name") or "").lower()
    hyphenated = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    flat = re.sub(r"[^a-z0-9]+", "", name)
    return [s for s in dict.fromkeys([coin["coin_id"], hyphenated, flat]) if s]


def pairs(coin):
    """[{venue, base, quote, volume, price}] for every spot pair CMC lists for the coin, or [] on any failure.
    A slug only counts when CMC's symbol for it equals the coin's (another coin can own the same slug)."""
    hit = _cache.get(coin["coin_id"])
    if hit and time.time() - hit[0] < CACHE_S:
        return hit[1]
    out = []
    try:
        for slug in slugs(coin):
            got = []
            for page in range(PAGES):
                d = (_get({"slug": slug, "start": 1 + page * 100, "limit": 100, "category": "spot",
                           "centerType": "all", "sort": "cmc_rank_advanced"}) or {}).get("data") or {}
                if (d.get("symbol") or "").upper() != (coin.get("symbol") or "").upper():
                    break
                ps = d.get("marketPairs") or []
                for p in ps:
                    base, _, quote = (p.get("marketPair") or "").partition("/")
                    got.append({"venue": p.get("exchangeSlug"), "base": base.upper(), "quote": quote.upper(),
                                "volume": float(p.get("volumeUsd") or 0), "price": float(p.get("price") or 0),
                                "outlier": bool(p.get("outlierDetected"))})
                if len(ps) < 100:
                    break
            if got:
                out = got
                break
    except Exception:
        out = []
    _cache[coin["coin_id"]] = (time.time(), out)
    return out
