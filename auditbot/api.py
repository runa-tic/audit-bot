"""HTTP API for an outreach system: it registers a lead's coin on day 0 and fetches the cards on day 3 (2026-10-30).

  POST /v1/watch {"coin_id", "lead"}   -> 200 {coin_id, venues, ready_at} | 422 no_whitelisted_venue
  GET  /v1/card/<coin_id>.png          -> the top venue's card
  GET  /v1/card/<coin_id>/<venue>.png  -> one venue's card
  GET  /v1/cards/<coin_id>             -> JSON: each venue's readiness and window
  GET  /v1/health                      -> no auth

The outreach client's follow-up attachment contract (2026-11), the same token:
  GET  /v1/echo/ping                   -> 200 {ok, contract}: the client's connection test; starts nothing
  POST /v1/echo/register {conversation, lane, lead: {refs: {coingecko}}, steps}
                                       -> 200 {handle, ready_at, existing, venues} | 422 {reason} (no CoinGecko id,
                                          not an id, no coin, no USDT pair) | 400 bad_json / bad_length /
                                          bad_conversation / body_too_large | 503 at_capacity / upstream (asked again later)
  GET  /v1/echo/attachment/<handle>?step=<n>&conversation=<id>
                                       -> 200 PNG | 425 + Retry-After while that audit's window fills |
                                          404 not_watched / no_file_for_step / venue_not_watched / no_data |
                                          400 bad_step / bad_conversation | 503 render_failed
  A handle answers only for the conversation it was registered for (404 for any other).
  One audit per conversation (lead "echo:<conversation>"), so a retried register returns the same handle.
  Readiness is that audit's own window, never another audit's on the same coin. Step 1 is the top venue's
  card; no other step has a file yet (404, and the client sends that step without one).

Cards: 200 + X-Audit-From/X-Audit-To, 404 never watched, 425 + Retry-After while the window fills.
Auth: Authorization: Bearer <AUDIT_API_TOKEN>.
"""
import hmac
import json
import logging
import os
import re
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import coingecko, config, db, jobs, render

log = logging.getLogger("audit.api")
CARD = re.compile(r"^/v1/card/([a-z0-9-]+)(?:/([a-z]+))?\.png$")
CARDS = re.compile(r"^/v1/cards/([a-z0-9-]+)$")
ECHO_ATTACHMENT = re.compile(r"^/v1/echo/attachment/(\d{1,12})$")
BODY_MAX = 20000  # a request body larger than this is refused unread
CONVERSATION = re.compile(r"[A-Za-z0-9._:-]{1,64}")


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def readiness(coin_id):
    """-> (ready, retry_after_s) or None when the coin was never watched."""
    ws = db.q("SELECT * FROM watches WHERE coin_id=? AND status != 'cancelled'", coin_id)
    if not ws or not db.markets_for(coin_id):
        return None
    first = min(w["ready_at"] for w in ws)
    if db.now() >= first:
        return True, 0
    # an older watch may already have collected a full window
    span = db.one("SELECT MAX(ts) - MIN(ts) s FROM snapshots WHERE coin_id=? AND error IS NULL AND ts > ?",
                  coin_id, db.now() - config.WINDOW_H * 3600 - 3600)["s"] or 0
    if span >= (config.WINDOW_H - 1) * 3600:
        return True, 0
    return False, first - db.now()


