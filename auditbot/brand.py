"""The card's look: who it is from (logo or wordmark, team name) and its colours.

Nothing here is hard-coded to one company. With no AUDIT_BRAND the cards use DEFAULT, a neutral dark theme with
a text wordmark. A deployment that wants its own look points AUDIT_BRAND at a JSON file:

    {
      "name": "Acme Markets",            # wordmark when there is no logo; also the team the bot names
      "logo": "logo.png",                # png, svg, jpg or webp, relative to the JSON file; optional
      "title": "Market Audit",
      "tagline": "Independent market quality report",
      "colors": {"accent": "#6ea8fe", "background": "#0d0d0d", ...}   # any subset of DEFAULT["colors"]
    }

Every key is optional; a missing one keeps its default. Colours are #rrggbb. The brand file is read once per
process, so a change needs a restart. examples/brand/ has a worked example.
"""
import base64
import hashlib
import json
import os
import re

from . import config

DEFAULT = {
    "name": "audit-bot",
    "logo": "",
    "title": "Market Audit",
    "tagline": "Independent market quality report",
    "colors": {
        "background": "#0d0d0d",   # page
        "surface": "#181818",      # tiles and charts
        "text": "#f4f4f5",         # headings; every grey on the card is this at lower opacity
        "accent": "#6ea8fe",       # the Spot pill, the spread line, the header glow
        "accent_deep": "#1f4f8f",  # the far end of the placeholder token's gradient
        "positive": "#20b26c",     # compliant: numbers, bid line
        "positive_text": "#4bd38d",  # compliant badge text
        "negative": "#ff6c62",     # non-compliant: numbers, ask line, maximum line
        "negative_text": "#ff8279",  # non-compliant badge text
        "warning": "#e6b96f",      # the partial-data banner
        "neutral": "#aaaaaa",      # "Not enough data" / "Stable" badge text
    },
}

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_TYPES = {".png": "image/png", ".svg": "image/svg+xml", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
          ".webp": "image/webp"}
_cache = {}


class BrandError(Exception):
    """A brand file that can't be used: said at start-up, not on the first card."""


def load(path=None):
    """The brand: DEFAULT with the file at `path` (default AUDIT_BRAND) laid over it, logo read into a data URI."""
    path = config.BRAND_PATH if path is None else path
    if path in _cache:
        return _cache[path]
    b = {**DEFAULT, "colors": dict(DEFAULT["colors"]), "logo_uri": ""}
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                over = json.load(f)
        except (OSError, ValueError) as ex:
            raise BrandError(f"brand file {path}: {ex}") from ex
        unknown = set(over) - set(DEFAULT)
        if unknown:
            raise BrandError(f"brand file {path}: unknown keys {sorted(unknown)}")
        for k in ("name", "logo", "title", "tagline"):
            if k in over:
                b[k] = str(over[k] or "")
        colors = over.get("colors") or {}
        bad = sorted(k for k in colors if k not in DEFAULT["colors"])
        if bad:
            raise BrandError(f"brand file {path}: unknown colours {bad}")
        for k, v in colors.items():
            if not _HEX.match(str(v)):
                raise BrandError(f"brand file {path}: colour {k} must be #rrggbb, not {v!r}")
            b["colors"][k] = v.lower()
        if b["logo"]:
            logo = os.path.join(os.path.dirname(os.path.abspath(path)), b["logo"])
            ctype = _TYPES.get(os.path.splitext(logo)[1].lower())
            if not ctype:
                raise BrandError(f"brand file {path}: logo must be one of {sorted(_TYPES)}")
            try:
                with open(logo, "rb") as f:
                    b["logo_uri"] = f"data:{ctype};base64,{base64.b64encode(f.read()).decode()}"
            except OSError as ex:
                raise BrandError(f"brand file {path}: logo: {ex}") from ex
    _cache[path] = b
    return b


def color(name, alpha=None):
    """A theme colour as CSS: '#rrggbb', or 'rgba(r,g,b,a)' at an opacity."""
    h = load()["colors"][name]
    if alpha is None:
        return h
    r, g, bl = (int(h[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r},{g},{bl},{alpha})"


def fingerprint():
    """A short hash of everything that changes how a card looks, for render()'s cache key."""
    b = load()
    return hashlib.sha256(json.dumps([b["name"], b["title"], b["tagline"], b["colors"], b["logo_uri"]],
                                     sort_keys=True).encode()).hexdigest()[:12]


_TOKEN = re.compile(r"\[\[(\w+)(?: ([.\d]+))?\]\]")


def fill(css):
    """CSS with [[name]] / [[name .6]] tokens replaced by the theme's colours."""
    return _TOKEN.sub(lambda m: color(m.group(1), m.group(2)), css)
