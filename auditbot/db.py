"""SQLite store. One connection per thread; WAL so the collector, bot and API don't block each other."""
import json
import os
import sqlite3
import threading
import time

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
  tg_id TEXT PRIMARY KEY, name TEXT, username TEXT, asked_name TEXT,
  status TEXT NOT NULL,              -- pending | approved | denied | revoked
  role TEXT NOT NULL DEFAULT 'member',  -- member | root
  join_code TEXT UNIQUE, created_at INTEGER, decided_at INTEGER, decided_by TEXT
);
CREATE TABLE IF NOT EXISTS root_codes (code TEXT PRIMARY KEY, created_at INTEGER, used_by TEXT);
CREATE TABLE IF NOT EXISTS coins (
  coin_id TEXT PRIMARY KEY, symbol TEXT, name TEXT, image_url TEXT, fetched_at INTEGER,
  shadow_checked_at INTEGER,         -- when its shadow venues were looked up (once per coin)
  all_checked_at INTEGER,            -- when every venue's pair was looked up for the rep's picker (once per coin)
  lookup_rules INTEGER,              -- coingecko.LOOKUP_RULES that lookup ran under; older = looked up again
  cmc_slug TEXT                      -- CoinMarketCap's slug, when the coin came in by a CMC link (2026-11-18)
);
CREATE TABLE IF NOT EXISTS markets (           -- the venues chosen for a coin at its first watch
  coin_id TEXT, venue TEXT, base TEXT, quote TEXT, cg_volume_usd REAL, rank INTEGER, chosen_at INTEGER,
  shadow INTEGER NOT NULL DEFAULT 0, -- 1 = not one of the coin's default card venues; a rep may tick it (watch_venues)
  PRIMARY KEY (coin_id, venue)
);
CREATE TABLE IF NOT EXISTS watches (
  id INTEGER PRIMARY KEY AUTOINCREMENT, coin_id TEXT NOT NULL,
  owner TEXT,                         -- tg_id for bot watches
  lead TEXT,                          -- opaque key for API watches
  source TEXT NOT NULL,               -- bot | api
  created_at INTEGER NOT NULL, ready_at INTEGER NOT NULL,
  status TEXT NOT NULL,               -- collecting | delivered | failed | cancelled
  attempts INTEGER NOT NULL DEFAULT 0, delivered_at INTEGER, note TEXT,
  reqs TEXT                           -- JSON {venue: {check_id: number | null}}: this audit's own requirements
);
CREATE INDEX IF NOT EXISTS watches_coin ON watches (coin_id, status);
CREATE TABLE IF NOT EXISTS watch_venues (  -- the venues this audit sends cards for (the rep's ticks)
  watch_id INTEGER, venue TEXT, PRIMARY KEY (watch_id, venue)
);
CREATE TABLE IF NOT EXISTS pending_input (  -- a rep was asked to type a new requirement value
  tg_id TEXT PRIMARY KEY, watch_id INTEGER, venue TEXT, check_id TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS deliveries (   -- which venue's card went to which watch, and when
  watch_id INTEGER, venue TEXT, delivered_at INTEGER, PRIMARY KEY (watch_id, venue)
);
CREATE TABLE IF NOT EXISTS snapshots (
  coin_id TEXT, venue TEXT, pair TEXT, ts INTEGER,
  mid REAL, best_bid REAL, best_ask REAL, spread_pct REAL,
  bid_2pct_usd REAL, ask_2pct_usd REAL, trunc_bid INTEGER, trunc_ask INTEGER,
  vol24h_usd REAL, error TEXT,
  book TEXT                          -- JSON: per-band depth + level counts (venues.book_stats), NULL before 2026-11
);
CREATE INDEX IF NOT EXISTS snapshots_cv ON snapshots (coin_id, venue, ts);
"""


def conn():
    c = getattr(_local, "c", None)
    if c is None:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        c = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        c.executescript(SCHEMA)
        _migrate(c)
        _local.c = c
    return c


def _migrate(c):
    """Columns added after the first deploy (2026-10-30)."""
    have = lambda t: {r[1] for r in c.execute(f"PRAGMA table_info({t})")}
    if "shadow_checked_at" not in have("coins"):
        c.execute("ALTER TABLE coins ADD COLUMN shadow_checked_at INTEGER")
    if "shadow" not in have("markets"):
        c.execute("ALTER TABLE markets ADD COLUMN shadow INTEGER NOT NULL DEFAULT 0")
    # 2026-11-03: per-audit venues and requirements, per-band book stats
    if "all_checked_at" not in have("coins"):
        c.execute("ALTER TABLE coins ADD COLUMN all_checked_at INTEGER")
    if "lookup_rules" not in have("coins"):
        c.execute("ALTER TABLE coins ADD COLUMN lookup_rules INTEGER")
    if "reqs" not in have("watches"):
        c.execute("ALTER TABLE watches ADD COLUMN reqs TEXT")
    if "book" not in have("snapshots"):
        c.execute("ALTER TABLE snapshots ADD COLUMN book TEXT")
    # 2026-11-18: CoinMarketCap links
    if "cmc_slug" not in have("coins"):
        c.execute("ALTER TABLE coins ADD COLUMN cmc_slug TEXT")
    # audits from before the picker send cards for the coin's default venues, as they always did
    c.execute("""INSERT OR IGNORE INTO watch_venues (watch_id, venue)
                 SELECT w.id, m.venue FROM watches w JOIN markets m ON m.coin_id = w.coin_id AND m.shadow = 0
                 WHERE NOT EXISTS (SELECT 1 FROM watch_venues v WHERE v.watch_id = w.id)""")


def close():
    """For short-lived threads (one per API request)."""
    c = getattr(_local, "c", None)
    if c is not None:
        c.close()
        _local.c = None


def q(sql, *args):
    return conn().execute(sql, args).fetchall()


def one(sql, *args):
    return conn().execute(sql, args).fetchone()


def run(sql, *args):
    return conn().execute(sql, args)


def tx():
    """`with db.tx():` — BEGIN IMMEDIATE so check-then-write is atomic across threads."""
    return _Tx()


class _Tx:
    def __enter__(self):
        conn().execute("BEGIN IMMEDIATE")

    def __exit__(self, et, *_):
        conn().execute("ROLLBACK" if et else "COMMIT")


def now():
    return int(time.time())


def active_markets():
    """(coin_id, venue, base, quote) pairs some watch still needs collected."""
    t = now()
    grace = config.API_GRACE_H * 3600
    return q(
        """SELECT DISTINCT m.coin_id, m.venue, m.base, m.quote FROM markets m
           JOIN watches w ON w.coin_id = m.coin_id
           WHERE w.status = 'collecting' OR (w.source = 'api' AND w.ready_at + ? > ?)""",
        grace, t)


def markets_for(coin_id, shadow=False):
    """The coin's default card venues, best first. shadow=True: the others collected for it."""
    return q("SELECT * FROM markets WHERE coin_id = ? AND shadow = ? ORDER BY rank", coin_id, int(shadow))


def all_markets(coin_id):
    """Every venue collected for the coin: the defaults first, then the rest by CoinGecko volume."""
    return q("SELECT * FROM markets WHERE coin_id = ? ORDER BY shadow, CASE WHEN shadow = 0 THEN rank END,"
             " cg_volume_usd DESC, venue", coin_id)


def market(coin_id, venue):
    r = one("SELECT * FROM markets WHERE coin_id = ? AND venue = ?", coin_id, venue)
    return dict(r) if r else None


def watch_markets(watch_id):
    """The venues one audit sends cards for, in the coin's order."""
    w = one("SELECT coin_id FROM watches WHERE id = ?", watch_id)
    if not w:
        return []
    picked = {r["venue"] for r in q("SELECT venue FROM watch_venues WHERE watch_id = ?", watch_id)}
    return [m for m in all_markets(w["coin_id"]) if m["venue"] in picked]


def watch_reqs(w):
    """{venue: {check_id: value}} for a watch row."""
    try:
        return json.loads(w["reqs"] or "{}")
    except Exception:
        return {}


def snapshots(coin_id, venue, since_ts):
    return [dict(r) for r in q(
        "SELECT * FROM snapshots WHERE coin_id = ? AND venue = ? AND ts >= ? ORDER BY ts",
        coin_id, venue, since_ts)]


def last_ts(coin_id, venue):
    r = one("SELECT MAX(ts) AS t FROM snapshots WHERE coin_id = ? AND venue = ? AND error IS NULL", coin_id, venue)
    return r["t"] if r else None


def coin(coin_id):
    r = one("SELECT * FROM coins WHERE coin_id = ?", coin_id)
    return dict(r) if r else None


def dumps(x):
    return json.dumps(x, separators=(",", ":"))
