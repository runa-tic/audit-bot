"""Server-side admin. Run with the service's env loaded.

  python -m auditbot.admin root-code                 # one-time code; send the bot /claim <code>
  python -m auditbot.admin people | watches
  python -m auditbot.admin add --owner <tg_id> <links…>   # bulk intake (e.g. a list of 30 projects); CoinGecko or CMC links
  python -m auditbot.admin render <coin_id> [venue] [--out file.png]
  python -m auditbot.admin collect                   # one collection pass now
  python -m auditbot.admin add-venue <coin_id> <venue> <base> <quote>   # add a card venue to a watched coin
"""
import shutil
import sys
from datetime import datetime, timezone

from . import coingecko, db, jobs, people, render


def main(argv):
    cmd, rest = (argv[0], argv[1:]) if argv else ("help", [])
    if cmd == "root-code":
        c = people.mint_root_code()
        print(f"Send the bot:  /claim {people.pretty(c)}   (valid 24 h, once)")
    elif cmd == "people":
        for p in db.q("SELECT * FROM people ORDER BY status, created_at"):
            print(p["tg_id"], p["status"], p["role"], p["name"] or p["asked_name"], "@" + (p["username"] or ""))
    elif cmd == "watches":
        for w in db.q("SELECT * FROM watches ORDER BY id DESC LIMIT 100"):
            print(w["id"], w["coin_id"], w["source"], w["owner"] or w["lead"], w["status"], w["ready_at"])
    elif cmd == "add":
        owner = rest[rest.index("--owner") + 1] if "--owner" in rest else None
        slugs = coingecko.refs(" ".join(rest))
        for s in slugs:
            try:
                r = jobs.start_watch(s, owner=owner)
                print("ok", r["coin"]["coin_id"], [m["venue"] for m in r["markets"]], "existing" if r["existing"] else "")
            except Exception as ex:
                print("refused", s, ex)
    elif cmd == "render":
        coin_id = rest[0]
        venue = rest[1] if len(rest) > 1 and not rest[1].startswith("--") else db.markets_for(coin_id)[0]["venue"]
        out = render.render(coin_id, venue)
        if not out:
            sys.exit("no data")
        dest = rest[rest.index("--out") + 1] if "--out" in rest else out[0]
        if dest != out[0]:
            shutil.copy(out[0], dest)
        print(dest)
    elif cmd == "add-venue":  # add-venue <coin_id> <venue> <base> <quote>
        m = jobs.add_venue(*rest[:4])
        print("added", m["coin_id"], m["venue"], f'{m["base"]}/{m["quote"]}', "rank", m["rank"], "first snapshot taken")
        for w in db.q("SELECT * FROM watches WHERE coin_id=? AND status='collecting'", m["coin_id"]):
            due = datetime.fromtimestamp(jobs.market_due(dict(w), m), timezone.utc).strftime("%Y-%m-%d %H:%MZ")
            print(f'  watch {w["id"]} ({w["source"]}): this venue\'s card due {due}')
    elif cmd == "collect":
        print("collected %d/%d" % jobs.collect_once())
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
