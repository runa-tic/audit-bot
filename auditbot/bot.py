"""The Telegram side: access (people.py), link intake, /list, and the root's commands.

Long-polls getUpdates. One token supports one poller, so this bot's token must be used by nothing
else. Intake runs on its own worker so a 10-link message doesn't stall the poll loop.
"""
import html
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone

from . import brand, checks, coingecko, config, db, jobs, people, telegram, venues

log = logging.getLogger("audit.bot")
OFFSET_FILE = os.path.join(config.DATA_DIR, "tg_offset")
intake_q = queue.Queue()
e = html.escape

HELP_MEMBER = (
    "Send me one or more coin links from CoinGecko or CoinMarketCap, e.g.\nhttps://www.coingecko.com/en/coins/bitcoin\n"
    "https://coinmarketcap.com/currencies/bitcoin/\n\n"
    f"I find every supported exchange the token trades on, collect {'hourly' if config.CADENCE_MIN == 60 else f'every {config.CADENCE_MIN} min'} for 3 days, then send one card per exchange "
    "plus a suggested message.\n\n"
    "Each new audit comes with buttons: every exchange gets a card unless you untick it, and you can change the requirements "
    "a card is judged against (that audit only).\n\n"
    "/list — your running and recent audits\n/edit &lt;link or coin id&gt; — the buttons again for a running audit\n"
    "/cancel &lt;link or coin id&gt; — stop one")
HELP_ROOT = HELP_MEMBER + (
    "\n\n<b>Root</b>\n/pending — who is waiting for access\n"
    "/approve &lt;code&gt; &lt;their name&gt; — let someone in; they read you the code the bot gave them\n"
    "/deny &lt;code&gt; — refuse a request\n/people — who has access\n"
    "/revoke &lt;tg id&gt; — take access away (their running audits stop)\n"
    "/reopen &lt;tg id&gt; — let a denied or revoked person ask again\n/list all — every rep's running and last-7-day audits: who, coin, exchanges, started, cards due\n/stats — load")


def when(ts):
    d = datetime.fromtimestamp(ts, timezone.utc)
    return f"{d.day} {d:%b %H:%M} UTC"


def _who(msg):
    u = msg.get("from") or {}
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)
    return str(u.get("id")), u.get("username") or "", name


def handle(update):
    if update.get("callback_query"):
        return on_callback(update["callback_query"])
    msg = update.get("message")
    if not msg or msg.get("chat", {}).get("type") != "private":
        return
    tg_id, username, name = _who(msg)
    text = (msg.get("text") or "").strip()
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower() if cmd.startswith("/") else ""
    arg = arg.strip()

    if cmd == "/claim":
        try:
            people.claim_root(arg, tg_id, username, name)
            return telegram.send(tg_id, "You're root on this bot now.\n\n" + HELP_ROOT)
        except people.PeopleError as ex:
            return telegram.send(tg_id, e(str(ex)))

    if not people.may_use(tg_id):
        return access_request(tg_id, username, name)

    root = people.is_root(tg_id)
    pend = db.one("SELECT * FROM pending_input WHERE tg_id=?", tg_id)
    if pend:
        if not cmd and not coingecko.refs(text) and db.now() - pend["created_at"] < PENDING_S:
            return set_value(tg_id, dict(pend), text)
        db.run("DELETE FROM pending_input WHERE tg_id=?", tg_id)  # moved on: a later number is not an answer
    if cmd == "/edit":
        return edit_cmd(tg_id, arg, root)
    if cmd in ("/start", "/help"):
        return telegram.send(tg_id, HELP_ROOT if root else HELP_MEMBER)
    if cmd == "/list":
        for page in list_pages(None if (root and arg == "all") else tg_id):
            telegram.send(tg_id, page)
        return
    if cmd == "/cancel":
        return cancel(tg_id, arg)
    if root and cmd in ROOT_CMDS:
        try:
            return ROOT_CMDS[cmd](tg_id, arg)
        except people.PeopleError as ex:
            return telegram.send(tg_id, e(str(ex)))
    slugs = coingecko.refs(text)
    if slugs:
        telegram.send(tg_id, f"⏳ Checking {len(slugs)} link{'s' if len(slugs) > 1 else ''}…")
        for s in slugs:
            intake_q.put((tg_id, s))
        return
    if cmd:
        return telegram.send(tg_id, f"{e(cmd)} isn't something I can do for you. /help lists what you can use.")
    telegram.send(tg_id, "Send me a CoinGecko or CoinMarketCap coin link (…coingecko.com/en/coins/&lt;name&gt; or "
                         "…coinmarketcap.com/currencies/&lt;name&gt;). /help for more.")


