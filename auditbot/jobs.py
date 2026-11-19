"""Watches, the collector and day-3 delivery. Shared by the bot, the API and the admin CLI."""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from . import coingecko, config, db, message, people, render, telegram, venues

log = logging.getLogger("audit")


def store_snapshot(coin_id, m, ts=None):
    ts = ts or db.now()
    pair = f'{m["base"]}/{m["quote"]}'
    try:
        r = venues.snapshot(m["venue"], m["base"], m["quote"])
        db.run("""INSERT INTO snapshots (coin_id, venue, pair, ts, mid, best_bid, best_ask, spread_pct,
                  bid_2pct_usd, ask_2pct_usd, trunc_bid, trunc_ask, vol24h_usd, error, book) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)""",
               coin_id, m["venue"], pair, ts, r["mid"], r["best_bid"], r["best_ask"], r["spread_pct"],
               r["bid_2pct_usd"], r["ask_2pct_usd"], r["trunc_bid"], r["trunc_ask"], r["vol24h_usd"],
               db.dumps(r["book"]) if r.get("book") else None)
        return True
    except Exception as e:  # recorded, never fatal: one venue down must not cost the others
        db.run("INSERT INTO snapshots (coin_id, venue, pair, ts, error) VALUES (?,?,?,?,?)",
               coin_id, m["venue"], pair, ts, f"{type(e).__name__}: {e}"[:200])
        return False


class CapacityError(coingecko.IntakeError):
    """The bot is full right now: not a verdict on the coin. The API answers 503 (ask again later), never 422."""


def start_watch(ref, owner=None, lead=None, source="bot"):
    """ref: a CoinGecko slug or coin id, or "cmc:<slug>" from a CoinMarketCap link. -> dict(coin, markets, watch, existing). Raises IntakeError
    (CapacityError when the bot is full). Reps' (bot) and the API client's (api) audits have their own caps."""
    api = source == "api"
    if api:
        # The same caller asking again (a retry after a lost answer) gets the audit it already has,
        # before any cap: a full bot must not cost it the handle it was given.
        # An outreach conversation ("echo:<id>") has one audit, whatever spelling of the coin it sent; any
        # other caller's lead is only unique together with the coin.
        if lead and lead.startswith("echo:"):
            w = db.one("""SELECT * FROM watches WHERE source='api' AND lead=? AND ready_at + ? > ?
                          ORDER BY id DESC LIMIT 1""", lead, config.API_GRACE_H * 3600, db.now())
        else:
            w = db.one("""SELECT * FROM watches WHERE coin_id=? AND source='api' AND COALESCE(lead,'')=?
                          AND ready_at + ? > ?""", str(ref).lower(), lead or "", config.API_GRACE_H * 3600, db.now())
        if w:
            picked = [dict(m) for m in db.watch_markets(w["id"])]
            return {"coin": db.coin(w["coin_id"]) or {"coin_id": w["coin_id"]}, "markets": picked, "watch": dict(w),
                    "existing": True, "picked": picked}
    cap = config.MAX_ACTIVE_API if api else config.MAX_ACTIVE_TOTAL
    active = db.one("SELECT COUNT(*) n FROM watches WHERE status='collecting' AND source " + ("= 'api'" if api else "!= 'api'"))["n"]
    if active >= cap:
        raise CapacityError(f"The API's audits are at their limit of {cap} running; try again later." if api
                            else f"The bot is at its limit of {cap} running audits; try again later.")
    if owner and db.one("SELECT COUNT(*) n FROM watches WHERE owner=? AND status='collecting'", str(owner))["n"] >= config.MAX_ACTIVE_PER_PERSON:
        raise CapacityError(f"You have {config.MAX_ACTIVE_PER_PERSON} audits running, the per-person limit.")
    coin = db.coin(ref) if source == "api" else None
    if not coin:
        coin = (coingecko.resolve_cmc(ref[len(coingecko.CMC_PREFIX):]) if ref.startswith(coingecko.CMC_PREFIX)
                else coingecko.resolve(ref, web=source != "api"))
    cid = coin["coin_id"]
    markets = coingecko.choose_markets(cid)
    if not markets:
        raise coingecko.IntakeError(
            f"{coin['name']} has no USDT pair on any supported exchange "
            f"(checked: {coingecko.checked_venues()}).")
    ts = db.now()
    for m in db.all_markets(cid):  # a first row now, so the window starts at intake rather than the next tick
        last = db.last_ts(cid, m["venue"])
        if not last or ts - last > config.CADENCE_MIN * 60:
            store_snapshot(cid, m, ts)
    with db.tx():
        if source == "api":
            w = db.one("""SELECT * FROM watches WHERE coin_id=? AND source='api' AND COALESCE(lead,'')=?
                          AND ready_at + ? > ?""", cid, lead or "", config.API_GRACE_H * 3600, db.now())
        else:
            w = db.one("SELECT * FROM watches WHERE coin_id=? AND owner=? AND status='collecting'", cid, str(owner))
        existing = w is not None
        if not w:
            t = db.now()
            cur = db.run("""INSERT INTO watches (coin_id, owner, lead, source, created_at, ready_at, status)
                            VALUES (?,?,?,?,?,?,'collecting')""",
                         cid, str(owner) if owner else None, lead, source, t, t + config.WINDOW_H * 3600)
            # bot audits: every collected venue is ticked (2026-11-03), not only
            # the top 3; the rep unticks with the buttons (bot.panel). API audits keep the top 3.
            for m in (db.all_markets(cid) if source == "bot" else markets):
                db.run("INSERT OR IGNORE INTO watch_venues (watch_id, venue) VALUES (?,?)", cur.lastrowid, m["venue"])
            w = db.one("SELECT * FROM watches WHERE id=?", cur.lastrowid)
    return {"coin": coin, "markets": markets, "watch": dict(w), "existing": existing,
            "picked": [dict(m) for m in db.watch_markets(w["id"])]}


