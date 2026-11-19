import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

TMP = tempfile.mkdtemp(prefix="auditbot-test-")
os.environ.update(AUDIT_DATA=TMP, AUDIT_API_TOKEN="t0k", AUDIT_API_BIND="127.0.0.1:18089",
                  AUDIT_ROOT_TG_IDS="111", AUDIT_BOT_TOKEN="")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auditbot import api, bot, brand, checks, cmc, coingecko, config, db, jobs, metrics, people, render, telegram, venues  # noqa: E402

REAL_CMC_PAIRS = cmc.pairs
cmc.pairs = lambda coin: []  # never the network; CmcCrossCheck sets its own
SENT = []
telegram.send = lambda chat, text, **kw: SENT.append((str(chat), text))
telegram.send_photos = lambda chat, items: SENT.append((str(chat), [p for p, _ in items]))
EDITS, ANSWERS, MARKUP = [], [], {}
_send = telegram.send
telegram.send = lambda chat, text, **kw: (SENT.append((str(chat), text)), MARKUP.__setitem__(str(chat), kw.get("reply_markup")))[0]
telegram.edit = lambda chat, mid, text, reply_markup=None: EDITS.append((str(chat), text, reply_markup))
telegram.answer = lambda cid, text=None: ANSWERS.append(text)
venues.snapshot = lambda v, b, q: {"mid": 1.0, "best_bid": 0.999, "best_ask": 1.001, "spread_pct": 0.2,
                                   "bid_2pct_usd": 1000.0, "ask_2pct_usd": 900.0, "trunc_bid": 0, "trunc_ask": 0,
                                   "vol24h_usd": 5000.0}
COINS = {"alpha": {"coin_id": "alpha", "symbol": "ALPHA", "name": "Alpha", "image_url": ""},
         "dexonly": {"coin_id": "dexonly", "symbol": "DX", "name": "Dex Only", "image_url": ""},
         "mexconly": {"coin_id": "mexconly", "symbol": "MXO", "name": "Mexc Only", "image_url": ""}}


REAL_LOOKUP = coingecko.lookup


def fake_resolve(slug, web=True):
    c = COINS.get(slug)
    if not c:
        raise coingecko.IntakeError(f"CoinGecko has no coin “{slug}”.")
    db.run("""INSERT INTO coins (coin_id, symbol, name, image_url, fetched_at) VALUES (?,?,?,?,?)
              ON CONFLICT(coin_id) DO UPDATE SET fetched_at=excluded.fetched_at""", c["coin_id"], c["symbol"], c["name"], "", db.now())
    return c


coingecko.resolve = fake_resolve
SHADOW_CANDS = [{"venue": "mexc", "base": "ALPHA", "quote": "USDT", "volume": 99000},  # bigger than any card venue
                {"venue": "htx", "base": "ALPHA", "quote": "USDT", "volume": 500}]


def fake_candidates(cid, venue_ids=None):
    if cid == "dexonly":
        return []
    if cid == "mexconly":
        return [{"venue": "mexc", "base": "MXO", "quote": "USDT", "volume": 69000}] if venue_ids and "mexc" in venue_ids else []
    if venue_ids:
        return [c for c in MAIN_CANDS + SHADOW_CANDS if c["venue"] in venue_ids]
    return MAIN_CANDS


REAL_CANDIDATES = coingecko.candidates
coingecko.candidates = fake_candidates
MAIN_CANDS = [
    {"venue": "okx", "base": "ALPHA", "quote": "USDT", "volume": 25000},
    {"venue": "gate", "base": "ALPHA", "quote": "USDT", "volume": 8000},
    {"venue": "kucoin", "base": "ALPHA", "quote": "USDT", "volume": 3400},
    {"venue": "bybit", "base": "ALPHA", "quote": "USDT", "volume": 100}]


def row(ts, bid=1000, ask=900, spread=0.1, vol=5000, err=None):
    return {"ts": ts, "bid_2pct_usd": bid, "ask_2pct_usd": ask, "spread_pct": spread, "vol24h_usd": vol,
            "trunc_bid": 0, "trunc_ask": 0, "error": err}


class Links(unittest.TestCase):
    def test_parse(self):
        t = ("look https://www.coingecko.com/en/coins/alpha and coingecko.com/coins/the-graph "
             "https://www.coingecko.com/ru/coins/alpha https://www.coingecko.com/zh-tw/coins/pepe x.com/coins/no")
        self.assertEqual(coingecko.links(t), ["alpha", "the-graph", "pepe"])
        self.assertEqual(coingecko.links("no links"), [])


class Metrics(unittest.TestCase):
    TH = [{"id": "depth2", "kind": "depth", "band": 2, "side": "each", "min": 1000},
          {"id": "spread", "kind": "spread", "max": 0.2}, {"id": "vol", "kind": "volume", "min": 4000}]

    def test_averages_and_now(self):
        rows = [row(i * 3600, bid=1000 + i, spread=0.1) for i in range(73)] + [row(73 * 3600, err="x")]
        s = metrics.summarize(rows, self.TH)
        self.assertEqual(s["n"], 73)
        self.assertAlmostEqual(s["bid_avg"], 1036)
        self.assertEqual(s["bid_now"], 1072)
        d2 = metrics.check(s, "depth2")
        self.assertTrue(d2["ok_bid"])
        self.assertFalse(d2["ok_ask"])  # 900 < 1000
        self.assertFalse(d2["ok"])
        self.assertTrue(metrics.check(s, "spread")["ok"] and metrics.check(s, "vol")["ok"])
        self.assertFalse(s["all_ok"])

    def test_trend_needs_48h(self):
        self.assertIsNone(metrics.summarize([row(i * 3600) for i in range(40)], self.TH)["trend"])
        rows = [row(i * 3600, bid=1000 if i < 36 else 500, ask=1000 if i < 36 else 500) for i in range(73)]
        s = metrics.summarize(rows, self.TH)
        self.assertAlmostEqual(s["trend"], -50)
        self.assertEqual(s["trend_state"], "declining")

    def test_vol_trend(self):
        self.assertIsNone(metrics.summarize([row(i * 3600) for i in range(40)], self.TH)["vol_trend"])
        rows = [row(i * 3600, vol=4000 if i < 36 else 6000) for i in range(73)]
        s = metrics.summarize(rows, self.TH)
        self.assertAlmostEqual(s["vol_trend"], 50)
        self.assertEqual(s["vol_trend_state"], "improving")
        self.assertEqual(s["trend_state"], "stable")  # depth untouched
        rows = [row(i * 3600, vol=None if i < 36 else 6000) for i in range(73)]
        self.assertIsNone(metrics.summarize(rows, self.TH)["vol_trend"])  # no first-day volume, no verdict

    def test_window_is_last_72h(self):
        rows = [row(i * 3600, bid=1) for i in range(24)] + [row((24 + i) * 3600, bid=2000) for i in range(73)]
        self.assertEqual(metrics.summarize(rows, self.TH)["bid_avg"], 2000)

    def test_all_errors(self):
        self.assertIsNone(metrics.summarize([row(0, err="x")], self.TH))


class People(unittest.TestCase):
    def setUp(self):
        SENT.clear()

    def test_request_approve_flow(self):
        st, code, new = people.request("222", "alex_s", "Alex S")
        self.assertEqual((st, new), ("pending", True))
        self.assertEqual(people.request("222", "alex_s", "Alex S")[1], code)  # same code on repeat
        self.assertFalse(people.may_use("222"))
        with self.assertRaises(people.PeopleError):
            people.approve("ZZZZZZZZ", "Alex", "111")
        with self.assertRaises(people.PeopleError):
            people.approve(code, "", "111")
        people.approve(people.pretty(code).lower(), "Alex Sample", "111")
        self.assertTrue(people.may_use("222"))
        self.assertFalse(people.is_root("222"))
        self.assertEqual(people.name_of("222"), "Alex Sample")

    def test_env_root_and_revoke(self):
        self.assertTrue(people.is_root("111") and people.may_use("111"))
        with self.assertRaises(people.PeopleError):
            people.revoke("111", "111")
        _, code, _ = people.request("333", "sam_t", "Sam T")
        people.approve(code, "Sam", "111")
        db.run("INSERT INTO watches (coin_id, owner, source, created_at, ready_at, status) VALUES ('x','333','bot',0,1,'collecting')")
        people.revoke("333", "111")
        self.assertFalse(people.may_use("333"))
        self.assertEqual(db.one("SELECT status FROM watches WHERE owner='333'")["status"], "cancelled")
        self.assertEqual(people.request("333", "sam_t", "Sam T")[0], "revoked")
        people.reopen("333", "111")
        self.assertEqual(people.request("333", "sam_t", "Sam T")[0], "pending")

    def test_claim_root_once(self):
        c = people.mint_root_code()
        people.claim_root(c, "444", "op2", "Op Two")
        self.assertTrue(people.is_root("444"))
        with self.assertRaises(people.PeopleError):
            people.claim_root(c, "555", "x", "X")

    def test_bot_access_request_hides_code_from_root(self):
        bot.handle({"message": {"chat": {"type": "private"}, "from": {"id": 666, "first_name": "Stranger"}, "text": "/start"}})
        _, code, _ = people.request("666", "", "Stranger")
        to_user = [t for c, t in SENT if c == "666"][0]
        to_root = [t for c, t in SENT if c == "111"][0]
        self.assertIn(people.pretty(code), to_user)
        self.assertNotIn(code[:4], to_root)
        SENT.clear()
        bot.handle({"message": {"chat": {"type": "private"}, "from": {"id": 111}, "text": f"/approve {people.pretty(code)} Jo Example"}})
        self.assertTrue(people.may_use("666"))
        self.assertTrue(any(c == "666" and "You're in" in t for c, t in SENT))

    def test_group_chats_ignored(self):
        bot.handle({"message": {"chat": {"type": "group"}, "from": {"id": 777}, "text": "/start"}})
        self.assertIsNone(people.get("777"))