def access_request(tg_id, username, name):
    status, code, new = people.request(tg_id, username, name)
    if status in ("denied", "revoked"):
        return telegram.send(tg_id, f"Your access to this bot was reviewed and not given. Ask {e(config.OPERATOR_NAME)} if that's a mistake.")
    who_ok = e(config.OPERATOR_NAME)
    team = brand.load()["name"] if config.BRAND_PATH else ""
    head = ((f"This bot is for the {e(team)} sales team." if team else "This bot is for an approved sales team.")
            if new else "You're still waiting for access.")
    telegram.send(tg_id,
                  f"{head} Your access code is <code>{people.pretty(code)}</code>.\n\n"
                  f"Send it to {who_ok} yourself, in person or in a chat you already have. "
                  "Once you're approved I'll tell you here, and you can start sending coin links.")
    if new:
        who = e(name or "?") + (f" (@{e(username)})" if username else "") + f", id <code>{tg_id}</code>"
        jobs.notify_roots(f"🙋 <b>Access request</b>: {who}\nThe bot gave them a code; ask them for it, then:\n"
                          f"<code>/approve &lt;code&gt; &lt;their name&gt;</code>")


def venue_lines(markets):
    """One exchange per line with its pair (2026-10-30)."""
    return "\n".join(f"• {e(venues.NAME.get(m['venue'], m['venue']))} — {e(m['base'])}/{e(m['quote'])}" for m in markets) or "• —"


STATE = {"collecting": "collecting", "delivered": "✅ delivered", "failed": "⚠️ failed", "cancelled": "cancelled"}


def echo_line():
    """The root list's line for the outreach client's running audits ("" when none). Its audits are the API audits
    whose lead is a conversation ("echo:…"); any other API caller's stay in the list as "API caller"."""
    n = db.one("SELECT COUNT(*) n FROM watches WHERE source='api' AND lead LIKE 'echo:%' AND status='collecting'")["n"]
    return f"\n{e(config.API_CLIENT_NAME)}: {n} audit{'s' if n != 1 else ''} running for its follow-ups" if n else ""


