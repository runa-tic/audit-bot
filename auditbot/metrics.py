"""Window aggregates for one (coin, venue): the 3-day averages are the headline, `now` is the aside.

Each venue check (config.requirements) becomes one result: its 3-day value, the latest value, the verdict
(True / False, or None when the window holds no data for it) and whether the depth is only a lower bound.
Depth and spread also carry `miss`, the share of snapshots that missed the requirement on their own: the verdict
is on the average, and an average that passes can hide a market that fails for hours (2026-11-11).
"""
import json
import statistics

from . import config


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _book(r):
    b = r.get("book")
    if isinstance(b, str):
        try:
            return json.loads(b)
        except ValueError:
            return None
    return b


def depth(r, band):
    """(bid, ask, bid_truncated, ask_truncated) within ±band% of mid, or None if the row didn't record it.
    Rows from before per-band stats (2026-11-03) still answer for ±2% from their own columns."""
    b = _book(r)
    k = f"{band:g}"
    if b and k in b.get("d", {}):
        (bid, ask), (tb, ta) = b["d"][k], b["t"][k]
        return bid, ask, tb, ta
    if band == 2 and r.get("bid_2pct_usd") is not None:
        return r["bid_2pct_usd"], r["ask_2pct_usd"], r.get("trunc_bid") or 0, r.get("trunc_ask") or 0
    return None


def levels(r, band=None):
    b = _book(r)
    if not b:
        return None
    return b["lv10"] if band == 10 else b["lv"] if band is None else None


def max_drop(points, window_s):
    """Largest fall from a high to a later low no more than window_s after it, % of the high."""
    worst = 0.0
    for i, (t0, p0) in enumerate(points):
        if not p0:
            continue
        for t1, p1 in points[i + 1:]:
            if t1 - t0 > window_s:
                break
            worst = max(worst, (p0 - p1) / p0 * 100)
    return worst


def evaluate(c, ok_rows, peers=None):
    """One check over the window's rows -> result dict (see module doc)."""
    k = c["kind"]
    res = {"c": c, "ok": None, "value": None, "now": None, "floor": False, "n": 0}
    last = ok_rows[-1] if ok_rows else None
    if k == "depth":
        ds = [d for d in (depth(r, c["band"]) for r in ok_rows) if d]
        if not ds:
            return res
        bid, ask = _avg([d[0] for d in ds]), _avg([d[1] for d in ds])
        nowd = depth(last, c["band"])
        res.update(n=len(ds), floor=sum(d[2] or d[3] for d in ds) * 2 > len(ds))
        if c.get("side") == "combined":
            res.update(value=bid + ask, now=(nowd[0] + nowd[1]) if nowd else None, ok=bid + ask >= c["min"],
                       bid=bid, ask=ask, miss=sum(d[0] + d[1] < c["min"] for d in ds) / len(ds))
        else:
            res.update(value=(bid, ask), now=nowd[:2] if nowd else None,
                       ok=bid >= c["min"] and ask >= c["min"], ok_bid=bid >= c["min"], ok_ask=ask >= c["min"],
                       miss=sum(d[0] < c["min"] or d[1] < c["min"] for d in ds) / len(ds))
    elif k in ("spread", "volume"):
        col = "spread_pct" if k == "spread" else "vol24h_usd"
        xs = [r[col] for r in ok_rows if r.get(col) is not None]
        if not xs:
            return res
        v = sum(xs) / len(xs)
        res.update(n=len(xs), value=v, now=last.get(col),
                   ok=v <= c["max"] if k == "spread" else v >= c["min"])
        if k == "spread":  # volume is already a 24 h figure, so it has no per-snapshot share
            res["miss"] = sum(x > c["max"] for x in xs) / len(xs)
    elif k == "levels":
        ls = [x for x in (levels(r, c.get("band")) for r in ok_rows) if x]
        if not ls:
            return res
        agg = min if c.get("agg") == "min" else _avg
        bid, ask = agg([x[0] for x in ls]), agg([x[1] for x in ls])
        res.update(n=len(ls), value=(bid, ask), now=levels(last, c.get("band")),
                   ok=bid >= c["min"] and ask >= c["min"])
    elif k == "drop":
        pts = [(r["ts"], r["mid"]) for r in ok_rows if r.get("mid")]
        if len(pts) < 2:
            return res
        v = max_drop(pts, c["window_h"] * 3600)
        res.update(n=len(pts), value=v, ok=v <= c["max"])
    elif k == "price_diff":
        diffs = []
        for r in ok_rows:
            others = (peers or {}).get(r["ts"])
            if r.get("mid") and others:
                med = statistics.median(others)
                diffs.append(abs(r["mid"] - med) / med * 100)
        if not diffs:
            return res
        v = sum(diffs) / len(diffs)
        res.update(n=len(diffs), value=v, now=diffs[-1], ok=v <= c["max"])
    return res