class Watches(unittest.TestCase):
    def test_top3_and_idempotent(self):
        r = jobs.start_watch("alpha", owner="111")
        self.assertEqual([m["venue"] for m in r["markets"]], ["okx", "gate", "kucoin"])
        self.assertFalse(r["existing"])
        # a first snapshot on every venue the coin is collected on (defaults + the others a rep may tick)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM snapshots WHERE coin_id='alpha'")["n"], 6)
        self.assertTrue(jobs.start_watch("alpha", owner="111")["existing"])
        self.assertFalse(jobs.start_watch("alpha", owner="888")["existing"])
        self.assertEqual(r["watch"]["ready_at"] - r["watch"]["created_at"], 72 * 3600)

    def test_no_venue(self):
        with self.assertRaises(coingecko.IntakeError) as cm:
            jobs.start_watch("dexonly", owner="111")
        self.assertIn("supported exchange", str(cm.exception))


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stop = threading.Event()
        threading.Thread(target=api.serve, args=(cls.stop,), daemon=True).start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.stop.set()

    def req(self, method, path, body=None, token="t0k"):
        r = urllib.request.Request(f"http://127.0.0.1:18089{path}", method=method,
                                   data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Authorization": f"Bearer {token}"} if token else {})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_auth(self):
        self.assertEqual(self.req("GET", "/v1/health", token=None)[0], 200)
        self.assertEqual(self.req("GET", "/v1/card/alpha.png", token="nope")[0], 401)
        self.assertEqual(self.req("GET", "/v1/card/alpha.png", token=None)[0], 401)

    def test_watch_flow(self):
        self.assertEqual(self.req("GET", "/v1/card/neverseen.png")[0], 404)
        st, _, body = self.req("POST", "/v1/watch", {"coin_id": "dexonly", "lead": "L1"})
        self.assertEqual((st, json.loads(body)["error"]), (422, "no_whitelisted_venue"))
        db.run("INSERT OR IGNORE INTO coins (coin_id, symbol, name, image_url, fetched_at) VALUES ('alpha','ALPHA','Alpha','',0)")
        st, _, body = self.req("POST", "/v1/watch", {"coin_id": "alpha", "lead": "L1"})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["venues"][0]["venue"], "okx")
        self.assertTrue(json.loads(self.req("POST", "/v1/watch", {"coin_id": "alpha", "lead": "L1"})[2])["existing"])
        db.run("UPDATE watches SET ready_at = ? WHERE coin_id='alpha'", db.now() + 3600)
        st, h, _ = self.req("GET", "/v1/card/alpha.png")
        self.assertEqual(st, 425)
        self.assertGreaterEqual(int(h["Retry-After"]), 60)
        db.run("UPDATE watches SET ready_at = ? WHERE coin_id='alpha'", db.now() - 1)
        png = os.path.join(TMP, "fake.png")
        open(png, "wb").write(b"\x89PNG fake")
        orig = render.render
        render.render = lambda c, v, *a: (png, {"from": 0, "to": 3600})
        try:
            st, h, body = self.req("GET", "/v1/card/alpha.png")
            self.assertEqual((st, body, h["X-Audit-Venue"], h["X-Audit-To"]), (200, b"\x89PNG fake", "okx", "1970-01-01T01:00:00Z"))
            self.assertEqual(self.req("GET", "/v1/card/alpha/binance.png")[0], 404)
            self.assertEqual(len(json.loads(self.req("GET", "/v1/cards/alpha")[2])["venues"]), 3)
        finally:
            render.render = orig

    # ── the outreach client's attachment contract ──
    def echo_reg(self, conv, coin="alpha", token="t0k"):
        st, _, body = self.req("POST", "/v1/echo/register",
                               {"conversation": conv, "lane": "cg-rank", "lead": {"website": "x.example", "refs": {"coingecko": coin} if coin else {}}, "steps": [1]},
                               token=token)
        return st, json.loads(body or b"{}")

    def test_echo_register(self):
        self.assertEqual(self.echo_reg("c0", token=None)[0], 401)
        st, b = self.echo_reg("c0", coin=None)
        self.assertEqual((st, b["reason"]), (422, "no CoinGecko id for this lead"))
        st, b = self.echo_reg("c0", coin="dexonly")
        self.assertEqual(st, 422)
        self.assertIn("supported exchange", b["reason"])
        self.assertEqual(self.echo_reg("bad conv!")[0], 400)
        st, b = self.echo_reg("echo-c1")
        self.assertEqual(st, 200)
        self.assertTrue(b["handle"].isdigit())
        self.assertEqual([v["venue"] for v in b["venues"]], ["okx", "gate", "kucoin"])  # the API keeps the top 3
        w = db.one("SELECT * FROM watches WHERE id=?", int(b["handle"]))
        self.assertEqual((w["source"], w["lead"], w["owner"]), ("api", "echo:echo-c1", None))
        st2, b2 = self.echo_reg("echo-c1")  # a retried register: the same audit
        self.assertEqual((st2, b2["handle"], b2["existing"]), (200, b["handle"], True))
        st3, b3 = self.echo_reg("echo-c2")  # another conversation: its own audit, its own window
        self.assertEqual(st3, 200)
        self.assertNotEqual(b3["handle"], b["handle"])
        saved = config.MAX_ACTIVE_API
        config.MAX_ACTIVE_API = 0
        try:
            again = self.echo_reg("echo-c1")  # a retry while the bot is full still gets its own audit back
            self.assertEqual((again[0], again[1]["handle"], again[1]["existing"]), (200, b["handle"], True))
            st4, h4, _ = self.req("POST", "/v1/echo/register", {"conversation": "echo-c3", "lead": {"refs": {"coingecko": "alpha"}}})
            self.assertEqual(st4, 503)  # a full bot is asked again later, never a refusal of the lead
            self.assertIn("Retry-After", h4)
            # ...and the outreach client's cap is its own: the reps can still start audits
            self.assertEqual(jobs.start_watch("alpha", owner="777")["coin"]["coin_id"], "alpha")
        finally:
            config.MAX_ACTIVE_API = saved
        saved = config.MAX_ACTIVE_TOTAL
        config.MAX_ACTIVE_TOTAL = 0
        try:  # and the reps' cap does not stop the outreach client
            self.assertEqual(self.echo_reg("echo-c4")[0], 200)
        finally:
            config.MAX_ACTIVE_TOTAL = saved
        # a coin whose NAME says "limit" is still a refusal of the lead (422), not a full bot (503)
        COINS["unlimited"] = {"coin_id": "unlimited", "symbol": "UNL", "name": "Unlimited Network", "image_url": ""}
        try:
            orig = coingecko.candidates
            coingecko.candidates = lambda cid, venue_ids=None: [] if cid == "unlimited" else orig(cid, venue_ids)
            st5, b5 = self.echo_reg("echo-c5", coin="unlimited")
            self.assertEqual(st5, 422)
            self.assertIn("Unlimited Network", b5["reason"])
        finally:
            coingecko.candidates = orig
            COINS.pop("unlimited", None)

    def test_echo_ping_and_body_limits(self):
        st, _, body = self.req("GET", "/v1/echo/ping")
        self.assertEqual((st, json.loads(body)["ok"]), (200, True))
        self.assertEqual(self.req("GET", "/v1/echo/ping", token="nope")[0], 401)
        import http.client
        for length in ("-1", "999999"):
            c = http.client.HTTPConnection("127.0.0.1", 18089, timeout=5)
            c.putrequest("POST", "/v1/echo/register")
            c.putheader("Authorization", "Bearer t0k")
            c.putheader("Content-Length", length)
            c.endheaders()
            t0 = time.time()
            r = c.getresponse()
            self.assertEqual(r.status, 400, length)  # answered at once, nothing read
            self.assertLess(time.time() - t0, 3)
            c.close()

    def test_echo_attachment(self):
        _, b = self.echo_reg("echo-a1")
        h = b["handle"]
        q = "conversation=echo-a1"
        self.assertEqual(self.req("GET", f"/v1/echo/attachment/{h}?step=1&{q}", token=None)[0], 401)
        # Another audit of the same coin, long since ready, must not make this one ready (readiness by audit).
        old = jobs.start_watch("alpha", owner="888")
        db.run("UPDATE watches SET ready_at = ? WHERE id = ?", db.now() - 86400, old["watch"]["id"])
        st, hd, _ = self.req("GET", f"/v1/echo/attachment/{h}?step=1&{q}")
        self.assertEqual(st, 425)
        self.assertGreaterEqual(int(hd["Retry-After"]), 60)
        db.run("UPDATE watches SET ready_at = ? WHERE id = ?", db.now() - 1, int(h))
        png = os.path.join(TMP, "echo.png")
        open(png, "wb").write(b"\x89PNG echo")
        orig = render.render
        render.render = lambda c, v, *a: (png, {"from": 0, "to": 3600})
        try:
            st, hd, body = self.req("GET", f"/v1/echo/attachment/{h}?step=1&{q}")
            self.assertEqual((st, body, hd["Content-Type"], hd["X-Audit-Venue"]), (200, b"\x89PNG echo", "image/png", "okx"))
            self.assertEqual(self.req("GET", f"/v1/echo/attachment/{h}?{q}")[0], 200)  # no step = step 1
            self.assertEqual(self.req("GET", f"/v1/echo/attachment/{h}?step=2&{q}")[0], 404)  # no second file yet
            self.assertEqual(self.req("GET", f"/v1/echo/attachment/{h}?step=x&{q}")[0], 400)
            # another conversation asking for this handle (a moved provider, a reused id) gets nothing
            self.assertEqual(self.req("GET", f"/v1/echo/attachment/{h}?step=1&conversation=echo-other")[0], 404)
            self.assertEqual(self.req("GET", f"/v1/echo/attachment/{h}?step=1")[0], 400)  # no conversation named
            self.assertEqual(self.req("GET", "/v1/echo/attachment/999999?step=1&conversation=echo-a1")[0], 404)
            self.assertEqual(self.req("GET", f"/v1/echo/attachment/{old['watch']['id']}?step=1&{q}")[0], 404)  # a rep's audit is not the outreach client's
            # The window and its grace over: the audit is closed, stops counting and collecting, and its card still comes.
            db.run("UPDATE watches SET ready_at = ? WHERE id = ?", db.now() - config.API_GRACE_H * 3600 - 1, int(h))
            self.assertGreaterEqual(jobs.close_api_watches(), 1)
            self.assertEqual(db.one("SELECT status FROM watches WHERE id=?", int(h))["status"], "delivered")
            self.assertEqual(self.req("GET", f"/v1/echo/attachment/{h}?step=1&{q}")[0], 200)
            db.run("UPDATE watches SET status='cancelled' WHERE id=?", int(h))
            self.assertEqual(self.req("GET", f"/v1/echo/attachment/{h}?step=1&{q}")[0], 404)
        finally:
            render.render = orig

    def test_root_list_counts_echo_audits(self):
        self.echo_reg("echo-l1")
        pages = bot.list_pages(None)
        n = db.one("SELECT COUNT(*) n FROM watches WHERE source='api' AND lead LIKE 'echo:%' AND status='collecting'")["n"]
        self.assertIn(f"API client: {n} audit", pages[0])
        self.assertNotIn("API caller", "".join(pages))  # the client's audits are counted, not listed
        self.assertNotIn("API client", "".join(bot.list_pages("999")))  # a rep's own list is unchanged

    def test_echo_retry_by_alias_at_capacity(self):
        # a conversation that registered by one spelling of the coin and asks again by another, at the cap,
        # still gets its own audit back (it is matched by its conversation)
        st, b = self.echo_reg("echo-alias")
        self.assertEqual(st, 200)
        saved = config.MAX_ACTIVE_API
        config.MAX_ACTIVE_API = 0
        try:
            again = self.req("POST", "/v1/echo/register", {"conversation": "echo-alias", "lead": {"refs": {"coingecko": "alpha-network"}}})
            self.assertEqual((again[0], json.loads(again[2])["handle"]), (200, b["handle"]))
        finally:
            config.MAX_ACTIVE_API = saved


class Delivery(unittest.TestCase):
    def test_due_bot_watch_delivers_cards_and_message(self):
        SENT.clear()
        r = jobs.start_watch("alpha", owner="999")
        db.run("UPDATE watches SET ready_at = ? WHERE id = ?", db.now() - 1, r["watch"]["id"])
        db.run("UPDATE markets SET chosen_at = ? WHERE coin_id='alpha'", db.now() - 80 * 3600)  # chosen at intake
        orig = render.render
        s = metrics.summarize([row(i * 3600) for i in range(73)], config.requirements("okx"))
        render.render = lambda c, v, *a: (f"/tmp/{v}.png", s)
        try:
            jobs.deliver_due()
        finally:
            render.render = orig
        mine = [x for c, x in SENT if c == "999"]
        self.assertEqual(mine[0], ["/tmp/okx.png", "/tmp/gate.png", "/tmp/kucoin.png", "/tmp/mexc.png", "/tmp/htx.png", "/tmp/bybit.png"])  # every collected venue
        self.assertIn("Suggested message", mine[1])
        self.assertIn("OKX", mine[1])
        self.assertEqual(db.one("SELECT status FROM watches WHERE id=?", r["watch"]["id"])["status"], "delivered")


class Lists(unittest.TestCase):
    def test_root_sees_every_rep_member_sees_own(self):
        _, code, _ = people.request("1201", "rep_a", "Rep A")
        people.approve(code, "Rep Alpha", "111")
        _, code, _ = people.request("1202", "rep_b", "Rep B")
        people.approve(code, "Rep Beta", "111")
        jobs.start_watch("alpha", owner="1201")
        w = jobs.start_watch("alpha", owner="1202")["watch"]
        db.run("UPDATE watches SET status='delivered', delivered_at=? WHERE id=?", db.now(), w["id"])
        allp = "\n".join(bot.list_pages(None))
        self.assertIn("All reps' audits", allp)
        for want in ("Rep Alpha", "Rep Beta", "Alpha", "• OKX — ALPHA/USDT\n• Gate — ALPHA/USDT\n• KuCoin — ALPHA/USDT",
                     "started ", "cards ", "delivered"):
            self.assertIn(want, allp)
        mine = "\n".join(bot.list_pages("1201"))
        self.assertIn("Your audits", mine)
        self.assertNotIn("Rep Beta", mine)
        self.assertNotIn("Rep Alpha", mine)  # own list carries no owner names

    def test_pages_fit_telegram(self):
        for i in range(120):
            db.run("INSERT INTO watches (coin_id, owner, source, created_at, ready_at, status) VALUES ('alpha','1201','bot',?,?,'collecting')",
                   db.now(), db.now() + 3600)
        pages = bot.list_pages(None)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(p) <= 4096 for p in pages))
        db.run("DELETE FROM watches WHERE owner='1201' AND ready_at = created_at + 3600")

    def test_root_help_and_member_refusal(self):
        self.assertIn("/list all", bot.HELP_ROOT)
        self.assertNotIn("/list all", bot.HELP_MEMBER)
        _, code, _ = people.request("1203", "rep_c", "Rep C")
        people.approve(code, "Rep Gamma", "111")
        SENT.clear()
        bot.handle({"message": {"chat": {"type": "private"}, "from": {"id": 1203}, "text": "/people"}})
        self.assertEqual(SENT, [("1203", "/people isn't something I can do for you. /help lists what you can use.")])
        SENT.clear()
        bot.handle({"message": {"chat": {"type": "private"}, "from": {"id": 1203}, "text": "/list all"}})
        self.assertEqual(len(SENT), 1)
        self.assertNotIn("All reps", SENT[0][1])  # a member's "/list all" is their own list


