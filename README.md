# wx

A terminal weather report built with rich and
the free [Open-Meteo](https://open-meteo.com) API. Current conditions in ASCII art
and block digits, a 24-hour temperature graph, and a multi-day forecast — all in
one panel that reflows down to 80 columns.

## Setup

Needs Python 3.10+ (developed on 3.13).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
python main.py                      # defaults to Chemnitz, 7 days
python main.py tokyo                # any city name
python main.py "san francisco" -d 3 # three-day forecast
python main.py reykjavik -i         # °F, mph, inches
```

| Flag | Description |
| --- | --- |
| `city` | City to look up (default: `Arecife`) |
| `-d`, `--days` | Forecast days, 1–16 (default: `7`) |
| `-i`, `--imperial` | Use °F, mph and inches instead of °C, km/h and mm |
| `--geo` | Geocoding provider: `auto` (default), `latlng`, `open-meteo` |
| `--key` | latlng API key — better set via `LATLNG_API_KEY` |

## What's on screen

- **Hero** — weather art (10 conditions, separate day and night variants) beside the
  temperature in block digits, colored on a nine-stop cold-to-hot gradient.
- **Metrics** — humidity, wind speed with compass direction, gusts, cloud cover,
  pressure, precipitation, UV index and coordinates.
- **Daylight bar** — sunrise to sunset with a marker for the current time; reads
  *before sunrise* / *after sunset* when you're outside the window.
- **24-hour graph** — sub-cell block chart where each column is tinted by its own
  temperature, over a strip of precipitation probability.
- **Forecast** — daily min/max drawn as range bars on one shared scale, so the days
  compare at a glance.

Everything sizes itself to the terminal: below ~84 columns the metric strip folds to
two columns and the graph switches to one cell per hour.

## Geocoding

City lookup goes through Open-Meteo's geocoding API first, because it ranks results
by population — `reykjavik` should mean Iceland, not the village in Manitoba that
`latlng` returns. If Open-Meteo has no match, it falls back to `latlng`. Force one or
the other with `--geo latlng` / `--geo open-meteo`.

The weather data itself is Open-Meteo, which needs no key.

## API key

`--key` defaults to a hard-coded latlng key, so the app runs out of the box. That key
is in plain text in `main.py` and in git history — rotate it and pass yours through
the environment instead:

```bash
export LATLNG_API_KEY=your_key_here
```

You only need a key at all if you use `--geo latlng`, or if Open-Meteo fails to
resolve a name.

Yes, its made with AI.
