"""Minimal Bot API client (stdlib). The token never leaves this module and is never logged."""
import http.client
import json
import os
import socket
import ssl
import uuid
import urllib.error
import urllib.request

from . import config


class TgError(Exception):
    pass


class _V4Connection(http.client.HTTPSConnection):
    """IPv4 only. From the audit VPS about 1 in 7 IPv6 requests to api.telegram.org stalls for 10 s or more
    while IPv4 answers in under 0.1 s (measured 2026-11-03): button presses expired before the bot could answer
    them, and getUpdates timed out ~50 times a day."""

    def connect(self):
        addr = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
        sock = socket.create_connection(addr, self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _V4Handler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_V4Connection, req, context=self._context)


_opener = urllib.request.build_opener(_V4Handler(context=ssl.create_default_context()))


def _url(method):
    return f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}"


def _post(method, body, ctype, timeout):
    req = urllib.request.Request(_url(method), data=body, headers={"Content-Type": ctype})
    try:
        with _opener.open(req, timeout=timeout) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            out = json.loads(e.read().decode())
        except Exception:
            raise TgError(f"{method}: HTTP {e.code}") from None
    if not out.get("ok"):
        raise TgError(f"{method}: {out.get('description', 'refused')}")
    return out["result"]


def call(method, http_timeout=30, **params):
    params = {k: v for k, v in params.items() if v is not None}
    return _post(method, json.dumps(params).encode(), "application/json", http_timeout)


def send(chat_id, text, **kw):
    return call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                disable_web_page_preview=True, **kw)


def edit(chat_id, message_id, text, reply_markup=None):
    """Replace a message's text and buttons in place. Telegram refuses an edit that changes nothing; that is fine."""
    try:
        return call("editMessageText", chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML",
                    disable_web_page_preview=True, reply_markup=reply_markup or {"inline_keyboard": []})
    except TgError as e:
        if "not modified" not in str(e):
            raise


def answer(callback_id, text=None):
    """Stop the button's spinner; text shows as a small toast."""
    return call("answerCallbackQuery", http_timeout=8, callback_query_id=callback_id, text=text)


def _multipart(fields, files):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, path in files.items():
        with open(path, "rb") as f:
            data = f.read()
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{os.path.basename(path)}"\r\n'
                     f"Content-Type: image/png\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def send_photos(chat_id, items):
    """items: [(png_path, caption)]. One photo -> sendPhoto, 2-10 -> one album."""
    if len(items) == 1:
        path, cap = items[0]
        body, ctype = _multipart({"chat_id": chat_id, "caption": cap, "parse_mode": "HTML"}, {"photo": path})
        return _post("sendPhoto", body, ctype, 120)
    media, files = [], {}
    for i, (path, cap) in enumerate(items):
        files[f"p{i}"] = path
        media.append({"type": "photo", "media": f"attach://p{i}", "caption": cap, "parse_mode": "HTML"})
    body, ctype = _multipart({"chat_id": chat_id, "media": json.dumps(media)}, files)
    return _post("sendMediaGroup", body, ctype, 180)