class Scheduler(unittest.TestCase):
    def test_restart_inside_a_filled_slot_does_not_collect(self):
        calls = []
        orig = jobs.collect_once, jobs.deliver_due
        jobs.collect_once, jobs.deliver_due = (lambda: calls.append(1) or (0, 0)), (lambda: None)
        stop = threading.Event()
        stop.wait = lambda *_: stop.set()  # one loop pass
        try:
            db.run("INSERT INTO snapshots (coin_id, venue, pair, ts) VALUES ('alpha','okx','ALPHA/USDT',?)", db.now())
            jobs.scheduler(stop)
            self.assertEqual(calls, [])
            db.run("DELETE FROM snapshots")
            stop.clear()
            jobs.scheduler(stop)
            self.assertEqual(calls, [1])
        finally:
            jobs.collect_once, jobs.deliver_due = orig


class Notices(unittest.TestCase):
    def test_root_gets_one_line_per_new_audit_not_repeats(self):
        _, code, _ = people.request("1301", "rep_n", "Rep N")
        people.approve(code, "Rep Notice", "111")
        SENT.clear()
        bot.intake_one("1301", "alpha")
        to_root = [t for c, t in SENT if c == "111"]
        self.assertEqual(len(to_root), 1)
        self.assertRegex(to_root[0], r"^🆕 Rep Notice started <b>Alpha \(ALPHA\)</b>\n• OKX — ALPHA/USDT\n• Gate — ALPHA/USDT\n"
                                     r"• KuCoin — ALPHA/USDT\n• MEXC — ALPHA/USDT\n• HTX — ALPHA/USDT\n• Bybit — ALPHA/USDT\nCards arrive \d+ \w{3} \d\d:\d\d UTC$")
        SENT.clear()
        bot.intake_one("1301", "alpha")  # already running: the rep is told, root is not
        self.assertEqual([c for c, _ in SENT], ["1301"])

    def test_root_starting_own_audit_is_not_notified(self):
        SENT.clear()
        bot.intake_one("111", "alpha")
        self.assertEqual([c for c, _ in SENT], ["111"])
        db.run("UPDATE watches SET status='cancelled' WHERE owner='111'")  # leave no watch for later tests

    def test_refused_link_sends_no_notice(self):
        SENT.clear()
        bot.intake_one("1301", "dexonly")
        self.assertEqual([c for c, _ in SENT], ["1301"])


class Shadow(unittest.TestCase):
    def test_every_venue_is_offered_and_ticked(self):
        _, code, _ = people.request("1401", "rep_s", "Rep S")
        people.approve(code, "Rep Shadow", "111")
        SENT.clear()
        bot.intake_one("1401", "alpha")
        reply = [t for c, t in SENT if c == "1401"][0]
        self.assertIn("accepted, analytics started\nCards arrive ", reply)
        buttons = [r[0]["text"] for r in MARKUP["1401"]["inline_keyboard"]]
        self.assertEqual(buttons[:3], ["✅ OKX — ALPHA/USDT", "✅ Gate — ALPHA/USDT", "✅ KuCoin — ALPHA/USDT"])
        self.assertEqual(sorted(buttons[3:-1]), ["✅ Bybit — ALPHA/USDT", "✅ HTX — ALPHA/USDT", "✅ MEXC — ALPHA/USDT"])
        self.assertEqual(buttons[-1], "⚙️ Requirements")
        self.assertEqual([m["venue"] for m in db.markets_for("alpha")], ["okx", "gate", "kucoin"])
        self.assertEqual({m["venue"] for m in db.markets_for("alpha", shadow=True)}, {"mexc", "htx", "bybit"})
        collected = {(m["coin_id"], m["venue"]) for m in db.active_markets()}
        self.assertTrue({("alpha", "mexc"), ("alpha", "htx"), ("alpha", "okx"), ("alpha", "bybit")} <= collected)

    def test_shadow_lookup_is_once_per_coin(self):
        calls = []
        orig = coingecko.candidates
        coingecko.candidates = lambda cid, venue_ids=None: calls.append(venue_ids) or orig(cid, venue_ids)
        try:
            db.run("UPDATE coins SET lookup_rules = NULL WHERE coin_id='alpha'")
            coingecko.ensure_shadow("alpha")
            coingecko.ensure_shadow("alpha")
        finally:
            coingecko.candidates = orig
        self.assertEqual(len(calls), 1)

    def test_old_database_migrates(self):
        import sqlite3
        d = tempfile.mkdtemp()
        old = sqlite3.connect(os.path.join(d, "audit.db"))
        old.executescript("CREATE TABLE coins (coin_id TEXT PRIMARY KEY, symbol TEXT, name TEXT, image_url TEXT, fetched_at INTEGER);"
                          "CREATE TABLE markets (coin_id TEXT, venue TEXT, base TEXT, quote TEXT, cg_volume_usd REAL, rank INTEGER, chosen_at INTEGER, PRIMARY KEY (coin_id, venue));"
                          "INSERT INTO markets VALUES ('x','okx','X','USDT',1,0,0);")
        old.close()
        c = sqlite3.connect(os.path.join(d, "audit.db"))
        c.row_factory = sqlite3.Row
        c.executescript(db.SCHEMA)
        db._migrate(c)
        self.assertEqual(c.execute("SELECT shadow FROM markets").fetchone()[0], 0)
        self.assertIn("shadow_checked_at", {r[1] for r in c.execute("PRAGMA table_info(coins)")})
        self.assertIn("book", {r[1] for r in c.execute("PRAGMA table_info(snapshots)")})
        self.assertIn("reqs", {r[1] for r in c.execute("PRAGMA table_info(watches)")})


class Quotes(unittest.TestCase):
    def test_usdt_only(self):
        def t(ex, target, vol, **kw):
            return {"market": {"identifier": ex}, "base": "ABC", "target": target,
                    "converted_volume": {"usd": vol}, "is_anomaly": False, "is_stale": False} | kw
        payload = {"tickers": [t("binance", "FDUSD", 9e6), t("binance", "BTC", 8e6), t("okex", "USDC", 7e6),
                               t("okex", "ETH", 6e6), t("gate", "USDT", 1e5), t("kucoin", "USD", 5e6),
                               t("bybit_spot", "USDT", 2e5, is_anomaly=True), t("mxc", "USDT", 3e5), t("bingx", "USDT", 4e5)]}
        orig = coingecko.get
        coingecko.get = lambda path, params=None, tries=4: payload
        try:
            main = REAL_CANDIDATES("abc")
            shadow = REAL_CANDIDATES("abc", config.SHADOW_VENUES)
        finally:
            coingecko.get = orig
        # flagged (thin/wide) Bybit is kept: that is a market maker's lead (2026-11-03)
        self.assertEqual([(c["venue"], c["quote"], c["anomaly"]) for c in main], [("bybit", "USDT", True), ("gate", "USDT", False)])
        self.assertEqual([(c["venue"], c["quote"]) for c in shadow], [("bingx", "USDT"), ("mexc", "USDT")])

    def test_no_usdt_anywhere_is_said_plainly(self):
        SENT.clear()
        bot.intake_one("111", "dexonly")
        self.assertIn("has no USDT pair on any supported exchange", SENT[-1][1])

    def test_bingx_style_worst_first_asks_are_sorted(self):
        bids = [(0.99, 100), (1.0, 100), (0.5, 1e6)]
        asks = [(1.5, 10), (1.02, 100), (1.01, 100)]  # worst first, as BingX sends them
        m = venues.measure(bids, asks)
        self.assertEqual((m["best_bid"], m["best_ask"]), (1.0, 1.01))
        self.assertAlmostEqual(m["spread_pct"], 0.01 / 1.005 * 100)
        self.assertEqual(m["ask_2pct_usd"], round(1.01 * 100 + 1.02 * 100, 2))
        self.assertEqual((m["trunc_bid"], m["trunc_ask"]), (0, 0))

    def test_bingx_is_collect_only(self):
        self.assertIn("bingx", config.SHADOW_VENUES)
        self.assertNotIn("bingx", config.VENUES)
        self.assertEqual(venues.CG_ID["bingx"], "bingx")


class ListAllVenues(unittest.TestCase):
    def test_list_shows_card_then_collect_only_venues(self):
        _, code, _ = people.request("1501", "rep_l", "Rep L")
        people.approve(code, "Rep List", "111")
        jobs.start_watch("alpha", owner="1501")
        coingecko.ensure_shadow("alpha")
        self.assertTrue(db.markets_for("alpha", shadow=True))
        w = db.one("SELECT id FROM watches WHERE owner='1501' AND status='collecting'")["id"]
        db.run("DELETE FROM watch_venues WHERE watch_id=? AND venue IN ('mexc','htx','bybit')", w)  # the rep unticked three
        want = ("Cards:\n• OKX — ALPHA/USDT\n• Gate — ALPHA/USDT\n• KuCoin — ALPHA/USDT\n"
                "Also collecting (no cards yet):\n• Bybit — ALPHA/USDT\n• HTX — ALPHA/USDT\n• MEXC — ALPHA/USDT")  # by name
        self.assertIn(want, "\n".join(bot.list_pages("1501")))
        self.assertIn(want, "\n".join(bot.list_pages(None)))
        db.run("UPDATE watches SET status='delivered', delivered_at=? WHERE owner='1501'", db.now())
        self.assertIn("Also collected (no cards):\n• Bybit — ALPHA/USDT\n• HTX — ALPHA/USDT\n• MEXC — ALPHA/USDT", "\n".join(bot.list_pages("1501")))

    def test_root_notice_lists_every_ticked_venue(self):
        _, code, _ = people.request("1502", "rep_m", "Rep M")
        people.approve(code, "Rep More", "111")
        db.run("UPDATE watches SET status='cancelled' WHERE coin_id='alpha'")
        SENT.clear()
        bot.intake_one("1502", "alpha")
        to_root = [t for c, t in SENT if c == "111"][0]
        self.assertIn("• MEXC — ALPHA/USDT", to_root)
        for _, t in SENT:
            self.assertNotIn("Also collecting", t)


class CardHeader(unittest.TestCase):
    def test_one_exchange_logo_badge_on_the_token(self):
        render._ex_img["kucoin"] = "data:image/png;base64,EXLOGO"
        s = metrics.summarize([row(i * 3600) for i in range(73)], config.requirements("kucoin"))
        coin = {"coin_id": "alpha", "symbol": "ALPHA", "name": "Alpha", "image_url": ""}
        m = {"venue": "kucoin", "base": "ALPHA", "quote": "USDT"}
        h = render.card_html(coin, m, [row(i * 3600) for i in range(73)], s)
        self.assertEqual(h.count("data:image/png;base64,EXLOGO"), 1)  # the badge only
        self.assertIn('class="ex" src="data:image/png;base64,EXLOGO"', h)
        self.assertIn('<p class="venue">KuCoin · Spot</p>', h)


