import os
import sys
import time
from pathlib import Path

import pytest

# Set required env vars before importing main so module-level reads succeed
os.environ.setdefault("STATION_ID", "TESTID")
os.environ.setdefault("STATION_PASSWORD", "TESTPASS")
os.environ.setdefault("INFLUXDB_TOKEN", "TESTTOKEN")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402 — must follow env var setup
from fastapi.testclient import TestClient

client = TestClient(main.app, raise_server_exceptions=False)

_VALID = {
    "ID": "TESTID",
    "PASSWORD": "TESTPASS",
    "tempf": "72.0",
    "dewptf": "60.0",
    "humidity": "65",
    "indoortempf": "70.0",
    "indoorhumidity": "55",
    "baromin": "29.92",
    "windspeedmph": "5.0",
    "windgustmph": "8.0",
    "winddir": "180",
    "rainin": "0.0",
    "dailyrainin": "0.1",
    "solarradiation": "500.0",
    "UV": "3",
    "dateutc": "2024-01-01 12:00:00",
    "softwaretype": "WS80",
    "action": "updateraw",
    "realtime": "1",
    "rtfreq": "5",
}

_URL = "/weatherstation/updateweatherstation.php"


@pytest.fixture(autouse=True)
def influx_writes(monkeypatch):
    writes = []

    async def fake_write_influx_lines(lines):
        writes.extend(lines)

    monkeypatch.setattr(main, "write_influx_lines", fake_write_influx_lines)
    monkeypatch.setattr(main, "_last_station_snapshot", None)
    monkeypatch.setattr(main, "_forecast_t_c", None)
    monkeypatch.setattr(main, "_forecast_wind_kmh", None)
    monkeypatch.setattr(main, "_external_fields", {})
    monkeypatch.setattr(
        main,
        "_chmi_warning_levels",
        {event: 0 for event in main._CHMI_KNOWN_EVENTS},
    )
    return writes


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_payload_returns_success(influx_writes):
    resp = client.get(_URL, params=_VALID)
    assert resp.status_code == 200
    assert resp.text == "success"
    assert len(influx_writes) == 1 + len(main._chmi_warning_levels)
    assert influx_writes[0].startswith("meteo,source=station,station_id=TESTID ")
    assert "meteo_temperature_celsius=22.22" in influx_writes[0]
    assert "meteo_weather_score=" in influx_writes[0]
    assert "meteo_sensor_health_score=100.0" in influx_writes[0]
    assert "meteo_sensor_anomaly_count=0i" in influx_writes[0]
    assert "meteo_station_last_update_timestamp_seconds=" in influx_writes[0]


def test_valid_payload_includes_fetched_values(influx_writes, monkeypatch):
    monkeypatch.setattr(main, "_forecast_t_c", 20.0)
    monkeypatch.setattr(main, "_forecast_wind_kmh", 12.5)
    monkeypatch.setattr(
        main,
        "_external_fields",
        {
            "meteo_forecast_temperature_celsius": 20.0,
            "meteo_forecast_wind_speed_kmh": 12.5,
            "meteo_aqi_european": 18,
            "meteo_pm25_ugm3": 6.5,
            "meteo_pm10_ugm3": 12.1,
        },
    )

    resp = client.get(_URL, params=_VALID)

    assert resp.text == "success"
    station_line = influx_writes[0]
    assert "meteo_forecast_temperature_celsius=20.0" in station_line
    assert "meteo_forecast_wind_speed_kmh=12.5" in station_line
    assert "meteo_aqi_european=18i" in station_line
    assert "meteo_temperature_deviation_celsius=2.22" in station_line
    assert "meteo_forecast_temperature_error_abs_celsius=2.22" in station_line
    assert "meteo_wind_speed_deviation_kmh=-4.45" in station_line
    assert "meteo_forecast_quality_score=" in station_line


def test_sensor_anomalies_are_flagged(influx_writes, monkeypatch):
    monkeypatch.setattr(main, "LAT", 50.0)
    monkeypatch.setattr(main, "LON", 14.0)
    monkeypatch.setattr(main, "compute_clearsky_ghi", lambda *_: 0.0)

    params = {
        **_VALID,
        "tempf": "60.0",
        "dewptf": "65.0",
        "windspeedmph": "10.0",
        "windgustmph": "5.0",
        "solarradiation": "100.0",
    }
    resp = client.get(_URL, params=params)

    assert resp.text == "success"
    station_line = influx_writes[0]
    assert "meteo_sensor_anomaly_dew_point_above_temperature=1i" in station_line
    assert "meteo_sensor_anomaly_wind_gust_below_speed=1i" in station_line
    assert "meteo_sensor_anomaly_solar_at_night=1i" in station_line
    assert "meteo_sensor_anomaly_count=3i" in station_line
    assert "meteo_sensor_health_score=25.0" in station_line