def collect_once():
    rows = [dict(r) for r in db.active_markets()]
    if not rows:
        return 0, 0
    ts = db.now()
    with ThreadPoolExecutor(max_workers=6) as ex:
        ok = sum(ex.map(lambda m: store_snapshot(m["coin_id"], m, ts), rows))
    log.info("collected %d/%d markets", ok, len(rows))
    return ok, len(rows)


def card_caption(coin, m, s):
    return (f'<b>{m["base"]}/{m["quote"]} · {venues.NAME.get(m["venue"], m["venue"])}</b> — {coin["name"]}\n'
            f'{render.period(s["from"], s["to"])}')


def market_due(w, m):
    """A card is due at the watch's ready time, or 72 h after its venue joined, whichever is later: a venue
    added to a running audit (admin add-venue) gets its own full window, never a partial card."""
    return max(w["ready_at"], (m["chosen_at"] or 0) + config.WINDOW_H * 3600)


def delivered_venues(watch_id):
    return {r["venue"] for r in db.q("SELECT venue FROM deliveries WHERE watch_id = ?", watch_id)}


def deliver(w, due, first):
    """Send the due venues' cards for one bot watch: the first batch with the suggested message, a venue
    that joined later as a short follow-up. Records each venue as delivered."""
    coin = db.coin(w["coin_id"])
    own = db.watch_reqs(w)
    cards = []
    for m in due:
        out = render.render(w["coin_id"], m["venue"], config.requirements(m["venue"], own.get(m["venue"])))
        if out:
            cards.append((out[0], dict(m), out[1]))
    t = db.now()
    if cards and first:
        telegram.send_photos(w["owner"], [(p, card_caption(coin, m, s)) for p, m, s in cards])
        telegram.send(w["owner"], message.suggested(coin, [(m, s) for _, m, s in cards]))
    elif cards:
        telegram.send_photos(w["owner"], [(p, f'<b>{venues.NAME.get(m["venue"], m["venue"])} card for {coin["name"]} '
                                                f'({coin["symbol"]})</b>\n' + card_caption(coin, m, s)) for p, m, s in cards])
    elif first:
        telegram.send(w["owner"], f"⚠️ No usable data was collected for <b>{coin['name']}</b> in 3 days, so there is no card. "
                                  f"The exchanges' public APIs refused every snapshot; {config.OPERATOR_NAME} has been told.")
        notify_roots(f"⚠️ Audit {w['id']} ({w['coin_id']}) for {people.name_of(w['owner'])} failed: no data collected.")
    else:
        notify_roots(f"⚠️ Audit {w['id']} ({w['coin_id']}): no data for the late venue(s) "
                     f"{', '.join(m['venue'] for m in due)}; nothing sent to {people.name_of(w['owner'])}.")
    with db.tx():
        for m in due:
            db.run("INSERT OR IGNORE INTO deliveries (watch_id, venue, delivered_at) VALUES (?,?,?)", w["id"], m["venue"], t)
        if first:
            db.run("UPDATE watches SET delivered_at=?, note=? WHERE id=?", t, None if cards else "no data collected", w["id"])
    return bool(cards)