class RequirementLines(unittest.TestCase):
    def _html(self, need, depth):
        rows = [row(i * 3600, bid=depth, ask=depth) for i in range(73)]
        s = metrics.summarize(rows, config.requirements("gate", {"depth2": need}))
        return render.card_html({"coin_id": "x", "symbol": "X", "name": "X", "image_url": ""},
                                {"venue": "gate", "base": "X", "quote": "USDT"}, rows, s)

    def test_one_required_line(self):
        h = self._html(50000, 600000)
        self.assertEqual(h.count(">Required</text>"), 1)
        self.assertNotIn("Bid required", h)

    def test_requirement_above_chart_is_named_once(self):
        self.assertIn("Bid &amp; ask requirement (50K) is above the chart", self._html(50000, 2000))


class CardIsSelfContained(unittest.TestCase):
    def _html(self):
        rows = [row(i * 3600) for i in range(73)]
        s = metrics.summarize(rows, config.requirements("gate"))
        return render.card_html({"coin_id": "x", "symbol": "X", "name": "X", "image_url": ""},
                                {"venue": "gate", "base": "X", "quote": "USDT"}, rows, s)

    def test_no_external_font_or_network_url(self):
        import re
        h = self._html()
        self.assertNotIn("googleapis", h)
        self.assertNotIn("<link", h)
        # every src/href/url() is a data: URI (the svg xmlns is a namespace, not a fetch)
        refs = re.findall(r'(?:src|href)="([^"]*)"|url\(([^)]*)\)', h)
        for a, b in refs:
            self.assertTrue((a or b).strip("'\"").startswith("data:"), (a or b)[:60])

    def test_bundled_inter_variable_covers_400_to_700(self):
        h = self._html()
        self.assertIn("font-family:InterCard;font-style:normal;font-weight:100 900;", h)
        self.assertEqual(h.count("data:font/woff2;base64,"), 1)
        self.assertIn("font-family:InterCard,sans-serif", h)
        self.assertTrue(os.path.exists(os.path.join(config.ROOT, "card", "fonts", "OFL-Inter.txt")))

    def test_threshold_labels_sit_outside_the_plot(self):
        svg = render.chart_svg([("#fff", [(0, 0.1), (100, 0.3)])], 0, 100, 1000, 260, str, y_max=0.4,
                               refs=[(0.2, "#f00", "Maximum")])
        import re
        x = float(re.search(r'<text x="([0-9.]+)"[^>]*class="ref"', svg).group(1))
        plot_right = 1000 - 92
        self.assertGreater(x, plot_right)  # the data path ends at plot_right


class FallbackAndKraken(unittest.TestCase):
    def test_no_card_venue_falls_back_to_collect_only(self):
        _, code, _ = people.request("1601", "rep_f", "Rep F")
        people.approve(code, "Rep Fallback", "111")
        SENT.clear()
        bot.intake_one("1601", "mexconly")
        reply = [t for c, t in SENT if c == "1601"][-1]
        self.assertIn("accepted, analytics started\nCards arrive", reply)
        self.assertEqual(MARKUP["1601"]["inline_keyboard"][0][0]["text"], "✅ MEXC — MXO/USDT")
        self.assertEqual([m["venue"] for m in db.markets_for("mexconly")], ["mexc"])
        self.assertEqual(db.markets_for("mexconly", shadow=True), [])  # not duplicated as collect-only

    def test_card_venues_win_when_any_exist(self):
        self.assertNotIn("mexc", [m["venue"] for m in db.markets_for("alpha")])

    def test_refusal_names_every_venue_checked(self):
        SENT.clear()
        bot.intake_one("111", "dexonly")
        msg = SENT[-1][1]
        for name in ("Binance", "OKX", "Bybit", "Gate", "KuCoin", "Kraken (USD too)", "MEXC", "Bitget", "HTX", "BingX"):
            self.assertIn(name, msg)

    def test_usd_only_on_kraken(self):
        def t(ex, target, vol):
            return {"market": {"identifier": ex}, "base": "GAMMA", "target": target,
                    "converted_volume": {"usd": vol}, "is_anomaly": False, "is_stale": False}
        payload = {"tickers": [t("kucoin", "USDT", 8e5), t("kraken", "USD", 3e4), t("gate", "USD", 9e9), t("okex", "USDC", 9e9)]}
        orig = coingecko.get
        coingecko.get = lambda path, params=None, tries=4: payload
        try:
            got = [(c["venue"], c["quote"]) for c in REAL_CANDIDATES("gamma-land")]
        finally:
            coingecko.get = orig
        self.assertEqual(got, [("kucoin", "USDT"), ("kraken", "USD")])

    def test_kraken_adapter_parses_depth_and_ticker(self):
        depth = {"error": [], "result": {"GAMMAUSD": {
            "asks": [["0.0583", "100", 1], ["0.0590", "1000", 1]], "bids": [["0.0582", "200", 1], ["0.0570", "1000", 1]]}}}
        ticker = {"error": [], "result": {"GAMMAUSD": {"v": ["1", "610000"], "p": ["0", "0.0587"]}}}
        orig = venues.get
        venues.get = lambda url, timeout=15: depth if "Depth" in url else ticker
        try:
            bids, asks, vol = venues.book_and_volume("kraken", "GAMMA", "USD")
        finally:
            venues.get = orig
        self.assertEqual((bids[0], asks[0]), ((0.0582, 200.0), (0.0583, 100.0)))
        self.assertAlmostEqual(vol, 610000 * 0.0587)

    def test_kraken_calls_are_paced(self):
        old = dict(venues.PACE)
        venues.PACE["api.kraken.com"] = 0.2
        venues._pace_next.clear()
        try:
            t0 = time.time()
            for _ in range(3):
                venues._pace("https://api.kraken.com/0/public/Depth?pair=X")
            self.assertGreaterEqual(time.time() - t0, 0.38)
            t1 = time.time()
            venues._pace("https://api.gateio.ws/x")  # unpaced host: no wait
            self.assertLess(time.time() - t1, 0.05)
        finally:
            venues.PACE.clear(); venues.PACE.update(old)


class FallbackOverLeftoverShadowRow(unittest.TestCase):
    def test_fallback_promotes_an_existing_collect_only_row(self):
        # the live 2026-10-30 state: a pre-fallback refusal had stored MEXC as one coin's collect-only row
        db.run("""INSERT INTO coins (coin_id, symbol, name, image_url, fetched_at, shadow_checked_at)
                  VALUES ('mexconly','MXO','Mexc Only','',0,1) ON CONFLICT(coin_id) DO NOTHING""")
        db.run("DELETE FROM markets WHERE coin_id='mexconly'")
        db.run("DELETE FROM watches WHERE coin_id='mexconly'")
        db.run("INSERT INTO markets (coin_id, venue, base, quote, cg_volume_usd, rank, chosen_at, shadow) VALUES ('mexconly','mexc','MXO','USDT',1,100,0,1)")
        r = jobs.start_watch("mexconly", owner="111")
        self.assertEqual([(m["venue"], m["rank"]) for m in r["markets"]], [("mexc", 0)])
        self.assertEqual(db.markets_for("mexconly", shadow=True), [])
        db.run("UPDATE watches SET status='cancelled' WHERE owner='111'")

    def test_refused_coin_stores_no_collect_only_rows(self):
        db.run("DELETE FROM markets WHERE coin_id='dexonly'")
        with self.assertRaises(coingecko.IntakeError):
            jobs.start_watch("dexonly", owner="111")
        self.assertEqual(db.q("SELECT * FROM markets WHERE coin_id='dexonly'"), [])


class LateVenue(unittest.TestCase):
    def test_added_venue_gets_its_own_full_window_and_a_follow_up(self):
        db.run("UPDATE watches SET status='cancelled' WHERE coin_id='alpha'")
        db.run("DELETE FROM markets WHERE coin_id='alpha' AND venue='kraken'")
        _, code, _ = people.request("1701", "rep_k", "Rep K")
        people.approve(code, "Rep Kraken", "111")
        w = jobs.start_watch("alpha", owner="1701")["watch"]
        db.run("UPDATE markets SET chosen_at = ? WHERE coin_id='alpha'", db.now() - 80 * 3600)
        db.run("UPDATE watches SET ready_at = ? WHERE id = ?", db.now() - 60, w["id"])
        orig_snap = venues.snapshot
        m = jobs.add_venue("alpha", "kraken", "ALPHA", "USD")  # joins now: its card is due in 72 h
        self.assertEqual((m["venue"], m["quote"], m["shadow"]), ("kraken", "USD", 0))
        self.assertTrue(db.q("SELECT 1 FROM snapshots WHERE coin_id='alpha' AND venue='kraken'"))
        orig = render.render
        s = metrics.summarize([row(i * 3600) for i in range(73)], config.requirements("okx"))
        render.render = lambda c, v, *a: (f"/tmp/{v}.png", s)
        try:
            SENT.clear()
            jobs.deliver_due()  # first batch: the three original venues + the suggested message
            mine = [x for c, x in SENT if c == "1701"]
            self.assertEqual(mine[0], ["/tmp/okx.png", "/tmp/gate.png", "/tmp/kucoin.png", "/tmp/mexc.png", "/tmp/htx.png", "/tmp/bybit.png"])
            self.assertIn("Suggested message", mine[1])
            st = db.one("SELECT status FROM watches WHERE id=?", w["id"])["status"]
            self.assertEqual(st, "collecting")  # Kraken still owed: keep collecting it
            self.assertIn(("alpha", "kraken"), {(x["coin_id"], x["venue"]) for x in db.active_markets()})
            listed = "\n".join(bot.list_pages("1701"))
            self.assertIn("• Kraken — ALPHA/USD · card ", listed)
            self.assertIn("• OKX — ALPHA/USDT · ✅ sent", listed)
            SENT.clear()
            jobs.deliver_due()  # nothing new before Kraken's window is full
            self.assertEqual([c for c, _ in SENT if c == "1701"], [])
            db.run("UPDATE markets SET chosen_at = ? WHERE coin_id='alpha' AND venue='kraken'", db.now() - 72 * 3600 - 1)
            jobs.deliver_due()  # follow-up: Kraken alone, captioned, no second suggested message
            mine = [x for c, x in SENT if c == "1701"]
            self.assertEqual(mine, [["/tmp/kraken.png"]])
            self.assertEqual(db.one("SELECT status FROM watches WHERE id=?", w["id"])["status"], "delivered")
        finally:
            render.render = orig
            venues.snapshot = orig_snap
            db.run("DELETE FROM markets WHERE coin_id='alpha' AND venue='kraken'")

    def test_follow_up_caption_names_the_venue(self):
        sent = []
        orig = telegram.send_photos
        telegram.send_photos = lambda chat, items: sent.extend(items)
        orig_r = render.render
        s = metrics.summarize([row(i * 3600) for i in range(73)], config.requirements("kraken"))
        render.render = lambda c, v, *a: (f"/tmp/{v}.png", s)
        try:
            w = {"id": 999999, "coin_id": "alpha", "owner": "1701"}
            jobs.deliver(w, [{"venue": "kraken", "base": "ALPHA", "quote": "USD"}], first=False)
        finally:
            telegram.send_photos = orig
            render.render = orig_r
        self.assertTrue(sent[0][1].startswith("<b>Kraken card for Alpha (ALPHA)</b>\n"))

    def test_add_venue_refuses_non_card_venue_and_unknown_coin(self):
        with self.assertRaises(ValueError):
            jobs.add_venue("alpha", "mexc", "ALPHA", "USDT")
        with self.assertRaises(ValueError):
            jobs.add_venue("never-seen", "kraken", "X", "USD")


def book_row(ts, mid=1.0, d=None, lv=(40, 40), lv10=(35, 35), **kw):
    d = d or {"0.25": [300, 300], "1": [3000, 3000], "2": [6000, 6000], "5": [9000, 9000], "10": [25000, 25000]}
    r = row(ts, bid=d["2"][0], ask=d["2"][1], **kw)
    r.update(mid=mid, book=json.dumps({"d": d, "t": {k: [0, 0] for k in d}, "lv": list(lv), "lv10": list(lv10)}))
    return r