def list_pages(owner):
    """Running + last-7-day audits, as Telegram-sized pages. owner=None: every rep's (root)."""
    since = db.now() - 7 * 86400
    if owner:
        rows = db.q("""SELECT * FROM watches WHERE owner=? AND (status='collecting' OR created_at > ?)
                       ORDER BY status!='collecting', ready_at""", owner, since)
    else:
        # the outreach client's audits (source 'api', one per conversation) are counted, not listed: there is one
        # for every contact a follow-up lane reached, and they would bury the reps' own.
        rows = db.q("""SELECT * FROM watches WHERE NOT (source = 'api' AND COALESCE(lead,'') LIKE 'echo:%')
                         AND (status='collecting' OR created_at > ?)
                       ORDER BY status!='collecting', ready_at""", since)
    echo = "" if owner else echo_line()
    if not rows:
        return ["No audits running. Send a CoinGecko or CoinMarketCap link to start one." if owner else "No reps' audits in the last 7 days." + echo]
    running = sum(1 for w in rows if w["status"] == "collecting")
    head = (f"<b>Your audits</b>: {running} running" if owner else
            f"<b>All reps' audits</b>: {running} running, {len(rows) - running} other in the last 7 days" + echo)
    blocks = []
    for w in rows:
        c = db.coin(w["coin_id"]) or {"name": w["coin_id"]}
        state = (f"cards {when(w['ready_at'])}" if w["status"] == "collecting" else
                 f"{STATE.get(w['status'], w['status'])} {when(w['delivered_at'])}" if w["delivered_at"] else
                 STATE.get(w["status"], w["status"]))
        who = "" if owner else (e(people.name_of(w["owner"])) if w["owner"] else "API caller") + " · "
        # /list shows everything collected (2026-10-30): this audit's card venues, then the rest
        picked = db.watch_markets(w["id"])
        cards = venue_lines(picked)
        if w["status"] == "collecting":  # a venue added later has its own due time (jobs.market_due)
            done = jobs.delivered_venues(w["id"])
            if done:
                state = f"✅ delivered {when(w['delivered_at'])}" if w["delivered_at"] else state
            lines = []
            for m in picked:
                line = venue_lines([m])
                due = jobs.market_due(dict(w), dict(m))
                if m["venue"] in done:
                    line += " · ✅ sent"
                elif due > w["ready_at"]:
                    line += f" · card {when(due)}"
                lines.append(line)
            cards = "\n".join(lines)
        block = (f"<b>{e(c['name'])}</b>\n{who}started {when(w['created_at'])} · {state}\n"
                 f"Cards:\n{cards}")
        names = {m["venue"] for m in picked}
        extra = sorted((m for m in db.all_markets(w["coin_id"]) if m["venue"] not in names),
                       key=lambda m: venues.NAME.get(m["venue"], m["venue"]).lower())
        if extra:
            label = "Also collecting (no cards yet)" if w["status"] == "collecting" else "Also collected (no cards)"
            block += f"\n{label}:\n{venue_lines(extra)}"
        blocks.append(block)
    pages, cur = [], head
    for b in blocks:
        if len(cur) + len(b) > 3800:
            pages.append(cur)
            cur = ""
        cur += "\n\n" + b if cur else b
    return pages + [cur]


def coin_ref(tg_id, arg):
    """The coin id a /cancel or /edit argument names: a CoinGecko link's slug, a coin id typed as is, or a CMC link,
    which names the coin stored with that cmc_slug (a CoinGecko id or cmc-<slug>), the rep's running one first."""
    ref = (coingecko.refs(arg) or [arg.strip().lower().lstrip("#")])[0] if arg else ""
    if not ref.startswith(coingecko.CMC_PREFIX):
        return ref
    slug = ref[len(coingecko.CMC_PREFIX):]
    r = db.one("""SELECT c.coin_id FROM coins c LEFT JOIN watches w ON w.coin_id = c.coin_id AND w.owner = ?
                  AND w.status = 'collecting' WHERE c.cmc_slug = ? ORDER BY w.id IS NULL, w.id DESC LIMIT 1""", str(tg_id), slug)
    return r["coin_id"] if r else coingecko.CMC_ID + slug


def cancel(tg_id, arg):
    ref = coin_ref(tg_id, arg)
    if not ref:
        return telegram.send(tg_id, "Which one? /cancel &lt;link or coin id&gt;")
    n = db.run("UPDATE watches SET status='cancelled' WHERE owner=? AND coin_id=? AND status='collecting'", tg_id, ref).rowcount
    telegram.send(tg_id, f"Stopped {ref}." if n else f"You have no running audit for {e(ref)} (see /list).")


def intake_one(tg_id, slug):
    shown = coingecko.label(slug)
    try:
        r = jobs.start_watch(slug, owner=tg_id)
    except coingecko.IntakeError as ex:
        return telegram.send(tg_id, f"❌ {e(shown)}: {e(str(ex))}")
    except Exception:
        log.exception("intake %s failed", slug)
        site = "CoinMarketCap, CoinGecko" if slug.startswith(coingecko.CMC_PREFIX) else "CoinGecko"  # a CMC link asks both
        return telegram.send(tg_id, f"❌ {e(shown)}: {site} or the exchange didn't answer. Send the link again in a few minutes.")
    c, w = r["coin"], r["watch"]
    vs = venue_lines(r["markets"])
    if r["existing"]:
        text, kb = panel(w, f"↩️ <b>{e(c['name'])}</b> is already running for you")
        return telegram.send(tg_id, text, reply_markup=kb)
    text, kb = panel(w, f"✅ <b>{e(c['name'])} ({e(c['symbol'])})</b>: accepted, analytics started")
    telegram.send(tg_id, text, reply_markup=kb)
    # every new audit, to every root but the one who started it (2026-10-30)
    note = (f"🆕 {e(people.name_of(tg_id))} started <b>{e(c['name'])} ({e(c['symbol'])})</b>\n"
            f"{venue_lines(r['picked'])}\nCards arrive {when(w['ready_at'])}")
    for r_id in people.roots():
        if r_id != str(tg_id):
            try:
                telegram.send(r_id, note)
            except Exception:
                log.warning("new-audit notice to root %s failed", r_id)


