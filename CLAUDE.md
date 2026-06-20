# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A minimal FastAPI server that receives weather data from a personal weather station. It implements the Wunderground-compatible GET endpoint (`/weatherstation/updateweatherstation.php`), authenticates the station by ID and password, computes derived weather values, and writes accepted readings to InfluxDB 3.

## Running locally

```bash
pip install -r requirements.txt
STATION_ID=myid STATION_PASSWORD=mypass INFLUXDB_TOKEN=mytoken python main.py
```

The server listens on port 8080 by default. Override with `PORT` env var. `LOG_LEVEL` defaults to `INFO`.
Outside Docker it serves plain HTTP unless the files referenced by `SSL_KEY` and `SSL_CERT` exist.

Optional env vars: `LAT`/`LON` (enables Open-Meteo fetcher and clear-sky solar model), `INFLUXDB_URL` (default `http://meteo-db:8181`), `INFLUXDB_DATABASE` (default `meteo`), `INFLUXDB_DISABLED=1` (skips InfluxDB writes), `ENABLE_DOCS=1` (enables `/docs`), `SSL_KEY`/`SSL_CERT` (enables TLS if both files exist).

## Testing

```bash
pip install -r requirements-test.txt

# Unit tests (mocked InfluxDB)
pytest tests/test_main.py

# Single test
pytest tests/test_main.py::test_valid_payload_returns_success

# End-to-end tests (requires Docker; spins up InfluxDB 3 Core via testcontainers)
RUN_E2E=1 pytest tests/test_e2e_influx.py

# E2E against an already-running InfluxDB (skips Docker)
RUN_E2E=1 INFLUXDB_E2E_URL=http://localhost:8181 pytest tests/test_e2e_influx.py
```

Unit tests use `monkeypatch` to replace `write_influx_lines` with a list collector. The `autouse` fixture in `test_main.py` also resets all module-level state (`_last_station_snapshot`, `_forecast_t_c`, `_forecast_wind_kmh`, `_external_fields`, `_chmi_warning_levels`) between tests. E2E tests are skipped by default and spin up a real InfluxDB 3 container.

## Building and running with Docker

```bash
docker build -t meteo-server .
docker run -e STATION_ID=myid -e STATION_PASSWORD=mypass -e INFLUXDB_TOKEN=mytoken -p 8080:8080 meteo-server
```

The Docker image generates a self-signed TLS certificate at `/app/key.pem` and `/app/cert.pem`, so the container serves HTTPS on port `8080` by default. Set `SSL_KEY`/`SSL_CERT` to non-existent paths to force plain HTTP.

## Deployment

`docker-compose.yaml` in the repository root defines both services. Docker Compose reads variables from `.env` in the same directory; use `.env.example` as the starting point.

Both services start with `docker compose up -d`. After first startup, create the InfluxDB database:

```bash
curl -sf -X POST http://localhost:8181/api/v3/configure/database \
  -H "Authorization: Bearer $INFLUXDB_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"db": "meteo"}'
```

For CI/CD, set `METEO_SERVER_IMAGE` to the full image name pushed by the build step and pass other variables as secrets. The app maps host port `9996` to container port `8080`; InfluxDB maps host port `8181` to container port `8181`.

## Architecture

Everything is in `main.py`.

Request pipeline:

The single GET endpoint reads query params, checks the per-IP rate limit (10 failures/60s), verifies credentials with `secrets.compare_digest`, validates a Pydantic `StationPayload`, derives metrics, and writes to InfluxDB.

Two InfluxDB measurements are written per station update:

- `meteo` (tagged `source=station,station_id=…`): all converted and derived station fields plus any external data collected since the last fetch
- `meteo_chmi_warning` (tagged `event=<type>`): one line per known Meteoalarm event type, pre-seeded to 0 so Grafana always has series

All field names in the `meteo` measurement use the `meteo_` prefix by convention.

Module-level mutable state:

- `_external_fields` / `_forecast_t_c` / `_forecast_wind_kmh`: values fetched from Open-Meteo, merged into the next station write
- `_chmi_warning_levels`: current severity level per ČHMÚ event type, also merged into the next station write
- `_last_station_snapshot`: the previous accepted station reading (`timestamp_s`, `temperature_c`, `pressure_hpa`), used by `compute_sensor_anomaly_fields` to detect inter-update anomalies
- `_auth_failures`: per-IP auth failure timestamps for rate limiting

External data fetchers run as background asyncio loops started in the lifespan context:

- `_open_meteo_loop`: fetches forecast temperature/wind and AQ (PM2.5, PM10, European AQI) every 5 minutes; requires `LAT`/`LON`
- `_chmi_loop`: fetches ČHMÚ weather warnings via Meteoalarm atom feed every 15 minutes

Fetched values accumulate in module-level `_external_fields` and `_forecast_t_c`; they are merged into the `meteo` measurement on the next station-triggered write (not written independently). If `LAT`/`LON` are not set, the Open-Meteo fetcher and clear-sky solar model are both disabled.

Derived metrics computed in `build_station_fields`:

- Unit conversions: °F to °C, mph to m/s and km/h, inHg to hPa, in to mm
- Thermodynamics: feels-like (heat index ≥27°C/40% RH, wind chill ≤10°C/>4.8 km/h, otherwise raw), wet-bulb temperature (Stull 2011), absolute humidity, vapor pressure deficit, cloud base (LCL)
- Solar: clear-sky GHI (Cooper/Spencer/Kasten-Young/Hottel), cloud cover index (measured/clear-sky ratio, only when clear-sky ≥20 W/m²)
- Forecast comparison: temperature and wind deviation from Open-Meteo current values; `meteo_forecast_quality_score` (0–100, temperature 65%, wind 35%)
- Sensor anomaly flags (`compute_sensor_anomaly_fields`): dew point above temperature, wind gust below speed, solar at night (when clear-sky <1 W/m² but station reports >20 W/m²), update gap >120 s, temperature jump >5°C within 180 s, pressure jump >3 hPa within 180 s; also `meteo_sensor_anomaly_count`, `meteo_sensor_health_score` (100 − 25×count), `meteo_sensor_update_gap_seconds`
- `meteo_weather_score` (0–100 outdoor comfort index) combining temperature, humidity, wind, rain, UV, AQI, and ČHMÚ warning level