class WebSlug(unittest.TestCase):
    """A coingecko.com link's slug is the coin's web_slug, which is not always its API id (Spark, 2026-11-03)."""
    COINS = {"spark": {"id": "spark", "symbol": "spark", "name": "Spark", "web_slug": "spark-2"},
             "spark-network-token": {"id": "spark-network-token", "symbol": "spark", "name": "Spark", "web_slug": "spark"},
             "alpha": {"id": "alpha", "symbol": "alpha", "name": "Alpha", "web_slug": "alpha"},
             "the-graph": {"id": "the-graph", "symbol": "grt", "name": "The Graph"}}  # no web_slug field: trusted
    HITS = [{"id": "spark-network-token"}, {"id": "spark"}]

    def lookup(self, slug, web=True):
        calls = []

        def fake_get(path, params=None, tries=4):
            calls.append(path)
            if path == "/search":
                return {"coins": self.HITS}
            return self.COINS.get(path.rsplit("/", 1)[1])
        orig, coingecko.get = coingecko.get, fake_get
        try:
            return REAL_LOOKUP(slug, web)["id"], calls
        finally:
            coingecko.get = orig

    def test_link_slug_is_the_web_slug(self):
        self.assertEqual(self.lookup("spark")[0], "spark-network-token")
        self.assertEqual(self.lookup("spark-2")[0], "spark")  # 404 on the id, found by search + web_slug

    def test_matching_slug_costs_one_call(self):
        self.assertEqual(self.lookup("alpha"), ("alpha", ["/coins/alpha"]))
        self.assertEqual(self.lookup("the-graph"), ("the-graph", ["/coins/the-graph"]))

    def test_api_ids_are_taken_as_ids(self):
        self.assertEqual(self.lookup("spark", web=False)[0], "spark")

    def test_numbered_slug_searches_the_name(self):
        # coingecko.com/en/coins/vexa-2 is the API's "vexa"; search finds it by "vexa", not "vexa 2" (2026-11-03)
        coins = {"vexa": {"id": "vexa", "symbol": "us", "name": "Vexa", "web_slug": "vexa-2"}}
        search = {"vexa": [{"id": "vexa"}]}

        def fake_get(path, params=None, tries=4):
            if path == "/search":
                return {"coins": search.get(params["query"], [])}
            return coins.get(path.rsplit("/", 1)[1])
        orig, coingecko.get = coingecko.get, fake_get
        try:
            self.assertEqual(REAL_LOOKUP("vexa-2")["id"], "vexa")
            with self.assertRaises(coingecko.IntakeError):
                REAL_LOOKUP("vexa-2", web=False)  # API ids are not reinterpreted
        finally:
            coingecko.get = orig

    def test_renamed_coin_is_found_in_the_coin_list(self):
        # coingecko.com/en/coins/newname-web3 is the API's "oldname", renamed "New Name Web3"; search finds nothing
        coins = {"oldname": {"id": "oldname", "symbol": "old", "name": "New Name Web3", "web_slug": "newname-web3"},
                 "newname-clone": {"id": "newname-clone", "symbol": "nc", "name": "Newname Web3", "web_slug": "other"},
                 "list": [{"id": "newname-clone", "symbol": "nc", "name": "Newname Web3"},
                          {"id": "oldname", "symbol": "old", "name": "New Name Web3"},
                          {"id": "alpha", "symbol": "alpha", "name": "Alpha"}]}
        calls = []

        def fake_get(path, params=None, tries=4):
            calls.append(path)
            if path == "/search":
                return {"coins": []}
            return coins.get(path.rsplit("/", 1)[1])
        orig, coingecko.get = coingecko.get, fake_get
        saved = dict(coingecko._list)
        coingecko._list.update(at=0.0, coins=[])
        try:
            self.assertEqual(REAL_LOOKUP("newname-web3")["id"], "oldname")  # the name match whose web_slug agrees
            REAL_LOOKUP("newname-web3")
            self.assertEqual(calls.count("/coins/list"), 1)  # the list is cached
            with self.assertRaises(coingecko.IntakeError):
                REAL_LOOKUP("newname-web3", web=False)  # API ids are not reinterpreted
        finally:
            coingecko.get = orig
            coingecko._list.update(saved)


class BookStats(unittest.TestCase):
    def test_bands_and_levels(self):
        bids = [(1.0 - i * 0.001, 100) for i in range(150)]   # down to 0.851
        asks = [(1.001 + i * 0.001, 100) for i in range(30)]   # up to 1.030: shorter than ±5%
        m = venues.measure(bids, asks)
        b = m["book"]
        self.assertEqual(b["lv"], [150, 30])
        self.assertEqual(b["lv10"][1], 30)
        self.assertEqual(b["d"]["2"], [m["bid_2pct_usd"], m["ask_2pct_usd"]])
        self.assertEqual(b["t"]["5"], [0, 1])  # ask side ends inside ±5%: a lower bound
        self.assertLess(b["d"]["0.25"][0], b["d"]["1"][0])


class Checks(unittest.TestCase):
    def test_kinds(self):
        rows = [book_row(i * 3600, mid=1.0 if i < 30 else 0.6) for i in range(73)]
        peers = {r["ts"]: [1.0, 1.02] for r in rows}
        cs = [{"id": "c", "kind": "depth", "band": 2, "side": "combined", "min": 11000},
              {"id": "l", "kind": "levels", "band": 10, "min": 30, "agg": "min"},
              {"id": "lv", "kind": "levels", "min": 50},
              {"id": "d24", "kind": "drop", "window_h": 24, "max": 30},
              {"id": "p", "kind": "price_diff", "max": 5},
              {"id": "k", "kind": "depth", "band": 0.25, "side": "each", "min": 250}]
        s = metrics.summarize(rows, cs, peers=peers)
        get = lambda i: metrics.check(s, i)
        self.assertEqual((get("c")["value"], get("c")["ok"]), (12000, True))
        self.assertEqual((get("l")["value"], get("l")["ok"]), ((35, 35), True))
        self.assertFalse(get("lv")["ok"])                    # 40 < 50
        self.assertAlmostEqual(get("d24")["value"], 40)      # 1.0 -> 0.6 inside an hour
        self.assertFalse(get("d24")["ok"])
        self.assertFalse(get("p")["ok"])                     # 0.6 vs a 1.01 median for most of the window
        self.assertTrue(get("k")["ok"])
        self.assertFalse(s["all_ok"])

    def test_old_rows_answer_for_2pct_only(self):
        rows = [row(i * 3600) for i in range(73)]  # no book: from before 2026-11-03
        cs = config.requirements("kraken") + [{"id": "depth1", "kind": "depth", "band": 1, "side": "each", "min": 2500},
                                              {"id": "pdiff", "kind": "price_diff", "max": 5}]
        s = metrics.summarize(rows, cs)
        self.assertIsNone(metrics.check(s, "depth1")["ok"])  # not enough data, not a fail
        self.assertEqual(metrics.check(s, "depth2")["value"], (1000, 900))
        self.assertIsNone(metrics.check(s, "pdiff")["ok"])  # no peers

    def test_house_volume_minimum(self):
        for v in ("kucoin", "bitget", "gate", "kraken"):  # no published volume rule: house 28,333 (2026-11-03)
            self.assertEqual([c["min"] for c in config.requirements(v) if c["kind"] == "volume"], [28333], v)
        for v, need in (("bybit", 50000), ("mexc", 5000)):  # their own taker / non-self-trading numbers, on total volume
            self.assertEqual([c["min"] for c in config.requirements(v) if c["kind"] == "volume"], [need], v)

    def test_overrides_and_off(self):
        base = config.requirements("kucoin")
        self.assertEqual([(c["id"], c.get("min", c.get("max"))) for c in base], [("depth2", 1000), ("spread", 0.2), ("vol", 28333)])
        own = config.requirements("kucoin", {"depth2": 2500, "spread": None})
        self.assertEqual(own, [dict(base[0], min=2500, custom=True), base[2]])
        self.assertTrue(all(config.requirements(v) for v in config.VENUES + config.SHADOW_VENUES))

    def test_parse(self):
        self.assertEqual([checks.parse(x) for x in ("2,500", "2.5k", "1m", "0.3%", "off", "$4000 USDT")],
                         [2500, 2500, 1e6, 0.3, None, 4000])
        for bad in ("abc", "0", "-5"):
            with self.assertRaises(checks.BadValue):
                checks.parse(bad)

    def test_every_kind_renders_a_tile(self):
        rows = [book_row(i * 3600) for i in range(73)]
        cs = [{"id": "depth2", "kind": "depth", "band": 2, "side": "each", "min": 5000},
              {"id": "depth10", "kind": "depth", "band": 10, "side": "each", "min": 20000},
              {"id": "levels10", "kind": "levels", "band": 10, "min": 30, "agg": "min"},
              {"id": "drop24", "kind": "drop", "window_h": 24, "max": 30},
              {"id": "drop72", "kind": "drop", "window_h": 72, "max": 50},
              {"id": "pdiff", "kind": "price_diff", "max": 5}]
        s = metrics.summarize(rows, cs, peers={r["ts"]: [1.0] for r in rows})
        h = render.card_html({"coin_id": "x", "symbol": "X", "name": "X", "image_url": ""},
                             {"venue": "bitget", "base": "X", "quote": "USDT"}, rows, s)
        for want in ("Liquidity within ±10%", "Order levels within ±10%", "Largest 24H price drop",
                     "Largest 3D price drop", "Price vs other exchanges", "≥ 20,000 USDT per side"):
            self.assertIn(want, h)
        s = metrics.summarize(rows, [])
        h = render.card_html({"coin_id": "x", "symbol": "X", "name": "X", "image_url": ""},
                             {"venue": "bitget", "base": "X", "quote": "USDT"}, rows, s)
        self.assertIn("no Bitget requirements set", h)
        self.assertNotIn("Non-compliant", h)

    def test_share_of_snapshots_that_miss(self):
        # 2026-11-11: the verdict is on the 3-day average; the tile also says how often the market missed
        rows = [book_row(i * 3600) for i in range(73)]
        for r in rows[:18] + rows[40:41]:  # 19 of 73 with a thin bid and a wide spread
            r["bid_2pct_usd"], r["spread_pct"] = 100, 3.0
            b = json.loads(r["book"]) if isinstance(r["book"], str) else r["book"]
            b["d"]["2"][0] = 100
            r["book"] = json.dumps(b) if isinstance(r["book"], str) else b
        good = max(r["bid_2pct_usd"] for r in rows)
        cs = [{"id": "depth2", "kind": "depth", "band": 2, "side": "each", "min": good / 2},
              {"id": "spread", "kind": "spread", "max": 1.5},
              {"id": "vol", "kind": "volume", "min": 1}]
        s = metrics.summarize(rows, cs)
        d2, sp = metrics.check(s, "depth2"), metrics.check(s, "spread")
        self.assertTrue(d2["ok"] and sp["ok"])                # both pass on the average
        self.assertAlmostEqual(d2["miss"], 19 / 73)
        self.assertAlmostEqual(sp["miss"], 19 / 73)
        self.assertNotIn("miss", metrics.check(s, "vol"))
        h = render.card_html({"coin_id": "x", "symbol": "X", "name": "X", "image_url": ""},
                             {"venue": "gate", "base": "X", "quote": "USDT"}, rows, s)
        self.assertIn("<dt>Below minimum</dt><dd>26% of the time</dd>", h)
        self.assertIn("<dt>Above maximum</dt><dd>26% of the time</dd>", h)
        self.assertEqual(h.count('<article class="kpi">'), 4)
        # 2026-11-11: the volume tile has one badge, the verdict; its change stays as a number
        for r in rows[49:]:
            r["vol24h_usd"] = r["vol24h_usd"] / 2
        s2 = metrics.summarize(rows, cs)
        self.assertEqual(s2["vol_trend_state"], "declining")
        vol = render.kpi_tile(metrics.check(s2, "vol"), "USDT", s2["n"], s2)
        self.assertEqual(vol.count('class="status'), 1)
        self.assertIn("Compliant", vol)
        self.assertNotIn("Declining", vol)
        self.assertIn("Change:", vol)
        self.assertIn(render.signed_pct(s2["vol_trend"]), vol)
        self.assertEqual((render._share(0), render._share(0.004), render._share(1)),
                         ("never", "under 1% of the time", "100% of the time"))
        c = metrics.summarize(rows, [{"id": "depth2", "kind": "depth", "band": 2, "side": "combined", "min": good}])
        self.assertAlmostEqual(metrics.check(c, "depth2")["miss"], 0)  # bid 100 + a full ask still clears it

    def test_every_venue_card_has_the_same_four_tiles(self):
        # 2026-11-10: fixed fields on every card, whatever else the venue publishes
        rows = [book_row(i * 3600) for i in range(73)]
        venues_set = config._load()["venues"]
        self.assertEqual(set(venues_set), set(config.VENUES) | set(config.SHADOW_VENUES))
        for v in venues_set:
            cs = config.requirements(v)
            self.assertEqual([(c["id"], c["kind"]) for c in cs],
                             [("depth2", "depth"), ("spread", "spread"), ("vol", "volume")], v)
            self.assertEqual(cs[0]["band"], 2, v)
            s = metrics.summarize(rows, cs)
            h = render.card_html({"coin_id": "x", "symbol": "X", "name": "X", "image_url": ""},
                                 {"venue": v, "base": "X", "quote": "USDT"}, rows, s)
            self.assertEqual(h.count('<article class="kpi">'), 4, v)
            for want in ("within ±2%", "Top-of-book spread", "24H volume", "Liquidity Trend (3D)"):
                self.assertIn(want, h, v)
        self.assertEqual(checks.limit(config.requirements("kraken")[1]), 1)  # house spread, as HTX