# ---------- the per-audit buttons: exchanges, then requirements (2026-11-03)

PENDING_S = 1800  # a typed value answers the last ✏️ press for 30 min


def _kb(rows):
    return {"inline_keyboard": rows}


def _b(text, data):
    return {"text": text[:60], "callback_data": data}


def vname(v):
    return venues.NAME.get(v, v)


def panel(w, head=None):
    """The audit's exchange picker: one toggle per venue the coin is collected on, ✅ = gets a card."""
    w = dict(db.one("SELECT * FROM watches WHERE id=?", w["id"]))
    coin = db.coin(w["coin_id"]) or {"name": w["coin_id"], "symbol": ""}
    head = head or f"<b>{e(coin['name'])} ({e(coin['symbol'])})</b>: analytics running"
    picked = {m["venue"] for m in db.watch_markets(w["id"])}
    done = jobs.delivered_venues(w["id"])
    text = (f"{head}\nCards arrive {when(w['ready_at'])}\n\n"
            "Every exchange the coin trades on gets a card (✅). Tap one to drop or re-add its card; all are collected either way. "
            "⚙️ Requirements shows what each card is judged against and lets you change it for this audit.")
    rows = []
    for m in db.all_markets(w["coin_id"]):
        mark = "📨" if m["venue"] in done else "✅" if m["venue"] in picked else "▫️"
        rows.append([_b(f"{mark} {vname(m['venue'])} — {m['base']}/{m['quote']}", f"t:{w['id']}:{m['venue']}")])
    rows.append([_b("⚙️ Requirements", f"r:{w['id']}"), _b("✖️ Close", f"x:{w['id']}")])
    return text, _kb(rows)


def effective(w, venue):
    """[(published check, this audit's check or None if switched off)] for one venue."""
    own = db.watch_reqs(w).get(venue, {})
    eff = {c["id"]: c for c in config.requirements(venue, own)}
    return [(c, eff.get(c["id"])) for c in config.requirements(venue)]


def req_view(w):
    coin = db.coin(w["coin_id"]) or {"name": w["coin_id"]}
    blocks, rows = [], []
    for m in db.watch_markets(w["id"]):
        v, q = m["venue"], m["quote"]
        pairs = effective(w, v)
        lines = [f"• {e(checks.line(x, q))}" if x else f"• {e(checks.label(c))}: off" for c, x in pairs]
        blocks.append(f"<b>{e(vname(v))}</b>\n" + ("\n".join(lines) or "• no requirements set: numbers only, no verdict"))
        if pairs:
            rows.append([_b(f"✏️ {vname(v)}", f"rv:{w['id']}:{v}")])
    rows.append([_b("← Exchanges", f"p:{w['id']}"), _b("✖️ Close", f"x:{w['id']}")])
    return (f"<b>Requirements · {e(coin['name'])}</b> (this audit only)\n\n" + "\n\n".join(blocks)), _kb(rows)


def venue_view(w, venue):
    m = db.market(w["coin_id"], venue)
    q = m["quote"] if m else "USDT"
    pairs = effective(w, venue)
    own = db.watch_reqs(w).get(venue, {})
    rows = [[_b(f"{checks.label(c)}: " + (checks.req_text(x, q) if x else "off"), f"re:{w['id']}:{venue}:{c['id']}")]
            for c, x in pairs]
    if own:
        rows.append([_b(f"↺ {vname(venue)}'s published numbers", f"rd:{w['id']}:{venue}")])
    rows.append([_b("← Requirements", f"r:{w['id']}"), _b("✖️ Close", f"x:{w['id']}")])
    lines = [f"• {e(checks.line(x, q))}" if x else f"• {e(checks.label(c))}: off" for c, x in pairs]
    return (f"<b>{e(vname(venue))}</b> requirements for this audit\n" + "\n".join(lines) +
            "\n\nTap one to change it."), _kb(rows)


