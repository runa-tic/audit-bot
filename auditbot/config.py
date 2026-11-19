"""Everything tunable, from the environment (an env file on the server, mode 600)."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _env(name, default=""):
    return os.environ.get(name, default).strip()


DATA_DIR = _env("AUDIT_DATA", os.path.join(ROOT, "var"))
DB_PATH = os.path.join(DATA_DIR, "audit.db")

BOT_TOKEN = _env("AUDIT_BOT_TOKEN")
# Root = numeric Telegram ids, never usernames (handles get released and re-registered).
# More roots can be added at runtime with a one-time claim code.
ROOT_TG_IDS = {x for x in _env("AUDIT_ROOT_TG_IDS").replace(" ", "").split(",") if x}

API_TOKEN = _env("AUDIT_API_TOKEN")
API_BIND = _env("AUDIT_API_BIND", "127.0.0.1:8088")

CADENCE_MIN = int(_env("AUDIT_CADENCE_MIN", "60"))   # 15 for the test drive
WINDOW_H = int(_env("AUDIT_WINDOW_H", "72"))
# Venues a card may come from, in our ids (venues.py). Sales 10-30: Binance, OKX, Bybit, Gate;
# KuCoin because the reference card is KuCoin; Kraken (2026-10-30) with USD pairs allowed there.
VENUES = [v for v in _env("AUDIT_VENUES", "binance,okx,bybit,gate,kucoin,kraken").split(",") if v]
MAX_VENUES = int(_env("AUDIT_MAX_VENUES", "3"))
# Collected but never shown (2026-10-30): a watched coin's
# pairs on these venues are snapshotted like the rest and kept out of selection, cards, replies and the API.
SHADOW_VENUES = [v for v in _env("AUDIT_SHADOW_VENUES", "mexc,bitget,htx,bingx").split(",") if v and v not in VENUES]
MAX_ACTIVE_PER_PERSON = int(_env("AUDIT_MAX_ACTIVE_PER_PERSON", "80"))
MAX_ACTIVE_TOTAL = int(_env("AUDIT_MAX_ACTIVE_TOTAL", "400"))  # the reps' (bot) audits running at once
# The API client's audits running at once, counted apart: the client registers one per conversation, and it
# must never be able to fill the reps' room (a rep's audit refused because a lane was busy).
MAX_ACTIVE_API = int(_env("AUDIT_MAX_ACTIVE_API", "400"))
# API watches keep collecting this long past ready_at, so a day-3 fetch finds fresh data.
API_GRACE_H = int(_env("AUDIT_API_GRACE_H", "48"))
OPERATOR_NAME = _env("AUDIT_OPERATOR_NAME", "the operator")  # who approves access, in bot replies
API_CLIENT_NAME = _env("AUDIT_API_CLIENT_NAME", "API client")  # what root's /list all calls the API's audits
COINGECKO_KEY = _env("AUDIT_COINGECKO_KEY")  # optional demo key; keyless works for intake volumes
# The cards' logo, names and colours (auditbot/brand.py): a JSON file; unset = the neutral default theme
BRAND_PATH = _env("AUDIT_BRAND")


BANDS = (0.25, 1, 2, 5, 10)  # % from mid at which each snapshot stores per-side depth (venues.measure)


def _load():
    with open(os.path.join(ROOT, "thresholds.json"), encoding="utf-8") as f:
        return json.load(f)


def trend_band_pct():
    return _load().get("trend_band_pct", 1)


def requirements(venue, overrides=None):
    """The venue's checks (thresholds.json), with one audit's overrides applied: {check_id: number | None}.
    None switches a check off for that audit. A venue with no entry has no checks (numbers only, no verdict)."""
    out = []
    for c in _load()["venues"].get(venue, []):
        c = dict(c)
        if overrides and c["id"] in overrides:
            v = overrides[c["id"]]
            if v is None:
                continue
            c["max" if "max" in c else "min"] = v
            c["custom"] = True
        out.append(c)
    return out
