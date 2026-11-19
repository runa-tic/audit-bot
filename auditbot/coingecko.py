"""Intake: CoinGecko or CoinMarketCap link -> coin -> its top whitelisted venues. Used once per coin, not per snapshot."""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import cmc, config, db, venues

API = "https://api.coingecko.com/api/v3"
QUOTES = ("USDT",)  # */USDT only, card and collect-only venues alike (2026-10-30)...
QUOTES_BY_VENUE = {"kraken": ("USDT", "USD")}  # ...except Kraken, which has little USDT: USD there too
LINK = re.compile(r"coingecko\.com/(?:[a-z]{2}(?:-[a-z]{2,4})?/)?coins/([a-z0-9][a-z0-9-]*)", re.I)

_lock = threading.Lock()
_next_at = 0.0
MIN_GAP_S = 2.5  # keyless public API: stay well under its per-minute limit


class IntakeError(Exception):
    """Something the salesperson should be told in plain words."""


def links(text):
    """Coin slugs from every CoinGecko coin link in a message, in order, deduplicated."""
    seen, out = set(), []
    for m in LINK.finditer(text or ""):
        s = m.group(1).lower()
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


CMC_PREFIX = "cmc:"  # a ref from a CMC link, "cmc:<slug>"; a bare ref is a CoinGecko slug or id
CMC_ID = "cmc-"     # coin_id of a coin CoinGecko doesn't list, "cmc-<slug>"


def refs(text):
    """Every coin link in a message, CoinGecko and CoinMarketCap alike, in message order, deduplicated:
    a CoinGecko slug as it is, a CMC slug as "cmc:<slug>"."""
    found = [(m.start(), m.group(1).lower()) for m in LINK.finditer(text or "")]
    found += [(m.start(), CMC_PREFIX + m.group(1).lower()) for m in cmc.LINK.finditer(text or "")]
    out = []
    for _, r in sorted(found):
        if r not in out:
            out.append(r)
    return out


def label(ref):
    """How a ref is shown to the rep: a CMC one as "some-coin (CoinMarketCap)"."""
    return ref[len(CMC_PREFIX):] + " (CoinMarketCap)" if ref.startswith(CMC_PREFIX) else ref


def get(path, params=None, tries=4):
    global _next_at
    url = f"{API}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"User-Agent": venues.UA, "Accept": "application/json"}
    if config.COINGECKO_KEY:
        headers["x-cg-demo-api-key"] = config.COINGECKO_KEY
    for i in range(tries):
        with _lock:
            wait = _next_at - time.time()
            if wait > 0:
                time.sleep(wait)
            _next_at = time.time() + MIN_GAP_S
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429 and i < tries - 1:
                time.sleep(15 * (i + 1))
                continue
            raise
    return None


_COIN_Q = {"localization": "false", "tickers": "false", "market_data": "false", "community_data": "false",
           "developer_data": "false"}
_LIST_TTL_S = 6 * 3600
_list = {"at": 0.0, "coins": []}


