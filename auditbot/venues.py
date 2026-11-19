"""Public order-book + 24h-volume adapters for the whitelisted venues. No keys, no accounts.

Each adapter returns (bids, asks, vol24h_quote) with bids/asks as [(price, size)] best first.
`measure` turns a book into the snapshot fields; the math is collect.py's, unchanged.
"""
import json
import threading
import time
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (mm-audit)"

# CoinGecko exchange id -> our venue id, display name
VENUES = {
    "binance": ("binance", "Binance"),
    "okex": ("okx", "OKX"),
    "bybit_spot": ("bybit", "Bybit"),
    "gate": ("gate", "Gate"),
    "kucoin": ("kucoin", "KuCoin"),
    "mxc": ("mexc", "MEXC"),
    "bitget": ("bitget", "Bitget"),
    "huobi": ("htx", "HTX"),
    "bingx": ("bingx", "BingX"),
    "kraken": ("kraken", "Kraken"),
}
CG_ID = {v: cg for cg, (v, _) in VENUES.items()}
NAME = {v: name for v, name in VENUES.values()}


# Hosts that need calls spaced out whatever the collector's thread count (seconds between calls).
# Kraken: "about one request per second" per IP for public endpoints (support.kraken.com 206548367).
PACE = {"api.kraken.com": 1.1}
_pace_lock = threading.Lock()
_pace_next = {}


def _pace(url):
    host = urllib.parse.urlparse(url).hostname
    gap = PACE.get(host)
    if not gap:
        return
    with _pace_lock:
        wait = _pace_next.get(host, 0) - time.time()
        _pace_next[host] = max(time.time(), _pace_next.get(host, 0)) + gap
    if wait > 0:
        time.sleep(wait)


