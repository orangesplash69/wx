#!/usr/bin/env python3
"""wx — a weather dashboard for the terminal.

Renders current conditions, an air-quality and pollen readout, a 12-hour
precipitation nowcast, a 24-hour temperature graph, sun/moon astronomy and a
multi-day forecast, all from Open-Meteo (no API key required).

    python main.py                  # dashboard for the default city
    python main.py tokyo --watch    # live-refreshing
    python main.py -c leipzig,oslo  # compare cities
    python main.py --oneline        # one line, for a shell prompt

Data:  api.open-meteo.com (forecast) · air-quality-api (AQI + pollen)
       archive-api (climate normals) · geocoding-api (city -> lat/lon)

Config: a .env file next to this script (see .env.example) sets
        LATLNG_API_KEY, WX_CITY, WX_CACHE_DIR or WX_CONFIG without exporting them
        in your shell. Real environment variables always take precedence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlencode

try:
    import requests
except ImportError:                                          # pragma: no cover
    sys.exit("wx needs 'requests' — pip install -r requirements.txt")

from rich import box
from rich.cells import cell_len
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

# ─────────────────────────────────────────────────────────────── endpoints ──

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
DWD_URL = "https://www.dwd.de/DWD/warnungen/warnapp/json/warnings.json"
NWS_URL = "https://api.weather.gov/alerts/active"
# Hardcoded fallbacks; LATLNG_API_KEY / WX_CITY (shell or .env) override them.
# Defined after load_dotenv() below so values from .env are visible.
_FALLBACK_KEY = "<latlng_api_key>"
_FALLBACK_CITY = "Arrecife, Spain"


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines from a .env file's contents.

    Supports blank lines, '#' comments, an optional leading 'export ', and
    single- or double-quoted values. Not the full dotenv spec (no multiline
    values, no variable interpolation) — just enough for simple config.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def load_dotenv(path: Optional[Path] = None) -> None:
    """Load a .env file into os.environ, without overriding what's already set.

    Looked up next to main.py itself rather than the caller's cwd, so `wx`
    behaves the same run from the project directory or installed system-wide.
    Override the location with WX_ENV. Missing file is silent — .env is
    always optional.
    """
    target = path or Path(os.getenv("WX_ENV") or (Path(__file__).resolve().parent / ".env"))
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return
    for key, value in parse_dotenv(text).items():
        os.environ.setdefault(key, value)   # a real env var always wins over .env


load_dotenv()

DEFAULT_KEY = os.getenv("LATLNG_API_KEY") or _FALLBACK_KEY
DEFAULT_CITY = os.getenv("WX_CITY") or _FALLBACK_CITY

CACHE_DIR = Path(os.getenv("WX_CACHE_DIR") or
                 Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache")) / "wx")
CONFIG_PATH = Path(os.getenv("WX_CONFIG") or
                   Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config")) / "wx" / "config.json")

TTL_FORECAST = 600        # 10 min — current conditions move fast
TTL_AIR = 3600            # 1 h    — AQI is hourly anyway
TTL_ALERTS = 600          # 10 min — official warnings change fast
TTL_GEO = 30 * 86400      # 30 d   — cities do not move
TTL_CLIMATE = 30 * 86400  # 30 d   — 10-year normals are stable
FETCH_DAYS = 16           # always fetch the max; --days slices at render time,
                          # so every --days value shares one cache entry (+14 KB)

# ────────────────────────────────────────────────────────────────── themes ──

@dataclass(frozen=True)
class Theme:
    """A palette. `ramp` is the temperature gradient: (°C, #rrggbb) stops."""
    name: str
    fg: str
    muted: str
    label: str
    rule: str
    dim_dot: str
    sun: str
    moon: str
    star: str
    cloud: str
    cloud_d: str
    rain: str
    snow: str
    bolt: str
    fog: str
    good: str
    warn: str
    bad: str
    ramp: tuple[tuple[float, str], ...]


_RAMP_DEFAULT = ((-25, "#6c5ce7"), (-10, "#4c8dff"), (0, "#3fc1e0"), (8, "#3dd598"),
                 (15, "#8fd64f"), (21, "#e8d04a"), (27, "#ffa04a"), (33, "#f45b4b"),
                 (42, "#c02b66"))

THEMES: dict[str, Theme] = {
    "night": Theme(
        name="night", fg="#e6edf3", muted="#6e7681", label="#8b949e", rule="#30363d",
        dim_dot="#30363d", sun="#ffd166", moon="#dfe3ff", star="#8a8fb0",
        cloud="#c9d1d9", cloud_d="#7d8590", rain="#58a6ff", snow="#e6f4ff",
        bolt="#ffcc33", fog="#98a1ad", good="#3fb950", warn="#d29922", bad="#f85149",
        ramp=_RAMP_DEFAULT),
    "day": Theme(
        name="day", fg="#1f2328", muted="#8c959f", label="#57606a", rule="#d0d7de",
        dim_dot="#d8dee4", sun="#d4a017", moon="#6a6fb0", star="#9a9fc0",
        cloud="#57606a", cloud_d="#8c959f", rain="#0969da", snow="#54aeff",
        bolt="#bf8700", fog="#8c959f", good="#1a7f37", warn="#9a6700", bad="#cf222e",
        ramp=((-25, "#5b4bd6"), (-10, "#1f6feb"), (0, "#1098ad"), (8, "#1a7f37"),
              (15, "#57922c"), (21, "#bf8700"), (27, "#d4622a"), (33, "#cf222e"),
              (42, "#a0203f"))),
    "nord": Theme(
        name="nord", fg="#eceff4", muted="#4c566a", label="#81a1c1", rule="#3b4252",
        dim_dot="#3b4252", sun="#ebcb8b", moon="#e5e9f0", star="#5e81ac",
        cloud="#d8dee9", cloud_d="#8fbcbb", rain="#88c0d0", snow="#eceff4",
        bolt="#ebcb8b", fog="#4c566a", good="#a3be8c", warn="#ebcb8b", bad="#bf616a",
        ramp=((-25, "#b48ead"), (-10, "#5e81ac"), (0, "#81a1c1"), (8, "#88c0d0"),
              (15, "#8fbcbb"), (21, "#a3be8c"), (27, "#ebcb8b"), (33, "#d08770"),
              (42, "#bf616a"))),
    "mono": Theme(
        name="mono", fg="default", muted="dim", label="dim", rule="dim",
        dim_dot="dim", sun="bold", moon="bold", star="dim", cloud="default",
        cloud_d="dim", rain="default", snow="bold", bolt="bold", fog="dim",
        good="default", warn="bold", bad="bold reverse",
        ramp=((0, "default"),)),
}

# ─────────────────────────────────────────────────────────────────── color ──

def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def blend(a: str, b: str, f: float) -> str:
    """Linear blend between two #rrggbb colors."""
    ra, ga, ba = _hex_to_rgb(a)
    rb, gb, bb = _hex_to_rgb(b)
    f = max(0.0, min(1.0, f))
    return "#%02x%02x%02x" % (round(ra + (rb - ra) * f),
                              round(ga + (gb - ga) * f),
                              round(ba + (bb - ba) * f))


def ramp_color(value: Optional[float], theme: Theme) -> str:
    """Map a temperature onto the theme's gradient."""
    if value is None:
        return theme.muted
    stops = theme.ramp
    if len(stops) == 1:                       # mono theme: no gradient
        return theme.fg
    if value <= stops[0][0]:
        return stops[0][1]
    if value >= stops[-1][0]:
        return stops[-1][1]
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t0 <= value <= t1:
            return blend(c0, c1, (value - t0) / (t1 - t0))
    return theme.fg


def scale_color(value: float, bands: Sequence[tuple[float, str]], theme: Theme) -> str:
    """Pick a color for a value from ascending (threshold, color) bands."""
    if theme.name == "mono":
        return theme.fg
    out = bands[0][1]
    for limit, color in bands:
        if value >= limit:
            out = color
    return out


# ───────────────────────────────────────────────────────────────────── art ──
# Each art is a list of rows; each row is a list of (text, theme-attribute).
# Colors are resolved against the active theme at render time.

ART: dict[str, list[list[tuple[str, str]]]] = {
    "sun": [
        [(r"    \   /", "sun")],
        [(r"     .-.", "sun")],
        [(r"  ― (   ) ―", "sun")],
        [(r"     `-´", "sun")],
        [("    /   \\", "sun")],
    ],
    "moon": [
        [("  ·", "star"), ("    ,--.", "moon")],
        [("    ,'", "moon"), ("    \\", "moon")],
        [("   (", "moon"), ("      |", "moon"), ("  ·", "star")],
        [("    `.", "moon"), ("    /", "moon")],
        [("      `--'", "moon")],
    ],
    "sun_cloud": [
        [(r"   \  /", "sun")],
        [(" _ /", "sun"), ('""', "sun"), (".-.", "cloud")],
        [(r"   \_(", "sun"), ("   ).", "cloud")],
        [("   /", "sun"), ("(___(__)", "cloud_d")],
        [("", "cloud")],
    ],
    "moon_cloud": [
        [("   ,-.", "moon")],
        [("  (   ", "moon"), (".-.", "cloud")],
        [("   `-", "moon"), ("(    ).", "cloud")],
        [("     (___(__)", "cloud_d")],
        [("", "cloud")],
    ],
    "cloud": [
        [("", "cloud")],
        [("     .--.", "cloud")],
        [("  .-(    ).", "cloud")],
        [(" (___.__)__)", "cloud_d")],
        [("", "cloud")],
    ],
    "fog": [
        [(" _ - _ - _ -", "fog")],
        [("  _ - _ - _", "fog")],
        [(" _ - _ - _ -", "fog")],
        [("  _ - _ - _", "fog")],
        [(" _ - _ - _ -", "fog")],
    ],
    "drizzle": [
        [("     .-.", "cloud")],
        [("    (   ).", "cloud")],
        [("   (___(__)", "cloud_d")],
        [("    ‘ ‘ ‘ ‘", "rain")],
        [("   ‘ ‘ ‘ ‘", "rain")],
    ],
    "rain": [
        [("     .-.", "cloud")],
        [("    (   ).", "cloud")],
        [("   (___(__)", "cloud_d")],
        [("   ‘‘‘‘‘‘‘‘", "rain")],
        [("  ‘‘‘‘‘‘‘‘", "rain")],
    ],
    "snow": [
        [("     .-.", "cloud")],
        [("    (   ).", "cloud")],
        [("   (___(__)", "cloud_d")],
        [("    *  *  *", "snow")],
        [("   *  *  *", "snow")],
    ],
    "sleet": [
        [("     .-.", "cloud")],
        [("    (   ).", "cloud")],
        [("   (___(__)", "cloud_d")],
        [("    ‘ * ‘ *", "rain")],
        [("   * ‘ * ‘", "snow")],
    ],
    "storm": [
        [("     .-.", "cloud_d")],
        [("    (   ).", "cloud_d")],
        [("   (___(__)", "cloud_d")],
        [("   ‘‘", "rain"), ("╱", "bolt"), ("‘‘", "rain"), ("╱", "bolt")],
        [("  ‘‘‘‘‘‘‘‘", "rain")],
    ],
    "hail": [
        [("     .-.", "cloud_d")],
        [("    (   ).", "cloud_d")],
        [("   (___(__)", "cloud_d")],
        [("   ‘", "rain"), ("o", "snow"), ("‘", "rain"), ("o", "snow"), ("‘", "rain"), ("o", "snow")],
        [("  ", "rain"), ("o", "snow"), ("‘", "rain"), ("o", "snow"), ("‘", "rain"), ("o", "snow")],
    ],
    "unknown": [
        [("", "muted")],
        [("     ,--.", "muted")],
        [("     `..'", "muted")],
        [("      ()", "muted")],
        [("", "muted")],
    ],
}

# WMO code -> (label, day art, night art, glyph, severity 0-3)
WMO: dict[int, tuple[str, str, str, str, int]] = {
    0: ("Clear sky", "sun", "moon", "☀", 0),
    1: ("Mainly clear", "sun", "moon", "☀", 0),
    2: ("Partly cloudy", "sun_cloud", "moon_cloud", "◑", 0),
    3: ("Overcast", "cloud", "cloud", "☁", 0),
    45: ("Fog", "fog", "fog", "≡", 1),
    48: ("Rime fog", "fog", "fog", "≡", 2),
    51: ("Light drizzle", "drizzle", "drizzle", "☂", 0),
    53: ("Drizzle", "drizzle", "drizzle", "☂", 1),
    55: ("Dense drizzle", "drizzle", "drizzle", "☂", 1),
    56: ("Freezing drizzle", "sleet", "sleet", "☂", 2),
    57: ("Freezing drizzle", "sleet", "sleet", "☂", 2),
    61: ("Light rain", "rain", "rain", "☂", 0),
    63: ("Rain", "rain", "rain", "☂", 1),
    65: ("Heavy rain", "rain", "rain", "☂", 2),
    66: ("Freezing rain", "sleet", "sleet", "☂", 3),
    67: ("Freezing rain", "sleet", "sleet", "☂", 3),
    71: ("Light snow", "snow", "snow", "❄", 1),
    73: ("Snow", "snow", "snow", "❄", 2),
    75: ("Heavy snow", "snow", "snow", "❄", 3),
    77: ("Snow grains", "snow", "snow", "❄", 1),
    80: ("Light showers", "rain", "rain", "☂", 0),
    81: ("Showers", "rain", "rain", "☂", 1),
    82: ("Violent showers", "rain", "rain", "☂", 3),
    85: ("Snow showers", "snow", "snow", "❄", 2),
    86: ("Heavy snow showers", "snow", "snow", "❄", 3),
    95: ("Thunderstorm", "storm", "storm", "↯", 3),
    96: ("Storm with hail", "hail", "hail", "↯", 3),
    99: ("Severe storm, hail", "hail", "hail", "↯", 3),
}


def describe(code: Optional[int], is_day: bool = True) -> tuple[str, str, str, int]:
    label, day, night, glyph, sev = WMO.get(
        code if code is not None else -1, ("Unknown", "unknown", "unknown", "?", 0))
    return label, (day if is_day else night), glyph, sev


def art_lines(key: str, theme: Theme, width: int = 14) -> list[Text]:
    """Render an art block, padded to a fixed cell width."""
    out: list[Text] = []
    for row in ART.get(key, ART["unknown"]):
        t = Text()
        for seg, attr in row:
            t.append(seg, style=getattr(theme, attr, theme.fg))
        pad = width - cell_len(t.plain)
        if pad > 0:
            t.append(" " * pad)
        out.append(t)
    return out


# Block font for the headline temperature.
BIG: dict[str, tuple[str, str, str]] = {
    "0": ("█▀█", "█ █", "█▄█"), "1": ("▄█ ", " █ ", "▄█▄"),
    "2": ("█▀█", " ▄▀", "█▄▄"), "3": ("█▀█", " ▀█", "█▄█"),
    "4": ("█ █", "█▄█", "  █"), "5": ("█▀▀", "▀▀█", "▄▄█"),
    "6": ("█▀▀", "█▀█", "█▄█"), "7": ("▀▀█", "  █", "  █"),
    "8": ("█▀█", "█▀█", "█▄█"), "9": ("█▀█", "▀▀█", "▄▄█"),
    ".": ("  ", "  ", "▄ "), "-": ("   ", "▄▄▄", "   "),
    "°": ("▛▜", "▙▟", "  "), "C": ("█▀▀", "█  ", "█▄▄"),
    "F": ("█▀▀", "█▀▀", "█  "), " ": (" ", " ", " "),
    "?": ("▀█", " ▄", " ▄"),
}


def big_text(s: str, style: str) -> list[Text]:
    rows = [Text(style=style) for _ in range(3)]
    glyphs = [BIG[c] for c in s.upper() if c.upper() in BIG]
    for i, glyph in enumerate(glyphs):
        for r in range(3):
            rows[r].append(glyph[r])
            if i != len(glyphs) - 1:
                rows[r].append(" ")
    return rows


# Moon: eight phase faces drawn as a shaded disc. Northern-hemisphere
# convention — a waxing moon is lit on the right, a waning moon on the left.
MOON_FACES = [
    ("New moon",        ("  ▒▒▒▒▒ ", " ▒▒▒▒▒▒▒", "  ▒▒▒▒▒ ")),
    ("Waxing crescent", ("  ▒▒▒▒█ ", " ▒▒▒▒▒██", "  ▒▒▒▒█ ")),
    ("First quarter",   ("  ▒▒███ ", " ▒▒▒████", "  ▒▒███ ")),
    ("Waxing gibbous",  ("  ▒████ ", " ▒▒█████", "  ▒████ ")),
    ("Full moon",       ("  █████ ", " ███████", "  █████ ")),
    ("Waning gibbous",  ("  ████▒ ", " █████▒▒", "  ████▒ ")),
    ("Last quarter",    ("  ███▒▒ ", " ████▒▒▒", "  ███▒▒ ")),
    ("Waning crescent", ("  █▒▒▒▒ ", " ██▒▒▒▒▒", "  █▒▒▒▒ ")),
]


def moon_art(phase: float, theme: Theme) -> tuple[str, list[Text]]:
    """phase 0..1 (0 = new, 0.5 = full) -> (name, three styled rows)."""
    idx = int(phase * 8 + 0.5) % 8
    name, rows = MOON_FACES[idx]
    out = []
    for row in rows:
        t = Text()
        for ch in row:
            t.append(ch, style=theme.moon if ch == "█" else
                     theme.star if ch == "▒" else theme.muted)
        out.append(t)
    return name, out


# ─────────────────────────────────────────────────────────────── astronomy ──
# Pure math; no API. Validated against Open-Meteo's own sunrise/sunset to
# within one minute from the tropics to 78°N.

SYNODIC = 29.530588853       # mean lunar month, days
_NEW_MOON_JD = 2451550.26    # 2000-01-06 18:14 UTC


def _julian(dt: datetime) -> float:
    dt = dt.astimezone(timezone.utc)
    y, m = dt.year, dt.month
    if m <= 2:
        y, m = y - 1, m + 12
    a = y // 100
    b = 2 - a + a // 4
    day = dt.day + (dt.hour + dt.minute / 60 + dt.second / 3600) / 24
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + day + b - 1524.5


def moon_phase(dt: datetime) -> tuple[float, float, float]:
    """Return (phase 0..1, illuminated fraction 0..1, age in days)."""
    age = (_julian(dt) - _NEW_MOON_JD) % SYNODIC
    phase = age / SYNODIC
    return phase, (1 - math.cos(2 * math.pi * phase)) / 2, age


def solar_elevation(dt: datetime, lat: float, lon: float) -> float:
    """Sun's elevation above the horizon, in degrees."""
    n = _julian(dt) - 2451545.0
    mean_lon = (280.460 + 0.9856474 * n) % 360
    anom = math.radians((357.528 + 0.9856003 * n) % 360)
    lam = math.radians(mean_lon + 1.915 * math.sin(anom) + 0.020 * math.sin(2 * anom))
    eps = math.radians(23.439 - 0.0000004 * n)
    dec = math.asin(math.sin(eps) * math.sin(lam))
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    gmst = (18.697374558 + 24.06570982441908 * n) % 24
    hour_angle = math.radians((gmst * 15 + lon) % 360) - ra
    phi = math.radians(lat)
    return math.degrees(math.asin(
        math.sin(phi) * math.sin(dec) +
        math.cos(phi) * math.cos(dec) * math.cos(hour_angle)))


def _crossing(lat: float, lon: float, day_start: datetime,
              target: float, rising: bool) -> Optional[datetime]:
    """When does the sun cross `target` degrees? None inside polar day/night."""
    step = timedelta(minutes=10)
    prev_t = day_start
    prev = solar_elevation(prev_t, lat, lon) - target
    for i in range(1, 145):
        t = day_start + step * i
        cur = solar_elevation(t, lat, lon) - target
        crossed = (prev <= 0 < cur) if rising else (prev >= 0 > cur)
        if crossed:
            lo, hi = prev_t, t
            for _ in range(28):                      # bisect to ~1 s
                mid = lo + (hi - lo) / 2
                if (solar_elevation(mid, lat, lon) - target < 0) == rising:
                    lo = mid
                else:
                    hi = mid
            return lo + (hi - lo) / 2
        prev_t, prev = t, cur
    return None


SUN_EVENTS = (
    ("dawn", -18.0, True), ("blue_am", -6.0, True), ("sunrise", -0.833, True),
    ("golden_am", 6.0, True), ("golden_pm", 6.0, False), ("sunset", -0.833, False),
    ("blue_pm", -6.0, False), ("dusk", -18.0, False),
)


def sun_events(day_start: datetime, lat: float, lon: float) -> dict[str, Optional[datetime]]:
    return {name: _crossing(lat, lon, day_start, deg, rising)
            for name, deg, rising in SUN_EVENTS}


# ─────────────────────────────────────────────────────────────── data layer ──

_CACHE_LOCK = threading.Lock()


class WxError(Exception):
    """Anything the user should see as a clean message rather than a traceback."""


@dataclass
class Fetched:
    data: dict
    age: float = -1.0                  # seconds since fetch; <0 means live
    @property
    def stale(self) -> bool:
        return self.age > 0


def _cache_path(url: str, params: dict) -> Path:
    raw = url + "?" + urlencode(sorted((k, str(v)) for k, v in params.items()))
    return CACHE_DIR / (hashlib.sha256(raw.encode()).hexdigest()[:24] + ".json")


def get_json(url: str, params: dict, ttl: float, *, timeout: float = 15.0,
             retries: int = 3, offline: bool = False, refresh: bool = False,
             unwrap: Optional[Callable[[str], dict]] = None) -> Fetched:
    """GET with a disk cache, exponential backoff, and stale-on-failure."""
    path = _cache_path(url, params)
    now = time.time()
    cached: Optional[dict] = None
    if path.exists():
        try:
            cached = json.loads(path.read_text())
            if not refresh and now - cached["t"] < ttl:
                return Fetched(cached["d"], now - cached["t"])
        except (ValueError, KeyError, OSError):
            cached = None

    if offline:
        if cached:
            return Fetched(cached["d"], now - cached["t"])
        raise WxError("offline, and nothing cached for this location")

    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {resp.status_code} from {resp.url.split('?')[0]}")
            resp.raise_for_status()
            data = unwrap(resp.text) if unwrap else resp.json()
            if isinstance(data, dict) and data.get("error"):
                raise WxError(str(data.get("reason", "API error")))
            with _CACHE_LOCK:
                try:
                    CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(f".{os.getpid()}.tmp")
                    tmp.write_text(json.dumps({"t": now, "d": data}))
                    tmp.replace(path)                 # atomic, and race-free
                except OSError:
                    pass                              # a read-only cache is not fatal
            return Fetched(data, -1.0)
        except WxError:
            raise
        except ValueError as exc:            # includes malformed JSON (`nan` from some models)
            last = WxError(f"the weather service returned unreadable data ({exc})")
        except requests.RequestException as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(0.4 * (2 ** attempt))

    if cached:                                        # network down: serve stale
        return Fetched(cached["d"], now - cached["t"])
    raise WxError(f"could not reach the weather service ({last})")


def gather(jobs: dict[str, tuple[Callable, dict]]) -> dict[str, Any]:
    """Run jobs concurrently. Failures come back as the exception, not a raise."""
    if not jobs:
        return {}
    out: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {pool.submit(fn, **kw): name for name, (fn, kw) in jobs.items()}
        for fut, name in futures.items():
            try:
                out[name] = fut.result()
            except Exception as exc:                  # noqa: BLE001 - reported per-job
                out[name] = exc
    return out


# ────────────────────────────────────────────────────────────────── geocode ──

@dataclass
class Place:
    name: str
    region: str
    lat: float
    lon: float
    timezone: str = "auto"
    elevation: Optional[float] = None
    country: str = ""
    admin1: str = ""

    @property
    def title(self) -> str:
        return f"{self.name} · {self.region}" if self.region else self.name


def geocode_open_meteo(query: str, **kw) -> Place:
    got = get_json(GEO_URL, {"name": query, "count": 1, "language": "en", "format": "json"},
                   TTL_GEO, **kw)
    results = (got.data or {}).get("results") or []
    if not results:
        raise WxError(f"no place called {query!r}")
    r = results[0]
    region = ", ".join(x for x in (r.get("admin1"), r.get("country")) if x)
    return Place(r.get("name", query), region, float(r["latitude"]), float(r["longitude"]),
                 r.get("timezone", "auto"), r.get("elevation"),
                 country=r.get("country", ""), admin1=r.get("admin1", ""))


def geocode_latlng(query: str, api_key: str = "", **kw) -> Place:
    if not api_key or api_key.startswith("<"):
        raise WxError("no latlng API key (set LATLNG_API_KEY or pass --key)")
    from latlng import LatlngClient                      # optional dependency
    hit = getattr(LatlngClient(api_key=api_key).geocode(query), "first", None)
    if not hit or hit.lat is None:
        raise WxError(f"latlng found no match for {query!r}")
    region = ", ".join(x for x in (hit.state, hit.country) if x)
    return Place(hit.city or hit.name or query, region, float(hit.lat), float(hit.lon),
                 country=hit.country or "", admin1=hit.state or "")


def geocode(query: str, provider: str, api_key: str, **kw) -> Place:
    """Coordinates pass straight through; otherwise Open-Meteo, then latlng.

    Open-Meteo ranks by population, so 'reykjavik' means Iceland rather than
    the village in Manitoba that latlng returns.
    """
    coords = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", query)
    if coords:
        lat, lon = float(coords.group(1)), float(coords.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise WxError(f"coordinates out of range: {lat}, {lon}")
        return Place(f"{lat:.3f}, {lon:.3f}", "", lat, lon)

    if provider == "latlng":
        return geocode_latlng(query, api_key, **kw)
    if provider == "open-meteo":
        return geocode_open_meteo(query, **kw)

    try:                                                  # auto
        return geocode_open_meteo(query, **kw)
    except WxError as primary:
        try:
            return geocode_latlng(query, api_key, **kw)
        except Exception:
            raise primary from None                       # report the primary's error


# ──────────────────────────────────────────────────────────────────── fetch ──

CURRENT_VARS = ("temperature_2m,relative_humidity_2m,apparent_temperature,is_day,"
                "precipitation,rain,showers,snowfall,weather_code,cloud_cover,"
                "pressure_msl,surface_pressure,wind_speed_10m,wind_direction_10m,"
                "wind_gusts_10m,uv_index,dew_point_2m,visibility")
HOURLY_VARS = ("temperature_2m,apparent_temperature,precipitation_probability,"
               "precipitation,weather_code,wind_speed_10m,relative_humidity_2m,"
               "dew_point_2m,uv_index,visibility,is_day")
DAILY_VARS = ("weather_code,temperature_2m_max,temperature_2m_min,"
              "apparent_temperature_max,apparent_temperature_min,"
              "precipitation_sum,precipitation_probability_max,precipitation_hours,"
              "wind_speed_10m_max,wind_gusts_10m_max,uv_index_max,"
              "sunrise,sunset,daylight_duration,sunshine_duration")
AIR_CURRENT = "european_aqi,us_aqi,pm2_5,pm10,nitrogen_dioxide,ozone,sulphur_dioxide"
POLLEN_VARS = ("alder_pollen,birch_pollen,grass_pollen,mugwort_pollen,"
               "olive_pollen,ragweed_pollen")


def fetch_all(place: Place, imperial: bool, *, want_air: bool = True,
              want_climate: bool = True, want_alerts: bool = True, **kw) -> dict[str, Any]:
    """One concurrent round-trip for everything the dashboard needs."""
    units: dict[str, str] = {}
    if imperial:
        units = {"temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                 "precipitation_unit": "inch"}

    forecast_params = {
        "latitude": place.lat, "longitude": place.lon,
        "current": CURRENT_VARS, "hourly": HOURLY_VARS, "daily": DAILY_VARS,
        "minutely_15": "precipitation,precipitation_probability",
        "forecast_minutely_15": 48,          # 12 h of nowcast
        "forecast_days": FETCH_DAYS, "timezone": "auto", **units,
    }
    jobs: dict[str, tuple[Callable, dict]] = {
        "forecast": (get_json, dict(url=FORECAST_URL, params=forecast_params,
                                    ttl=TTL_FORECAST, **kw)),
    }
    if want_air:
        jobs["air"] = (get_json, dict(
            url=AIR_URL,
            params={"latitude": place.lat, "longitude": place.lon,
                    "current": AIR_CURRENT, "hourly": POLLEN_VARS,
                    "forecast_days": 1, "timezone": "auto"},
            ttl=TTL_AIR, **kw))
    if want_climate:
        today = date.today()
        jobs["climate"] = (get_json, dict(
            url=ARCHIVE_URL,
            params={"latitude": round(place.lat, 2), "longitude": round(place.lon, 2),
                    "start_date": f"{today.year - 11}-01-01",
                    "end_date": f"{today.year - 2}-12-31",
                    "daily": "temperature_2m_max,temperature_2m_min",
                    "timezone": "auto", **units},
            ttl=TTL_CLIMATE, **kw))
    if want_alerts:
        jobs["alerts"] = (fetch_alerts, dict(place=place, tz=timezone.utc, **kw))
    return gather(jobs)


# ─────────────────────────────────────────────────────────── official alerts ──
# Open-Meteo carries no warnings, so these come from the national services:
# DWD for Germany and the NWS for the United States. Elsewhere the dashboard
# falls back to the derived highlights (frost, high UV, damaging gusts).

# Open-Meteo reports admin1 in English; DWD keys warnings by the German name.
DE_STATES = {
    "baden-württemberg": "Baden-Württemberg", "bavaria": "Bayern", "bayern": "Bayern",
    "berlin": "Berlin", "brandenburg": "Brandenburg", "bremen": "Bremen",
    "hamburg": "Hamburg", "hesse": "Hessen", "hessen": "Hessen",
    "lower saxony": "Niedersachsen", "niedersachsen": "Niedersachsen",
    "mecklenburg-vorpommern": "Mecklenburg-Vorpommern",
    "mecklenburg-western pomerania": "Mecklenburg-Vorpommern",
    "north rhine-westphalia": "Nordrhein-Westfalen",
    "nordrhein-westfalen": "Nordrhein-Westfalen",
    "rhineland-palatinate": "Rheinland-Pfalz", "rheinland-pfalz": "Rheinland-Pfalz",
    "saarland": "Saarland", "saxony": "Sachsen", "sachsen": "Sachsen",
    "saxony-anhalt": "Sachsen-Anhalt", "sachsen-anhalt": "Sachsen-Anhalt",
    "schleswig-holstein": "Schleswig-Holstein",
    "thuringia": "Thüringen", "thüringen": "Thüringen",
}

# DWD level -> our 0-4 scale; NWS severity -> the same scale.
DWD_LEVELS = {0: 0, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4}
NWS_LEVELS = {"minor": 1, "moderate": 2, "severe": 3, "extreme": 4, "unknown": 1}
ALERT_WORDS = ("", "advisory", "warning", "severe warning", "extreme warning")


@dataclass
class Alert:
    event: str
    level: int
    region: str = ""
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    source: str = ""
    detail: str = ""

    @property
    def window(self) -> str:
        if not self.start and not self.end:
            return ""
        fmt_ = lambda d: d.strftime("%a %H:%M") if d else "?"
        return f"{fmt_(self.start)} → {fmt_(self.end)}"


def _unwrap_jsonp(text: str) -> dict:
    """DWD serves JSONP: warnWetter.loadWarnings({...});"""
    body = text.strip()
    start, end = body.find("("), body.rfind(")")
    if start == -1 or end == -1 or end < start:
        raise ValueError("not JSONP")
    return json.loads(body[start + 1:end])


def _match_de_state(admin1: str) -> Optional[str]:
    """Open-Meteo's admin1 is verbose ('Free and Hanseatic City of Hamburg')."""
    key = (admin1 or "").strip().lower()
    if key in DE_STATES:
        return DE_STATES[key]
    # Longest first, so "lower saxony" is never shadowed by "saxony".
    for name in sorted(DE_STATES, key=len, reverse=True):
        if name in key:
            return DE_STATES[name]
    for german in sorted(set(DE_STATES.values()), key=len, reverse=True):
        if german.lower() in key:
            return german
    return None


def _dwd_alerts(place: Place, tz: timezone, **kw) -> list[Alert]:
    want = _match_de_state(place.admin1)
    if not want:
        return []
    got = get_json(DWD_URL, {}, TTL_ALERTS, unwrap=_unwrap_jsonp, **kw)
    payload = got.data
    if isinstance(payload, dict) and "warnings" in payload:
        blocks = [payload.get("warnings") or {}, payload.get("vorabInformation") or {}]
    else:
        return []
    city = (place.name or "").lower()
    exact, statewide = [], []
    for block in blocks:
        for entries in block.values():
            for w in entries or []:
                if w.get("state") != want:
                    continue
                alert = Alert(
                    event=(w.get("event") or "").title() or "Warnung",
                    level=DWD_LEVELS.get(w.get("level", 2), 1),
                    region=w.get("regionName", ""),
                    start=_epoch_ms(w.get("start"), tz), end=_epoch_ms(w.get("end"), tz),
                    source="DWD", detail=(w.get("headline") or "").strip())
                (exact if city and city in alert.region.lower() else statewide).append(alert)
    chosen = exact or statewide
    seen, out = set(), []
    for a in sorted(chosen, key=lambda a: -a.level):
        key = (a.event, a.level)
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out[:4]


def _nws_alerts(place: Place, tz: timezone, **kw) -> list[Alert]:
    got = get_json(NWS_URL, {"point": f"{place.lat:.4f},{place.lon:.4f}", "status": "actual"},
                   TTL_ALERTS, **kw)
    out = []
    for feat in (got.data or {}).get("features") or []:
        pr = feat.get("properties") or {}
        out.append(Alert(
            event=pr.get("event", "Alert"),
            level=NWS_LEVELS.get(str(pr.get("severity", "")).lower(), 1),
            region=(pr.get("areaDesc") or "").split(";")[0].strip(),
            start=_iso_dt(pr.get("onset") or pr.get("effective"), tz),
            end=_iso_dt(pr.get("ends") or pr.get("expires"), tz),
            source="NWS", detail=(pr.get("headline") or "").strip()))
    return sorted(out, key=lambda a: -a.level)[:4]


def _epoch_ms(value, tz: timezone) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(value / 1000, tz)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _iso_dt(value, tz: timezone) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value)).astimezone(tz)
    except (TypeError, ValueError):
        return None


def fetch_alerts(place: Place, tz: timezone, **kw) -> list[Alert]:
    """Official warnings, where a free national service covers this place."""
    country = (place.country or "").lower()
    try:
        if country in ("germany", "deutschland"):
            return _dwd_alerts(place, tz, **kw)
        if country in ("united states", "united states of america", "usa"):
            return _nws_alerts(place, tz, **kw)
    except Exception:
        return []                    # warnings are a bonus; never break the dashboard
    return []


# ───────────────────────────────────────────────────────────────── analysis ──

@dataclass
class Nowcast:
    """What the next 12 hours of precipitation look like, at 15-minute steps."""
    starts_in: Optional[int] = None      # minutes until precip begins
    stops_in: Optional[int] = None       # minutes until it stops (if raining now)
    raining_now: bool = False
    horizon_min: int = 0
    series: list[float] = field(default_factory=list)
    probs: list[float] = field(default_factory=list)

    @property
    def headline(self) -> Optional[str]:
        if self.raining_now:
            return (f"easing in ~{_pretty_minutes(self.stops_in)}"
                    if self.stops_in else "precipitation ongoing")
        if self.starts_in is not None:
            return f"precipitation starting in ~{_pretty_minutes(self.starts_in)}"
        return None


def _pretty_minutes(m: Optional[int]) -> str:
    if m is None:
        return "?"
    if m < 60:
        return f"{max(m, 5)} min"
    h, rest = divmod(m, 60)
    return f"{h}h" if rest < 8 else f"{h}h{rest:02d}"


def build_nowcast(block: dict, now_iso: str, threshold: float = 0.05) -> Optional[Nowcast]:
    times = (block or {}).get("time") or []
    precip = (block or {}).get("precipitation") or []
    if not times or not precip:
        return None
    probs = block.get("precipitation_probability") or [0.0] * len(times)
    start = 0
    for i, t in enumerate(times):                 # align to the current quarter hour
        if t >= now_iso[:16]:
            start = i
            break
    times, precip = times[start:], precip[start:]
    probs = probs[start:start + len(times)]
    if not precip:
        return None

    values = [v or 0.0 for v in precip]
    nc = Nowcast(horizon_min=len(values) * 15, series=values,
                 probs=[p or 0.0 for p in probs])
    nc.raining_now = values[0] > threshold
    if nc.raining_now:
        dry = next((i for i, v in enumerate(values) if v <= threshold), None)
        nc.stops_in = dry * 15 if dry else None
    else:
        wet = next((i for i, v in enumerate(values) if v > threshold), None)
        nc.starts_in = wet * 15 if wet else None
    return nc


def climate_normal(archive: dict, target: date, window: int = 7) -> Optional[tuple[float, float, int]]:
    """Mean daily max/min for this time of year across the archive window."""
    daily = (archive or {}).get("daily") or {}
    times, highs, lows = daily.get("time"), daily.get("temperature_2m_max"), daily.get("temperature_2m_min")
    if not times or not highs:
        return None
    tgt = target.timetuple().tm_yday
    hi_vals, lo_vals = [], []
    for t, hi, lo in zip(times, highs, lows or [None] * len(times)):
        try:
            doy = date.fromisoformat(t).timetuple().tm_yday
        except ValueError:
            continue
        if min(abs(doy - tgt), 366 - abs(doy - tgt)) <= window and hi is not None:
            hi_vals.append(hi)
            if lo is not None:
                lo_vals.append(lo)
    if len(hi_vals) < 20:                       # too little history to be meaningful
        return None
    return (sum(hi_vals) / len(hi_vals),
            sum(lo_vals) / len(lo_vals) if lo_vals else float("nan"),
            len(hi_vals))


# The API returns both AQI scales; each is the accepted standard in its region.
EU_AQI_BANDS = ((0, "Good"), (20, "Fair"), (40, "Moderate"), (60, "Poor"),
                (80, "Very poor"), (100, "Extremely poor"))
US_AQI_BANDS = ((0, "Good"), (51, "Moderate"), (101, "Unhealthy (sensitive)"),
                (151, "Unhealthy"), (201, "Very unhealthy"), (301, "Hazardous"))
EU_AQI_WARN, US_AQI_WARN = (40, 60), (51, 101)


def in_europe(lat: float, lon: float) -> bool:
    """Rough bounding box for the European AQI / CAMS-Europe domain."""
    return 34.0 <= lat <= 72.0 and -25.0 <= lon <= 45.0


def aqi_reading(air_current: dict, lat: float, lon: float):
    """Pick the regionally appropriate AQI scale. Returns (label, value, bands, warn)."""
    eu, us = air_current.get("european_aqi"), air_current.get("us_aqi")
    if in_europe(lat, lon) and eu is not None:
        return "European AQI", eu, EU_AQI_BANDS, EU_AQI_WARN
    if us is not None:
        return "US AQI", us, US_AQI_BANDS, US_AQI_WARN
    if eu is not None:
        return "European AQI", eu, EU_AQI_BANDS, EU_AQI_WARN
    return None, None, EU_AQI_BANDS, EU_AQI_WARN
POLLEN_BANDS = ((0.0, "none"), (1.0, "low"), (20.0, "moderate"), (50.0, "high"), (150.0, "very high"))


def nearest_time_index(times: Sequence[str], now: datetime,
                       tz: timezone) -> Optional[int]:
    """Index of the timestamp closest to `now`. API times are naive local."""
    best, best_gap = None, None
    for i, t in enumerate(times or ()):
        try:
            stamp = datetime.fromisoformat(t).replace(tzinfo=tz)
        except (ValueError, TypeError):
            continue
        gap = abs((stamp - now).total_seconds())
        if best_gap is None or gap < best_gap:
            best, best_gap = i, gap
    return best


def band_label(value: float, bands: Sequence[tuple[float, str]]) -> str:
    out = bands[0][1]
    for limit, label in bands:
        if value >= limit:
            out = label
    return out


# ────────────────────────────────────────────────────────────────── widgets ──

COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
ARROWS = "↓↙↙←←↖↖↑↑↗↗→→↘↘↓"           # direction the wind blows toward
BLOCKS = " ▁▂▃▄▅▆▇█"


def wind_dir(deg: Optional[float]) -> str:
    if deg is None:
        return ""
    i = int(deg / 22.5 + 0.5) % 16
    return f"{COMPASS[i]} {ARROWS[i]}"


def fmt(value: Any, spec: str = "", dash: str = "—") -> str:
    if value is None:
        return dash
    try:
        out = format(value, spec) if spec else str(value)
    except (TypeError, ValueError):
        return str(value)
    return out[1:] if out.startswith("-") and float(out) == 0 else out


def deg(value: Optional[float]) -> str:
    """A temperature rounded to whole degrees, without a '-0'."""
    if value is None:
        return "—"
    return f"{value + 0.0:.0f}°".replace("-0°", "0°")


def metric(label: str, value: str, theme: Theme, style: Optional[str] = None) -> Text:
    t = Text()
    t.append(f"{label} ", style=theme.label)
    t.append(value, style=style or f"bold {theme.fg}")
    return t


def section(title: str, body: RenderableType, theme: Theme) -> Group:
    head = Text(title, style=f"bold {theme.label}")
    return Group(head, Text(""), body)


def flow(cells: Sequence[Text], columns: int, gap: int = 3) -> Table:
    """Lay cells out in a fixed number of columns, wrapping into rows."""
    grid = Table.grid(padding=(0, gap))
    for _ in range(max(1, columns)):
        grid.add_column(justify="left")
    for i in range(0, len(cells), max(1, columns)):
        row = list(cells[i:i + columns])
        row += [Text("")] * (columns - len(row))
        grid.add_row(*row)
    return grid


def temp_chart(temps: Sequence[float], probs: Sequence[float], times: Sequence[str],
               theme: Theme, unit: str, height: int = 7, step: int = 2) -> Group:
    """Block graph of temperature with a precipitation-probability strip."""
    lo, hi = min(temps), max(temps)
    if hi - lo < 1:
        lo, hi = lo - 1, hi + 1
    pad = (hi - lo) * 0.12
    lo, hi = lo - pad, hi + pad
    span = hi - lo
    gutter = 6

    rows: list[Text] = []
    for r in range(height, 0, -1):
        line = Text()
        if r == height:
            line.append(f"{max(temps):>4.0f}° ", style=theme.label)
        elif r == 1:
            line.append(f"{min(temps):>4.0f}° ", style=theme.label)
        else:
            line.append(" " * gutter)
        for t in temps:
            f = (t - lo) / span * height
            if f >= r:
                ch = "█"
            elif f > r - 1:
                ch = BLOCKS[max(1, min(8, int((f - (r - 1)) * 8)))]
            else:
                ch = " "
            line.append(ch * step, style=ramp_color(t, theme))
        rows.append(line)

    strip = Text(" " * gutter)
    for p in probs:
        p = p or 0
        if p <= 0:
            strip.append("·" * step, style=theme.dim_dot)
        else:
            shade = blend("#2d4a63", theme.rain, p / 100) if theme.name != "mono" else theme.fg
            strip.append("▄" * step, style=shade)
    rows.append(strip)

    axis = Text(" " * gutter)
    for i, ts in enumerate(times):
        if i % 3 == 0:
            axis.append(ts[11:13].ljust(step * 3), style=theme.label)
    rows.append(axis)

    legend = Text()
    legend.append(f"temperature °{unit}", style=theme.muted)
    legend.append("  ·  ", style=theme.dim_dot)
    legend.append("▄", style=theme.rain)
    legend.append(" chance of precipitation", style=theme.muted)
    return Group(legend, Text(""), *rows)


def nowcast_strip(nc: Nowcast, theme: Theme, width: int) -> Group:
    """12 hours of precipitation at 15-minute resolution."""
    cells = min(len(nc.series), max(24, width - 12))
    values = nc.series[:cells]
    peak = max(values) if values else 0.0
    bar = Text()
    for v in values:
        if v <= 0.02:
            bar.append("·", style=theme.dim_dot)
        else:
            f = min(1.0, v / max(peak, 0.5))
            idx = max(1, min(8, int(f * 8)))
            bar.append(BLOCKS[idx], style=blend(theme.rain, theme.bolt, f * 0.35)
                       if theme.name != "mono" else theme.fg)
    axis = Text()
    per_hour = 4
    for i in range(0, cells, per_hour * 2):
        axis.append(f"+{i // per_hour}h".ljust(per_hour * 2), style=theme.label)

    head = Text()
    if nc.headline:
        head.append("● ", style=theme.rain)
        head.append(nc.headline, style=f"bold {theme.fg}")
        if peak:
            head.append(f"   peak {peak:.2f} mm/15min", style=theme.muted)
    else:
        head.append("● ", style=theme.good)
        head.append(f"dry for the next {nc.horizon_min // 60} hours", style=f"bold {theme.fg}")
    return Group(head, Text(""), bar, axis)


def alerts_panel(alerts: Sequence["Alert"], theme: Theme, width: int) -> Table:
    """Official warnings, most severe first."""
    colors = (theme.label, theme.warn, theme.warn, theme.bad, theme.bad)
    wide = width >= 82
    t = Table.grid(padding=(0, 2))
    t.add_column(width=1)                       # severity stripe
    t.add_column(min_width=16)                  # event
    t.add_column(min_width=13)                  # region
    if wide:
        t.add_column(min_width=15)              # severity word
        t.add_column(justify="right")           # time window
    for a in alerts:
        level = max(0, min(4, a.level))
        color = colors[level]
        row = [Text("▌", style=f"bold {color}"),
               Text(a.event, style=f"bold {color}"),
               Text(a.region[:24], style=theme.label)]
        if wide:
            row.append(Text(f"{ALERT_WORDS[level]} · {a.source}", style=theme.muted))
            row.append(Text(a.window, style=theme.muted))
        t.add_row(*row)
    return t


def daylight_bar(events: dict, now: datetime, theme: Theme,
                 lat: float, lon: float, width: int = 30) -> Text:
    rise, set_ = events.get("sunrise"), events.get("sunset")
    bar = Text()
    if not rise or not set_:
        # Inside the polar circles the sun may not cross the horizon at all.
        noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
        up = solar_elevation(noon, lat, lon) > 0
        bar.append("☀ polar day — the sun stays up" if up else "☾ polar night — the sun stays down",
                   style=theme.sun if up else theme.moon)
        return bar

    total = (set_ - rise).total_seconds()
    frac = (now - rise).total_seconds() / total if total > 0 else 0.0
    daytime = 0.0 <= frac <= 1.0
    pos = round(max(0.0, min(1.0, frac)) * (width - 1))

    bar.append(rise.strftime("%H:%M") + " ", style=theme.sun)
    for i in range(width):
        if i == pos:
            bar.append("●" if daytime else "○", style=f"bold {theme.sun if daytime else theme.moon}")
        else:
            bar.append("━", style=(theme.sun if daytime else theme.muted) if i < pos else theme.muted)
    bar.append(" " + set_.strftime("%H:%M"), style=theme.sun)
    h, m = divmod(int(total // 60), 60)
    bar.append(f"  {h}h{m:02d}", style=theme.muted)
    return bar


def hourly_table(hourly: dict, theme: Theme, start: int, count: int,
                 unit: str, speed_unit: str, precip_unit: str) -> Table:
    """Hour-by-hour detail. Uses data the forecast request already returned."""
    times = hourly.get("time") or []
    sl = slice(start, start + count)

    def col(name):
        seq = hourly.get(name)
        return list(seq[sl]) if seq else []

    temps, feels = col("temperature_2m"), col("apparent_temperature")
    probs, amounts = col("precipitation_probability"), col("precipitation")
    winds, hums = col("wind_speed_10m"), col("relative_humidity_2m")
    codes, days = col("weather_code"), col("is_day")
    stamps = times[sl]

    t = Table.grid(padding=(0, 2))
    for spec in (dict(width=5), dict(width=1), dict(width=13), dict(width=5, justify="right"),
                 dict(width=5, justify="right"), dict(width=5, justify="right"),
                 dict(width=7, justify="right"), dict(width=8, justify="right"),
                 dict(width=4, justify="right")):
        t.add_column(**spec)
    t.add_row(*[Text(h, style=f"bold {theme.label}") for h in
                ("", "", "", "temp", "feels", "rain", "amount", "wind", "hum")])

    def at(seq, i):
        return seq[i] if i < len(seq) else None

    for i, stamp in enumerate(stamps):
        code = at(codes, i)
        label, _, glyph, sev = describe(code, bool(at(days, i) if at(days, i) is not None else 1))
        temp, feel = at(temps, i), at(feels, i)
        p, mm = at(probs, i) or 0, at(amounts, i) or 0
        t.add_row(
            Text(stamp[11:16], style=theme.label),
            Text(glyph, style=theme.sun if code in (0, 1) else theme.cloud),
            Text(label, style=theme.bad if sev >= 3 else theme.label),
            Text(deg(temp), style=ramp_color(temp, theme)),
            Text(deg(feel), style=theme.muted),
            Text(f"{p:.0f}%" if p else "·", style=theme.rain if p >= 40 else
                 theme.label if p else theme.dim_dot),
            Text(f"{mm:.1f} {precip_unit}" if mm else "·",
                 style=theme.rain if mm else theme.dim_dot),
            Text(f"{at(winds, i):.0f} {speed_unit}" if at(winds, i) is not None else "—",
                 style=theme.label),
            Text(f"{at(hums, i):.0f}%" if at(hums, i) is not None else "—", style=theme.muted))
    return t


def forecast_table(daily: dict, theme: Theme, unit: str, bar_width: int = 26) -> Table:
    bar_width = max(8, min(bar_width, 28))
    codes = daily.get("weather_code") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    probs = daily.get("precipitation_probability_max") or [None] * len(codes)
    sums = daily.get("precipitation_sum") or [None] * len(codes)
    winds = daily.get("wind_speed_10m_max") or [None] * len(codes)
    dates = daily.get("time") or []

    valid_lo = [v for v in lows if v is not None]
    valid_hi = [v for v in highs if v is not None]
    if not valid_lo or not valid_hi:
        return Table.grid()
    gmin, gmax = min(valid_lo), max(valid_hi)
    if gmax - gmin < 1:
        gmax = gmin + 1

    t = Table.grid(padding=(0, 1))
    for spec in (dict(width=10), dict(width=1, justify="left"), dict(width=15),
                 dict(width=5, justify="right"), dict(width=bar_width),
                 dict(width=5), dict(width=5, justify="right"),
                 dict(width=6, justify="right")):
        t.add_column(**spec)

    def at(seq: Sequence, i: int):
        return seq[i] if seq is not None and i < len(seq) else None

    for i, day in enumerate(dates):
        label, _, glyph, sev = describe(at(codes, i))
        try:
            d = datetime.fromisoformat(day)
            name = "Today" if i == 0 else "Tomorrow" if i == 1 else d.strftime("%a %d %b")
        except (ValueError, TypeError):
            name = str(day)
        lo, hi = at(lows, i), at(highs, i)
        bar = Text()
        if lo is None or hi is None:
            bar.append("·" * bar_width, style=theme.dim_dot)
        else:
            s = round((lo - gmin) / (gmax - gmin) * (bar_width - 1))
            e = round((hi - gmin) / (gmax - gmin) * (bar_width - 1))
            for x in range(bar_width):
                if s <= x <= e:
                    f = 0.0 if e == s else (x - s) / (e - s)
                    bar.append("━", style=f"bold {ramp_color(lo + (hi - lo) * f, theme)}")
                else:
                    bar.append("·", style=theme.dim_dot)

        p = at(probs, i)
        precip = (Text("—", style=theme.muted) if p is None else
                  Text(f"{p:.0f}%", style=theme.rain if p >= 40 else
                       theme.label if p else theme.muted))
        mm = at(sums, i)
        amount = Text(f"{mm:.1f}" if mm else "·", style=theme.rain if mm else theme.dim_dot)

        t.add_row(
            Text(name, style=f"bold {theme.fg}" if i == 0 else theme.fg),
            Text(glyph, style=theme.sun if at(codes, i) in (0, 1) else theme.cloud),
            Text(label, style=theme.bad if sev >= 3 else theme.warn if sev == 2 else theme.label),
            Text(deg(lo), style=ramp_color(lo, theme)),
            bar,
            Text(deg(hi), style=ramp_color(hi, theme)),
            precip, amount)
    return t


# ──────────────────────────────────────────────────────────────── dashboard ──

@dataclass
class Report:
    """Everything one city's dashboard is built from."""
    place: Place
    forecast: dict
    air: Optional[dict] = None
    climate: Optional[dict] = None
    imperial: bool = False
    days: int = 7
    alerts: list = field(default_factory=list)
    age: float = -1.0
    notes: list[str] = field(default_factory=list)

    @property
    def current(self) -> dict:
        return self.forecast.get("current") or {}

    @property
    def daily(self) -> dict:
        """The daily block, trimmed to the requested number of days."""
        block = self.forecast.get("daily") or {}
        n = max(1, min(16, self.days))
        return {k: (v[:n] if isinstance(v, list) else v) for k, v in block.items()}

    @property
    def hourly(self) -> dict:
        return self.forecast.get("hourly") or {}

    @property
    def unit(self) -> str:
        return "F" if self.imperial else "C"

    @property
    def tz(self) -> timezone:
        """The city's UTC offset, as reported by the API."""
        return timezone(timedelta(seconds=self.forecast.get("utc_offset_seconds", 0) or 0))

    @property
    def stale_note(self) -> str:
        """Non-empty when the dashboard is drawn from cache rather than live data."""
        if self.age is None or self.age < TTL_FORECAST:
            return ""          # inside the refresh window: this is current data
        mins = int(self.age // 60)
        return f"cached {mins} min ago" if mins < 90 else f"cached {mins // 60} h ago"

    @property
    def now(self) -> datetime:
        """Local wall-clock time at the city, timezone-aware."""
        try:
            naive = datetime.fromisoformat(self.current.get("time") or self.hourly["time"][0])
        except (ValueError, KeyError, IndexError, TypeError):
            naive = datetime.now()
        return naive.replace(tzinfo=self.tz)


def build_report(place: Place, days: int, imperial: bool, *, want_air: bool,
                 want_climate: bool, want_alerts: bool = True, **kw) -> Report:
    got = fetch_all(place, imperial, want_air=want_air, want_climate=want_climate,
                    want_alerts=want_alerts, **kw)
    fc = got.get("forecast")
    if isinstance(fc, Exception):
        raise fc if isinstance(fc, WxError) else WxError(str(fc))
    if not isinstance(fc, Fetched):
        raise WxError("no forecast data returned")

    report = Report(place, fc.data, imperial=imperial, days=days, age=fc.age)
    for key, attr in (("air", "air"), ("climate", "climate")):
        val = got.get(key)
        if isinstance(val, Fetched):
            setattr(report, attr, val.data)
        elif isinstance(val, Exception):
            report.notes.append(f"{key} unavailable")
    alerts = got.get("alerts")
    if isinstance(alerts, list):
        # Re-stamp the times in the city's own zone now that we know it.
        for a in alerts:
            for attr in ("start", "end"):
                v = getattr(a, attr)
                if v is not None:
                    setattr(a, attr, v.astimezone(report.tz))
        report.alerts = alerts
    return report


def highlights(rep: Report, theme: Theme) -> list[Text]:
    """Short, high-value warnings worth surfacing above the fold."""
    out: list[Text] = []
    cur, daily = rep.current, rep.daily
    _, _, _, sev = describe(cur.get("weather_code"), bool(cur.get("is_day", 1)))
    if sev >= 3:
        label, _, _, _ = describe(cur.get("weather_code"))
        out.append(Text(f"⚠ {label}", style=f"bold {theme.bad}"))

    lows = daily.get("temperature_2m_min") or []
    freeze = 32 if rep.imperial else 0
    if lows and lows[0] is not None and lows[0] <= freeze:
        out.append(Text(f"❄ frost tonight ({lows[0]:.0f}°)", style=f"bold {theme.rain}"))

    uv = (daily.get("uv_index_max") or [None])[0]
    if uv is not None and uv >= 8:
        out.append(Text(f"☀ very high UV ({uv:.0f})", style=f"bold {theme.warn}"))

    gust = (daily.get("wind_gusts_10m_max") or [None])[0]
    limit = 50 if rep.imperial else 80
    if gust is not None and gust >= limit:
        out.append(Text(f"⇈ gusts to {gust:.0f}", style=f"bold {theme.warn}"))
    return out


def render_dashboard(rep: Report, theme: Theme, width: int, *,
                     show: set[str], hours: int = 12) -> Panel:
    inner = max(44, width - 8)
    cur, daily, hourly = rep.current, rep.daily, rep.hourly
    unit, now = rep.unit, rep.now
    is_day = bool(cur.get("is_day", 1))
    temp = cur.get("temperature_2m")
    label, art_key, _, _ = describe(cur.get("weather_code"), is_day)
    accent = ramp_color(temp, theme)

    # -- hero ---------------------------------------------------------------
    facts = Text()
    facts.append(label, style=f"bold {theme.fg}")
    feels = cur.get("apparent_temperature")
    if feels is not None and temp is not None and abs(feels - temp) >= 0.5:
        facts.append("\nfeels like ", style=theme.label)
        facts.append(f"{feels:.0f}°", style=ramp_color(feels, theme))

    normal = climate_normal(rep.climate or {}, now.date()) if rep.climate else None
    if normal and temp is not None:
        hi_today = (daily.get("temperature_2m_max") or [temp])[0] or temp
        delta = hi_today - normal[0]
        word = "above" if delta >= 0 else "below"
        style = theme.bad if delta >= 3 else theme.rain if delta <= -3 else theme.label
        facts.append("\n", style=theme.label)
        facts.append(f"{abs(delta):.1f}° {word} normal", style=f"bold {style}")
        facts.append(f" for {now:%d %b}", style=theme.muted)

    hero = Table.grid(padding=(0, 3))
    hero.add_column(width=14)
    hero.add_column()
    big = big_text(f"{temp:.0f}°{unit}" if temp is not None else "?", accent)
    hero.add_row(Group(*art_lines(art_key, theme)), Group(*big, Text(""), facts))

    blocks: list[RenderableType] = [hero]

    if "alerts" in show and rep.alerts:
        blocks += [Text(""), Rule(style=theme.bad), Text(""),
                   section(f"⚠ OFFICIAL WARNINGS · {rep.alerts[0].source}",
                           alerts_panel(rep.alerts, theme, inner), theme)]

    warn = highlights(rep, theme)
    if warn:
        row = Text()
        for i, w in enumerate(warn):
            if i:
                row.append("   ")
            row.append_text(w)
        blocks += [Text(""), row]

    def rule() -> RenderableType:
        return Group(Text(""), Rule(style=theme.rule), Text(""))

    # -- metrics ------------------------------------------------------------
    speed_unit = (rep.forecast.get("current_units", {})
                  .get("wind_speed_10m", "km/h").replace("mp/h", "mph"))
    precip_unit = rep.forecast.get("current_units", {}).get("precipitation", "mm")
    pressure = cur.get("pressure_msl")
    uv_now, uv_max = cur.get("uv_index"), (daily.get("uv_index_max") or [None])[0]
    if uv_now is not None:
        uv_text = f"{uv_now:.0f}" + (f" · {uv_max:.0f} max" if uv_max is not None else "")
    else:
        uv_text = f"{uv_max:.0f} max" if uv_max is not None else "—"
    vis = cur.get("visibility")
    dew = cur.get("dew_point_2m")
    cells = [
        metric("Humidity", f"{fmt(cur.get('relative_humidity_2m'))}%", theme),
        metric("Dew pt", deg(dew), theme, style=f"bold {ramp_color(dew, theme)}"),
        metric("Wind", f"{fmt(cur.get('wind_speed_10m'))} {speed_unit} "
                       f"{wind_dir(cur.get('wind_direction_10m'))}", theme),
        metric("Gusts", f"{fmt(cur.get('wind_gusts_10m'))} {speed_unit}", theme),
        metric("Clouds", f"{fmt(cur.get('cloud_cover'))}%", theme),
        metric("Pressure", f"{pressure:.0f} hPa" if isinstance(pressure, (int, float)) else "—", theme),
        metric("Precip", f"{fmt(cur.get('precipitation'), '.1f')} {precip_unit}", theme),
        metric("UV", uv_text, theme,
               style=f"bold {scale_color(uv_now if uv_now is not None else (uv_max or 0), ((0, theme.good), (6, theme.warn), (8, theme.bad)), theme)}"),
    ]
    if isinstance(vis, (int, float)):
        cells.append(metric("Visibility", f"{vis / 1000:.0f} km" if vis >= 1000
                            else f"{vis:.0f} m", theme))
    blocks += [rule(), flow(cells, 4 if inner >= 84 else 2)]

    # -- nowcast ------------------------------------------------------------
    if "nowcast" in show:
        nc = build_nowcast(rep.forecast.get("minutely_15") or {}, cur.get("time", ""))
        if nc:
            blocks += [rule(), section("NOWCAST · NEXT 12 HOURS",
                                       nowcast_strip(nc, theme, inner), theme)]

    # -- air quality --------------------------------------------------------
    if "air" in show and rep.air:
        aq = rep.air.get("current") or {}
        aq_name, aqi, aq_bands, (aq_warn, aq_bad) = aqi_reading(aq, rep.place.lat, rep.place.lon)
        aq_cells = []
        if aqi is not None:
            aq_cells.append(metric(aq_name, f"{aqi:.0f} {band_label(aqi, aq_bands)}", theme,
                                   style=f"bold {scale_color(aqi, ((0, theme.good), (aq_warn, theme.warn), (aq_bad, theme.bad)), theme)}"))
        for key, name in (("pm2_5", "PM2.5"), ("pm10", "PM10"),
                          ("ozone", "O₃"), ("nitrogen_dioxide", "NO₂")):
            if aq.get(key) is not None:
                aq_cells.append(metric(name, f"{aq[key]:.0f}", theme))
        pollen = rep.air.get("hourly") or {}
        ptimes = pollen.get("time") or []
        pidx = nearest_time_index(ptimes, now, rep.tz)
        active, covered = [], False
        if pidx is not None:
            for key, series in pollen.items():
                if not key.endswith("_pollen") or not series or pidx >= len(series):
                    continue
                v = series[pidx]
                if v is None:            # outside the CAMS-Europe pollen domain
                    continue
                covered = True           # 0.0 is real data: "no pollen right now"
                if v >= 1:
                    active.append((key[:-7].capitalize(), v))
        active.sort(key=lambda kv: -kv[1])
        for name, v in active[:3]:
            aq_cells.append(metric(name, f"{v:.0f} {band_label(v, POLLEN_BANDS)}", theme,
                                   style=f"bold {scale_color(v, ((0, theme.good), (20, theme.warn), (50, theme.bad)), theme)}"))
        if covered and not active:
            aq_cells.append(metric("Pollen", "none today", theme, style=theme.good))
        if aq_cells:
            blocks += [rule(), section("AIR", flow(aq_cells, 4 if inner >= 84 else 2), theme)]

    # -- sun & moon ---------------------------------------------------------
    if "astro" in show:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        ev = sun_events(day_start, rep.place.lat, rep.place.lon)
        phase, illum, age_days = moon_phase(now)
        mname, mrows = moon_art(phase, theme)

        astro = Table.grid(padding=(0, 4))
        astro.add_column()
        astro.add_column()
        left = [daylight_bar(ev, now, theme, rep.place.lat, rep.place.lon,
                             min(30, max(14, inner - 46)))]
        gold = Text()
        if ev.get("golden_pm") and ev.get("sunset"):
            gold.append("golden ", style=theme.label)
            gold.append(f"{ev['golden_pm']:%H:%M}–{ev['sunset']:%H:%M}", style=theme.sun)
        if ev.get("blue_pm") and ev.get("sunset"):
            gold.append("   blue ", style=theme.label)
            gold.append(f"{ev['sunset']:%H:%M}–{ev['blue_pm']:%H:%M}", style=theme.rain)
        left.append(gold)
        moon_info = Text()
        moon_info.append(f"{mname}\n", style=f"bold {theme.fg}")
        moon_info.append(f"{illum * 100:.0f}% lit · day {age_days:.0f}", style=theme.muted)
        moon_grid = Table.grid(padding=(0, 2))
        moon_grid.add_column()
        moon_grid.add_column()
        moon_grid.add_row(Group(*mrows), moon_info)
        astro.add_row(Group(*left), moon_grid)
        blocks += [rule(), section("SUN & MOON", astro, theme)]

    # -- 24 h chart ---------------------------------------------------------
    if "chart" in show and hourly.get("temperature_2m"):
        times = hourly.get("time") or []
        key = (cur.get("time") or times[0])[:13] + ":00"
        start = times.index(key) if key in times else 0
        sl = slice(start, start + 24)
        temps = [t for t in hourly["temperature_2m"][sl] if t is not None]
        if len(temps) >= 6:
            probs = (hourly.get("precipitation_probability") or [0] * len(times))[sl]
            blocks += [rule(), section(
                "NEXT 24 HOURS",
                temp_chart(temps, probs, times[sl], theme, unit,
                           step=2 if inner >= 56 else 1), theme)]

    # -- hourly detail ------------------------------------------------------
    if "hourly" in show and hourly.get("time"):
        times = hourly.get("time") or []
        key = (cur.get("time") or times[0])[:13] + ":00"
        start = times.index(key) if key in times else 0
        n = min(hours, max(0, len(times) - start))
        if n:
            blocks += [rule(), section(
                f"HOUR BY HOUR · NEXT {n}",
                hourly_table(hourly, theme, start, n, unit, speed_unit, precip_unit), theme)]

    # -- forecast -----------------------------------------------------------
    if "forecast" in show and daily.get("time"):
        blocks += [rule(), section("FORECAST",
                                   forecast_table(daily, theme, unit, inner - 52), theme)]

    title = Text()
    title.append(f" {rep.place.name.upper()} ", style=f"bold {accent}")
    if rep.place.region:
        title.append(f"· {rep.place.region} ", style=theme.label)

    elev = rep.forecast.get("elevation")
    where = f"{rep.forecast.get('timezone', 'local')}"
    if isinstance(elev, (int, float)):
        where += f" · {elev:.0f} m"
    sub = Text(f" {now:%A %d %B, %H:%M} · {where} ", style=theme.muted)
    if rep.stale_note:
        sub = Text(f" {rep.stale_note} · {now:%H:%M} ", style=theme.warn)

    return Panel(Group(*blocks), title=title, subtitle=sub, border_style=accent,
                 padding=(1, 3), title_align="left", subtitle_align="right",
                 box=box.ROUNDED)


# ───────────────────────────────────────────────────────────── output modes ──

def render_oneline(rep: Report, theme: Theme) -> Text:
    """A single line, for a shell prompt or status bar."""
    cur = rep.current
    temp = cur.get("temperature_2m")
    _, _, glyph, _ = describe(cur.get("weather_code"), bool(cur.get("is_day", 1)))
    t = Text()
    t.append(f"{glyph} ", style=theme.sun)
    t.append(f"{temp:.0f}°{rep.unit}" if temp is not None else "?",
             style=f"bold {ramp_color(temp, theme)}")
    hi = (rep.daily.get("temperature_2m_max") or [None])[0]
    lo = (rep.daily.get("temperature_2m_min") or [None])[0]
    if hi is not None and lo is not None:
        t.append(f"  {deg(lo)[:-1]}/{deg(hi)[:-1]}", style=theme.label)
    p = (rep.daily.get("precipitation_probability_max") or [None])[0]
    if p:
        t.append(f"  ☂{p:.0f}%", style=theme.rain)
    t.append(f"  {rep.place.name}", style=theme.muted)
    return t


def report_to_dict(rep: Report) -> dict:
    """Machine-readable projection, for --json."""
    cur, daily = rep.current, rep.daily
    now = rep.now
    label, _, _, sev = describe(cur.get("weather_code"), bool(cur.get("is_day", 1)))
    nc = build_nowcast(rep.forecast.get("minutely_15") or {}, cur.get("time", ""))
    normal = climate_normal(rep.climate or {}, now.date()) if rep.climate else None
    phase, illum, age_days = moon_phase(now)
    ev = sun_events(now.replace(hour=0, minute=0, second=0, microsecond=0),
                    rep.place.lat, rep.place.lon)
    out = {
        "place": {"name": rep.place.name, "region": rep.place.region,
                  "lat": rep.place.lat, "lon": rep.place.lon,
                  "timezone": rep.forecast.get("timezone")},
        "observed_at": cur.get("time"),
        "cached_seconds": round(rep.age, 1) if rep.age > 0 else 0,
        "units": rep.forecast.get("current_units", {}),
        "current": {**cur, "condition": label, "severity": sev},
        "climate_normal": ({"high": round(normal[0], 1), "low": round(normal[1], 1),
                            "samples": normal[2]} if normal else None),
        "nowcast": ({"raining_now": nc.raining_now, "starts_in_min": nc.starts_in,
                     "stops_in_min": nc.stops_in, "horizon_min": nc.horizon_min}
                    if nc else None),
        "moon": {"phase": round(phase, 4), "illumination": round(illum, 4),
                 "age_days": round(age_days, 2)},
        "sun": {k: (v.isoformat() if v else None) for k, v in ev.items()},
        "daily": daily,
    }
    if rep.air:
        out["air"] = rep.air.get("current")
    out["alerts"] = [{"event": a.event, "level": a.level, "region": a.region,
                      "source": a.source, "headline": a.detail,
                      "start": a.start.isoformat() if a.start else None,
                      "end": a.end.isoformat() if a.end else None} for a in rep.alerts]
    return out


def render_compare(reports: Sequence[Report], theme: Theme) -> Table:
    """Side-by-side comparison of several cities."""
    t = Table(box=box.SIMPLE_HEAD, header_style=f"bold {theme.label}",
              border_style=theme.rule, pad_edge=False)
    t.add_column("City", style=f"bold {theme.fg}")
    t.add_column("", justify="left", width=1)   # every glyph is exactly one cell
    t.add_column("Now", justify="right")
    t.add_column("Feels", justify="right")
    t.add_column("Today", justify="right")
    t.add_column("Condition")
    t.add_column("Wind", justify="right")
    t.add_column("Hum", justify="right")
    t.add_column("Rain", justify="right")
    for rep in reports:
        cur, daily = rep.current, rep.daily
        label, _, glyph, sev = describe(cur.get("weather_code"), bool(cur.get("is_day", 1)))
        temp, feels = cur.get("temperature_2m"), cur.get("apparent_temperature")
        hi = (daily.get("temperature_2m_max") or [None])[0]
        lo = (daily.get("temperature_2m_min") or [None])[0]
        p = (daily.get("precipitation_probability_max") or [None])[0]
        t.add_row(
            rep.place.name,
            Text(glyph, style=theme.sun if cur.get("weather_code") in (0, 1) else theme.cloud),
            Text(f"{temp:.0f}°" if temp is not None else "—", style=ramp_color(temp, theme)),
            Text(f"{feels:.0f}°" if feels is not None else "—", style=theme.label),
            Text(f"{deg(lo)[:-1]}/{deg(hi)[:-1]}" if hi is not None and lo is not None else "—",
                 style=theme.label),
            Text(label, style=theme.bad if sev >= 3 else theme.label),
            Text(f"{fmt(cur.get('wind_speed_10m'), '.0f')}", style=theme.label),
            Text(f"{fmt(cur.get('relative_humidity_2m'), '.0f')}%", style=theme.label),
            Text(f"{p:.0f}%" if p else "·", style=theme.rain if p and p >= 40 else theme.muted))
    return t


# ───────────────────────────────────────────────────────────────────── config ──

def load_config() -> dict:
    """Optional JSON config; CLI flags always win."""
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text())
            return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        pass
    return {}


def pick_theme(name: str, console: Console) -> Theme:
    if name == "auto":
        if os.getenv("NO_COLOR") is not None or console.color_system is None:
            return THEMES["mono"]
        return THEMES["night"]
    return THEMES.get(name, THEMES["night"])


# ──────────────────────────────────────────────────────────────────────── cli ──

SECTIONS = ("alerts", "nowcast", "air", "astro", "chart", "hourly", "forecast")

COORD_RE = re.compile(r"\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*")
# Flags that consume the token after them, so we never steal their value.
VALUE_FLAGS = {"-d", "--days", "-c", "--compare", "-t", "--theme", "-w", "--watch",
               "--only", "--export", "--width", "--geo", "--key"}


def extract_negative_coords(argv: Sequence[str]) -> tuple[list[str], Optional[str]]:
    """Let '-77.85,166.67' be used as the positional city.

    argparse sees a leading '-' and calls it an unknown flag, which would make
    every southern/western coordinate unusable.
    """
    rest: list[str] = []
    coord: Optional[str] = None
    prev = ""
    for tok in argv:
        if prev in VALUE_FLAGS and tok.startswith("-"):
            # argparse refuses a '-'-prefixed value; "--flag=value" is accepted.
            rest[-1] = f"{rest[-1]}={tok}"
        elif coord is None and tok.startswith("-") and COORD_RE.fullmatch(tok):
            coord = tok.strip()
        else:
            rest.append(tok)
        prev = tok
    return rest, coord


def build_parser(cfg: dict) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wx", description="A weather dashboard for the terminal.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Data from Open-Meteo (no API key needed). "
               f"Config: {CONFIG_PATH}   Cache: {CACHE_DIR}")
    p.add_argument("city", nargs="?", default=cfg.get("city", DEFAULT_CITY),
                   help="city name, or 'lat,lon' (default: %(default)s)")
    p.add_argument("-d", "--days", type=int, default=cfg.get("days", 7),
                   help="forecast days, 1-16 (default: %(default)s)")
    p.add_argument("-i", "--imperial", action="store_true", default=cfg.get("imperial", False),
                   help="use °F, mph and inches")
    p.add_argument("-c", "--compare", metavar="CITIES",
                   help="cities to compare, comma-separated "
                        "(use ';' if any entry is a 'lat,lon' pair)")
    p.add_argument("-t", "--theme", default=cfg.get("theme", "auto"),
                   choices=("auto", *THEMES), help="color theme (default: %(default)s)")
    p.add_argument("-w", "--watch", nargs="?", type=int, const=120, metavar="SEC",
                   help="refresh continuously every SEC seconds (default 120)")
    p.add_argument("-H", "--hourly", nargs="?", type=int, const=12, metavar="N",
                   help="add an hour-by-hour table for the next N hours (default 12)")
    p.add_argument("--oneline", action="store_true", help="one-line output for a prompt")
    p.add_argument("--json", action="store_true", help="machine-readable JSON")
    p.add_argument("--only", metavar="SECTIONS",
                   help=f"comma-separated subset of: {','.join(SECTIONS)}")
    p.add_argument("--no-air", action="store_true", help="skip air quality and pollen")
    p.add_argument("--no-alerts", action="store_true",
                   help="skip official weather warnings (DWD in Germany, NWS in the US)")
    p.add_argument("--no-climate", action="store_true", help="skip climate-normal comparison")
    p.add_argument("--offline", action="store_true", help="use cached data only")
    p.add_argument("--refresh", action="store_true", help="ignore the cache")
    p.add_argument("--clear-cache", action="store_true", help="delete cached responses and exit")
    p.add_argument("--export", metavar="FILE", help="write output to .svg, .html or .txt")
    p.add_argument("--width", type=int, help="force a terminal width")
    p.add_argument("--geo", choices=("auto", "latlng", "open-meteo"),
                   default=cfg.get("geo", "auto"), help="geocoding provider")
    p.add_argument("--key", default=DEFAULT_KEY,
                   help="latlng API key (env: LATLNG_API_KEY, or set it in .env)")
    return p


def resolve_sections(only: Optional[str], no_air: bool,
                     want_hourly: bool = False, no_alerts: bool = False) -> set[str]:
    show = set(SECTIONS) - {"hourly"}      # opt-in: --hourly, or name it in --only
    if only:
        picked = {s.strip() for s in only.split(",") if s.strip()}
        bad = picked - set(SECTIONS)
        if bad:
            raise WxError(f"unknown section(s): {', '.join(sorted(bad))}. "
                          f"Choose from: {', '.join(SECTIONS)}")
        show = picked
    if want_hourly:
        show.add("hourly")
    if no_air:
        show.discard("air")
    if no_alerts:
        show.discard("alerts")
    return show


def main(argv: Optional[Sequence[str]] = None) -> int:
    cfg = load_config()
    raw = list(argv) if argv is not None else sys.argv[1:]
    raw, negative_coord = extract_negative_coords(raw)
    args = build_parser(cfg).parse_args(raw)
    if negative_coord:
        args.city = negative_coord

    if args.clear_cache:
        n = len(list(CACHE_DIR.glob("*.json"))) if CACHE_DIR.exists() else 0
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        print(f"cleared {n} cached response(s) from {CACHE_DIR}")
        return 0

    if args.width is not None and args.width < 20:
        print("wx: --width must be at least 20", file=sys.stderr)
        return 2
    console = Console(width=args.width, record=bool(args.export),
                      force_terminal=True if args.export else None)
    theme = pick_theme(args.theme, console)
    days = max(1, min(16, args.days))
    net = dict(offline=args.offline, refresh=args.refresh)

    try:
        show = resolve_sections(args.only, args.no_air, args.hourly is not None,
                                args.no_alerts)

        # -- compare mode ---------------------------------------------------
        if args.compare is not None:
            # ';' lets a compare list contain 'lat,lon' pairs, which have commas.
            sep = ";" if ";" in args.compare else ","
            names = [c.strip() for c in args.compare.split(sep) if c.strip()]
            if not names:
                raise WxError("--compare needs at least one city")
            places = gather({n: (geocode, dict(query=n, provider=args.geo,
                                               api_key=args.key, **net)) for n in names})
            reports: list[Report] = []
            errors: list[str] = []
            for n in names:
                place = places[n]
                if isinstance(place, Exception):
                    errors.append(f"{n}: {place}")
                    continue
                try:
                    reports.append(build_report(place, 2, args.imperial, want_air=False,
                                                want_climate=False, want_alerts=False, **net))
                except WxError as e:
                    errors.append(f"{n}: {e}")
            if not reports:
                raise WxError("; ".join(errors) or "nothing to compare")
            console.print()
            console.print(render_compare(reports, theme))
            for e in errors:
                console.print(Text(f"  {e}", style=theme.warn))
            console.print()
            return 0

        # -- single city ----------------------------------------------------
        place = geocode(args.city, args.geo, args.key, **net)
        rep = build_report(place, days, args.imperial, want_air="air" in show,
                           want_climate=not args.no_climate,
                           want_alerts="alerts" in show, **net)

        if args.json:
            print(json.dumps(report_to_dict(rep), indent=2, default=str))
            return 0
        if args.oneline:
            console.print(render_oneline(rep, theme))
            return 0

        hours = max(1, min(48, args.hourly)) if args.hourly else 12

        def draw() -> Panel:
            return render_dashboard(rep, theme, console.width, show=show, hours=hours)

        if args.watch:
            from rich.live import Live
            interval = max(15, args.watch)
            try:
                with Live(draw(), console=console, screen=True,
                          refresh_per_second=4, vertical_overflow="crop") as live:
                    while True:
                        time.sleep(interval)
                        try:
                            rep = build_report(place, days, args.imperial,
                                               want_air="air" in show,
                                               want_climate=not args.no_climate,
                                               want_alerts="alerts" in show,
                                               offline=False, refresh=True)
                        except WxError:
                            pass                    # keep showing the last good frame
                        live.update(draw())
            except KeyboardInterrupt:
                pass
            # screen=True implies transient=True, which tears down the alt screen and
            # takes the final frame with it. Reprint so it survives in scrollback.
            console.print()
            console.print(draw())
            console.print()
            return 0

        console.print()
        console.print(draw())
        console.print()

        if args.export:
            path = Path(args.export)
            if path.suffix == ".svg":
                body = console.export_svg(title=f"wx — {place.name}")
            elif path.suffix in (".html", ".htm"):
                body = console.export_html()
            else:
                body = console.export_text()
            try:
                path.write_text(body)
            except OSError as exc:
                raise WxError(f"could not write {path}: {exc.strerror or exc}") from None
            print(f"wrote {path}")
        return 0

    except WxError as exc:
        console.print(Text(f"\n  {exc}\n", style=f"bold {theme.bad}"))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