def press(tg_id, data):
    bot.handle({"callback_query": {"id": "cb", "from": {"id": int(tg_id)}, "data": data,
                                   "message": {"chat": {"id": int(tg_id)}, "message_id": 5}}})


def say(tg_id, text):
    bot.handle({"message": {"chat": {"type": "private"}, "from": {"id": int(tg_id)}, "text": text}})


class Picker(unittest.TestCase):
    def setUp(self):
        db.run("UPDATE watches SET status='cancelled' WHERE coin_id='alpha'")
        for tid, name in (("1801", "Rep Pick"), ("1802", "Rep Other")):
            if not people.may_use(tid):
                _, code, _ = people.request(tid, "", name)
                people.approve(code, name, "111")
        SENT.clear(); EDITS.clear(); ANSWERS.clear()
        bot.intake_one("1801", "alpha")
        self.w = db.one("SELECT * FROM watches WHERE owner='1801' AND status='collecting'")["id"]

    def picked(self):
        return [m["venue"] for m in db.watch_markets(self.w)]

    def test_toggle_venues(self):
        self.assertEqual(self.picked(), ["okx", "gate", "kucoin", "mexc", "htx", "bybit"])  # all ticked
        for v in ("htx", "bybit", "okx"):
            press("1801", f"t:{self.w}:{v}")
        self.assertEqual(sorted(self.picked()), ["gate", "kucoin", "mexc"])
        self.assertIn("▫️ OKX — ALPHA/USDT", [r[0]["text"] for r in EDITS[-1][2]["inline_keyboard"]])
        press("1801", f"t:{self.w}:okx")  # re-added
        self.assertIn("✅ OKX — ALPHA/USDT", [r[0]["text"] for r in EDITS[-1][2]["inline_keyboard"]])
        for v in ("okx", "gate", "kucoin"):
            press("1801", f"t:{self.w}:{v}")
        press("1801", f"t:{self.w}:mexc")  # the last one stays
        self.assertEqual(self.picked(), ["mexc"])
        self.assertEqual(ANSWERS[-1], "Keep at least one exchange.")
        press("1802", f"t:{self.w}:okx")  # someone else's audit
        self.assertEqual(ANSWERS[-1], "That audit isn't yours.")
        self.assertEqual(self.picked(), ["mexc"])

    def test_cards_follow_the_ticks_and_the_overrides(self):
        press("1801", f"t:{self.w}:gate")
        press("1801", f"t:{self.w}:mexc")
        press("1801", f"re:{self.w}:kucoin:depth2")
        self.assertIn("New minimum for <b>KuCoin · Liquidity within ±2%</b> (now ≥ 1,000 USDT per side)", SENT[-1][1])
        say("1801", "lots")  # not a number: asked again, nothing saved
        self.assertIn("Send a number", SENT[-1][1])
        say("1801", "2.5k")
        self.assertIn("Saved for this audit · KuCoin\nLiquidity within ±2%: ≥ 2,500 USDT per side · changed", SENT[-1][1])
        press("1801", f"re:{self.w}:kucoin:spread")
        say("1801", "off")
        w = db.one("SELECT * FROM watches WHERE id=?", self.w)
        self.assertEqual(db.watch_reqs(w), {"kucoin": {"depth2": 2500, "spread": None}})
        say("1801", "3000")  # no question pending: a plain number is not a requirement
        self.assertEqual(db.watch_reqs(db.one("SELECT * FROM watches WHERE id=?", self.w)), {"kucoin": {"depth2": 2500, "spread": None}})
        db.run("UPDATE watches SET ready_at=? WHERE id=?", db.now() - 1, self.w)
        db.run("UPDATE markets SET chosen_at=? WHERE coin_id='alpha'", db.now() - 80 * 3600)
        got = {}
        orig = render.render
        s = metrics.summarize([row(i * 3600) for i in range(73)], config.requirements("okx"))
        render.render = lambda c, v, reqs=None: (got.__setitem__(v, reqs), (f"/tmp/{v}.png", s))[1]
        try:
            SENT.clear()
            jobs.deliver_due()
        finally:
            render.render = orig
        self.assertEqual([x for c, x in SENT if c == "1801"][0], ["/tmp/okx.png", "/tmp/kucoin.png", "/tmp/htx.png", "/tmp/bybit.png"])
        self.assertEqual(got["kucoin"], [dict(config.requirements("kucoin")[0], min=2500, custom=True), config.requirements("kucoin")[2]])
        self.assertEqual(got["okx"], config.requirements("okx"))
        press("1801", f"t:{self.w}:gate")
        self.assertIn("finished", ANSWERS[-1])

    def test_reset_and_same_as_published(self):
        press("1801", f"re:{self.w}:gate:depth2")
        say("1801", "500")  # gate's published number: no override kept
        self.assertEqual(db.watch_reqs(db.one("SELECT * FROM watches WHERE id=?", self.w)), {})
        press("1801", f"re:{self.w}:gate:spread")
        say("1801", "0.5")
        press("1801", f"rd:{self.w}:gate")
        self.assertEqual(db.watch_reqs(db.one("SELECT * FROM watches WHERE id=?", self.w)), {})
        press("1801", f"r:{self.w}")
        self.assertIn("<b>Requirements · Alpha</b> (this audit only)", EDITS[-1][1])
        self.assertIn("• Liquidity within ±2%: ≥ 10,000 USDT per side", EDITS[-1][1])  # OKX: 3x the average

    def test_close_and_cancel(self):
        press("1801", f"re:{self.w}:gate:depth2")
        self.assertIn("cancel", SENT[-1][1])
        say("1801", "cancel")
        self.assertEqual(SENT[-1][1], "Nothing changed.")
        self.assertIsNone(db.one("SELECT 1 FROM pending_input WHERE tg_id='1801'"))
        press("1801", f"x:{self.w}")
        text, markup = EDITS[-1][1], EDITS[-1][2]
        self.assertIn("cards from\n• OKX — ALPHA/USDT", text)
        self.assertIn("/edit alpha opens the buttons again", text)
        self.assertIsNone(markup)
        w = {"id": self.w}
        for text, kb in (bot.panel(w), bot.req_view(dict(db.one("SELECT * FROM watches WHERE id=?", self.w))),
                         bot.venue_view(dict(db.one("SELECT * FROM watches WHERE id=?", self.w)), "gate")):
            self.assertIn("✖️ Close", [b["text"] for r in kb["inline_keyboard"] for b in r])

    def test_a_failed_answer_still_edits(self):
        def boom(*a, **k):
            raise telegram.TgError("answerCallbackQuery: Bad Request: query is too old")
        orig = telegram.answer
        telegram.answer = boom
        try:
            EDITS.clear()
            press("1801", f"t:{self.w}:okx")
        finally:
            telegram.answer = orig
        self.assertNotIn("okx", [m["venue"] for m in db.watch_markets(self.w)])
        self.assertEqual(len(EDITS), 1)

    def test_edit_command_and_legacy_watch_backfill(self):
        SENT.clear()
        say("1801", "/edit https://www.coingecko.com/en/coins/alpha")
        self.assertIn("Cards arrive", SENT[-1][1])
        self.assertTrue(MARKUP["1801"]["inline_keyboard"])
        cur = db.run("INSERT INTO watches (coin_id, owner, source, created_at, ready_at, status) VALUES ('alpha','1802','bot',0,1,'collecting')")
        db.run("DELETE FROM watch_venues WHERE watch_id=?", cur.lastrowid)
        db._migrate(db.conn())
        self.assertEqual([m["venue"] for m in db.watch_markets(cur.lastrowid)], ["okx", "gate", "kucoin"])
        db.run("UPDATE watches SET status='cancelled' WHERE id=?", cur.lastrowid)



class FlaggedMarkets(unittest.TestCase):
    def test_flagged_kept_when_its_price_matches(self):
        snap = venues.snapshot
        try:
            venues.snapshot = lambda v, b, q: {"mid": 0.0013 if v == "kucoin" else 0.5}
            ok = {"venue": "kucoin", "base": "IOTX9", "quote": "USDT", "volume": 2000, "anomaly": True, "ref": 0.0013}
            self.assertEqual(coingecko.probe(ok)["mid"], 0.0013)
            with self.assertRaises(ValueError):  # same symbol, another token's price
                coingecko.probe(dict(ok, venue="okx"))
            coingecko.probe(dict(ok, venue="okx", anomaly=False))  # unflagged: no price guard
        finally:
            venues.snapshot = snap

    def test_old_lookup_runs_again_and_ticks_a_found_card_venue(self):
        db.run("UPDATE watches SET status='cancelled' WHERE coin_id='alpha'")
        w = jobs.start_watch("alpha", owner="1801")["watch"]["id"]
        db.run("DELETE FROM watch_venues WHERE watch_id=? AND venue='kucoin'", w)
        db.run("DELETE FROM markets WHERE coin_id='alpha' AND venue='bybit'")
        db.run("UPDATE coins SET lookup_rules=1 WHERE coin_id='alpha'")  # looked up under the old rules
        coingecko.ensure_shadow("alpha")
        self.assertIn("bybit", [m["venue"] for m in db.all_markets("alpha")])
        # bybit, found now, is ticked; kucoin, which the rep unticked, stays off (2026-11-08)
        self.assertEqual(sorted(m["venue"] for m in db.watch_markets(w)), ["bybit", "gate", "htx", "mexc", "okx"])
        db.run("UPDATE watches SET status='cancelled' WHERE id=?", w)



class TelegramIPv4(unittest.TestCase):
    def test_connections_resolve_ipv4_only(self):
        import importlib, socket as so
        tg = importlib.reload(importlib.import_module("auditbot.telegram"))  # the real module, not the test stubs
        seen = []
        orig = so.getaddrinfo
        so.getaddrinfo = lambda host, port, fam=0, *a, **k: seen.append(fam) or (_ for _ in ()).throw(OSError("stop"))
        try:
            with self.assertRaises(Exception):
                tg._opener.open("https://api.telegram.org/", timeout=2)
        finally:
            so.getaddrinfo = orig
        self.assertEqual(seen[:1], [so.AF_INET])



