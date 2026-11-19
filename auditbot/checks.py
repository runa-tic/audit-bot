"""How a venue check (thresholds.json) reads to people: its name, its requirement, its measured value,
and how a typed override is parsed. One place, shared by the card, the bot's buttons and the message."""
import re


def _num(v, digits=0):
    return f"{v:,.{digits}f}"


def money(v, quote="USDT"):
    return "—" if v is None else f"{_num(v)} {quote}"


def pct(v, digits=2):
    return "—" if v is None else f"{v:.{digits}f}%"


def band(b):
    return f"±{b:g}%"


def label(c):
    """Short title: 'Liquidity within ±2%', 'Top-of-book spread', ..."""
    k = c["kind"]
    if k == "depth":
        return f"{'Combined liquidity' if c.get('side') == 'combined' else 'Liquidity'} within {band(c['band'])}"
    if k == "spread":
        return "Top-of-book spread"
    if k == "volume":
        return "24H volume"
    if k == "levels":
        return f"Order levels within {band(c['band'])}" if c.get("band") else "Order levels"
    if k == "drop":
        return "Largest 24H price drop" if c["window_h"] <= 24 else f"Largest {c['window_h'] // 24}D price drop"
    if k == "price_diff":
        return "Price vs other exchanges"
    return c["id"]


def is_max(c):
    return "max" in c


def limit(c):
    return c["max"] if is_max(c) else c["min"]


def req_text(c, quote="USDT"):
    """The requirement as one phrase: '≥ 1,000 USDT per side', '≤ 0.20%', '≥ 30 per side'."""
    k, v = c["kind"], limit(c)
    if k == "depth":
        return f"≥ {money(v, quote)}" + (" bid + ask" if c.get("side") == "combined" else " per side")
    if k == "volume":
        return f"≥ {money(v, quote)}"
    if k == "levels":
        return f"≥ {_num(v)} per side" + (" (at all times)" if c.get("agg") == "min" else "")
    return f"≤ {pct(v)}"


def line(c, quote="USDT"):
    """'Liquidity within ±2%: ≥ 1,000 USDT per side' (+ ' · changed' when this audit overrides it)."""
    return f"{label(c)}: {req_text(c, quote)}" + (" · changed" if c.get("custom") else "")


def prompt(c, venue_name, quote="USDT"):
    unit = {"depth": quote, "volume": quote, "levels": "levels per side"}.get(c["kind"], "%")
    word = "maximum" if is_max(c) else "minimum"
    return (f"New {word} for <b>{venue_name} · {label(c)}</b> (now {req_text(c, quote)}).\n"
            f"Reply with a number in {unit}, e.g. {'0.5' if unit == '%' else '2000 or 2.5k'}, "
            f"or <code>off</code> to leave this check off the card.")


class BadValue(ValueError):
    pass


def parse(text):
    """'2,500' '2.5k' '1m' '0.3%' -> float; 'off' -> None. Raises BadValue."""
    t = text.strip().lower().replace(",", "").replace(" ", "").replace("$", "").replace("usdt", "").replace("usd", "")
    if t in ("off", "none", "-", "no"):
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([km]?)%?", t)
    if not m:
        raise BadValue("Send a number like 2000, 2.5k or 0.3, or off.")
    v = float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6}[m.group(2)]
    if v <= 0:
        raise BadValue("It has to be above zero (or off).")
    return v
