"""Special-data skills — authoritative structured data sources (design-spec §8).

A purpose-built data source beats web-scraping for **known data types**: it's
free, reliable, and CAPTCHA-free. ``data.weather`` is the first; currency/FX and
metals layer in behind the same pattern.

* ``data.weather`` (read) — weather forecast by place name via **Open-Meteo**
  (free, **no API key**): geocode the place, then fetch a daily forecast.

The HTTP call goes through a module-level seam (``_get_json``) so tests run fully
offline by monkeypatching it.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from app.skills.context import SkillContext
from app.skills.registry import skill

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_SOURCE_URL = "https://open-meteo.com/"
DEFAULT_TIMEOUT = 15.0

# WMO weather-interpretation codes → short human label (Open-Meteo `weather_code`).
_WMO_CODES: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snow",
    73: "moderate snow",
    75: "heavy snow",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


class WeatherParams(BaseModel):
    location: str = Field(
        ..., min_length=1, description="Place name, e.g. 'Sydney' or 'Paris, France'."
    )
    days: int = Field(7, ge=1, le=16, description="Number of forecast days (from today).")


class DayForecast(BaseModel):
    date: str
    summary: str
    temp_min_c: float | None = None
    temp_max_c: float | None = None
    precip_prob_pct: int | None = None


class WeatherResult(BaseModel):
    ok: bool
    location: str | None = None
    country: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    timezone: str | None = None
    days: list[DayForecast] = Field(default_factory=list)
    source_url: str = _SOURCE_URL
    error: str | None = None


def _get_json(url: str, params: dict, *, timeout: float) -> dict:
    """Seam: a single GET returning JSON (tests monkeypatch this)."""
    resp = httpx.get(url, params=params, timeout=timeout, headers={"User-Agent": "ai-research"})
    resp.raise_for_status()
    return resp.json()


def _at(seq: object, index: int) -> object | None:
    return seq[index] if isinstance(seq, list) and index < len(seq) else None


@skill(
    name="data.weather",
    description=(
        "Get the weather forecast for a place by name from a free, authoritative "
        "source. Use for any weather question, e.g. 'weather this weekend in Sydney'."
    ),
    params=WeatherParams,
    returns=WeatherResult,
    permissions=["data.read"],
    effect="read",
)
def data_weather(params: WeatherParams, ctx: SkillContext) -> WeatherResult:
    # 1) Resolve the place name to coordinates (Open-Meteo geocoding).
    try:
        geo = _get_json(
            _GEOCODE_URL,
            {"name": params.location, "count": 1, "language": "en", "format": "json"},
            timeout=DEFAULT_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return WeatherResult(ok=False, error=f"geocoding failed: {exc}")
    matches = geo.get("results") or []
    if not matches:
        return WeatherResult(ok=False, error=f"could not find a place named {params.location!r}")
    place = matches[0]
    lat, lon = place.get("latitude"), place.get("longitude")

    # 2) Fetch the daily forecast for those coordinates.
    try:
        forecast = _get_json(
            _FORECAST_URL,
            {
                "latitude": lat,
                "longitude": lon,
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max",
                "timezone": "auto",
                "forecast_days": params.days,
            },
            timeout=DEFAULT_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return WeatherResult(ok=False, error=f"forecast failed: {exc}")

    daily = forecast.get("daily") or {}
    days: list[DayForecast] = []
    for i, date in enumerate(daily.get("time") or []):
        code = _at(daily.get("weather_code"), i)
        days.append(
            DayForecast(
                date=str(date),
                summary=_WMO_CODES.get(int(code), "unknown") if code is not None else "unknown",
                temp_min_c=_at(daily.get("temperature_2m_min"), i),  # type: ignore[arg-type]
                temp_max_c=_at(daily.get("temperature_2m_max"), i),  # type: ignore[arg-type]
                precip_prob_pct=_at(daily.get("precipitation_probability_max"), i),  # type: ignore[arg-type]
            )
        )
    return WeatherResult(
        ok=True,
        location=place.get("name"),
        country=place.get("country"),
        latitude=lat,
        longitude=lon,
        timezone=forecast.get("timezone"),
        days=days,
    )
