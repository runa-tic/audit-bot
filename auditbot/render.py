"""One venue's card: HTML + inline-SVG charts, screenshotted by headless Chromium to a PNG.

Plain HTML/CSS on purpose: a redesign replaces CARD_CSS / card_html and nothing else. Logo, names and colours
come from the brand (auditbot/brand.py, AUDIT_BRAND): CARD_CSS names theme colours as [[name]] / [[name .6]].
Design rules from the sales team (2026-10-30): pair with the exchange name and logo; the 3-day average
is the headline number (green/red against the requirement), the current snapshot small and grey
under it; clear time ticks on the 72 h charts.
"""
import base64
import hashlib
import html
import math
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone

from . import brand, checks, coingecko, config, db, metrics, venues

CARD_DIR = os.path.join(config.ROOT, "card")
# The card's colours come from the brand (auditbot/brand.py); these read it at render time.
def GREEN():
    return brand.color("positive")


def RED():
    return brand.color("negative")


def CYAN():
    return brand.color("accent")


def GREY():
    return brand.color("text", ".45")


def TEXT():
    return brand.color("text")


_render_lock = threading.Lock()


# ---------- formatting

def money(v, quote="USDT"):
    if v is None:
        return "—"
    return f"{v:,.0f} {quote}"


def _signed_num(a, b):
    return "—" if a is None or b is None else f"{round(b) - round(a):+,}".replace("-", "−")


def num(v):
    return "—" if v is None else f"{v:,.0f}"


def _unit(quote):
    """The quote currency once in a tile's title, so the numbers under it fit two columns without a suffix each."""
    return f'<span class="u"> · {quote}</span>'


def compact(v):
    a = abs(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            x = v / div
            return f"{x:.1f}".rstrip("0").rstrip(".") + suf
    return f"{v:.0f}"


def pct(v, digits=2):
    return "—" if v is None else f"{v:.{digits}f}%"


def signed_pct(v):
    return "—" if v is None else f"{v:+.2f}%".replace("-", "−")  # a true minus: the hyphen sits loose in tabular figures


def utc(ts, fmt="%d %b, %H:%M UTC"):
    return datetime.fromtimestamp(ts, timezone.utc).strftime(fmt).lstrip("0")


def period(a, b):
    da, dbb = datetime.fromtimestamp(a, timezone.utc), datetime.fromtimestamp(b, timezone.utc)
    return f"{da.day} {da:%b %H:%M} – {dbb.day} {dbb:%b %H:%M} UTC"


# ---------- images (data URIs so the page needs no network but fonts)

def _data_uri(url):
    if not url:
        return ""
    d = os.path.join(config.DATA_DIR, "img")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, hashlib.sha1(url.encode()).hexdigest())
    if not os.path.exists(path) or time.time() - os.path.getmtime(path) > 14 * 86400:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": venues.UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                body, ctype = r.read(), r.headers.get_content_type()
            with open(path, "wb") as f:
                f.write(ctype.encode() + b"\n" + body)
        except Exception:
            return ""
    with open(path, "rb") as f:
        ctype, body = f.read().split(b"\n", 1)
    return f"data:{ctype.decode()};base64,{base64.b64encode(body).decode()}"


def _file_uri(name, ctype):
    with open(os.path.join(CARD_DIR, name), "rb") as f:
        return f"data:{ctype};base64,{base64.b64encode(f.read()).decode()}"


_FONTS = None
# Inter 4.1 variable (SIL OFL 1.1, card/fonts/OFL-Inter.txt) embedded, so a render never fetches a font and
# can't drift with whatever the host has installed. Variable rather than four static cuts: one file covers
# 400-700. Optical sizing is off, so every size uses the default text cut (opsz 14), which is what the Google
# Fonts Inter these cards used until 2026-10-30 served; the display cut set the big numbers too tight.
# A family name of its own, so a system-installed Inter can't stand in for it.
FONT_FILE = "InterVariable.woff2"


def font_faces():
    global _FONTS
    if _FONTS is None:
        _FONTS = ("@font-face{font-family:InterCard;font-style:normal;font-weight:100 900;font-display:block;"
                  f"src:url({_file_uri(os.path.join('fonts', FONT_FILE), 'font/woff2')}) format('woff2')}}")
    return _FONTS