def _squash(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _coin_list():
    """CoinGecko's full id/symbol/name list (~23k coins), fetched only when a link's search fails, kept 6 h."""
    if not _list["coins"] or time.time() - _list["at"] > _LIST_TTL_S:
        coins = get("/coins/list")
        if coins:
            _list.update(at=time.time(), coins=coins)
    return _list["coins"]


def lookup(slug, web=True):
    """The CoinGecko coin a slug names, as /coins/<id> returns it.
    web=True: the slug comes from a coingecko.com link, so the coin is the one whose web_slug it is. A web slug is
    usually the API id, but not always: a page /coins/<name> can be the API's <name>-network-token, while the API's
    "<name>" is an unrelated token whose page is /coins/<name>-2 (2026-11-03, an audit that found no exchanges).
    web=False: the slug is an API id (the HTTP API)."""
    c = get(f"/coins/{slug}", _COIN_Q)
    if c is not None and (not web or c.get("web_slug") in (None, "", slug)):
        return c
    hits = (get("/search", {"query": slug.replace("-", " ")}) or {}).get("coins", [])
    if web:
        # A numbered page slug names a coin whose plain name was taken: coingecko.com/en/coins/<name>-2 can be the API's
        # "<name>", and searching "<name> 2" finds nothing (2026-11-03), so the name without the number is searched too.
        base = re.sub(r"-\d+$", "", slug)
        more = (get("/search", {"query": base.replace("-", " ")}) or {}).get("coins", []) if base != slug else []
        seen = set() if c is None else {c["id"]}
        for h in hits[:5] + more[:5]:
            if h["id"] in seen:
                continue
            seen.add(h["id"])
            o = get(f"/coins/{h['id']}", _COIN_Q)
            if o is not None and o.get("web_slug") == slug:
                return o
        # A renamed coin can be missing from search altogether: a page slug from its new name, an API id from its old
        # one, and search finds neither name (2026-11-03). Its name, letters and digits
        # only, still spells the slug, so the coin list is matched by name, and web_slug confirms the match.
        want = {_squash(slug), _squash(base)}
        for e in [e for e in _coin_list() if _squash(e.get("name")) in want and e["id"] not in seen][:5]:
            seen.add(e["id"])
            o = get(f"/coins/{e['id']}", _COIN_Q)
            if o is not None and o.get("web_slug") == slug:
                return o
    if c is not None:
        return c
    hit = next((h for h in hits if h["id"] == slug), None) or (hits[0] if hits else None)
    if not hit:
        raise IntakeError(f"CoinGecko has no coin “{slug}”.")
    c = get(f"/coins/{hit['id']}", _COIN_Q)
    if c is None:
        raise IntakeError(f"CoinGecko has no coin “{slug}”.")
    return c


def resolve(slug, web=True):
    """The coin a slug names (see lookup), stored in coins."""
    c = lookup(slug, web)
    return _store({"coin_id": c["id"], "symbol": (c.get("symbol") or "").upper(), "name": c.get("name") or c["id"],
                   "image_url": (c.get("image") or {}).get("large") or ""})


def _store(row, cmc_slug=None):
    """Upsert a coin row. Keeps shadow_checked_at and the rest; cmc_slug is only ever set, never cleared, so a
    later CoinGecko link for the same coin keeps the CMC slug its pair lookups use."""
    db.run("""INSERT INTO coins (coin_id, symbol, name, image_url, fetched_at, cmc_slug) VALUES (?,?,?,?,?,?)
              ON CONFLICT(coin_id) DO UPDATE SET symbol=excluded.symbol, name=excluded.name,
              image_url=excluded.image_url, fetched_at=excluded.fetched_at,
              cmc_slug=COALESCE(excluded.cmc_slug, coins.cmc_slug)""",
           row["coin_id"], row["symbol"], row["name"], row["image_url"], db.now(), cmc_slug)
    return dict(row, cmc_slug=db.coin(row["coin_id"]).get("cmc_slug"))


# ---------- CoinMarketCap links (2026-11-18)
# A CMC coin is looked for on CoinGecko by what identifies a token: its contract address, then its exact name and
# symbol together. Slugs are never compared across the two sites: CMC's "bonk" is megaBONK (MBONK), not Bonk.
# Found, the audit runs under the CoinGecko coin (one token, one id, whichever link came in), with exchanges from
# both sites. Not found, the coin is cmc-<slug> and its exchanges come from CMC's pair list alone, each behind the
# price guard against CMC's median (candidates). A wrong match would audit another token, a missed one still audits
# the right token, so anything ambiguous counts as not found.

MAX_CONTRACTS = 4  # CoinGecko calls are 2.5 s apart; a coin on 4 chains is still under 15 s
_platforms = {"at": 0.0, "by_chain": {}, "by_name": {}}


def _platform(chain_id, name):
    """CoinGecko's asset platform id for one of CMC's contract platforms: by EVM chain id, else by name
    (Solana: CMC chain id -1, CoinGecko chain_identifier null)."""
    if not _platforms["by_chain"] or time.time() - _platforms["at"] > _LIST_TTL_S:
        ps = get("/asset_platforms") or []
        if ps:
            _platforms.update(at=time.time(),
                              by_chain={p["chain_identifier"]: p["id"] for p in ps if isinstance(p.get("chain_identifier"), int)},
                              by_name={_squash(p.get("name")): p["id"] for p in ps if p.get("name")})
    if isinstance(chain_id, int) and chain_id > 0:
        return _platforms["by_chain"].get(chain_id)
    return _platforms["by_name"].get(_squash(name))


def match_cmc(d):
    """The CoinGecko coin (as /coins/<id> returns it) that is the same token as CMC detail d, or None."""
    sym = d["symbol"].upper()
    hits = {}
    for p in d["platforms"][:MAX_CONTRACTS]:
        plat = _platform(p["chain_id"], p["platform"])
        if not plat:
            continue
        addr = p["address"].lower() if p["address"].startswith("0x") else p["address"]  # base58 is case-sensitive
        c = get(f"/coins/{plat}/contract/{urllib.parse.quote(addr, safe='')}")
        if c and c.get("id") and (c.get("symbol") or "").upper() == sym:
            hits[c["id"]] = c
    if len(hits) == 1:
        return next(iter(hits.values()))
    if hits:  # two CoinGecko coins claim the token's contracts
        return None
    want = _squash(d["name"])
    same = {h["id"] for h in (get("/search", {"query": d["name"]}) or {}).get("coins", [])
            if (h.get("symbol") or "").upper() == sym and _squash(h.get("name")) == want}
    if len(same) != 1:
        return None
    return get(f"/coins/{same.pop()}", _COIN_Q)


def resolve_cmc(slug):
    """The coin a CMC link names, stored in coins with its cmc_slug: the CoinGecko coin when CoinGecko lists the
    same token, else cmc-<slug> with CMC's name, symbol and logo."""
    d = cmc.detail(slug)
    if not d:
        raise IntakeError(f"CoinMarketCap has no coin “{slug}”.")
    c = match_cmc(d)
    if c:
        row = {"coin_id": c["id"], "symbol": (c.get("symbol") or "").upper(), "name": c.get("name") or c["id"],
               "image_url": (c.get("image") or {}).get("large") or d["logo"]}
    else:
        row = {"coin_id": CMC_ID + d["slug"], "symbol": d["symbol"], "name": d["name"], "image_url": d["logo"]}
    return _store(row, d["slug"])


# CoinGecko flags thin or wide markets as anomalies; those are the leads a market maker wants
# (2026-11-03), so they are kept. The flag can also mean a different token under the same
# symbol, so a flagged market's live mid must sit within PRICE_GUARD of the coin's unflagged tickers.
PRICE_GUARD = 0.25
# coins.lookup_rules below this: the coin's venues were looked up under older rules and get one more lookup
# (2 = flagged markets kept, 2026-11-03)
LOOKUP_RULES = 4  # 3 = CoinMarketCap cross-check (2026-11-03); 4 = 1000x pairs + flat CMC slug (2026-11-06)

# Some venues quote a cheap token per 1,000 or per million of it: Binance trades some (~$1e-6 each) only as
# 1000XYZ/USDT, often with far more volume than the plain pair elsewhere (2026-11-06). Such a pair's mid is unit x the coin's price, so the
# price guard and price_diff divide by unit. Depth, spread and volume are in USDT and need nothing.
_UNIT = re.compile(r"^(10{3,}|1M)(.+)$")


def unit(base, symbol):
    """How many of the coin's tokens one unit of a venue's base is: 1000 for 1000XYZ when the coin is XYZ, else 1."""
    m = _UNIT.match((base or "").upper())
    if not m or not symbol or m.group(2) != symbol.upper():
        return 1
    return 10 ** 6 if m.group(1) == "1M" else int(m.group(1))


def candidates(coin_id, venue_ids=None):
    """Venues (default: the card whitelist) for a coin, its */USDT pair on each, by CoinGecko USD volume, best first.
    Each carries anomaly (CoinGecko's flag) and ref (median USD price of the coin's unflagged tickers, or None)."""
    allowed = {venues.CG_ID[v] for v in (venue_ids or config.VENUES) if v in venues.CG_ID}
    # a cmc- coin is one CoinGecko doesn't list: CMC's pairs alone (below)
    data = {} if coin_id.startswith(CMC_ID) else get(f"/coins/{coin_id}/tickers", {"exchange_ids": ",".join(sorted(allowed))}) or {}
    ticks = data.get("tickers", [])
    coin = db.coin(coin_id)
    sym = (coin or {}).get("symbol")
    prices = sorted(float((t.get("converted_last") or {}).get("usd") or 0) for t in ticks
                    if not t.get("is_anomaly") and not t.get("is_stale") and unit(t.get("base"), sym) == 1)
    prices = [x for x in prices if x > 0]
    ref = prices[len(prices) // 2] if prices else None
    best = {}
    for t in ticks:
        cg = t.get("market", {}).get("identifier")
        if cg not in allowed or t.get("is_stale"):  # stale: no recent trades, often a delisted pair
            continue
        venue = venues.VENUES[cg][0]
        if (t.get("target") or "").upper() not in QUOTES_BY_VENUE.get(venue, QUOTES):
            continue
        vol = float((t.get("converted_volume") or {}).get("usd") or 0)
        if venue not in best or vol > best[venue]["volume"]:
            best[venue] = {"venue": venue, "base": t["base"].upper(), "quote": t["target"].upper(), "volume": vol,
                           "anomaly": bool(t.get("is_anomaly")), "ref": ref, "unit": unit(t["base"], sym)}
    # CoinMarketCap cross-check (2026-11-03): venues CMC lists that CoinGecko doesn't. They always pass
    # the price guard (anomaly=True), with CoinGecko's price as the reference.
    if coin:
        listed = cmc.pairs(coin)
        if ref is None:  # CoinGecko lists none of our venues: CMC's own unflagged median, across every exchange
            ps = sorted(p["price"] / unit(p["base"], sym) for p in listed if p["price"] > 0 and not p["outlier"] and p["quote"] in ("USDT", "USD", "USDC"))
            ref = ps[len(ps) // 2] if ps else None
        for p in listed:
            venue = p["venue"]
            if venue not in venues.NAME or venues.CG_ID.get(venue) not in allowed:
                continue
            if venue in best and best[venue].get("source") != "cmc":  # CoinGecko already lists it
                continue
            if venue in best and p["volume"] <= best[venue]["volume"]:
                continue
            if p["quote"] not in QUOTES_BY_VENUE.get(venue, QUOTES):
                continue
            if ref is None:  # nothing to check the price against: a same-symbol token could slip in
                continue
            best[venue] = {"venue": venue, "base": p["base"], "quote": p["quote"], "volume": p["volume"],
                           "anomaly": True, "ref": ref, "source": "cmc", "unit": unit(p["base"], sym)}
    return sorted(best.values(), key=lambda x: -x["volume"])


def probe(c):
    """Live check of one candidate: the pair answers, and a flagged market trades near the coin's price."""
    r = venues.snapshot(c["venue"], c["base"], c["quote"])
    mid = r["mid"] / (c.get("unit") or 1)
    if c.get("anomaly") and c.get("ref") and abs(mid - c["ref"]) / c["ref"] > PRICE_GUARD:
        raise ValueError(f"{c['venue']} {c['base']}/{c['quote']} mid {r['mid']} is far from {c['ref']}: another token?")
    return r


def choose_markets(coin_id):
    """The coin's venues: reused if chosen before, else the top MAX_VENUES that answer a live probe."""
    have = db.markets_for(coin_id)
    if have:
        ensure_shadow(coin_id)
        return [dict(r) for r in have]
    chosen = _probe_top(candidates(coin_id))
    if not chosen and config.SHADOW_VENUES:
        # Fallback (2026-10-30): no card venue lists it, so the collect-only venues become this
        # coin's card venues. Never mixed: a coin with any card venue keeps the collect-only ones out.
        chosen = _probe_top(candidates(coin_id, config.SHADOW_VENUES))
    with db.tx():
        if not db.markets_for(coin_id):
            for i, c in enumerate(chosen):
                # upsert: a fallback venue may already be stored as this coin's collect-only row (a refusal
                # before the fallback existed left one coin's MEXC row so, 2026-10-30); it becomes a card venue
                db.run("""INSERT INTO markets (coin_id, venue, base, quote, cg_volume_usd, rank, chosen_at, shadow)
                          VALUES (?,?,?,?,?,?,?,0)
                          ON CONFLICT(coin_id, venue) DO UPDATE SET base=excluded.base, quote=excluded.quote,
                          cg_volume_usd=excluded.cg_volume_usd, rank=excluded.rank, chosen_at=excluded.chosen_at, shadow=0""",
                       coin_id, c["venue"], c["base"], c["quote"], c["volume"], i, db.now())
    if chosen or db.markets_for(coin_id):  # a refused coin gets no collect-only rows: nothing would collect them
        ensure_shadow(coin_id)
    return [dict(r) for r in db.markets_for(coin_id)]


def _probe_top(cands):
    chosen = []
    for c in cands:
        if len(chosen) >= config.MAX_VENUES:
            break
        try:  # a symbol CoinGecko lists but the venue's API spells differently is skipped, not stored
            probe(c)
        except Exception:
            continue
        chosen.append(c)
    return chosen


def checked_venues():
    """Every venue an intake looks at, for a refusal that says where it looked."""
    label = lambda v: venues.NAME[v] + (" (USD too)" if "USD" in QUOTES_BY_VENUE.get(v, ()) else "")
    return ", ".join(label(v) for v in config.VENUES + config.SHADOW_VENUES if v in venues.NAME)


def ensure_shadow(coin_id):
    """Once per coin: store its pairs on every other supported venue (card and collect-only alike) as
    non-default markets (shadow=1, rank 100+), so a rep can tick any of them and each has data from the
    start. Never raises, never touches the coin's default venues."""
    every = config.VENUES + config.SHADOW_VENUES
    c = db.one("SELECT lookup_rules FROM coins WHERE coin_id = ?", coin_id)
    if not c or (c["lookup_rules"] or 0) >= LOOKUP_RULES:
        return
    try:
        have = {m["venue"] for m in db.all_markets(coin_id)}
        found = []
        for cand in candidates(coin_id, every):
            if cand["venue"] in have or cand["venue"] not in every:
                continue
            try:
                probe(cand)
                found.append(cand)
            except Exception:
                continue
        with db.tx():
            for i, cand in enumerate(found):
                db.run("""INSERT OR IGNORE INTO markets (coin_id, venue, base, quote, cg_volume_usd, rank, chosen_at, shadow)
                          VALUES (?,?,?,?,?,?,?,1)""", coin_id, cand["venue"], cand["base"], cand["quote"], cand["volume"], 100 + i, db.now())
            # running rep audits get a card for each venue found now (2026-11-03), with its own 72 h
            # (jobs.market_due). Only the new ones: a venue the rep unticked in the picker stays off. Ticking every
            # collected venue here re-ticked reps' choices on 13 running audits at the LOOKUP_RULES 4 deploy (2026-11-08).
            for w in db.q("SELECT id FROM watches WHERE coin_id=? AND status='collecting' AND source='bot'", coin_id):
                for cand in found:
                    db.run("INSERT OR IGNORE INTO watch_venues (watch_id, venue) VALUES (?,?)", w["id"], cand["venue"])
            db.run("""UPDATE coins SET all_checked_at = ?, lookup_rules = ?, shadow_checked_at = COALESCE(shadow_checked_at, ?)
                      WHERE coin_id = ?""", db.now(), LOOKUP_RULES, db.now(), coin_id)
    except Exception:
        pass  # CoinGecko down: retried at the next intake or service start


def exchange_image(venue):
    cg = venues.CG_ID.get(venue)
    e = get(f"/exchanges/{cg}") if cg else None
    return (e or {}).get("image") or ""