def _owned(tg_id, wid):
    w = db.one("SELECT * FROM watches WHERE id=?", wid)
    if not w or (w["owner"] != str(tg_id) and not people.is_root(tg_id)):
        return None
    return dict(w)


def close_view(w):
    """The panel folded away: what this audit will send, no buttons."""
    coin = db.coin(w["coin_id"]) or {"name": w["coin_id"], "symbol": ""}
    picked = db.watch_markets(w["id"])
    own = db.watch_reqs(w)
    changed = [vname(v) for v in own if v in {m["venue"] for m in picked}]
    return (f"<b>{e(coin['name'])} ({e(coin['symbol'])})</b>: cards from\n{venue_lines(picked)}\n"
            f"Requirements: {'changed for ' + e(', '.join(changed)) if changed else 'published numbers'}\n"
            f"Cards arrive {when(w['ready_at'])}\n\n/edit {e(w['coin_id'])} opens the buttons again."), None


def _safe(what, fn, *a):
    try:
        return fn(*a)
    except Exception as ex:
        log.warning("%s failed: %s", what, ex)


def on_callback(cb):
    """A button press. The press is answered first (Telegram expires an unanswered one within seconds and the
    rep sees a spinner), then the message is edited; neither failure stops the other."""
    t0 = time.time()
    tg_id = str((cb.get("from") or {}).get("id"))
    msg = cb.get("message") or {}
    chat, mid = (msg.get("chat") or {}).get("id"), msg.get("message_id")
    data = cb.get("data") or ""
    toast, view, send = _decide(tg_id, data)
    _safe("answerCallbackQuery", telegram.answer, cb["id"], toast)
    if view:
        _safe("editMessageText", telegram.edit, chat, mid, *view)
    if send:
        _safe("sendMessage", lambda: telegram.send(chat, send[0], reply_markup=send[1]))
    log.info("button %s from %s: %s (%.2f s)", data, tg_id, toast or "ok", time.time() - t0)


def _decide(tg_id, data):
    """-> (toast or None, (text, markup) to edit the pressed message to, or None, (text, markup) to send, or None).
    Database work only: fast, so the press is answered in time."""
    if not people.may_use(tg_id):
        return "You don't have access to this bot.", None, None
    parts = data.split(":")
    try:
        op, wid = parts[0], int(parts[1])
    except (IndexError, ValueError):
        return None, None, None
    w = _owned(tg_id, wid)
    if not w:
        return "That audit isn't yours.", None, None
    if op == "x":
        db.run("DELETE FROM pending_input WHERE tg_id=?", tg_id)
        return None, close_view(w), None
    if w["status"] != "collecting":
        return "This audit has finished; its buttons no longer change anything.", None, None
    venue = parts[2] if len(parts) > 2 else None
    if op == "t":
        picked = {m["venue"] for m in db.watch_markets(wid)}
        if not db.market(w["coin_id"], venue):
            return None, None, None
        if venue in jobs.delivered_venues(wid):
            return f"The {vname(venue)} card is already sent.", None, None
        if venue in picked:
            if len(picked) == 1:
                return "Keep at least one exchange.", None, None
            db.run("DELETE FROM watch_venues WHERE watch_id=? AND venue=?", wid, venue)
            note = f"{vname(venue)}: no card"
        else:
            db.run("INSERT OR IGNORE INTO watch_venues (watch_id, venue) VALUES (?,?)", wid, venue)
            note = f"{vname(venue)}: card added"
        return note, panel(w), None
    if op == "p":
        return None, panel(w), None
    if op == "r":
        return None, req_view(w), None
    if op == "rv" and venue:
        return None, venue_view(w, venue), None
    if op == "rd" and venue:
        reqs = db.watch_reqs(w)
        reqs.pop(venue, None)
        db.run("UPDATE watches SET reqs=? WHERE id=?", db.dumps(reqs), wid)
        return (f"Back to {vname(venue)}'s published numbers",
                venue_view(dict(db.one("SELECT * FROM watches WHERE id=?", wid)), venue), None)
    if op == "re" and venue and len(parts) > 3:
        pairs = dict((c["id"], (c, x)) for c, x in effective(w, venue))
        if parts[3] not in pairs:
            return None, None, None
        c, x = pairs[parts[3]]
        m = db.market(w["coin_id"], venue)
        q = m["quote"] if m else "USDT"
        db.run("""INSERT INTO pending_input (tg_id, watch_id, venue, check_id, created_at) VALUES (?,?,?,?,?)
                  ON CONFLICT(tg_id) DO UPDATE SET watch_id=excluded.watch_id, venue=excluded.venue,
                  check_id=excluded.check_id, created_at=excluded.created_at""", tg_id, wid, venue, c["id"], db.now())
        shown = x or dict(c, custom=False)
        text = checks.prompt(shown, vname(venue), q)
        if not x:
            text = text.replace(f"(now {checks.req_text(shown, q)})", "(now off)")
        text += "\nOr <code>cancel</code> to leave it as it is."
        return "Type the new value", None, (text, {"force_reply": True, "input_field_placeholder": "a number, off, or cancel"})
    return None, None, None