def summarize(rows, checks, window_h=72, peers=None):
    """rows: snapshots for one venue, oldest first (errors included). checks: config.requirements(venue, ...).
    peers: {ts: [other venues' mids]} for price_diff checks."""
    ok = [r for r in rows if not r.get("error")]
    if not ok:
        return None
    end = ok[-1]["ts"]
    ok = [r for r in ok if r["ts"] >= end - window_h * 3600]
    errors = sum(1 for r in rows if r.get("error") and r["ts"] >= ok[0]["ts"])
    last = ok[-1]
    s = {
        "from": ok[0]["ts"], "to": end, "n": len(ok), "errors": errors,
        "bid_avg": _avg([r["bid_2pct_usd"] for r in ok]), "ask_avg": _avg([r["ask_2pct_usd"] for r in ok]),
        "bid_now": last["bid_2pct_usd"], "ask_now": last["ask_2pct_usd"],
        "spread_avg": _avg([r["spread_pct"] for r in ok]), "spread_now": last["spread_pct"],
        "vol_avg": _avg([r["vol24h_usd"] for r in ok]), "vol_now": last["vol24h_usd"],
        # majority-truncated side: the venue's public book doesn't reach ±2%, so depth is a floor
        "depth_floor": sum(r["trunc_bid"] or r["trunc_ask"] for r in ok) * 2 > len(ok),
        "checks": [evaluate(c, ok, peers) for c in checks],
    }
    judged = [x["ok"] for x in s["checks"] if x["ok"] is not None]
    s["all_ok"] = all(judged) if judged else None
    band = config.trend_band_pct()
    s["trend"] = trend(ok)
    s["trend_first"], s["trend_last"] = depth_ends(ok)
    s["trend_state"] = trend_state(s["trend"], band)
    s["vol_trend"] = vol_trend(ok)
    s["vol_trend_state"] = trend_state(s["vol_trend"], band)
    return s


def trend_state(v, band):
    return None if v is None else "improving" if v > band else "declining" if v < -band else "stable"


def check(s, check_id):
    """The result for one check id, or None if the venue has no such check (or it was switched off)."""
    return next((x for x in s["checks"] if x["c"]["id"] == check_id), None)


def _ends(ok, value):
    """(first 24 h avg, last 24 h avg) of value(row) over the window. Needs 48 h of data, else (None, None)."""
    if len(ok) < 4 or ok[-1]["ts"] - ok[0]["ts"] < 48 * 3600:
        return None, None
    return (_avg([value(r) for r in ok if r["ts"] <= ok[0]["ts"] + 86400]),
            _avg([value(r) for r in ok if r["ts"] >= ok[-1]["ts"] - 86400]))


def _change(ok, value):
    """% change of value(row), first 24 h vs last 24 h of the window. Needs 48 h of data, else None."""
    first, last = _ends(ok, value)
    if not first or last is None:
        return None
    return (last - first) / first * 100


def depth_ends(ok):
    """(first 24 h, last 24 h) average total ±2% depth: the two values the liquidity delta is taken between."""
    return _ends(ok, lambda r: r["bid_2pct_usd"] + r["ask_2pct_usd"])


def trend(ok):
    """% change of total ±2% depth, first 24 h vs last 24 h."""
    return _change(ok, lambda r: r["bid_2pct_usd"] + r["ask_2pct_usd"])


def vol_trend(ok):
    """% change of the venue's reported 24 h volume, first 24 h vs last 24 h (2026-11-03)."""
    return _change(ok, lambda r: r.get("vol24h_usd"))