def get(url, timeout=15):
    _pace(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _pairs(rows):
    return [(float(r[0]), float(r[1])) for r in rows]


def book_and_volume(venue, base, quote):
    b, q = base.upper(), quote.upper()
    if venue == "binance":
        s = b + q
        d = get(f"https://api.binance.com/api/v3/depth?symbol={s}&limit=1000")
        t = get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={s}")
        return _pairs(d["bids"]), _pairs(d["asks"]), float(t["quoteVolume"])
    if venue == "okx":
        s = f"{b}-{q}"
        d = get(f"https://www.okx.com/api/v5/market/books?instId={s}&sz=400")["data"][0]
        t = get(f"https://www.okx.com/api/v5/market/ticker?instId={s}")["data"][0]
        return _pairs(d["bids"]), _pairs(d["asks"]), float(t["volCcy24h"])
    if venue == "bybit":
        s = b + q
        d = get(f"https://api.bybit.com/v5/market/orderbook?category=spot&symbol={s}&limit=200")["result"]
        t = get(f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={s}")["result"]["list"][0]
        return _pairs(d["b"]), _pairs(d["a"]), float(t["turnover24h"])
    if venue == "gate":
        s = f"{b}_{q}"
        d = get(f"https://api.gateio.ws/api/v4/spot/order_book?currency_pair={s}&limit=1000")
        t = get(f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={s}")[0]
        return _pairs(d["bids"]), _pairs(d["asks"]), float(t["quote_volume"])
    if venue == "kucoin":
        s = f"{b}-{q}"
        d = get(f"https://api.kucoin.com/api/v1/market/orderbook/level2_100?symbol={s}")["data"]
        t = get(f"https://api.kucoin.com/api/v1/market/stats?symbol={s}")["data"]
        return _pairs(d["bids"]), _pairs(d["asks"]), float(t["volValue"])
    if venue == "mexc":
        s = b + q
        d = get(f"https://api.mexc.com/api/v3/depth?symbol={s}&limit=1000")
        t = get(f"https://api.mexc.com/api/v3/ticker/24hr?symbol={s}")
        return _pairs(d["bids"]), _pairs(d["asks"]), float(t["quoteVolume"])
    if venue == "bitget":
        s = b + q
        d = get(f"https://api.bitget.com/api/v2/spot/market/orderbook?symbol={s}&type=step0&limit=150")["data"]
        t = get(f"https://api.bitget.com/api/v2/spot/market/tickers?symbol={s}")["data"][0]
        return _pairs(d["bids"]), _pairs(d["asks"]), float(t["quoteVolume"])
    if venue == "htx":
        s = (b + q).lower()
        d = get(f"https://api.huobi.pro/market/depth?symbol={s}&type=step0")["tick"]
        t = get(f"https://api.huobi.pro/market/detail?symbol={s}")["tick"]
        return _pairs(d["bids"]), _pairs(d["asks"]), float(t["vol"])
    if venue == "bingx":  # 500 req / 10 s per IP (x-ratelimit headers); asks come worst-first, measure() sorts
        s = f"{b}-{q}"
        d = get(f"https://open-api.bingx.com/openApi/spot/v1/market/depth?symbol={s}&limit=1000")
        if d.get("code") != 0:
            raise ValueError(f"bingx depth: {d.get('msg') or d.get('code')}")
        t = get(f"https://open-api.bingx.com/openApi/spot/v1/ticker/24hr?symbol={s}&timestamp={int(time.time() * 1000)}")
        if t.get("code") != 0:
            raise ValueError(f"bingx ticker: {t.get('msg') or t.get('code')}")
        return _pairs(d["data"]["bids"]), _pairs(d["data"]["asks"]), float(t["data"][0]["quoteVolume"])
    if venue == "kraken":  # USD pairs allowed here only (coingecko.QUOTES_BY_VENUE); Depth count max 500
        s = b + q
        d = get(f"https://api.kraken.com/0/public/Depth?pair={s}&count=500")
        if d.get("error"):
            raise ValueError(f"kraken depth: {d['error']}")
        book = next(iter(d["result"].values()))  # result is keyed by Kraken's own pair name (e.g. XXBTZUSD)
        t = get(f"https://api.kraken.com/0/public/Ticker?pair={s}")
        if t.get("error"):
            raise ValueError(f"kraken ticker: {t['error']}")
        tk = next(iter(t["result"].values()))
        vol_quote = float(tk["v"][1]) * float(tk["p"][1])  # 24 h base volume x 24 h VWAP
        return (_pairs([x[:2] for x in book["bids"]]), _pairs([x[:2] for x in book["asks"]]), vol_quote)
    raise ValueError(f"unknown venue {venue}")


def measure(bids, asks):
    # best first whatever order the venue used (BingX sends asks worst-first)
    bids = sorted(bids, key=lambda x: -x[0])
    asks = sorted(asks, key=lambda x: x[0])
    if not bids or not asks:
        raise ValueError("empty book")
    bb, ba = bids[0][0], asks[0][0]
    if bb <= 0 or ba < bb:
        raise ValueError(f"bad top of book {bb}/{ba}")
    mid = (bb + ba) / 2
    lo, hi = mid * 0.98, mid * 1.02
    return {
        "mid": mid, "best_bid": bb, "best_ask": ba,
        "spread_pct": (ba - bb) / mid * 100,
        "bid_2pct_usd": round(sum(p * q for p, q in bids if p >= lo), 2),
        "ask_2pct_usd": round(sum(p * q for p, q in asks if p <= hi), 2),
        # the venue's book ended inside the band: that side's depth is a lower bound
        "trunc_bid": int(bids[-1][0] > lo), "trunc_ask": int(asks[-1][0] < hi),
        "book": book_stats(bids, asks, mid),
    }


def book_stats(bids, asks, mid):
    """Per-band depth and level counts for the venue checks (thresholds.json), stored as snapshots.book.
    d: {band: [bid_usd, ask_usd]}, t: {band: [bid_truncated, ask_truncated]}, lv: levels returned per side,
    lv10: levels within ±10%. Bands are config.BANDS; keys are strings ("0.25", "1", ...) as JSON keeps them."""
    from .config import BANDS
    d, t = {}, {}
    for b in BANDS:
        lo, hi = mid * (1 - b / 100), mid * (1 + b / 100)
        k = f"{b:g}"
        d[k] = [round(sum(p * q for p, q in bids if p >= lo), 2), round(sum(p * q for p, q in asks if p <= hi), 2)]
        t[k] = [int(bids[-1][0] > lo), int(asks[-1][0] < hi)]
    lo, hi = mid * 0.9, mid * 1.1
    return {"d": d, "t": t, "lv": [len(bids), len(asks)],
            "lv10": [sum(1 for p, _ in bids if p >= lo), sum(1 for p, _ in asks if p <= hi)]}


def snapshot(venue, base, quote):
    bids, asks, vol = book_and_volume(venue, base, quote)
    row = measure(bids, asks)
    row["vol24h_usd"] = round(vol, 2)
    return row