def set_value(tg_id, pend, text):
    if text.strip().lower() in ("cancel", "exit", "stop", "back"):
        db.run("DELETE FROM pending_input WHERE tg_id=?", tg_id)
        return telegram.send(tg_id, "Nothing changed.")
    try:
        v = checks.parse(text)
    except checks.BadValue as ex:
        return telegram.send(tg_id, f"{e(str(ex))} Or <code>cancel</code>.")
    w = _owned(tg_id, pend["watch_id"])
    if not w or w["status"] != "collecting":
        db.run("DELETE FROM pending_input WHERE tg_id=?", tg_id)
        return telegram.send(tg_id, "That audit has finished, so nothing changed.")
    venue, cid = pend["venue"], pend["check_id"]
    default = next((c for c in config.requirements(venue) if c["id"] == cid), None)
    if not default:
        db.run("DELETE FROM pending_input WHERE tg_id=?", tg_id)
        return telegram.send(tg_id, "That requirement no longer exists, so nothing changed.")
    reqs = db.watch_reqs(w)
    own = reqs.setdefault(venue, {})
    if v is not None and v == checks.limit(default):
        own.pop(cid, None)  # back to the published number
    else:
        own[cid] = v
    if not own:
        reqs.pop(venue)
    with db.tx():
        db.run("UPDATE watches SET reqs=? WHERE id=?", db.dumps(reqs), w["id"])
        db.run("DELETE FROM pending_input WHERE tg_id=?", tg_id)
    w = dict(db.one("SELECT * FROM watches WHERE id=?", w["id"]))
    m = db.market(w["coin_id"], venue)
    q = m["quote"] if m else "USDT"
    eff = next((x for c, x in effective(w, venue) if c["id"] == cid), None)
    said = e(checks.line(eff, q)) if eff else f"{e(checks.label(default))}: off (left off the card)"
    text, kb = venue_view(w, venue)
    telegram.send(tg_id, f"Saved for this audit · {e(vname(venue))}\n{said}\n\n" + text, reply_markup=kb)


def edit_cmd(tg_id, arg, root):
    ref = coin_ref(tg_id, arg)
    if not ref:
        return telegram.send(tg_id, "Which one? /edit &lt;link or coin id&gt;")
    w = db.one("SELECT * FROM watches WHERE owner=? AND coin_id=? AND status='collecting' ORDER BY id DESC LIMIT 1", tg_id, ref)
    if not w and root and ref.isdigit():
        w = db.one("SELECT * FROM watches WHERE id=? AND status='collecting'", int(ref))
    if not w:
        return telegram.send(tg_id, f"You have no running audit for {e(ref)} (see /list).")
    text, kb = panel(dict(w))
    telegram.send(tg_id, text, reply_markup=kb)