def deliver_due():
    now = db.now()
    for w in db.q("SELECT * FROM watches WHERE source='bot' AND status='collecting' AND ready_at <= ? ORDER BY ready_at", now):
        w = dict(w)
        try:
            done = delivered_venues(w["id"])
            todo = [dict(m) for m in db.watch_markets(w["id"]) if m["venue"] not in done]
            due = [m for m in todo if market_due(w, m) <= now]
            if due:
                deliver(w, due, first=not done)
            if len(due) == len(todo):  # every venue's card is out: stop collecting for this watch
                note = db.one("SELECT note FROM watches WHERE id=?", w["id"])["note"]
                db.run("UPDATE watches SET status=? WHERE id=?", "failed" if note == "no data collected" else "delivered", w["id"])
        except Exception as e:
            n = w["attempts"] + 1
            log.exception("delivery %s failed (attempt %d)", w["id"], n)
            if n >= 5:
                db.run("UPDATE watches SET status='failed', attempts=?, note=? WHERE id=?", n, str(e)[:200], w["id"])
                notify_roots(f"⚠️ Audit {w['id']} ({w['coin_id']}) for {people.name_of(w['owner'])} failed 5 times: {str(e)[:150]}")
            else:
                db.run("UPDATE watches SET attempts=? WHERE id=?", n, w["id"])


def close_api_watches():
    """An API audit has no Telegram delivery to end it. It ends when its window and
    the grace after it are over (AUDIT_API_GRACE_H). Then it stops being collected and stops counting
    toward MAX_ACTIVE_API. Its card can still be fetched: render reads the 72 h up to the venue's last snapshot."""
    t = db.now()
    n = db.run("""UPDATE watches SET status='delivered', delivered_at=?, note=COALESCE(note, 'api window closed')
                  WHERE source='api' AND status='collecting' AND ready_at + ? <= ?""",
               t, config.API_GRACE_H * 3600, t).rowcount
    if n:
        log.info("closed %d API audit(s) past their window and grace", n)
    return n


def add_venue(coin_id, venue, base, quote):
    """Admin: add a card venue to a coin that is already watched (its running audits get that venue's card
    when it has its own full window, see market_due). Probes the pair and takes the first snapshot."""
    if venue not in config.VENUES:
        raise ValueError(f"{venue} is not a card venue ({', '.join(config.VENUES)})")
    if not db.coin(coin_id):
        raise ValueError(f"unknown coin {coin_id}: it has never been through intake")
    venues.snapshot(venue, base, quote)  # raises if the venue doesn't answer for that pair
    with db.tx():
        rank = (db.one("SELECT MAX(rank) r FROM markets WHERE coin_id=? AND shadow=0", coin_id)["r"] or 0) + 1
        db.run("""INSERT INTO markets (coin_id, venue, base, quote, cg_volume_usd, rank, chosen_at, shadow)
                  VALUES (?,?,?,?,NULL,?,?,0)
                  ON CONFLICT(coin_id, venue) DO UPDATE SET base=excluded.base, quote=excluded.quote,
                  rank=excluded.rank, chosen_at=excluded.chosen_at, shadow=0""",
               coin_id, venue, base.upper(), quote.upper(), rank, db.now())
        for w in db.q("SELECT id FROM watches WHERE coin_id=? AND status='collecting'", coin_id):
            db.run("INSERT OR IGNORE INTO watch_venues (watch_id, venue) VALUES (?,?)", w["id"], venue)
    m = db.market(coin_id, venue)
    store_snapshot(coin_id, m)
    return m


def notify_roots(text):
    for r in people.roots():
        try:
            telegram.send(r, text)
        except Exception:
            log.warning("could not notify root %s", r)


def scheduler(stop):
    """Collect on cadence boundaries (wall clock, UTC), check deliveries every minute."""
    try:  # coins watched before shadow venues existed get theirs once (CoinGecko-paced, a few seconds each)
        for r in db.q("""SELECT DISTINCT c.coin_id FROM coins c JOIN watches w ON w.coin_id = c.coin_id
                         WHERE w.status = 'collecting' AND COALESCE(c.lookup_rules, 0) < ?""", coingecko.LOOKUP_RULES):
            coingecko.ensure_shadow(r["coin_id"])
    except Exception:
        log.exception("shadow backfill failed")
    width = config.CADENCE_MIN * 60
    last = db.one("SELECT MAX(ts) t FROM snapshots WHERE error IS NULL")["t"]
    # A restart inside a slot that already has data must not collect again: unattended-upgrades'
    # needrestart bounces the unit after every batch, and each extra row would over-weight its minute.
    last_slot = int(last // width) if last and int(last // width) == int(time.time() // width) else None
    while not stop.is_set():
        slot = int(time.time() // (config.CADENCE_MIN * 60))
        if slot != last_slot:
            last_slot = slot
            try:
                collect_once()
            except Exception:
                log.exception("collect failed")
        try:
            deliver_due()
        except Exception:
            log.exception("deliver_due failed")
        try:
            close_api_watches()
        except Exception:
            log.exception("close_api_watches failed")
        stop.wait(60 - time.time() % 60 + 1)
