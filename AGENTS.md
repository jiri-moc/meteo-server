# Repository Guidelines

## Project structure and module organization

This repository contains a small FastAPI weather-station ingestion service. The application code is currently centralized in `main.py`, including configuration, request validation, derived metric calculation, background fetchers, and InfluxDB writes. Tests live in `tests/`: `test_main.py` covers unit and endpoint behavior, while `test_e2e_influx.py` exercises InfluxDB integration. Deployment assets are kept beside the app: `Dockerfile`, `.gitlab-ci.yml`, `playbook.yml`, `inventory/homelab.yml`, the Compose file in `templates/compose.yaml`, and its env templates in `templates/.env.j2` and `templates/.env.example`. JSON Schemas and generated metric documentation belong in `docs/`.

## Build, test, and development commands

Create a local environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-test.txt
```

Run the service locally:

```bash
STATION_ID=myid STATION_PASSWORD=mypass INFLUXDB_TOKEN=mytoken python main.py
```

Run the main test suite:

```bash
pytest -q tests/test_main.py
```

Run the optional InfluxDB end-to-end test:

```bash
RUN_E2E=1 pytest -q tests/test_e2e_influx.py
```

Build and run the container:

```bash
docker build -t meteo-server .
docker run -e STATION_ID=myid -e STATION_PASSWORD=mypass -e INFLUXDB_TOKEN=mytoken -p 8080:8080 meteo-server
```

## Coding style and naming conventions

Use Python 3.14-compatible syntax, 4-space indentation, explicit type hints where they clarify behavior, and small helper functions for validation or conversion logic. Keep environment variable names uppercase and module constants uppercase, as in `INFLUXDB_URL` and `OPEN_METEO_FORECAST_URL`. Tests should use descriptive `test_*` function names and pytest fixtures for monkeypatching external writes.

## Testing guidelines

Add or update tests for every behavior change that affects station payload handling, authentication, logging redaction, derived metrics, or InfluxDB line protocol. Unit tests should avoid real network and database writes by monkeypatching collaborators. E2E tests are gated with `RUN_E2E=1`; do not make them required for quick local validation unless the change specifically touches InfluxDB integration.

## Commit and pull request guidelines

Recent history uses conventional commit messages such as `feat: Expose Prometheus metrics endpoint`, `fix(metrics): Pass app object to uvicorn`, and `docs: Add JSON Schema for weather station payload`. Follow that pattern with concise imperative summaries and optional scopes. Pull requests should describe the behavior change, list the test commands run, mention deployment/configuration impacts, and link related issues when applicable.

## Security and configuration tips

Never commit real station credentials, InfluxDB tokens, SSH keys, generated TLS private keys, or filled deployment `.env` files. Keep secrets in environment variables, GitLab CI variables, GitHub Actions secrets, or the target host's private `.env`; `templates/.env.example` must stay dummy-only. Preserve password/token redaction in logs, and treat query parameters as untrusted input even when they come from the weather station.

Deployment Compose settings should stay environment-driven through `templates/.env.j2` and `templates/.env.example`. Put portable deploy values such as app image, published ports, InfluxDB image/version, database name, data directory, UID/GID, and node prefix in `.env`; keep stable container-internal paths in `templates/compose.yaml`.