class Handler(BaseHTTPRequestHandler):
    server_version = "audit-bot"
    timeout = 15  # seconds a socket may sit without sending: a stalled client frees its thread

    def handle(self):
        try:
            super().handle()
        finally:
            db.close()

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _json(self, code, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        got = self.headers.get("Authorization", "")
        want = f"Bearer {config.API_TOKEN}"
        if config.API_TOKEN and hmac.compare_digest(got.encode(), want.encode()):
            return True
        self._json(401, {"error": "unauthorized"})
        return False

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/v1/health":
            return self._json(200, {"ok": True})
        if not self._authed():
            return
        if path == "/v1/echo/ping":
            return self._json(200, {"ok": True, "contract": "echo-attachments/1"})
        m = ECHO_ATTACHMENT.match(path)
        if m:
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            try:
                step = int((q.get("step") or ["1"])[0])
            except ValueError:
                return self._json(400, {"error": "bad_step"})
            conv = (q.get("conversation") or [""])[0]
            if not CONVERSATION.fullmatch(conv):
                return self._json(400, {"error": "bad_conversation"})
            return self._echo_attachment(int(m.group(1)), step, conv)
        m = CARD.match(path)
        if m:
            return self._card(m.group(1), m.group(2))
        m = CARDS.match(path)
        if m:
            return self._cards(m.group(1))
        self._json(404, {"error": "not_found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/v1/watch", "/v1/echo/register"):
            return self._json(404, {"error": "not_found"})
        if not self._authed():
            return
        if path == "/v1/echo/register":
            return self._echo_register()
        body = self._body()
        if body is None:
            return
        try:
            coin_id = str(body.get("coin_id") or "").strip().lower()
            lead = str(body.get("lead") or "")[:200] or None
        except Exception:
            return self._json(400, {"error": "bad_json"})
        if not re.fullmatch(r"[a-z0-9-]{1,100}", coin_id):
            return self._json(400, {"error": "bad_coin_id"})
        try:
            r = jobs.start_watch(coin_id, lead=lead, source="api")
        except coingecko.IntakeError as ex:
            code = "no_whitelisted_venue" if "supported exchange" in str(ex) else "intake_refused"
            return self._json(422, {"error": code, "detail": str(ex)})
        except Exception as ex:
            log.exception("watch %s", coin_id)
            return self._json(503, {"error": "upstream", "detail": str(ex)[:200]}, {"Retry-After": "300"})
        self._json(200, {"coin_id": r["coin"]["coin_id"],
                         "venues": [{"venue": m["venue"], "pair": f'{m["base"]}/{m["quote"]}'} for m in r["markets"]],
                         "ready_at": iso(r["watch"]["ready_at"]), "existing": r["existing"]})

    def _card(self, coin_id, venue):
        st = readiness(coin_id)
        if st is None:
            return self._json(404, {"error": "not_watched"})
        ready, wait = st
        if not ready:
            return self._json(425, {"error": "window_filling", "ready_in_s": wait}, {"Retry-After": str(max(60, wait))})
        ms = db.markets_for(coin_id)
        m = ms[0] if not venue else next((x for x in ms if x["venue"] == venue), None)
        if not m:
            return self._json(404, {"error": "venue_not_watched"})
        self._png(coin_id, m)

    def _png(self, coin_id, m):
        """The venue's card as the response: the 72 h up to its last snapshot (render.render)."""
        try:
            out = render.render(coin_id, m["venue"])
        except Exception as ex:
            log.exception("render %s/%s", coin_id, m["venue"])
            return self._json(503, {"error": "render_failed", "detail": str(ex)[:200]}, {"Retry-After": "120"})
        if not out:
            return self._json(404, {"error": "no_data"})
        path, s = out
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Audit-From", iso(s["from"]))
        self.send_header("X-Audit-To", iso(s["to"]))
        self.send_header("X-Audit-Venue", m["venue"])
        self.send_header("X-Audit-Pair", f'{m["base"]}/{m["quote"]}')
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        """The JSON body, or None after answering 400. Content-Length is checked first: a negative or
        missing-but-streamed length must never become read(-1), which reads until the peer hangs up."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > BODY_MAX:
            self.close_connection = True
            self._json(400, {"error": "body_too_large" if n > BODY_MAX else "bad_length"})
            return None
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            self._json(400, {"error": "bad_json"})
            return None
        if not isinstance(body, dict):
            self._json(400, {"error": "bad_json"})
            return None
        return body

    def _echo_register(self):
        body = self._body()
        if body is None:
            return
        try:
            refs = (body.get("lead") or {}).get("refs") or {}
            coin_id = str(refs.get("coingecko") or "").strip().lower()
            conv = str(body.get("conversation") or "")
        except Exception:
            return self._json(400, {"error": "bad_json"})
        if not CONVERSATION.fullmatch(conv):
            return self._json(400, {"error": "bad_conversation"})
        if not coin_id:
            return self._json(422, {"reason": "no CoinGecko id for this lead"})
        if not re.fullmatch(r"[a-z0-9-]{1,100}", coin_id):
            return self._json(422, {"reason": f"'{coin_id[:100]}' is not a CoinGecko id"})
        try:
            r = jobs.start_watch(coin_id, lead=f"echo:{conv}", source="api")
        except jobs.CapacityError as ex:  # a full bot: not a verdict on the lead, so asked again later
            return self._json(503, {"error": "at_capacity", "detail": str(ex)}, {"Retry-After": "3600"})
        except coingecko.IntakeError as ex:
            return self._json(422, {"reason": str(ex)})
        except Exception as ex:
            log.exception("echo register %s", coin_id)
            return self._json(503, {"error": "upstream", "detail": str(ex)[:200]}, {"Retry-After": "300"})
        w = r["watch"]
        self._json(200, {"handle": str(w["id"]), "ready_at": iso(w["ready_at"]), "existing": r["existing"],
                         "venues": [{"venue": m["venue"], "pair": f'{m["base"]}/{m["quote"]}'} for m in r["picked"]]})

    def _echo_attachment(self, watch_id, step, conversation):
        w = db.one("SELECT * FROM watches WHERE id=? AND source='api'", watch_id)
        # A handle is one conversation's: asked for by any other (a moved provider, a reused id), it is
        # not there, so no lead is ever sent another lead's card.
        if not w or w["status"] == "cancelled" or (w["lead"] or "") != f"echo:{conversation}":
            return self._json(404, {"error": "not_watched"})
        if step != 1:
            return self._json(404, {"error": "no_file_for_step", "detail": f"there is no file for step {step} yet"})
        wait = w["ready_at"] - db.now()
        if wait > 0:  # this audit's own window, whatever other audits of the coin have collected
            return self._json(425, {"error": "window_filling", "ready_in_s": wait}, {"Retry-After": str(max(60, wait))})
        ms = db.watch_markets(watch_id)
        if not ms:
            return self._json(404, {"error": "venue_not_watched"})
        self._png(w["coin_id"], ms[0])

    def _cards(self, coin_id):
        st = readiness(coin_id)
        if st is None:
            return self._json(404, {"error": "not_watched"})
        out = []
        for m in db.markets_for(coin_id):
            r = db.one("SELECT MIN(ts) a, MAX(ts) b FROM snapshots WHERE coin_id=? AND venue=? AND error IS NULL AND ts > ?",
                       coin_id, m["venue"], db.now() - config.WINDOW_H * 3600)
            out.append({"venue": m["venue"], "pair": f'{m["base"]}/{m["quote"]}', "ready": st[0],
                        "from": iso(r["a"]) if r["a"] else None, "to": iso(r["b"]) if r["b"] else None,
                        "url": f"/v1/card/{coin_id}/{m['venue']}.png"})
        self._json(200, {"coin_id": coin_id, "ready": st[0], "retry_after_s": st[1], "venues": out})


def serve(stop):
    host, _, port = config.API_BIND.rpartition(":")
    srv = ThreadingHTTPServer((host or "127.0.0.1", int(port)), Handler)
    srv.timeout = 1
    log.info("api on %s", config.API_BIND)
    while not stop.is_set():
        srv.handle_request()
    srv.server_close()