def test_sensor_gap_and_jump_anomalies_are_flagged(influx_writes, monkeypatch):
    monkeypatch.setattr(
        main,
        "_last_station_snapshot",
        {
            "timestamp_s": 1000.0,
            "temperature_c": 10.0,
            "pressure_hpa": 1010.0,
        },
    )
    monkeypatch.setattr(main.time, "time", lambda: 1100.0)

    resp = client.get(
        _URL,
        params={**_VALID, "tempf": "80.0", "baromin": "29.70"},
    )

    assert resp.text == "success"
    station_line = influx_writes[0]
    assert "meteo_sensor_update_gap_seconds=100.0" in station_line
    assert "meteo_sensor_anomaly_update_gap=0i" in station_line
    assert "meteo_sensor_anomaly_temperature_jump=1i" in station_line
    assert "meteo_sensor_anomaly_pressure_jump=1i" in station_line


def test_external_service_urls_use_current_defaults():
    assert main.OPEN_METEO_FORECAST_URL == "https://api.open-meteo.com/v1/forecast"
    assert main.OPEN_METEO_AIR_QUALITY_URL == (
        "https://air-quality-api.open-meteo.com/v1/air-quality"
    )
    assert main.CHMI_WARNINGS_URL == (
        "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-czechia"
    )
    assert "czech-republic" not in main.CHMI_WARNINGS_URL


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_wrong_password_returns_invalid_id():
    resp = client.get(_URL, params={**_VALID, "PASSWORD": "wrongpass"})
    assert resp.status_code == 200
    assert resp.text == "invalid_id"


def test_wrong_id_returns_invalid_id():
    resp = client.get(_URL, params={**_VALID, "ID": "wrongid"})
    assert resp.status_code == 200
    assert resp.text == "invalid_id"


def test_auth_failure_redacts_password_from_logs(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="main"):
        client.get(_URL, params={**_VALID, "PASSWORD": "supersecretvalue"})
    for record in caplog.records:
        assert "supersecretvalue" not in record.getMessage()


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------


def test_missing_station_id_fails_closed(monkeypatch):
    monkeypatch.setattr(main, "STATION_ID", "")
    with pytest.raises(RuntimeError, match="STATION_ID"):
        main._validate_config()


def test_missing_station_password_fails_closed(monkeypatch):
    monkeypatch.setattr(main, "STATION_PASSWORD", "")
    with pytest.raises(RuntimeError, match="STATION_PASSWORD"):
        main._validate_config()


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


def test_missing_required_field_returns_invalid_data():
    params = {k: v for k, v in _VALID.items() if k != "tempf"}
    resp = client.get(_URL, params=params)
    assert resp.status_code == 200
    assert resp.text == "invalid_data"


def test_nan_tempf_rejected():
    resp = client.get(_URL, params={**_VALID, "tempf": "nan"})
    assert resp.text == "invalid_data"


def test_inf_humidity_rejected():
    resp = client.get(_URL, params={**_VALID, "humidity": "inf"})
    assert resp.text == "invalid_data"


def test_out_of_range_humidity_rejected():
    resp = client.get(_URL, params={**_VALID, "humidity": "150"})
    assert resp.text == "invalid_data"


def test_negative_rain_rejected():
    resp = client.get(_URL, params={**_VALID, "rainin": "-1"})
    assert resp.text == "invalid_data"


def test_invalid_payload_does_not_return_success():
    resp = client.get(_URL, params={**_VALID, "UV": "999"})
    assert resp.text != "success"


def test_invalid_action_rejected():
    resp = client.get(_URL, params={**_VALID, "action": "updateraww"})
    assert resp.text == "invalid_data"


def test_invalid_realtime_rejected():
    resp = client.get(_URL, params={**_VALID, "realtime": "0"})
    assert resp.text == "invalid_data"


def test_invalid_payload_does_not_write_to_influx(influx_writes):
    client.get(_URL, params={**_VALID, "humidity": "200"})
    assert influx_writes == []


# ---------------------------------------------------------------------------
# Log redaction
# ---------------------------------------------------------------------------


def test_format_log_params_redacts_password():
    params = {"ID": "myid", "PASSWORD": "secret", "tempf": "72"}
    result = main.format_log_params(params)
    assert "secret" not in result
    assert "[REDACTED]" in result
    assert "myid" in result