class CmcCrossCheck(unittest.TestCase):
    def test_cmc_only_venue_is_added_behind_the_price_guard(self):
        db.run("""INSERT INTO coins (coin_id, symbol, name, image_url, fetched_at) VALUES ('kappa','KAPPA','KAPPA','',0)
                  ON CONFLICT(coin_id) DO NOTHING""")
        gecko = {"tickers": [{"market": {"identifier": "gate"}, "base": "KAPPA", "target": "USDT", "is_anomaly": False,
                              "is_stale": False, "converted_volume": {"usd": 52000}, "converted_last": {"usd": 0.0049}}]}
        listed = [{"venue": "kucoin", "base": "KAPPA", "quote": "USDT", "volume": 1.19e6, "price": 0.00487, "outlier": False},
                  {"venue": "gate", "base": "KAPPA", "quote": "USDT", "volume": 55880, "price": 0.0049, "outlier": False},
                  {"venue": "ourbit", "base": "KAPPA", "quote": "USDT", "volume": 105000, "price": 0.00495, "outlier": False},
                  {"venue": "mexc", "base": "KAPPA", "quote": "USDC", "volume": 9e6, "price": 0.0049, "outlier": False}]
        orig_get, orig_pairs = coingecko.get, cmc.pairs
        coingecko.get = lambda path, params=None, tries=4: gecko
        cmc.pairs = lambda coin: listed
        try:
            got = REAL_CANDIDATES("kappa", config.VENUES + config.SHADOW_VENUES)
        finally:
            coingecko.get, cmc.pairs = orig_get, orig_pairs
        self.assertEqual([(c["venue"], c.get("source")) for c in got], [("kucoin", "cmc"), ("gate", None)])  # ourbit unsupported, USDC skipped
        kc = got[0]
        self.assertEqual((kc["anomaly"], kc["ref"]), (True, 0.0049))
        snap = venues.snapshot
        try:
            venues.snapshot = lambda v, b, q: {"mid": 0.00493}
            coingecko.probe(kc)
            venues.snapshot = lambda v, b, q: {"mid": 1.7}  # another token called KAPPA
            with self.assertRaises(ValueError):
                coingecko.probe(kc)
        finally:
            venues.snapshot = snap

    def test_slug_must_carry_the_same_symbol(self):
        calls = []
        orig = cmc._get
        cmc._get = lambda params: calls.append(params["slug"]) or {"data": {"symbol": "OTHER", "marketPairs": [{"exchangeSlug": "kucoin"}]}}
        cmc._cache.clear()
        try:
            self.assertEqual(REAL_CMC_PAIRS({"coin_id": "abc", "symbol": "ABC", "name": "Abc Token"}), [])
        finally:
            cmc._get = orig
        self.assertEqual(calls, ["abc", "abc-token", "abctoken"])

    def test_flat_name_slug_finds_an_apostrophe_name(self):  # CoinGecko x-s-coin is CMC's xscoin (2026-11-06)
        self.assertEqual(cmc.slugs({"coin_id": "x-s-coin", "name": "X's Coin"}),
                         ["x-s-coin", "xscoin"])


class ScaledPairs(unittest.TestCase):
    """Binance trades X's Coin only as 1000XSC/USDT (2026-11-06): its mid is 1000x the coin's price."""

    def setUp(self):
        db.run("""INSERT INTO coins (coin_id, symbol, name, image_url, fetched_at) VALUES ('x-s-coin','XSC',?,'',0)
                  ON CONFLICT(coin_id) DO NOTHING""", "X's Coin")

    def test_unit(self):
        self.assertEqual(coingecko.unit("1000XSC", "XSC"), 1000)
        self.assertEqual(coingecko.unit("1000000MOG", "mog"), 10 ** 6)
        self.assertEqual(coingecko.unit("1MBABYDOGE", "BABYDOGE"), 10 ** 6)
        self.assertEqual(coingecko.unit("XSC", "XSC"), 1)
        self.assertEqual(coingecko.unit("XSCOIN", "XSC"), 1)  # Gate's own name for it, not a multiple
        self.assertEqual(coingecko.unit("1000XSC", "1000XSC"), 1)  # a coin whose symbol really is 1000XSC
        self.assertEqual(coingecko.unit("1000SATS", "XSC"), 1)  # another coin's multiple
        self.assertEqual(coingecko.unit("1000XSC", None), 1)

    def test_cmc_1000x_pair_passes_the_guard_and_a_wrong_token_does_not(self):
        gecko = {"tickers": [{"market": {"identifier": "okex"}, "base": "XSC", "target": "USDT", "is_anomaly": False,
                              "is_stale": False, "converted_volume": {"usd": 241000}, "converted_last": {"usd": 1.75e-06}}]}
        listed = [{"venue": "binance", "base": "1000XSC", "quote": "TRY", "volume": 3.0e6, "price": 0.00169, "outlier": False},
                  {"venue": "binance", "base": "1000XSC", "quote": "USDT", "volume": 3.36e6, "price": 0.0017, "outlier": False},
                  {"venue": "okx", "base": "XSC", "quote": "USDT", "volume": 245000, "price": 1.75e-06, "outlier": False}]
        orig_get, orig_pairs = coingecko.get, cmc.pairs
        coingecko.get = lambda path, params=None, tries=4: gecko
        cmc.pairs = lambda coin: listed
        try:
            got = REAL_CANDIDATES("x-s-coin")
        finally:
            coingecko.get, cmc.pairs = orig_get, orig_pairs
        self.assertEqual([(c["venue"], c["base"], c["quote"], c["unit"]) for c in got],
                         [("binance", "1000XSC", "USDT", 1000), ("okx", "XSC", "USDT", 1)])
        snap = venues.snapshot
        try:
            venues.snapshot = lambda v, b, q: {"mid": 0.001704}
            coingecko.probe(got[0])  # 0.001704 / 1000 is within 25% of 1.75e-06
            venues.snapshot = lambda v, b, q: {"mid": 1.7e-06}  # a 1000XSC pair at the per-token price: not this coin
            with self.assertRaises(ValueError):
                coingecko.probe(got[0])
        finally:
            venues.snapshot = snap

    def test_cmc_only_reference_is_per_token(self):  # CoinGecko lists none of our venues
        listed = [{"venue": "binance", "base": "1000XSC", "quote": "USDT", "volume": 3.36e6, "price": 0.0017, "outlier": False},
                  {"venue": "ourbit", "base": "XSC", "quote": "USDT", "volume": 1e5, "price": 1.74e-06, "outlier": False},
                  {"venue": "lbank", "base": "XSC", "quote": "USDT", "volume": 1e5, "price": 1.76e-06, "outlier": False}]
        orig_get, orig_pairs = coingecko.get, cmc.pairs
        coingecko.get = lambda path, params=None, tries=4: {"tickers": []}
        cmc.pairs = lambda coin: listed
        try:
            got = REAL_CANDIDATES("x-s-coin")
        finally:
            coingecko.get, cmc.pairs = orig_get, orig_pairs
        self.assertEqual([c["venue"] for c in got], ["binance"])
        self.assertLess(got[0]["ref"], 2e-06)

    def test_price_diff_compares_per_token_mids(self):
        cid = "x-s-coin"
        db.run("DELETE FROM markets WHERE coin_id=?", cid)
        db.run("DELETE FROM snapshots WHERE coin_id=?", cid)
        for v, base in (("binance", "1000XSC"), ("okx", "XSC"), ("bybit", "XSC")):
            db.run("""INSERT INTO markets (coin_id, venue, base, quote, cg_volume_usd, rank, chosen_at, shadow)
                      VALUES (?,?,?,'USDT',0,0,0,0)""", cid, v, base)
        for ts in (1000, 1900):
            for v, mid in (("binance", 0.00175), ("okx", 1.75e-06), ("bybit", 1.75e-06)):
                db.run("""INSERT INTO snapshots (coin_id, venue, pair, ts, mid, spread_pct, bid_2pct_usd, ask_2pct_usd,
                          trunc_bid, trunc_ask, vol24h_usd) VALUES (?,?,?,?,?,0.2,1000,1000,0,0,5000)""", cid, v, v, ts, mid)
        self.assertEqual(render.peers(cid, "okx", 0)[1000], [1.75e-06, 1.75e-06])  # binance's mid / 1000
        self.assertEqual(render.peers(cid, "binance", 0)[1900], [1.75e-06, 1.75e-06])
        pd = {"id": "pdiff", "kind": "price_diff", "max": 5}
        per_token = [dict(r, mid=r["mid"] / 1000) for r in db.snapshots(cid, "binance", 0)]  # as render() passes them
        res = metrics.evaluate(pd, per_token, render.peers(cid, "binance", 0))
        self.assertTrue(res["ok"])
        self.assertAlmostEqual(res["value"], 0.0, places=6)


REAL_RESOLVE_CMC = coingecko.resolve_cmc
# Made-up contract addresses (never a real token's): secret scanners flag full EVM addresses.
EVM_ADDR = "0xAbCMadeUpContract"
SOL_ADDR = "So1MadeUpMint11111111111111111111111111Ab"
PLATFORMS = [{"id": "binance-smart-chain", "chain_identifier": 56, "name": "BNB Smart Chain"},
             {"id": "ethereum", "chain_identifier": 1, "name": "Ethereum"},
             {"id": "solana", "chain_identifier": None, "name": "Solana"}]


