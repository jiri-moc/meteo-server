import os
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

if os.getenv("RUN_E2E") != "1":
    pytest.skip("set RUN_E2E=1 to run Docker-backed end-to-end tests", allow_module_level=True)

from testcontainers.core.container import DockerContainer

_ADMIN_TOKEN = "apiv3_e2e-test"

os.environ.setdefault("STATION_ID", "TESTID")
os.environ.setdefault("STATION_PASSWORD", "TESTPASS")
os.environ.setdefault("INFLUXDB_TOKEN", "TESTTOKEN")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402

_URL = "/weatherstation/updateweatherstation.php"
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
    "dateutc": "now",
    "softwaretype": "e2e",
    "action": "updateraw",
    "realtime": "1",
}


@pytest.fixture(scope="session")
def influxdb():
    e2e_url = os.getenv("INFLUXDB_E2E_URL")
    if e2e_url:
        _wait_for_influxdb(e2e_url, _ADMIN_TOKEN, None)
        _create_database(e2e_url, _ADMIN_TOKEN)
        yield e2e_url, _ADMIN_TOKEN
        return

    container = (
        DockerContainer("influxdb:3.9.2-core")
        .with_exposed_ports(8181)
        .with_env("INFLUXDB3_OBJECT_STORE", "memory")
        .with_env("INFLUXDB3_NODE_IDENTIFIER_PREFIX", "meteo-e2e")
        .with_env("INFLUXDB3_START_WITHOUT_AUTH", "true")
    )
    with container:
        _ensure_container_running(container)
        base_url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8181)}"
        _wait_for_influxdb(base_url, _ADMIN_TOKEN, container)
        _create_database(base_url, _ADMIN_TOKEN)
        yield base_url, _ADMIN_TOKEN


def _ensure_container_running(container: DockerContainer) -> None:
    container.get_wrapped_container().reload()
    status = container.get_wrapped_container().status
    if status != "running":
        logs = _container_logs(container)
        raise RuntimeError(f"InfluxDB container is not running; status={status}, logs={logs}")


def _wait_for_influxdb(base_url: str, token: str, container: DockerContainer | None) -> None:
    deadline = time.time() + 60
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.post(
                f"{base_url}/api/v3/query_sql",
                headers={"Authorization": f"Bearer {token}"},
                json={"db": "_internal", "q": "SELECT 1", "format": "jsonl"},
                timeout=5,
            )
            if response.status_code < 500:
                return
        except Exception as exc:
            last_error = exc
        time.sleep(1)
    if container:
        container.get_wrapped_container().reload()
        status = container.get_wrapped_container().status
        logs = _container_logs(container)
    else:
        status = "n/a (gitlab service)"
        logs = ""
    raise TimeoutError(
        f"InfluxDB did not become ready at {base_url}: {last_error}; "
        f"container status={status}, logs={logs}"
    )


def _container_logs(container: DockerContainer) -> str:
    raw_logs = container.get_wrapped_container().logs(tail=200)
    return raw_logs.decode("utf-8", errors="replace")


def _create_database(base_url: str, token: str) -> None:
    response = httpx.post(
        f"{base_url}/api/v3/configure/database",
        headers={"Authorization": f"Bearer {token}"},
        json={"db": "meteo"},
        timeout=10,
    )
    assert response.status_code in (200, 201, 409), response.text


def _query(base_url: str, token: str, sql: str) -> dict:
    response = httpx.post(
        f"{base_url}/api/v3/query_sql",
        headers={"Authorization": f"Bearer {token}"},
        json={"db": "meteo", "q": sql, "format": "jsonl"},
        timeout=10,
    )
    response.raise_for_status()
    lines = [line for line in response.text.splitlines() if line.strip()]
    assert len(lines) == 1, response.text
    return response.json()


def test_station_update_writes_station_and_external_values_to_influxdb(influxdb, monkeypatch):
    base_url, token = influxdb
    monkeypatch.setattr(main, "INFLUXDB_URL", base_url)
    monkeypatch.setattr(main, "INFLUXDB_TOKEN", token)
    monkeypatch.setattr(main, "INFLUXDB_DATABASE", "meteo")
    monkeypatch.setattr(main, "LAT", 50.0755)
    monkeypatch.setattr(main, "LON", 14.4378)
    monkeypatch.setattr(main, "_forecast_t_c", 20.0)
    monkeypatch.setattr(main, "_forecast_wind_kmh", 12.5)
    monkeypatch.setattr(main, "_last_station_snapshot", None)
    # Fix clearsky to a daytime value so meteo_cloud_cover_index is always written
    # regardless of when CI runs; without this the column is absent at night → 500 from InfluxDB.
    monkeypatch.setattr(main, "compute_clearsky_ghi", lambda lat, lon, dt: 600.0)
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

    client = TestClient(main.app, raise_server_exceptions=False)
    for i in range(5):
        response = client.get(
            _URL,
            params={
                **_VALID,
                "tempf": f"{70 + i}.5",
                "dewptf": f"{56 + i}.0",
                "humidity": str(60 + i),
                "windspeedmph": f"{3 + i}.2",
                "windgustmph": f"{6 + i}.5",
                "winddir": str(120 + i * 10),
                "solarradiation": f"{500 + i * 20}.0",
            },
        )
        assert response.status_code == 200
        assert response.text == "success"

    station = _query(
        base_url,
        token,
        """
        SELECT
          count(*) AS rows,
          min(meteo_temperature_celsius) AS min_temp,
          max(meteo_temperature_celsius) AS max_temp,
          max(meteo_forecast_temperature_celsius) AS forecast_temp,
          max(meteo_aqi_european) AS aqi,
          max(meteo_cloud_cover_index) AS cloud_cover,
          max(meteo_temperature_deviation_celsius) AS deviation,
          min(meteo_forecast_quality_score) AS forecast_quality,
          min(meteo_weather_score) AS weather_score,
          max(meteo_sensor_anomaly_count) AS anomaly_count,
          min(meteo_sensor_health_score) AS sensor_health
        FROM meteo
        """,
    )
    assert station["rows"] == 5
    assert station["min_temp"] == 21.39
    assert station["max_temp"] == 23.61
    assert station["forecast_temp"] == 20.0
    assert station["aqi"] == 18
    assert station["cloud_cover"] is not None
    assert station["deviation"] == 3.61
    assert station["forecast_quality"] is not None
    assert station["weather_score"] is not None
    assert station["anomaly_count"] == 0
    assert station["sensor_health"] == 100.0

    warnings = _query(
        base_url,
        token,
        """
        SELECT
          count(*) AS rows,
          count(DISTINCT event) AS events,
          max(meteo_chmi_warning_level) AS max_level
        FROM meteo_chmi_warning
        """,
    )
    assert warnings["rows"] == 5 * len(main._chmi_warning_levels)
    assert warnings["events"] == len(main._chmi_warning_levels)
    assert warnings["max_level"] == 0

    assert client.get(_URL, params={**_VALID, "PASSWORD": "wrong"}).text == "invalid_id"
    assert client.get(_URL, params={**_VALID, "humidity": "999"}).text == "invalid_data"
    after_invalid = _query(base_url, token, "SELECT count(*) AS rows FROM meteo")
    assert after_invalid["rows"] == 5