def test_format_log_params_case_insensitive_redaction():
    result = main.format_log_params({"password": "s3cr3t", "Password": "also_secret"})
    assert "s3cr3t" not in result
    assert "also_secret" not in result


def test_format_log_params_escapes_newlines():
    result = main.format_log_params({"key": "val\nue\r\nX"})
    assert "\n" not in result
    assert "\\n" in result


def test_format_log_params_truncates_long_values():
    result = main.format_log_params({"key": "A" * 200})
    assert len(result) < 300
    assert "truncated" in result


def test_unknown_path_redacts_password_from_logs(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="main"):
        client.get("/some/unknown/path", params={"PASSWORD": "secretvalue"})
    for record in caplog.records:
        assert "secretvalue" not in record.getMessage()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_rate_limiting_blocks_after_threshold(monkeypatch):
    # Seed failures just below the limit, then confirm threshold triggers block
    ip = "10.0.0.99"
    now = time.time()
    main._auth_failures[ip] = [now] * (main._RATE_LIMIT_MAX - 1)
    # One more failure should tip it over
    main._record_auth_failure(ip)
    assert main._is_rate_limited(ip)


def test_rate_limiting_expires_old_failures(monkeypatch):
    ip = "10.0.0.88"
    old = time.time() - main._RATE_LIMIT_WINDOW - 1
    main._auth_failures[ip] = [old] * main._RATE_LIMIT_MAX
    assert not main._is_rate_limited(ip)


# ---------------------------------------------------------------------------
# FUP-001: validation errors must not leak PASSWORD
# ---------------------------------------------------------------------------


def test_validation_error_does_not_log_password(caplog):
    import logging
    # Auth succeeds (correct PASSWORD), validation fails on humidity — Pydantic ValidationError
    # must not include the raw PASSWORD value in the logged output.
    params = {**_VALID, "humidity": "999"}
    with caplog.at_level(logging.WARNING, logger="main"):
        resp = client.get(_URL, params=params)
    assert resp.text == "invalid_data"
    for record in caplog.records:
        assert main.STATION_PASSWORD not in record.getMessage()


def test_missing_field_validation_error_does_not_log_password(caplog):
    import logging
    # Auth succeeds, missing tempf triggers ValidationError — PASSWORD must stay out of logs.
    params = {k: v for k, v in _VALID.items() if k != "tempf"}
    with caplog.at_level(logging.WARNING, logger="main"):
        resp = client.get(_URL, params=params)
    assert resp.text == "invalid_data"
    for record in caplog.records:
        assert main.STATION_PASSWORD not in record.getMessage()


# ---------------------------------------------------------------------------
# CHMI Warnings parsing and event mapping
# ---------------------------------------------------------------------------


def test_fetch_chmi_warnings_parsing(monkeypatch):
    import asyncio
    xml_data = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">
  <entry>
    <cap:event>Very Strong Thunderstorms</cap:event>
    <cap:severity>Severe</cap:severity>
  </entry>
  <entry>
    <cap:event>Danger of Fires</cap:event>
    <cap:severity>Moderate</cap:severity>
  </entry>
  <entry>
    <cap:event>Strong Wind Warning</cap:event>
    <cap:severity>Minor</cap:severity>
  </entry>
</feed>
"""
    class MockResponse:
        def __init__(self, text):
            self.text = text
        def raise_for_status(self):
            pass

    async def mock_get(*args, **kwargs):
        return MockResponse(xml_data)

    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    # Reset warning levels before test
    main._chmi_warning_levels.clear()
    main._chmi_warning_levels.update({event: 0 for event in main._CHMI_KNOWN_EVENTS})

    asyncio.run(main.fetch_chmi_warnings())

    assert main._chmi_warning_levels["thunderstorm"] == 3
    assert main._chmi_warning_levels["forest-fire"] == 2
    assert main._chmi_warning_levels["wind"] == 1
    assert main._chmi_warning_levels["fog"] == 0
    assert main._external_fields["meteo_chmi_warning_max_level"] == 3


def test_coordinates_in_station_fields(influx_writes, monkeypatch):
    monkeypatch.setattr(main, "LAT", 48.9893)
    monkeypatch.setattr(main, "LON", 14.5304)

    resp = client.get(_URL, params=_VALID)
    assert resp.status_code == 200
    assert resp.text == "success"

    station_line = influx_writes[0]
    assert "meteo_latitude=48.9893" in station_line
    assert "meteo_longitude=14.5304" in station_line