class CmcLinks(unittest.TestCase):
    """CoinMarketCap links (2026-11-18): a coin with no CoinGecko page that trades on Bitget, MEXC and BingX."""

    def setUp(self):
        coingecko._platforms.update(at=0.0, by_chain={}, by_name={})
        cmc._cache.clear()

    def fake_gecko(self, routes):
        calls = []

        def fake_get(path, params=None, tries=4):
            calls.append(path)
            if path == "/asset_platforms":
                return PLATFORMS
            hit = routes.get(path)
            return hit(params) if callable(hit) else hit
        return fake_get, calls

    def with_gecko(self, routes, fn):
        fake, calls = self.fake_gecko(routes)
        orig, coingecko.get = coingecko.get, fake
        try:
            return fn(), calls
        finally:
            coingecko.get = orig

    def test_refs_keep_message_order_across_both_sites(self):
        t = ("https://coinmarketcap.com/currencies/omegaland/ then https://www.coingecko.com/en/coins/alpha "
             "and coinmarketcap.com/ru/currencies/gram/?period=7d and again https://coinmarketcap.com/currencies/omegaland "
             "coinmarketcap.com/exchanges/mexc/")
        self.assertEqual(coingecko.refs(t), ["cmc:omegaland", "alpha", "cmc:gram"])
        self.assertEqual(coingecko.refs("nothing here"), [])
        self.assertEqual(coingecko.label("cmc:omegaland"), "omegaland (CoinMarketCap)")
        self.assertEqual(coingecko.label("alpha"), "alpha")

    def test_detail_reads_the_coin_and_an_unknown_slug_is_none(self):
        orig = cmc._get
        cmc._get = lambda params, url=cmc.URL: {"status": {"error_code": "500"}}  # CMC's answer to an unknown slug
        try:
            self.assertIsNone(cmc.detail("nosuchcoin"))
            cmc._get = lambda params, url=cmc.URL: {"data": {
                "id": 99001, "slug": "madeup", "name": "Made Up", "symbol": "mup",
                "platforms": [{"contractAddress": EVM_ADDR, "contractPlatform": "BNB Smart Chain (BEP20)", "contractChainId": 56},
                              {"contractAddress": "", "contractPlatform": "Ethereum", "contractChainId": 1}]}}
            d = cmc.detail("madeup")
        finally:
            cmc._get = orig
        self.assertEqual((d["symbol"], d["name"], d["logo"]),
                         ("MUP", "Made Up", "https://s2.coinmarketcap.com/static/img/coins/200x200/99001.png"))
        self.assertEqual(d["platforms"], [{"chain_id": 56, "platform": "BNB Smart Chain (BEP20)", "address": EVM_ADDR}])

    def test_contract_match_lowercases_evm_but_not_solana(self):
        d = {"slug": "madeup", "name": "Made Up", "symbol": "MUP",
             "platforms": [{"chain_id": 56, "platform": "BNB Smart Chain (BEP20)", "address": EVM_ADDR},
                           {"chain_id": -1, "platform": "Solana", "address": SOL_ADDR}]}
        coin = {"id": "made-up", "symbol": "mup", "name": "Made Up"}
        got, calls = self.with_gecko({f"/coins/binance-smart-chain/contract/{EVM_ADDR.lower()}": coin,
                                      f"/coins/solana/contract/{SOL_ADDR}": coin}, lambda: coingecko.match_cmc(d))
        self.assertEqual(got["id"], "made-up")
        self.assertIn(f"/coins/solana/contract/{SOL_ADDR}", calls)
        self.assertNotIn("/search", calls)  # the contract settled it

    def test_contract_of_another_symbol_is_not_a_match(self):
        d = {"slug": "madeup", "name": "Made Up", "symbol": "MUP",
             "platforms": [{"chain_id": 56, "platform": "BNB", "address": EVM_ADDR}]}
        got, _ = self.with_gecko({f"/coins/binance-smart-chain/contract/{EVM_ADDR.lower()}": {"id": "wrapped", "symbol": "wmup", "name": "Wrapped"},
                                  "/search": {"coins": []}}, lambda: coingecko.match_cmc(d))
        self.assertIsNone(got)

    def test_name_and_symbol_both_must_agree_so_bonk_is_not_bonk(self):
        # CMC's "bonk" slug is megaBONK (MBONK); CoinGecko's search for it finds Bonk (BONK): not the same token
        d = {"slug": "bonk", "name": "megaBONK", "symbol": "MBONK", "platforms": []}
        got, _ = self.with_gecko({"/search": {"coins": [{"id": "bonk", "symbol": "BONK", "name": "Bonk"}]}},
                                 lambda: coingecko.match_cmc(d))
        self.assertIsNone(got)
        d = {"slug": "gram", "name": "Gram", "symbol": "GRAM", "platforms": []}
        got, _ = self.with_gecko({"/search": {"coins": [{"id": "gram-2", "symbol": "GRAM", "name": "Gram"},
                                                        {"id": "gramz", "symbol": "GRAMZ", "name": "Gram"}]},
                                  "/coins/gram-2": {"id": "gram-2", "symbol": "gram", "name": "Gram"}},
                                 lambda: coingecko.match_cmc(d))
        self.assertEqual(got["id"], "gram-2")

    def test_two_coins_with_the_same_name_and_symbol_is_no_match(self):
        d = {"slug": "twin", "name": "Twin", "symbol": "TWN", "platforms": []}
        got, _ = self.with_gecko({"/search": {"coins": [{"id": "twin", "symbol": "TWN", "name": "Twin"},
                                                        {"id": "twin-2", "symbol": "TWN", "name": "Twin"}]}},
                                 lambda: coingecko.match_cmc(d))
        self.assertIsNone(got)

    def resolve(self, detail, routes):
        orig = cmc.detail
        cmc.detail = lambda slug: detail
        try:
            return self.with_gecko(routes, lambda: REAL_RESOLVE_CMC(detail["slug"] if detail else "nosuchcoin"))[0]
        finally:
            cmc.detail = orig

    def test_not_on_coingecko_becomes_a_cmc_coin(self):
        d = {"id": 99002, "slug": "omegaland", "name": "OMEGALAND", "symbol": "OMGX", "logo": "https://logo.example/99002.png",
             "platforms": [{"chain_id": 56, "platform": "BNB Smart Chain (BEP20)", "address": EVM_ADDR}]}
        row = self.resolve(d, {"/search": {"coins": []}})
        self.assertEqual((row["coin_id"], row["symbol"], row["image_url"], row["cmc_slug"]),
                         ("cmc-omegaland", "OMGX", "https://logo.example/99002.png", "omegaland"))
        self.assertEqual(db.coin("cmc-omegaland")["cmc_slug"], "omegaland")
        with self.assertRaises(coingecko.IntakeError) as cm:
            self.resolve(None, {})
        self.assertIn("CoinMarketCap has no coin", str(cm.exception))

    def test_on_coingecko_is_the_coingecko_coin_and_keeps_its_cmc_slug(self):
        d = {"id": 99003, "slug": "xscoin", "name": "X's Coin", "symbol": "XSC", "logo": "https://logo.example/xsc.png",
             "platforms": [{"chain_id": 56, "platform": "BNB Smart Chain (BEP20)", "address": EVM_ADDR}]}
        xs = {"id": "x-s-coin-test", "symbol": "xsc", "name": "X's Coin", "image": {"large": "https://cg.example/xsc.png"}}
        row = self.resolve(d, {f"/coins/binance-smart-chain/contract/{EVM_ADDR.lower()}": xs})
        self.assertEqual((row["coin_id"], row["image_url"], row["cmc_slug"]), ("x-s-coin-test", "https://cg.example/xsc.png", "xscoin"))
        # a CoinGecko link for the same coin later does not forget the CMC slug
        coingecko._store({"coin_id": "x-s-coin-test", "symbol": "XSC", "name": "X's Coin", "image_url": ""})
        self.assertEqual(db.coin("x-s-coin-test")["cmc_slug"], "xscoin")
        self.assertEqual(cmc.slugs(db.coin("x-s-coin-test")), ["xscoin"])

    def test_a_cmc_coin_takes_its_venues_from_cmc_alone_behind_the_guard(self):
        db.run("""INSERT INTO coins (coin_id, symbol, name, image_url, fetched_at, cmc_slug) VALUES ('cmc-omgx','OMGX','OMEGALAND','',0,'omgx')
                  ON CONFLICT(coin_id) DO NOTHING""")
        listed = [{"venue": "bitget", "base": "OMGX", "quote": "USDT", "volume": 78000, "price": 0.0893, "outlier": False},
                  {"venue": "mexc", "base": "OMGX", "quote": "USDT", "volume": 62000, "price": 0.0893, "outlier": False},
                  {"venue": "bingx", "base": "OMGX", "quote": "USDT", "volume": 34000, "price": 0.0895, "outlier": False},
                  {"venue": "weex", "base": "OMGX", "quote": "USDT", "volume": 46000, "price": 0.0886, "outlier": False}]
        orig = cmc.pairs
        cmc.pairs = lambda coin: listed
        try:
            got, calls = self.with_gecko({}, lambda: REAL_CANDIDATES("cmc-omgx", config.VENUES + config.SHADOW_VENUES))
        finally:
            cmc.pairs = orig
        self.assertEqual(calls, [])  # CoinGecko is not asked about a coin it doesn't list
        self.assertEqual([c["venue"] for c in got], ["bitget", "mexc", "bingx"])  # weex unsupported
        self.assertTrue(all(c["anomaly"] and c["source"] == "cmc" for c in got))
        self.assertEqual(got[0]["ref"], 0.0893)  # CMC's median across every exchange
        snap = venues.snapshot
        try:
            venues.snapshot = lambda v, b, q: {"mid": 3.1}  # another token called OMGX
            with self.assertRaises(ValueError):
                coingecko.probe(got[0])
        finally:
            venues.snapshot = snap

    def test_a_cmc_link_through_the_bot_and_cancel_by_the_same_link(self):
        def fake_resolve_cmc(slug):
            return coingecko._store({"coin_id": "cmc-" + slug, "symbol": "OMGX", "name": "OMEGALAND", "image_url": ""}, slug)
        cands = [{"venue": "bitget", "base": "OMGX", "quote": "USDT", "volume": 78000},
                 {"venue": "mexc", "base": "OMGX", "quote": "USDT", "volume": 62000}]
        orig = coingecko.resolve_cmc, coingecko.candidates
        coingecko.resolve_cmc = fake_resolve_cmc
        coingecko.candidates = lambda cid, venue_ids=None: [c for c in cands if venue_ids and c["venue"] in venue_ids]
        q = []
        orig_put, bot.intake_q.put = bot.intake_q.put, q.append
        try:
            bot.handle({"message": {"chat": {"type": "private"}, "from": {"id": 111},
                                    "text": "https://coinmarketcap.com/currencies/omegaland-t/"}})
            self.assertEqual(q, [("111", "cmc:omegaland-t")])
            bot.intake_one("111", "cmc:omegaland-t")
        finally:
            bot.intake_q.put = orig_put
            coingecko.resolve_cmc, coingecko.candidates = orig
        self.assertIn("OMEGALAND (OMGX)", SENT[-1][1])
        w = db.one("SELECT * FROM watches WHERE coin_id='cmc-omegaland-t' AND owner='111' AND status='collecting'")
        self.assertIsNotNone(w)
        bot.handle({"message": {"chat": {"type": "private"}, "from": {"id": 111},
                                "text": "/cancel https://coinmarketcap.com/currencies/omegaland-t/"}})
        self.assertIn("Stopped cmc-omegaland-t", SENT[-1][1])
        self.assertEqual(db.one("SELECT status FROM watches WHERE id=?", w["id"])["status"], "cancelled")

    def test_a_refused_cmc_link_is_named_with_its_site(self):
        orig = coingecko.resolve_cmc
        coingecko.resolve_cmc = lambda slug: (_ for _ in ()).throw(coingecko.IntakeError(f"CoinMarketCap has no coin “{slug}”."))
        try:
            bot.intake_one("111", "cmc:nosuchcoin")
        finally:
            coingecko.resolve_cmc = orig
        self.assertIn("nosuchcoin (CoinMarketCap): CoinMarketCap has no coin", SENT[-1][1])

    def test_an_old_database_gets_the_column(self):
        import sqlite3
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE coins (coin_id TEXT PRIMARY KEY, symbol TEXT, name TEXT, image_url TEXT, fetched_at INTEGER)")
        c.executescript(db.SCHEMA)
        db._migrate(c)
        self.assertIn("cmc_slug", {r[1] for r in c.execute("PRAGMA table_info(coins)")})


class Brand(unittest.TestCase):
    """The card's logo, names and colours (auditbot/brand.py, 2026-11-19): neutral by default, a JSON file otherwise."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="brand-", dir=TMP)
        self.saved = config.BRAND_PATH
        brand._cache.clear()

    def tearDown(self):
        config.BRAND_PATH = self.saved
        brand._cache.clear()

    def write(self, data, logo=None):
        if logo:
            with open(os.path.join(self.dir, logo[0]), "wb") as f:
                f.write(logo[1])
        path = os.path.join(self.dir, "brand.json")
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    def _html(self):
        rows = [row(i * 3600) for i in range(73)]
        s = metrics.summarize(rows, config.requirements("gate"))
        return render.card_html({"coin_id": "x", "symbol": "X", "name": "X", "image_url": ""},
                                {"venue": "gate", "base": "X", "quote": "USDT"}, rows, s)

    def test_default_is_neutral_with_a_wordmark(self):
        config.BRAND_PATH = ""
        h = self._html()
        self.assertIn('<span class="wordmark">audit-bot</span>', h)
        self.assertNotIn('class="brand"', h)
        self.assertNotIn("[[", h)  # every colour token was filled
        self.assertIn("<h1>Market Audit</h1>", h)

    def test_a_brand_file_sets_logo_names_and_colours(self):
        config.BRAND_PATH = self.write({"name": "Example Markets", "logo": "mark.svg", "title": "Liquidity Report",
                                        "tagline": "Made-up tagline", "colors": {"accent": "#5B4FE0", "text": "#101010"}},
                                       ("mark.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>"))
        h = self._html()
        self.assertIn('<img class="brand" src="data:image/svg+xml;base64,', h)
        self.assertNotIn("wordmark\">", h)
        self.assertIn("<h1>Liquidity Report</h1>", h)
        self.assertIn("<p>Made-up tagline</p>", h)
        self.assertIn(".spot{font-size:12px;color:#5b4fe0;background:rgba(91,79,224,.1)", h)
        self.assertIn("color:#101010", h)
        self.assertIn("#20b26c", h)  # an unset colour keeps its default

    def test_a_bad_brand_file_is_refused_with_the_reason(self):
        for data, why in (({"colours": {}}, "unknown keys"), ({"colors": {"accent": "blue"}}, "#rrggbb"),
                          ({"colors": {"brand": "#ffffff"}}, "unknown colours"), ({"logo": "x.gif"}, "logo must be"),
                          ({"logo": "missing.png"}, "logo:")):
            brand._cache.clear()
            with self.assertRaises(brand.BrandError) as cm:
                brand.load(self.write(data))
            self.assertIn(why, str(cm.exception))

    def test_cards_are_cached_per_brand(self):
        config.BRAND_PATH = ""
        a = brand.fingerprint()
        config.BRAND_PATH = self.write({"colors": {"accent": "#123456"}})
        self.assertNotEqual(a, brand.fingerprint())

    def test_the_bot_names_the_team_only_from_a_brand_file(self):
        config.BRAND_PATH = ""
        bot.access_request("1901", "", "Made Up")
        self.assertIn("This bot is for an approved sales team.", [t for c, t in SENT if c == "1901"][-1])
        config.BRAND_PATH = self.write({"name": "Example Markets"})
        bot.access_request("1902", "", "Made Up Two")
        self.assertIn("This bot is for the Example Markets sales team.", [t for c, t in SENT if c == "1902"][-1])

    def test_the_example_brand_loads(self):
        b = brand.load(os.path.join(config.ROOT, "examples", "brand", "brand.json"))
        self.assertTrue(b["logo_uri"].startswith("data:image/svg+xml;base64,"))
        self.assertEqual(set(b["colors"]), set(brand.DEFAULT["colors"]))


if __name__ == "__main__":
    unittest.main()