def intake_worker(stop):
    while not stop.is_set():
        try:
            tg_id, slug = intake_q.get(timeout=1)
        except queue.Empty:
            continue
        intake_one(tg_id, slug)


def r_pending(tg_id, _):
    ps = people.pending()
    telegram.send(tg_id, "\n".join(f"• {e(p['asked_name'] or '?')}" + (f" @{e(p['username'])}" if p["username"] else "")
                                   + f" · id {p['tg_id']} · since {when(p['created_at'])}" for p in ps)
                  or "Nobody is waiting.")


def r_approve(tg_id, arg):
    code, _, name = arg.partition(" ")
    p = people.approve(code, name, tg_id)
    telegram.send(tg_id, f"✅ {e(p['name'])} is in (Telegram profile: {e(p['asked_name'] or '?')}"
                         + (f" @{e(p['username'])}" if p["username"] else "") + ").")
    telegram.send(p["tg_id"], "You're in. " + HELP_MEMBER)


def r_deny(tg_id, arg):
    p = people.deny(arg, tg_id)
    telegram.send(tg_id, f"Refused {e(p['asked_name'] or p['tg_id'])}.")


def r_people(tg_id, _):
    ms = people.members()
    lines = [f"• {e(m['name'] or m['asked_name'])}" + (f" @{e(m['username'])}" if m["username"] else "")
             + f" · id {m['tg_id']}" + (" · root" if m["role"] == "root" else "") for m in ms]
    env = [f"• id {r} · root (server env)" for r in sorted(config.ROOT_TG_IDS) if not people.get(r)]
    telegram.send(tg_id, "\n".join(env + lines) or "Nobody yet.")


def r_revoke(tg_id, arg):
    p = people.revoke(arg, tg_id)
    telegram.send(tg_id, f"Revoked {e(p['name'] or p['tg_id'])}; their running audits stopped.")


def r_reopen(tg_id, arg):
    people.reopen(arg, tg_id)
    telegram.send(tg_id, "Reopened: their next /start gives them a new code.")


def r_stats(tg_id, _):
    reps = db.one("SELECT COUNT(*) n FROM watches WHERE status='collecting' AND source != 'api'")["n"]
    api = db.one("SELECT COUNT(*) n FROM watches WHERE status='collecting' AND source = 'api'")["n"]
    mk = len(db.active_markets())
    day = db.one("SELECT COUNT(*) n, SUM(error IS NOT NULL) e FROM snapshots WHERE ts > ?", db.now() - 86400)
    telegram.send(tg_id, f"Running audits: {reps} reps' (limit {config.MAX_ACTIVE_TOTAL}) + {api} API (limit {config.MAX_ACTIVE_API})\n"
                         f"Markets collected: {mk} every {config.CADENCE_MIN} min\n"
                         f"Snapshots 24 h: {day['n']} ({day['e'] or 0} errors)\nVenues: {', '.join(config.VENUES)}")


ROOT_CMDS = {"/pending": r_pending, "/approve": r_approve, "/deny": r_deny, "/people": r_people,
             "/revoke": r_revoke, "/reopen": r_reopen, "/stats": r_stats}


def poll(stop):
    try:
        offset = int(open(OFFSET_FILE).read().strip())
    except Exception:
        offset = 0
    while not stop.is_set():
        try:
            updates = telegram.call("getUpdates", http_timeout=20, timeout=10, offset=offset, allowed_updates=["message", "callback_query"])
        except Exception as ex:
            log.warning("getUpdates: %s", ex)
            stop.wait(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            with open(OFFSET_FILE, "w") as f:  # before handling: a crash skips one update, never loops on it
                f.write(str(offset))
            t0 = time.time()
            try:
                handle(u)
            except Exception:
                log.exception("update %s failed", u.get("update_id"))
            if time.time() - t0 > 3:  # the poll loop is serial: a slow update delays every press behind it
                log.warning("update %s (%s) took %.1f s", u.get("update_id"), ",".join(k for k in u if k != "update_id"),
                            time.time() - t0)
