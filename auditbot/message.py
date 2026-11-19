"""The suggested message that goes with the cards. Optional for the salesperson to use.

PLACEHOLDER WORDING until the team's generic text (due at the 2026-11-02 morning call). Edit
message_template.txt, not this file: {project} {symbol} {venues} {findings} are filled in.
"""
import os

from . import checks, config, metrics, render, venues

TEMPLATE = os.path.join(config.ROOT, "message_template.txt")


def _value(x, q):
    c, v = x["c"], x["value"]
    if c["kind"] == "depth" and c.get("side") != "combined":
        return f"{render.compact(v[0])} bid / {render.compact(v[1])} ask {q}"
    if c["kind"] == "levels":
        return f"{v[0]:.0f} bid / {v[1]:.0f} ask levels"
    if c["kind"] in ("depth", "volume"):
        return f"{render.compact(v)} {q}"
    return f"{v:.2f}%"


def _finding(m, s):
    """One line per venue: ±2% depth and spread always, then every other requirement it misses."""
    name = venues.NAME.get(m["venue"], m["venue"])
    q = m["quote"]
    d2, sp = metrics.check(s, "depth2"), metrics.check(s, "spread")
    if d2 and d2["value"] is not None:
        part = f"±2% depth averaged {_value(d2, q)}"
        if d2["ok"] is False:
            part += f" (vs {checks.req_text(d2['c'], q).lstrip('≥ ')} expected)"
    else:
        part = f"±2% depth averaged {render.compact(s['bid_avg'])} bid / {render.compact(s['ask_avg'])} ask {q}"
    parts = [part]
    if sp and sp["value"] is not None:
        parts.append(f"spread {sp['value']:.2f}% on average" + ("" if sp["ok"] else f", above the {sp['c']['max']:.2f}% {name} asks for"))
    else:
        parts.append(f"spread {s['spread_avg']:.2f}% on average")
    for x in s["checks"]:
        if x["c"]["id"] in ("depth2", "spread") or x["ok"] is not False:
            continue
        parts.append(f"{checks.label(x['c']).lower()} {_value(x, q)} (needs {checks.req_text(x['c'], q)})")
    if s["trend"] is not None and s["trend_state"] == "declining":
        parts.append(f"depth down {abs(s['trend']):.0f}% over the 3 days")
    if s.get("vol_trend") is not None and s.get("vol_trend_state") == "declining":
        parts.append(f"24h volume down {abs(s['vol_trend']):.0f}% over the 3 days")
    return f"• {name}: " + "; ".join(parts) + "."


def suggested(coin, results):
    with open(TEMPLATE, encoding="utf-8") as f:
        tpl = f.read().strip()
    text = tpl.format(
        project=coin["name"], symbol=coin["symbol"],
        venues=", ".join(venues.NAME.get(m["venue"], m["venue"]) for m, _ in results),
        findings="\n".join(_finding(m, s) for m, s in results),
    )
    return "✍️ <b>Suggested message</b> (optional; edit freely)\n\n<code>" + _esc(text) + "</code>"


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