_ex_img = {}


def exchange_logo(venue):
    if venue not in _ex_img:
        try:
            _ex_img[venue] = _data_uri(coingecko.exchange_image(venue))
        except Exception:
            _ex_img[venue] = ""
    return _ex_img[venue]


# ---------- charts

def _nice_max(v):
    if v <= 0:
        return 1
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if m * e >= v:
            return m * e
    return 10 * e


def chart_svg(series, t0, t1, w, h, y_fmt, y_max=None, refs=(), area=None, gap_s=None):
    """series: [(color, [(ts, value|None), ...])]. refs: [(value, color, label)]. Lines break on gaps."""
    L, R, T, B = 68, 92, 14, 38  # labels for threshold lines sit in the right margin, clear of the data
    pw, ph = w - L - R, h - T - B
    vals = [v for _, pts in series for _, v in pts if v is not None]
    top = _nice_max(y_max if y_max is not None else max(vals or [1]) * 1.08)
    x = lambda ts: L + (ts - t0) / max(1, t1 - t0) * pw
    y = lambda v: T + ph - min(v, top) / top * ph
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">']
    for i in range(5):  # horizontal grid + y labels
        v = top * i / 4
        yy = y(v)
        out.append(f'<line x1="{L}" x2="{w - R}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="{brand.color('text', '.08')}"/>')
        out.append(f'<text x="{L - 10}" y="{yy + 4:.1f}" text-anchor="end" class="ax">{y_fmt(v)}</text>')
    # time ticks: every midnight labelled with the date, every noon with 12:00
    start = datetime.fromtimestamp(t0, timezone.utc).replace(minute=0, second=0, microsecond=0)
    tick = int(start.timestamp()) - int(start.timestamp()) % (12 * 3600) + 12 * 3600
    while tick < t1:
        xx = x(tick)
        if xx > L + 18 and xx < w - R - 18:
            d = datetime.fromtimestamp(tick, timezone.utc)
            midnight = d.hour == 0
            dash = "" if midnight else ' stroke-dasharray="3 4"'
            alpha = ".14" if midnight else ".06"
            out.append(f'<line x1="{xx:.1f}" x2="{xx:.1f}" y1="{T}" y2="{T + ph}" stroke="{brand.color('text', alpha)}"{dash}/>')
            label = f"{d.day} {d:%b}" if midnight else "12:00"
            out.append(f'<text x="{xx:.1f}" y="{h - 10}" text-anchor="middle" class="ax{" axd" if midnight else ""}">{label}</text>')
        tick += 12 * 3600
    for v, color, label in refs:
        if v <= top:
            yy = y(v)
            out.append(f'<line x1="{L}" x2="{w - R}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="{color}" stroke-dasharray="5 5" opacity=".7"/>')
            out.append(f'<text x="{w - R + 8}" y="{yy + 4:.1f}" class="ref" fill="{color}">{label}</text>')
    if area is not None and area <= top:  # shade the breach zone above a maximum
        out.append(f'<rect x="{L}" y="{T}" width="{pw}" height="{y(area) - T:.1f}" fill="{RED()}" opacity=".05"/>')
    for color, pts in series:
        segs, cur, prev = [], [], None
        for ts, v in pts:
            if v is None or (gap_s and prev is not None and ts - prev > gap_s):
                if cur:
                    segs.append(cur)
                cur = []
            if v is not None:
                cur.append((x(ts), y(v)))
            prev = ts
        if cur:
            segs.append(cur)
        # a dot on every snapshot (2026-11-05), sized to the spacing so 15-min snapshots stay separate beads
        xs = sorted(px for sg in segs for px, _ in sg)
        step = sorted(b - a for a, b in zip(xs, xs[1:]))[len(xs) // 2 - 1] if len(xs) > 2 else 10
        r = max(1.7, min(2.6, step * 0.32))
        for sg in segs:
            d = " ".join(f"{'M' if i == 0 else 'L'}{px:.1f},{py:.1f}" for i, (px, py) in enumerate(sg))
            out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.8" stroke-linejoin="round"/>')
            out.append("".join(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{r:.2f}" fill="{color}" stroke="{brand.color('surface')}" stroke-width=".7"/>' for px, py in sg))
    out.append("</svg>")
    return "".join(out)


# ---------- card

def _badge(ok, good="Compliant", bad="Non-compliant"):
    if ok is None:
        return '<span class="status neu"><i></i>Not enough data</span>'
    return f'<span class="status {"pos" if ok else "neg"}"><i></i>{good if ok else bad}</span>'


def _col(ok):
    return GREY() if ok is None else GREEN() if ok else RED()


def _row(dt, dd):
    return f'<div class="row"><dt>{dt}</dt><dd>{dd}</dd></div>'


def _share(v):
    """The share of snapshots that missed a requirement, as a phrase: 'never', 'under 1% of the time', '44% of the time'."""
    return "never" if v == 0 else "under 1% of the time" if v < 0.01 else f"{round(v * 100)}% of the time"


def _vals(lines, big=False, right_label="Now:"):
    """The tile's numbers (sales, 2026-11-05): label on the left, number on the right, the current snapshot in its own
    right-hand column ('Bid 3D avg. 12,345 USDT    Now: 10,000 USDT'). lines: [(label, value, colour, now|None)]."""
    cells = []
    for label, val, color, now in lines:
        rl, ncol = right_label, None
        if isinstance(now, tuple):  # (text, colour, label): a right-hand value that is a verdict of its own
            now, ncol, rl = now
        style = f' style="color:{ncol}"' if ncol else ""
        cells.append(f'<span class="vl">{label}</span><b style="color:{color}">{val}</b>'
                     + (f'<span class="vl nl">{rl}</span><span class="nv"{style}>{now}</span>' if now is not None
                        else '<span></span><span></span>'))
    return f'<div class="vals{" one" if big else ""}">{"".join(cells)}</div>'


def _vol_change(s):
    """The 24 h volume change (first vs last 24 h) for the volume tile's right column: 2026-11-05 folded the
    separate Volume Trend tile into it, back to four tiles. A number only: the tile's one badge is the verdict
    (2026-11-11: 'Declining' beside 'Compliant' read as two verdicts)."""
    return signed_pct(s["vol_trend"]), _trend_look(s["vol_trend_state"])[0], "Change:"


def kpi_tile(x, quote, n_all, s=None):
    """One venue check as a KPI tile: the 3-day value in green/red, the latest in grey, the requirement."""
    c, e = x["c"], html.escape
    k, v, now = c["kind"], x["value"], x["now"]
    rows = [_row("Required", e(checks.req_text(c, quote)))]
    if x.get("miss") is not None:
        rows.append(_row("Above maximum" if checks.is_max(c) else "Below minimum", _share(x["miss"])))
    if x["floor"]:
        rows.append(_row("Depth note", f"Public book shorter than {checks.band(c['band'])}: a lower bound"))
    if x["n"] and x["n"] < n_all:
        rows.append(_row("Measured on", f"{x['n']} of {n_all} snapshots"))
    fmt = (lambda y: money(y, quote)) if k in ("depth", "volume") else pct
    nowt, unit = "", ""
    if k == "depth" and c.get("side") != "combined":
        unit = _unit(quote)
        body = _vals([("Bid 3D avg.", num(v[0] if v else None), _col(x.get("ok_bid")), num(now[0]) if now else None),
                      ("Ask 3D avg.", num(v[1] if v else None), _col(x.get("ok_ask")), num(now[1]) if now else None)])
    elif k == "levels":
        agg = "3D min" if c.get("agg") == "min" else "3D avg."
        body = _vals([(f"Bid {agg}", f"{v[0]:,.0f}" if v else "—", _col(x["ok"]), f"{now[0]:,}" if now else None),
                      (f"Ask {agg}", f"{v[1]:,.0f}" if v else "—", _col(x["ok"]), f"{now[1]:,}" if now else None)])
    else:
        label = {"drop": "Largest", "price_diff": "3D avg. gap"}.get(k, "3D avg.")
        # 2026-11-03: volume shows the 3-day average and the requirement only, so no Now column
        has_now = k in ("spread", "depth") and c.get("side") != "combined"
        right = fmt(now) if has_now and now is not None else None
        if k == "volume" and s is not None:
            unit, v_txt = _unit(quote), num(v)
            right = _vol_change(s)
            rows.append(_row("Measured", "day 3 vs day 1 avg."))
        body = _vals([(label, v_txt if k == "volume" and s is not None else fmt(v), _col(x["ok"]), right)], big=True)
        if k == "drop":
            nowt = f"Peak-to-trough within any {c['window_h']} h"
        if k == "depth":
            nowt = f"Bid {money(x.get('bid'), quote)} + ask {money(x.get('ask'), quote)}" if v is not None else ""
        if k == "price_diff":
            nowt = ("Against the median mid of this coin's other exchanges" if v is not None
                    else "Needs a second exchange collected for this coin")
    return (f'<article class="kpi"><div class="kt"><p>{e(checks.label(c))}{unit}</p><span class="bs">{_badge(x["ok"])}</span></div>{body}'
            f'{f"<p class=now>{e(nowt)}</p>" if nowt else ""}<dl>{"".join(rows)}</dl></article>')


def _trend_look(state):
    """(headline colour, badge html) for a trend state: improving / stable / declining / None."""
    color = {None: GREY(), "improving": GREEN(), "stable": TEXT(), "declining": RED()}[state]
    badge = {None: '<span class="status neu"><i></i>Insufficient data</span>',
             "improving": '<span class="status pos"><i></i>Improving</span>',
             "stable": '<span class="status neu"><i></i>Stable</span>',
             "declining": '<span class="status neg"><i></i>Declining</span>'}[state]
    return color, badge


def card_html(coin, market, rows, s):
    quote = market["quote"]
    venue_name = venues.NAME.get(market["venue"], market["venue"])
    pair = f'{market["base"]} / {quote}'
    tok, ex = _data_uri(coin.get("image_url")), exchange_logo(market["venue"])
    span_h = max(1, round((s["to"] - s["from"]) / 3600))
    ok_rows = [r for r in rows if not r.get("error") and r["ts"] >= s["from"]]
    gap = config.CADENCE_MIN * 60 * 2.5
    t0, t1 = s["to"] - config.WINDOW_H * 3600, s["to"]
    liq_max = max([r["bid_2pct_usd"] for r in ok_rows] + [r["ask_2pct_usd"] for r in ok_rows] + [1])
    d2, sp = metrics.check(s, "depth2"), metrics.check(s, "spread")
    liq_refs, req_note = [], ""
    if d2 and d2["c"].get("side") == "combined":
        req_note = f'<span class="note">Requirement: {compact(d2["c"]["min"])} bid + ask combined</span>'
    elif d2:
        need = d2["c"]["min"]
        if need <= liq_max * 1.6:
            liq_refs.append((need, brand.color("text", ".55"), "Required"))
        else:
            req_note = f'<span class="note">Bid &amp; ask requirement ({compact(need)}) is above the chart</span>'
    liq = chart_svg([(GREEN(), [(r["ts"], r["bid_2pct_usd"]) for r in ok_rows]),
                     (RED(), [(r["ts"], r["ask_2pct_usd"]) for r in ok_rows])],
                    t0, t1, 1000, 300, compact, y_max=max(liq_max, *[v for v, _, _ in liq_refs] or [0]) * 1.08,
                    refs=liq_refs, gap_s=gap)
    sp_vals = [r["spread_pct"] for r in ok_rows]
    sp_need = sp["c"]["max"] if sp else None
    sp_top = max((sp_need or 0) * 1.5, max(sp_vals or [0]) * 1.08) or 1
    spread = chart_svg([(CYAN(), [(r["ts"], r["spread_pct"]) for r in ok_rows])], t0, t1, 1000, 260,
                       lambda v: f"{v:.2f}%", y_max=sp_top,
                       refs=[(sp_need, RED(), "Maximum")] if sp_need else [], area=sp_need, gap_s=gap)
    trend_color, trend_badge = _trend_look(s["trend_state"])
    e = html.escape
    b = brand.load()
    mark = (f'<img class="brand" src="{b["logo_uri"]}">' if b["logo_uri"] else f'<span class="wordmark">{e(b["name"])}</span>')
    got_h = (s["to"] - s["from"]) / 3600
    partial = ("" if got_h >= config.WINDOW_H - 1 else
               f'<div class="partial"><b>Preview · partial data</b> {got_h:.1f} h of the {config.WINDOW_H} h window collected '
               f'({s["n"]} snapshots). Averages cover only that span; the final card uses the full {config.WINDOW_H} h.</div>')
    # a check the window holds no data for (an audit started before per-band stats, a price check with no second
    # exchange) stays off the card rather than showing an empty tile to a lead
    tiles = "".join(kpi_tile(x, quote, s["n"], s) for x in s["checks"] if x["n"])
    subtitle = (f"3-day averages against {e(venue_name)} requirements · current snapshot in grey" if s["checks"]
                else f"3-day averages · current snapshot in grey · no {e(venue_name)} requirements set")
    if not s["checks"]:  # numbers only, no verdict
        tiles = (f'<article class="kpi"><div class="kt"><p>Liquidity within ±2%{_unit(quote)}</p></div>'
                 + _vals([("Bid 3D avg.", num(s["bid_avg"]), TEXT(), num(s.get("bid_now"))),
                          ("Ask 3D avg.", num(s["ask_avg"]), TEXT(), num(s.get("ask_now")))])
                 + '<dl></dl></article>'
                 f'<article class="kpi"><div class="kt"><p>Top-of-book spread</p></div>'
                 + _vals([("3D avg.", pct(s["spread_avg"]), TEXT(), pct(s["spread_now"]) if s.get("spread_now") is not None else None)], big=True)
                 + '<dl></dl></article>'
                 f'<article class="kpi"><div class="kt"><p>24H volume{_unit(quote)}</p></div>'
                 + _vals([("3D avg.", num(s["vol_avg"]), TEXT(), _vol_change(s))], big=True)
                 + f'<dl>{_row("Measured", "day 3 vs day 1 avg.")}</dl></article>')
    snaps = f"{s['n']} over {span_h} h"
    # Liquidity Trend (2026-11-05): one number, the absolute delta of average total ±2% depth, day 3 minus
    # day 1 (the last and first 24 h of the window), then the % change in the right column
    tiles += (f'<article class="kpi"><div class="kt"><p>Liquidity Trend (3D){_unit(quote)}</p>{trend_badge}</div>'
              + _vals([("Day 3 − Day 1", _signed_num(s.get("trend_first"), s.get("trend_last")), trend_color,
                        (signed_pct(s["trend"]), trend_color, "Change:"))], big=True)
              + f'<dl>{_row("Measured", "total ±2% depth, day 3 vs day 1 avg.")}{_row("Snapshots", snaps)}</dl></article>')
    return f"""<!doctype html><html><head><meta charset="utf-8">
<style>{font_faces()}{brand.fill(CARD_CSS)}</style></head><body><main id="card">
<header class="top">{mark}<span class="div"></span>
  <div><div class="title"><h1>{e(b["title"])}</h1><span class="spot">Spot</span></div><p>{e(b["tagline"])}</p></div></header>
<section class="head">
  <div class="pair">
    <span class="logos">{f'<img class="tok" src="{tok}">' if tok else f'<span class="tok ph">{e(market["base"][:1])}</span>'}{f'<img class="ex" src="{ex}">' if ex else ''}</span>
    <div><h2>{e(pair)}</h2><p class="venue">{e(venue_name)} · Spot</p></div>
  </div>
  <div class="meta"><p>Public {e(venue_name)} order-book data</p><span>{e(period(s["from"], s["to"]))}</span></div>
</section>
{partial}<section class="st"><div><h3>KPI Overview</h3><p>{subtitle}</p></div><span>{s["n"]} snapshots · {span_h} h</span></section>
<section class="kpis">{tiles}</section>
<section class="st"><div><h3>Market Dynamics</h3><p>Last 72 hours · UTC</p></div></section>
<article class="chart"><div class="ch"><div><h4>Liquidity Within ±2%</h4><p>Quote liquidity by order-book side {req_note}</p></div>
  <div class="legend"><span><i style="background:{GREEN()}"></i>Bid</span><span><i style="background:{RED()}"></i>Ask</span></div></div>{liq}</article>
<article class="chart"><div class="ch"><div><h4>Top-of-Book Spread</h4><p>Spread versus required maximum</p></div>
  <div class="legend"><span class="lv">Latest <b>{pct(s["spread_now"])}</b></span></div></div>{spread}</article>
<footer>Reporting period {e(period(s["from"], s["to"]))}</footer>
</main></body></html>"""


CARD_CSS = """
*{box-sizing:border-box}html,body{margin:0;background:[[background]]}
body{font-family:InterCard,sans-serif;font-optical-sizing:none;color:[[text]];-webkit-font-smoothing:antialiased}
#card{width:1080px;padding:0 36px 30px;background:radial-gradient(circle at 80% 0,[[accent .04]],transparent 30%),[[background]]}
.top{height:84px;display:flex;align-items:center;gap:22px;border-bottom:1px solid [[text .1]]}
.brand{width:150px}.wordmark{font-size:26px;font-weight:700;letter-spacing:-.6px;white-space:nowrap}.div{width:1px;height:32px;background:[[text .1]]}
.title{display:flex;align-items:center;gap:10px}.title h1{font-size:18px;margin:0}
.top p{margin:3px 0 0;color:[[text .6]];font-size:16px}
.spot{font-size:12px;color:[[accent]];background:[[accent .1]];padding:4px 8px;border-radius:20px}
.head{display:flex;justify-content:space-between;align-items:center;padding:30px 0 26px}
.pair{display:flex;align-items:center;gap:18px}
.logos{position:relative;width:104px;height:64px;flex:0 0 104px}
.tok{width:64px;height:64px;border-radius:50%;background:#1c1c1c;object-fit:cover;display:grid;place-items:center;font-size:28px;font-weight:700}
.tok.ph{background:linear-gradient(140deg,[[accent]],[[accent_deep]]);color:#07191a}
.ex{position:absolute;left:44px;top:4px;width:56px;height:56px;border-radius:50%;border:3px solid [[background]];background:#fff;box-sizing:content-box;margin:-3px}
.pair h2{margin:0;font-size:36px;line-height:40px;letter-spacing:-1.2px}
.venue{margin:6px 0 0;display:flex;align-items:center;gap:8px;font-size:18px;font-weight:500;color:[[text .85]]}
.meta{text-align:right}.meta p{margin:0;font-size:17px;color:[[text .75]]}.meta span{display:block;margin-top:6px;font-size:16px;color:[[text .55]]}
.st{display:flex;justify-content:space-between;align-items:flex-end;margin:6px 0 14px}
.st h3{margin:0;font-size:22px;letter-spacing:-.35px}.st p{margin:4px 0 0;font-size:16px;color:[[text .65]]}.st>span{font-size:16px;color:[[text .55]]}
.kpis{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:14px}
.kpi{background:[[surface]];border:1px solid [[text .04]];border-radius:16px;padding:20px 22px;display:flex;flex-direction:column;min-height:236px}
.kt{display:flex;justify-content:space-between;align-items:center}.kt p{margin:0;font-size:17px;font-weight:500;color:[[text .75]]}
.bs{display:flex;gap:6px}.status{display:inline-flex;align-items:center;gap:7px;border-radius:18px;padding:6px 11px;font-size:14.5px}
.status i{width:6px;height:6px;border-radius:50%;background:currentColor}
.pos{color:[[positive_text]];background:[[positive .1]]}.neg{color:[[negative_text]];background:[[negative .1]]}.neu{color:[[neutral]];background:rgba(160,160,160,.1)}
.vals{margin:18px 0 0;display:grid;grid-template-columns:auto auto 1fr auto;align-items:baseline;gap:6px 12px;font-variant-numeric:tabular-nums}
.vals b{font-size:26px;font-weight:600;letter-spacing:-.7px;text-align:right;white-space:nowrap}.vals.one b{font-size:34px;letter-spacing:-1.1px;font-variant-numeric:normal}
.u{color:[[text .45]];font-weight:400}
.vl{font-size:16px;color:[[text .55]];white-space:nowrap}.nl{text-align:right}
.nv{font-size:18px;font-weight:500;color:[[text .6]];text-align:right;white-space:nowrap}
.now{margin:10px 0 0;font-size:16.5px;color:[[text .6]]}
.kpi dl{margin:auto 0 0;padding-top:12px;border-top:1px solid [[text .08]]}
.row{display:flex;justify-content:space-between;gap:12px;margin-top:9px;font-size:16px}
.row dt{color:[[text .55]]}.row dd{margin:0;color:[[text .8]];text-align:right}
.st:nth-of-type(4){margin-top:30px}
.chart{background:[[surface]];border:1px solid [[text .04]];border-radius:16px;padding:18px 18px 8px;margin-bottom:14px}
.ch{display:flex;justify-content:space-between;align-items:flex-start;padding:0 4px 6px}
.ch h4{margin:0;font-size:18px;font-weight:500}.ch p{margin:4px 0 0;font-size:16px;color:[[text .6]]}
.note{color:[[negative_text .85]];margin-left:6px}
.legend{display:flex;gap:16px;font-size:16px;color:[[text .65]]}.legend i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.lv b{color:[[text]];font-weight:500;margin-left:6px}
svg{display:block;width:100%;height:auto}.ax{font-size:15px;fill:[[text .55]]}.axd{fill:[[text .75]];font-weight:500}.ref{font-size:14.5px}
.partial{margin:0 0 18px;padding:12px 15px;border:1px solid [[warning .3]];border-radius:10px;background:[[warning .07]];font-size:16.5px;color:[[text .75]]}
.partial b{color:[[warning]];font-weight:600;margin-right:6px}
footer{text-align:center;padding-top:14px;font-size:15px;color:[[text .5]]}
"""


def screenshot(html_text, out_path):
    from playwright.sync_api import sync_playwright
    with _render_lock, sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = b.new_page(viewport={"width": 1080, "height": 1600}, device_scale_factor=2)
            page.set_content(html_text, wait_until="networkidle", timeout=30000)
            page.locator("#card").screenshot(path=out_path, type="png")
        finally:
            b.close()
    return out_path


def peers(coin_id, venue, since_ts):
    """{ts: [mids of the coin's other venues at that tick]} for price_diff checks, per token (a 1000XYZ mid / 1000)."""
    sym = (db.coin(coin_id) or {}).get("symbol")
    units = {m["venue"]: coingecko.unit(m["base"], sym) for m in db.all_markets(coin_id)}
    out = {}
    for r in db.q("""SELECT ts, venue, mid FROM snapshots WHERE coin_id = ? AND venue != ? AND ts >= ?
                     AND error IS NULL AND mid IS NOT NULL""", coin_id, venue, since_ts):
        out.setdefault(r["ts"], []).append(r["mid"] / units.get(r["venue"], 1))
    return out


def render(coin_id, venue, reqs=None):
    """-> (png_path, summary) for one venue's card, cached by its last snapshot and requirements. None if no data.
    reqs: config.requirements() output; default = the venue's published requirements."""
    m = db.market(coin_id, venue)
    coin = db.coin(coin_id)
    last = db.last_ts(coin_id, venue)
    if not m or not coin or not last:
        return None
    since = last - config.WINDOW_H * 3600
    rows = db.snapshots(coin_id, venue, since)
    reqs = config.requirements(venue) if reqs is None else reqs
    u = coingecko.unit(m["base"], coin["symbol"])  # price_diff compares per-token mids; drop is a % either way
    per_token = [dict(r, mid=r["mid"] / u) if u != 1 and r.get("mid") else r for r in rows]
    s = metrics.summarize(per_token, reqs, config.WINDOW_H, peers(coin_id, venue, since))
    if not s:
        return None
    d = os.path.join(config.DATA_DIR, "cards")
    os.makedirs(d, exist_ok=True)
    # a card is cached per window end, requirements and brand: a new logo or colour renders afresh
    tag = hashlib.sha256((db.dumps(reqs) + brand.fingerprint()).encode()).hexdigest()[:8]
    out = os.path.join(d, f"{coin_id}--{venue}--{last}--{tag}.png")
    if not os.path.exists(out):
        tmp = out + ".tmp.png"
        screenshot(card_html(coin, m, rows, s), tmp)
        os.replace(tmp, out)
    return out, s
