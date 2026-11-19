#!/usr/bin/env python3
"""Render a sample card from made-up data: no exchange, API or Telegram token needed.

  python examples/demo.py                                    # neutral default theme -> demo-card.png
  python examples/demo.py --brand examples/brand/brand.json  # the light example brand
  python examples/demo.py --out /tmp/card.png --seed 7

It writes 72 h of 15-minute snapshots for a fictional DEMO/USDT market on Gate into a throwaway database, then
renders that market's card with the same code the bot uses. Needs Playwright's Chromium (see the README).
"""
import argparse
import math
import os
import random
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--brand", help="a brand JSON file (default: the neutral theme)")
    ap.add_argument("--out", default="demo-card.png")
    ap.add_argument("--seed", type=int, default=3)
    a = ap.parse_args()

    data = tempfile.mkdtemp(prefix="audit-bot-demo-")
    os.environ.update(AUDIT_DATA=data, AUDIT_BRAND=os.path.abspath(a.brand) if a.brand else "",
                      AUDIT_CADENCE_MIN="15", AUDIT_BOT_TOKEN="", AUDIT_API_TOKEN="")
    sys.path.insert(0, os.path.dirname(HERE))
    from auditbot import db, render  # noqa: E402  (config reads the environment at import)

    rnd = random.Random(a.seed)
    cid, venue, end = "demo-coin", "gate", (db.now() // 900) * 900
    db.run("INSERT INTO coins (coin_id, symbol, name, image_url, fetched_at) VALUES (?,?,?,?,?)",
           cid, "DEMO", "Demo Coin", "", end)
    db.run("""INSERT INTO markets (coin_id, venue, base, quote, cg_volume_usd, rank, chosen_at, shadow)
              VALUES (?,?,?,?,?,0,?,0)""", cid, venue, "DEMO", "USDT", 41000, end)
    mid, bid, ask = 0.42, 900.0, 700.0
    for i in range(289):  # 72 h at 15 min, oldest first
        ts = end - (288 - i) * 900
        day = i / 96
        mid *= 1 + rnd.gauss(0, 0.004)
        # each side wanders around a level that thins out over the three days
        bid = max(80.0, bid + 0.12 * (820 - 110 * day + 90 * math.sin(i / 14) - bid) + rnd.gauss(0, 45))
        ask = max(80.0, ask + 0.12 * (690 - 140 * day + 70 * math.cos(i / 11) - ask) + rnd.gauss(0, 45))
        spread = max(0.05, abs(rnd.gauss(0.35, 0.18)) + (0.9 if rnd.random() < 0.03 else 0))
        db.run("""INSERT INTO snapshots (coin_id, venue, pair, ts, mid, best_bid, best_ask, spread_pct,
                  bid_2pct_usd, ask_2pct_usd, trunc_bid, trunc_ask, vol24h_usd) VALUES (?,?,?,?,?,?,?,?,?,?,0,0,?)""",
               cid, venue, "DEMO/USDT", ts, mid, mid * (1 - spread / 200), mid * (1 + spread / 200), spread,
               bid, ask, 36000 - 2500 * day + rnd.gauss(0, 1500))
    render._ex_img[venue] = ""  # no exchange logo fetch: the demo stays offline
    out = render.render(cid, venue)
    if not out:
        sys.exit("no card rendered")
    shutil.copy(out[0], a.out)
    shutil.rmtree(data, ignore_errors=True)
    print(a.out)


if __name__ == "__main__":
    main()
