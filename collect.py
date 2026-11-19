#!/usr/bin/env python3
"""One market-quality snapshot per run, for a market-making audit report (the first version, 2026-10-15).

  python3 collect.py                 # snapshot every configured (venue, pair), append to data/
  python3 collect.py --print         # same, and print the rows

Cron it every 10 minutes. Public endpoints only (no keys); stdlib only. Per (venue, pair) it
records the best bid/ask, top-of-book spread, the USD resting within ±2% of mid on each side,
and the venue's own 24h quote volume. A row is written even when a call fails (`error` set), so
gaps in the series are visible rather than silent. `truncated` = the book the venue returned
ended before the ±2% band, so that side's depth is a lower bound.

Output: data/<pair>.jsonl, one JSON object per (snapshot, venue).
"""
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
PAIRS = [("BTC", "USDT", ["kucoin", "gate", "lbank"])]  # (base, quote, venues): edit for your coin
UA = "Mozilla/5.0 (mm-audit collector)"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def book_and_volume(venue, base, quote):
    """-> (bids, asks, vol24h_quote); bids/asks as [(price, size)] best first."""
    if venue == "kucoin":
        s = f"{base}-{quote}"
        b = get(f"https://api.kucoin.com/api/v1/market/orderbook/level2_100?symbol={s}")["data"]
        t = get(f"https://api.kucoin.com/api/v1/market/stats?symbol={s}")["data"]
        return b["bids"], b["asks"], float(t["volValue"])
    if venue == "gate":
        s = f"{base}_{quote}"
        b = get(f"https://api.gateio.ws/api/v4/spot/order_book?currency_pair={s}&limit=100")
        t = get(f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={s}")[0]
        return b["bids"], b["asks"], float(t["quote_volume"])
    if venue == "lbank":
        s = f"{base.lower()}_{quote.lower()}"
        b = get(f"https://api.lbkex.com/v2/depth.do?symbol={s}&size=200")["data"]
        t = get(f"https://api.lbkex.com/v2/ticker/24hr.do?symbol={s}")["data"][0]["ticker"]
        return b["bids"], b["asks"], float(t["turnover"])
    raise ValueError(f"unknown venue {venue}")


def measure(bids, asks):
    bids = [(float(p), float(q)) for p, q in bids]
    asks = [(float(p), float(q)) for p, q in asks]
    bb, ba = bids[0][0], asks[0][0]
    mid = (bb + ba) / 2
    lo, hi = mid * 0.98, mid * 1.02
    bid_usd = sum(p * q for p, q in bids if p >= lo)
    ask_usd = sum(p * q for p, q in asks if p <= hi)
    return {
        "mid": mid, "best_bid": bb, "best_ask": ba,
        "spread_pct": (ba - bb) / mid * 100,
        "bid_2pct_usd": round(bid_usd, 2), "ask_2pct_usd": round(ask_usd, 2),
        "truncated": {"bid": bids[-1][0] > lo, "ask": asks[-1][0] < hi},
        "levels": {"bid": len(bids), "ask": len(asks)},
    }


def main():
    os.makedirs(DATA, exist_ok=True)
    ts = int(time.time())
    for base, quote, venues in PAIRS:
        path = os.path.join(DATA, f"{base}-{quote}.jsonl".lower())
        with open(path, "a", encoding="utf-8") as f:
            for v in venues:
                row = {"ts": ts, "venue": v, "pair": f"{base}/{quote}"}
                try:
                    bids, asks, vol = book_and_volume(v, base, quote)
                    row.update(measure(bids, asks))
                    row["vol24h_usd"] = round(vol, 2)
                except Exception as e:  # recorded, never fatal: one venue down must not cost the others
                    row["error"] = f"{type(e).__name__}: {e}"[:200]
                f.write(json.dumps(row) + "\n")
                if "--print" in sys.argv:
                    print(json.dumps(row))


if __name__ == "__main__":
    main()
