"""Tests for the special-data weather skill `data.weather` (design-spec §8).

Offline: the Open-Meteo HTTP seam (`_get_json`) is monkeypatched to return canned
geocoding + forecast payloads, so nothing touches the network.
"""

from __future__ import annotations

import pytest

from app.skills import data
from app.skills.context import SkillContext
from app.skills.data import WeatherParams, data_weather
from app.storage.db import connect
from app.storage.migrations import migrate

GEO = {
    "results": [
        {
            "name": "Sydney",
            "country": "Australia",
            "latitude": -33.87,
            "longitude": 151.21,
            "timezone": "Australia/Sydney",
        }
    ]
}
FORECAST = {
    "timezone": "Australia/Sydney",
    "daily": {
        "time": ["2026-06-16", "2026-06-17"],
        "weather_code": [3, 61],
        "temperature_2m_max": [19.9, 18.0],
        "temperature_2m_min": [11.1, 12.0],
        "precipitation_probability_max": [0, 80],
    },
}


@pytest.fixture
def ctx():
    conn = connect()
    migrate(conn)
    try:
        yield SkillContext(user_id=0, conn=conn, permissions=frozenset({"data.read"}))
    finally:
        conn.close()


def _fake_get(geo=GEO, forecast=FORECAST):
    def _get(url, params, *, timeout):
        return geo if "geocoding" in url else forecast

    return _get


def test_weather_returns_daily_forecast(ctx, monkeypatch) -> None:
    monkeypatch.setattr(data, "_get_json", _fake_get())
    result = data_weather(WeatherParams(location="Sydney", days=2), ctx)

    assert result.ok is True
    assert result.location == "Sydney"
    assert result.country == "Australia"
    assert result.timezone == "Australia/Sydney"
    assert result.source_url == "https://open-meteo.com/"
    assert [d.date for d in result.days] == ["2026-06-16", "2026-06-17"]
    # WMO codes mapped to human labels.
    assert result.days[0].summary == "overcast"  # code 3
    assert result.days[1].summary == "slight rain"  # code 61
    assert result.days[1].precip_prob_pct == 80


def test_weather_unknown_place(ctx, monkeypatch) -> None:
    monkeypatch.setattr(data, "_get_json", _fake_get(geo={"results": []}))
    result = data_weather(WeatherParams(location="Nowheresville"), ctx)
    assert result.ok is False
    assert "could not find" in (result.error or "")
    assert result.days == []
