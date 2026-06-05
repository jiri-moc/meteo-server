"""
FastAPI server implementing the Wunderground-compatible weather station update endpoint.

Receives GET requests from a personal weather station, validates credentials and payload,
computes derived metrics, and writes results to InfluxDB 3 via line protocol.
"""
import asyncio
import logging
import math
import os
import secrets
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

STATION_ID = os.getenv("STATION_ID", "")
STATION_PASSWORD = os.getenv("STATION_PASSWORD", "")
SSL_KEY = os.getenv("SSL_KEY", "/app/key.pem")
SSL_CERT = os.getenv("SSL_CERT", "/app/cert.pem")
ENABLE_DOCS = os.getenv("ENABLE_DOCS", "").lower() in ("1", "true", "yes")
INFLUXDB_URL = os.getenv("INFLUXDB_URL", "http://meteo-db:8181").rstrip("/")
INFLUXDB_DATABASE = os.getenv("INFLUXDB_DATABASE", "meteo")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN", "")
INFLUXDB_TIMEOUT = float(os.getenv("INFLUXDB_TIMEOUT", "15"))
INFLUXDB_DISABLED = os.getenv("INFLUXDB_DISABLED", "").lower() in ("1", "true", "yes")
OPEN_METEO_FORECAST_URL = os.getenv(
    "OPEN_METEO_FORECAST_URL",
    "https://api.open-meteo.com/v1/forecast",
)
OPEN_METEO_AIR_QUALITY_URL = os.getenv(
    "OPEN_METEO_AIR_QUALITY_URL",
    "https://air-quality-api.open-meteo.com/v1/air-quality",
)
CHMI_WARNINGS_URL = os.getenv(
    "CHMI_WARNINGS_URL",
    "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-czechia",
)

_lat_env = os.getenv("LAT")
_lon_env = os.getenv("LON")
LAT: float | None = float(_lat_env) if _lat_env else None
LON: float | None = float(_lon_env) if _lon_env else None

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

_REDACTED_KEYS = frozenset({"password", "pass", "token", "api_key", "authorization"})
_MAX_LOG_VALUE_LEN = 100
_MAX_LOG_PARAMS = 30
_CTRL_TRANS = str.maketrans({"\n": "\\n", "\r": "\\r", "\t": "\\t"})


def format_log_params(params: dict[str, str]) -> str:
    """Format query params for safe logging: redact secrets, escape control chars, cap length."""
    parts = []
    items = list(params.items())
    for i, (k, v) in enumerate(items):
        if i >= _MAX_LOG_PARAMS:
            parts.append(f"...(+{len(items) - _MAX_LOG_PARAMS} more)")
            break
        safe_k = k.translate(_CTRL_TRANS)[:50]
        if k.lower() in _REDACTED_KEYS:
            parts.append(f"{safe_k}=[REDACTED]")
        else:
            safe_v = v.translate(_CTRL_TRANS)
            if len(safe_v) > _MAX_LOG_VALUE_LEN:
                safe_v = safe_v[:_MAX_LOG_VALUE_LEN] + "...(truncated)"
            parts.append(f"{safe_k}={safe_v}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------


def _validate_config() -> None:
    """Raise RuntimeError if required runtime configuration is missing."""
    if not STATION_ID:
        raise RuntimeError("STATION_ID environment variable is required but not set")
    if not STATION_PASSWORD:
        raise RuntimeError("STATION_PASSWORD environment variable is required but not set")
    if not INFLUXDB_DISABLED and not INFLUXDB_TOKEN:
        raise RuntimeError("INFLUXDB_TOKEN environment variable is required but not set")


# ---------------------------------------------------------------------------
# Authentication and rate limiting
# ---------------------------------------------------------------------------

_auth_failures: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT_WINDOW = 60.0
_RATE_LIMIT_MAX = 10


def _is_rate_limited(ip: str) -> bool:
    cutoff = time.time() - _RATE_LIMIT_WINDOW
    _auth_failures[ip] = [t for t in _auth_failures[ip] if t > cutoff]
    return len(_auth_failures[ip]) >= _RATE_LIMIT_MAX


def _record_auth_failure(ip: str) -> None:
    _auth_failures[ip].append(time.time())


def authenticate_station(station_id: str, password: str) -> bool:
    return (
        secrets.compare_digest(station_id, STATION_ID)
        and secrets.compare_digest(password, STATION_PASSWORD)
    )


# ---------------------------------------------------------------------------
# Station payload validation
# ---------------------------------------------------------------------------


class StationPayload(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="ignore")

    dateutc: str
    tempf: float
    dewptf: float
    humidity: float
    indoortempf: float
    indoorhumidity: float
    baromin: float
    windspeedmph: float
    windgustmph: float
    winddir: float
    rainin: float
    dailyrainin: float
    solarradiation: float
    UV: float
    softwaretype: str
    action: Literal["updateraw"]
    realtime: Literal["1"]

    @model_validator(mode="after")
    def validate_ranges(self) -> "StationPayload":
        checks = [
            (0 <= self.humidity <= 100, f"humidity out of range: {self.humidity}"),
            (0 <= self.indoorhumidity <= 100, f"indoorhumidity out of range: {self.indoorhumidity}"),
            (0 <= self.winddir <= 360, f"winddir out of range: {self.winddir}"),
            (0 <= self.UV <= 20, f"UV out of range: {self.UV}"),
            (0 <= self.solarradiation <= 1600, f"solarradiation out of range: {self.solarradiation}"),
            (self.rainin >= 0, f"rainin negative: {self.rainin}"),
            (self.dailyrainin >= 0, f"dailyrainin negative: {self.dailyrainin}"),
            (-100 <= self.tempf <= 150, f"tempf out of range: {self.tempf}"),
            (-100 <= self.dewptf <= 150, f"dewptf out of range: {self.dewptf}"),
            (-100 <= self.indoortempf <= 150, f"indoortempf out of range: {self.indoortempf}"),
            (20 <= self.baromin <= 35, f"baromin out of range: {self.baromin}"),
            (self.windspeedmph >= 0, f"windspeedmph negative: {self.windspeedmph}"),
            (self.windgustmph >= 0, f"windgustmph negative: {self.windgustmph}"),
        ]
        for ok, msg in checks:
            if not ok:
                raise ValueError(msg)
        return self


def parse_station_payload(params: dict[str, str]) -> StationPayload:
    return StationPayload.model_validate(params)


# ---------------------------------------------------------------------------
# Module-level state for values fetched between station updates
# ---------------------------------------------------------------------------

_forecast_t_c: float | None = None
_forecast_wind_kmh: float | None = None
_external_fields: dict[str, float | int] = {}
_last_station_snapshot: dict[str, float] | None = None

# ---------------------------------------------------------------------------
# Unit conversions
# ---------------------------------------------------------------------------


def f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


def mph_to_ms(mph: float) -> float:
    return mph * 0.44704


def mph_to_kmh(mph: float) -> float:
    return mph * 1.60934


def inhg_to_hpa(inhg: float) -> float:
    return inhg * 33.8639


def in_to_mm(inches: float) -> float:
    return inches * 25.4


# ---------------------------------------------------------------------------
# Derived metric computations — thermodynamics
# ---------------------------------------------------------------------------


def _saturation_vapor_pressure_hpa(t_c: float) -> float:
    """Magnus formula — saturation vapor pressure in hPa."""
    return 6.112 * math.exp((17.67 * t_c) / (t_c + 243.5))


def compute_absolute_humidity(t_c: float, rh: float) -> float:
    """Absolute humidity in g/m³ via ideal gas law."""
    e_hpa = _saturation_vapor_pressure_hpa(t_c) * rh / 100
    return (e_hpa * 100 * 18.015) / (8.314 * (t_c + 273.15))


def compute_vapor_pressure_deficit(t_c: float, rh: float) -> float:
    """Vapor pressure deficit in hPa."""
    return _saturation_vapor_pressure_hpa(t_c) * (1 - rh / 100)


def compute_cloud_base(t_c: float, dew_c: float) -> float:
    """Lifting condensation level in metres (125 m / °C rule)."""
    return max(0.0, (t_c - dew_c) * 125)


def compute_heat_index(t_c: float, rh: float) -> float:
    """Rothfusz regression heat index in °C. Valid for t_c >= 27 and rh >= 40."""
    c = [-8.78469475556, 1.61139411, 2.33854883889, -0.14611605,
         -0.012308094, -0.0164248277778, 0.002211732, 0.00072546, -0.000003582]
    return (c[0] + c[1]*t_c + c[2]*rh + c[3]*t_c*rh
            + c[4]*t_c**2 + c[5]*rh**2 + c[6]*t_c**2*rh
            + c[7]*t_c*rh**2 + c[8]*t_c**2*rh**2)


def compute_wind_chill(t_c: float, v_kmh: float) -> float:
    """Environment Canada wind chill in °C. Valid for t_c <= 10 and v_kmh > 4.8."""
    return 13.12 + 0.6215*t_c - 11.37*(v_kmh**0.16) + 0.3965*t_c*(v_kmh**0.16)


def compute_feels_like(t_c: float, rh: float, v_kmh: float) -> float:
    if t_c >= 27 and rh >= 40:
        return compute_heat_index(t_c, rh)
    if t_c <= 10 and v_kmh > 4.8:
        return compute_wind_chill(t_c, v_kmh)
    return t_c


def compute_wet_bulb(t_c: float, rh: float) -> float:
    """Stull (2011) wet-bulb approximation. Valid for T in [-20, 50] °C, RH in [5, 99] %."""
    return (t_c * math.atan(0.151977 * (rh + 8.313659)**0.5)
            + math.atan(t_c + rh)
            - math.atan(rh - 1.676331)
            + 0.00391838 * rh**1.5 * math.atan(0.023101 * rh)
            - 4.686035)


def _score_from_error(error: float, zero_at: float) -> float:
    """Score 0–100; reaches 0 when |error| >= zero_at."""
    return max(0.0, 100.0 * (1.0 - min(abs(error), zero_at) / zero_at))


def compute_forecast_quality_score(
    temp_error_c: float,
    wind_error_kmh: float | None = None,
) -> float:
    """Forecast quality score in 0..100, with temperature weighted above wind."""
    temp_score = _score_from_error(temp_error_c, 6.0)
    if wind_error_kmh is None:
        return temp_score
    wind_score = _score_from_error(wind_error_kmh, 25.0)
    return temp_score * 0.65 + wind_score * 0.35


def compute_weather_score(
    t_c: float,
    rh: float,
    wind_kmh: float,
    rain_mm: float,
    uv_index: float,
    aqi: float | int | None = None,
    warning_level: float | int | None = None,
) -> float:
    """Local outdoor comfort score in 0..100 for quick dashboard scanning."""
    penalty = abs(t_c - 21.0) * 3.5
    penalty += max(0.0, 40.0 - rh) * 0.25
    penalty += max(0.0, rh - 70.0) * 0.35
    penalty += max(0.0, wind_kmh - 12.0) * 0.9
    penalty += min(40.0, rain_mm * 12.0)
    penalty += max(0.0, uv_index - 6.0) * 4.0
    if aqi is not None:
        penalty += max(0.0, float(aqi) - 40.0) * 0.35
    if warning_level is not None:
        penalty += float(warning_level) * 12.0
    return max(0.0, min(100.0, 100.0 - penalty))


def compute_sensor_anomaly_fields(
    *,
    t_c: float,
    dew_c: float,
    pressure_hpa: float,
    wind_kmh: float,
    gust_kmh: float,
    solar_wm2: float,
    timestamp_s: float,
    clearsky_wm2: float | None,
    previous: dict[str, float] | None,
) -> dict[str, int | float]:
    """Flag physically suspicious sensor readings while keeping the point writable."""
    flags: dict[str, int] = {
        "meteo_sensor_anomaly_dew_point_above_temperature": int(dew_c > t_c + 0.5),
        "meteo_sensor_anomaly_wind_gust_below_speed": int(gust_kmh + 0.1 < wind_kmh),
        "meteo_sensor_anomaly_solar_at_night": int(
            clearsky_wm2 is not None and clearsky_wm2 < 1.0 and solar_wm2 > 20.0
        ),
        "meteo_sensor_anomaly_update_gap": 0,
        "meteo_sensor_anomaly_temperature_jump": 0,
        "meteo_sensor_anomaly_pressure_jump": 0,
    }

    gap_s = 0.0
    if previous is not None:
        gap_s = max(0.0, timestamp_s - previous["timestamp_s"])
        flags["meteo_sensor_anomaly_update_gap"] = int(gap_s > 120.0)
        flags["meteo_sensor_anomaly_temperature_jump"] = int(
            abs(t_c - previous["temperature_c"]) > 5.0 and gap_s <= 180.0
        )
        flags["meteo_sensor_anomaly_pressure_jump"] = int(
            abs(pressure_hpa - previous["pressure_hpa"]) > 3.0 and gap_s <= 180.0
        )

    anomaly_count = sum(flags.values())
    health_score = max(0.0, 100.0 - anomaly_count * 25.0)
    return {
        **flags,
        "meteo_sensor_update_gap_seconds": round(gap_s, 1),
        "meteo_sensor_anomaly_count": anomaly_count,
        "meteo_sensor_health_score": round(health_score, 1),
    }


# ---------------------------------------------------------------------------
# Derived metric computations — clear-sky solar model
# ---------------------------------------------------------------------------


def compute_clearsky_ghi(lat: float, lon: float, dt_utc: datetime) -> float:
    """
    Simplified clear-sky global horizontal irradiance (W/m²).

    Uses Cooper solar declination, Spencer (1971) equation of time,
    Kasten-Young (1989) air mass, and simplified Hottel transmittance.
    Typical accuracy ±10 W/m² under clear-sky conditions.
    """
    doy = dt_utc.timetuple().tm_yday
    # Solar declination (Cooper equation)
    decl = math.radians(23.45 * math.sin(math.radians((doy - 81) * 360 / 365)))
    # Equation of time (Spencer 1971) — result in minutes
    B = math.radians((doy - 1) * 360 / 365)
    eot_min = 229.18 * (0.000075 + 0.001868*math.cos(B) - 0.032077*math.sin(B)
                        - 0.014615*math.cos(2*B) - 0.04089*math.sin(2*B))
    # Solar hour angle
    utc_h = dt_utc.hour + dt_utc.minute / 60 + dt_utc.second / 3600
    solar_time = utc_h + lon / 15 + eot_min / 60
    hour_angle = math.radians(15 * (solar_time - 12))
    # Solar elevation angle
    lat_r = math.radians(lat)
    sin_elev = (math.sin(lat_r) * math.sin(decl)
                + math.cos(lat_r) * math.cos(decl) * math.cos(hour_angle))
    if sin_elev <= 0:
        return 0.0
    elev_deg = math.degrees(math.asin(min(1.0, sin_elev)))
    # Air mass (Kasten-Young 1989)
    am = 1 / (sin_elev + 0.50572 * (elev_deg + 6.07995)**-1.6364)
    # Extraterrestrial irradiance (varies ±3.3% over year)
    I_ext = 1361 * (1 + 0.033 * math.cos(math.radians(360 * doy / 365)))
    # Clear-sky GHI via simplified Hottel transmittance
    return I_ext * (0.7 ** (am ** 0.678)) * sin_elev


# ---------------------------------------------------------------------------
# InfluxDB line protocol
# ---------------------------------------------------------------------------


def _escape_key(value: str) -> str:
    return value.replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def _format_field_value(value: float | int | str) -> str:
    """Encode a Python value as an InfluxDB line-protocol field literal (int → ``i`` suffix, float → repr, str → quoted)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}i"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("InfluxDB field values must be finite")
        return repr(value)
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_line_protocol(
    measurement: str,
    fields: dict[str, float | int | str],
    timestamp_ns: int,
    tags: dict[str, str] | None = None,
) -> str:
    """Serialise a measurement to InfluxDB line protocol. Tags and fields are emitted in sorted key order for deterministic output."""
    if not fields:
        raise ValueError("at least one InfluxDB field is required")

    tag_part = ""
    if tags:
        tag_part = "," + ",".join(
            f"{_escape_key(k)}={_escape_key(v)}" for k, v in sorted(tags.items())
        )

    field_part = ",".join(
        f"{_escape_key(k)}={_format_field_value(v)}" for k, v in sorted(fields.items())
    )
    return f"{_escape_key(measurement)}{tag_part} {field_part} {timestamp_ns}"


async def write_influx_lines(lines: list[str]) -> None:
    """POST line-protocol lines to InfluxDB 3 Core via /api/v3/write_lp with nanosecond precision."""
    if INFLUXDB_DISABLED:
        logger.info("influxdb_write_disabled lines=%d", len(lines))
        return

    async with httpx.AsyncClient(timeout=INFLUXDB_TIMEOUT) as client:
        response = await client.post(
            f"{INFLUXDB_URL}/api/v3/write_lp",
            params={"db": INFLUXDB_DATABASE, "precision": "nanosecond"},
            headers={
                "Authorization": f"Bearer {INFLUXDB_TOKEN}",
                "Content-Type": "text/plain; charset=utf-8",
            },
            content="\n".join(lines),
        )
        response.raise_for_status()


def build_station_fields(payload: StationPayload) -> dict[str, float | int]:
    """Convert a validated station payload to InfluxDB fields, applying unit conversions, all derived metrics, and merging external data."""
    global _last_station_snapshot
    t_c = f_to_c(payload.tempf)
    dew_c = f_to_c(payload.dewptf)
    rh = payload.humidity
    v_mph = payload.windspeedmph
    gust_mph = payload.windgustmph
    v_kmh = mph_to_kmh(v_mph)
    gust_kmh = mph_to_kmh(gust_mph)
    solar_wm2 = payload.solarradiation
    pressure_hpa = inhg_to_hpa(payload.baromin)
    rain_mm = in_to_mm(payload.rainin)
    now_s = time.time()
    clearsky: float | None = None
    fields: dict[str, float | int] = {
        "meteo_temperature_celsius": round(t_c, 2),
        "meteo_dew_point_celsius": round(dew_c, 2),
        "meteo_indoor_temperature_celsius": round(f_to_c(payload.indoortempf), 2),
        "meteo_humidity_percent": rh,
        "meteo_indoor_humidity_percent": payload.indoorhumidity,
        "meteo_pressure_hpa": round(pressure_hpa, 2),
        "meteo_wind_speed_ms": round(mph_to_ms(v_mph), 3),
        "meteo_wind_gust_ms": round(mph_to_ms(gust_mph), 3),
        "meteo_wind_speed_kmh": round(v_kmh, 2),
        "meteo_wind_gust_kmh": round(gust_kmh, 2),
        "meteo_wind_direction_degrees": payload.winddir,
        "meteo_rain_mm": round(rain_mm, 2),
        "meteo_rain_daily_mm": round(in_to_mm(payload.dailyrainin), 2),
        "meteo_solar_radiation_wm2": solar_wm2,
        "meteo_uv_index": payload.UV,
        "meteo_feels_like_celsius": round(compute_feels_like(t_c, rh, v_kmh), 2),
        "meteo_wet_bulb_temperature_celsius": round(compute_wet_bulb(t_c, rh), 2),
        "meteo_absolute_humidity_gm3": round(compute_absolute_humidity(t_c, rh), 3),
        "meteo_vapor_pressure_deficit_hpa": round(compute_vapor_pressure_deficit(t_c, rh), 3),
        "meteo_cloud_base_meters": round(compute_cloud_base(t_c, dew_c), 1),
    }

    if LAT is not None and LON is not None:
        fields["meteo_latitude"] = LAT
        fields["meteo_longitude"] = LON
        now_utc = datetime.now(timezone.utc)
        clearsky = compute_clearsky_ghi(LAT, LON, now_utc)
        fields["meteo_solar_radiation_clearsky_wm2"] = round(clearsky, 1)
        if clearsky >= 20:
            fields["meteo_cloud_cover_index"] = round(min(1.0, max(0.0, solar_wm2 / clearsky)), 3)

    if _forecast_t_c is not None:
        temp_error_c = t_c - _forecast_t_c
        wind_error_kmh = (
            v_kmh - _forecast_wind_kmh
            if _forecast_wind_kmh is not None
            else None
        )
        fields["meteo_temperature_deviation_celsius"] = round(temp_error_c, 2)
        fields["meteo_forecast_temperature_error_abs_celsius"] = round(abs(temp_error_c), 2)
        fields["meteo_forecast_temperature_quality_score"] = round(
            _score_from_error(temp_error_c, 6.0),
            1,
        )
        fields["meteo_forecast_quality_score"] = round(
            compute_forecast_quality_score(temp_error_c, wind_error_kmh),
            1,
        )
        if wind_error_kmh is not None:
            fields["meteo_wind_speed_deviation_kmh"] = round(wind_error_kmh, 2)
            fields["meteo_forecast_wind_error_abs_kmh"] = round(abs(wind_error_kmh), 2)
            fields["meteo_forecast_wind_quality_score"] = round(
                _score_from_error(wind_error_kmh, 25.0),
                1,
            )

    fields.update(_external_fields)
    fields["meteo_weather_score"] = round(
        compute_weather_score(
            t_c,
            rh,
            v_kmh,
            rain_mm,
            payload.UV,
            _external_fields.get("meteo_aqi_european"),
            _external_fields.get("meteo_chmi_warning_max_level"),
        ),
        1,
    )
    fields.update(
        compute_sensor_anomaly_fields(
            t_c=t_c,
            dew_c=dew_c,
            pressure_hpa=pressure_hpa,
            wind_kmh=v_kmh,
            gust_kmh=gust_kmh,
            solar_wm2=solar_wm2,
            timestamp_s=now_s,
            clearsky_wm2=clearsky,
            previous=_last_station_snapshot,
        )
    )
    fields["meteo_station_last_update_timestamp_seconds"] = now_s
    _last_station_snapshot = {
        "timestamp_s": now_s,
        "temperature_c": t_c,
        "pressure_hpa": pressure_hpa,
    }
    return fields


# ---------------------------------------------------------------------------
# External data fetchers
# ---------------------------------------------------------------------------

_CHMI_CAP_NS = "urn:oasis:names:tc:emergency:cap:1.2"
_CHMI_ATOM_NS = "http://www.w3.org/2005/Atom"
_CHMI_SEVERITY_MAP = {"Minor": 1, "Moderate": 2, "Severe": 3, "Extreme": 3}
# Pre-seed all known Meteoalarm event types so Grafana sees them even before first warning
_CHMI_KNOWN_EVENTS = [
    "wind", "snow-ice", "thunderstorm", "fog", "high-temperature",
    "low-temperature", "forest-fire", "avalanches", "rain", "flooding", "rain-flood",
]
_chmi_warning_levels: dict[str, int] = {event: 0 for event in _CHMI_KNOWN_EVENTS}


async def fetch_open_meteo() -> None:
    """Fetch current forecast temperature/wind and air quality (PM2.5, PM10, European AQI) from Open-Meteo and update module-level state."""
    global _forecast_t_c, _forecast_wind_kmh
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            OPEN_METEO_FORECAST_URL,
            params={
                "latitude": LAT,
                "longitude": LON,
                "current": "temperature_2m,wind_speed_10m",
                "wind_speed_unit": "kmh",
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        current = resp.json()["current"]
        _forecast_t_c = current["temperature_2m"]
        _forecast_wind_kmh = current["wind_speed_10m"]
        _external_fields["meteo_forecast_temperature_celsius"] = round(_forecast_t_c, 2)
        _external_fields["meteo_forecast_wind_speed_kmh"] = round(_forecast_wind_kmh, 2)

        resp_aq = await client.get(
            OPEN_METEO_AIR_QUALITY_URL,
            params={
                "latitude": LAT,
                "longitude": LON,
                "current": "european_aqi,pm10,pm2_5",
            },
            timeout=15.0,
        )
        resp_aq.raise_for_status()
        aq = resp_aq.json()["current"]
        _external_fields["meteo_aqi_european"] = aq["european_aqi"]
        _external_fields["meteo_pm25_ugm3"] = aq["pm2_5"]
        _external_fields["meteo_pm10_ugm3"] = aq["pm10"]

    logger.info(
        "open_meteo_updated forecast_t=%.1f°C aqi=%d pm2.5=%.1f pm10=%.1f",
        _forecast_t_c, aq["european_aqi"], aq["pm2_5"], aq["pm10"],
    )


def _map_chmi_event(event_text: str) -> str:
    """Map a free-text Meteoalarm event string to a canonical slug; falls back to a slugified form for unknown types."""
    text_lower = event_text.lower()
    if "thunderstorm" in text_lower:
        return "thunderstorm"
    if "wind" in text_lower:
        return "wind"
    if "snow" in text_lower or "ice" in text_lower:
        return "snow-ice"
    if "fog" in text_lower:
        return "fog"
    if "high-temperature" in text_lower or "high temperature" in text_lower or "heat" in text_lower:
        return "high-temperature"
    if "low-temperature" in text_lower or "low temperature" in text_lower or "cold" in text_lower:
        return "low-temperature"
    if "fire" in text_lower or "forest-fire" in text_lower or "forest fire" in text_lower:
        return "forest-fire"
    if "avalanche" in text_lower:
        return "avalanches"
    if "rain" in text_lower and "flood" in text_lower:
        return "rain-flood"
    if "rain" in text_lower:
        return "rain"
    if "flood" in text_lower or "flooding" in text_lower:
        return "flooding"
    return event_text.lower().replace(" ", "-")


async def fetch_chmi_warnings() -> None:
    """Fetch ČHMÚ weather warnings redistributed via Meteoalarm atom feed."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            CHMI_WARNINGS_URL,
            timeout=15.0,
            headers={"User-Agent": "meteo-server/1.0"},
        )
        resp.raise_for_status()

    root = ET.fromstring(resp.text)
    active: dict[str, int] = {}
    for entry in root.iter(f"{{{_CHMI_ATOM_NS}}}entry"):
        infos = list(entry.iter(f"{{{_CHMI_CAP_NS}}}info"))
        if not infos:
            infos = [entry]

        for container in infos:
            event_elem = container.find(f"{{{_CHMI_CAP_NS}}}event")
            severity_elem = container.find(f"{{{_CHMI_CAP_NS}}}severity")
            if event_elem is None or severity_elem is None:
                continue
            if not event_elem.text:
                continue
            ev = _map_chmi_event(event_elem.text)
            lvl = _CHMI_SEVERITY_MAP.get(severity_elem.text, 0)
            active[ev] = max(active.get(ev, 0), lvl)

    _chmi_warning_levels.clear()
    _chmi_warning_levels.update({event: 0 for event in _CHMI_KNOWN_EVENTS})
    _chmi_warning_levels.update(active)
    _external_fields["meteo_chmi_warning_max_level"] = max(_chmi_warning_levels.values(), default=0)

    logger.info("chmi_warnings_updated active=%s", active or "none")


async def _open_meteo_loop() -> None:
    while True:
        try:
            await fetch_open_meteo()
        except Exception as exc:
            logger.warning("open_meteo_fetch_error %s", exc)
        await asyncio.sleep(300)


async def _chmi_loop() -> None:
    while True:
        try:
            await fetch_chmi_warnings()
        except Exception as exc:
            logger.warning("chmi_fetch_error %s", exc)
        await asyncio.sleep(900)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_config()
    tasks = [asyncio.create_task(_chmi_loop())]
    if LAT is not None and LON is not None:
        tasks.append(asyncio.create_task(_open_meteo_loop()))
        logger.info("open_meteo_fetcher_started lat=%.4f lon=%.4f", LAT, LON)
    else:
        logger.info("LAT/LON not configured — Open-Meteo fetcher and clear-sky model disabled")
    yield
    for t in tasks:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass


app = FastAPI(
    lifespan=lifespan,
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url="/redoc" if ENABLE_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - start) * 1000
    params = dict(request.query_params)
    redacted = format_log_params(params) if params else ""
    path = request.url.path
    logger.info(
        'access %s "%s %s%s" %d %.1fms',
        request.client.host if request.client else "unknown",
        request.method,
        path,
        ("?" + redacted) if redacted else "",
        response.status_code,
        ms,
    )
    return response


@app.get("/weatherstation/updateweatherstation.php", response_class=PlainTextResponse)
async def update_weather_station(request: Request) -> str:
    params = dict(request.query_params)
    station_id = params.get("ID", "")
    password = params.get("PASSWORD", "")
    client_ip = request.client.host if request.client else "unknown"

    if _is_rate_limited(client_ip):
        logger.warning("rate_limited ip=%s", client_ip)
        return "invalid_id"

    if not authenticate_station(station_id, password):
        _record_auth_failure(client_ip)
        logger.warning("auth_failure %s", format_log_params(params))
        return "invalid_id"

    logger.info("accepted %s", format_log_params(params))

    try:
        payload = parse_station_payload(params)
    except ValidationError as exc:
        errors = exc.errors(include_input=False)
        logger.warning("validation_error fields=%s", [e["loc"] for e in errors])
        return "invalid_data"
    except ValueError as exc:
        logger.warning("validation_error %s", exc)
        return "invalid_data"

    timestamp_ns = time.time_ns()
    lines = [
        to_line_protocol(
            "meteo",
            build_station_fields(payload),
            timestamp_ns,
            tags={"source": "station", "station_id": station_id},
        )
    ]
    lines.extend(
        to_line_protocol(
            "meteo_chmi_warning",
            {"meteo_chmi_warning_level": level},
            timestamp_ns,
            tags={"event": event},
        )
        for event, level in sorted(_chmi_warning_levels.items())
    )

    try:
        await write_influx_lines(lines)
    except httpx.HTTPError as exc:
        logger.error("influxdb_write_error %s", exc)
        return "server_error"
    except ValueError as exc:
        logger.error("influxdb_line_protocol_error %s", exc)
        return "server_error"

    logger.info("influxdb_write_success lines=%d", len(lines))
    return "success"


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT"], response_class=PlainTextResponse)
async def catch_all(request: Request, path: str) -> str:
    params = dict(request.query_params)
    logger.warning(
        "unknown_path method=%s path=/%s %s",
        request.method, path, format_log_params(params),
    )
    return ""


if __name__ == "__main__":
    try:
        _validate_config()
    except RuntimeError as e:
        logger.error("%s", e)
        sys.exit(1)

    port = int(os.getenv("PORT", "8080"))

    ssl_kwargs = {}
    if os.path.exists(SSL_KEY) and os.path.exists(SSL_CERT):
        ssl_kwargs = {"ssl_keyfile": SSL_KEY, "ssl_certfile": SSL_CERT}
        logger.info("TLS enabled")

    uvicorn.run(app, host="0.0.0.0", port=port, access_log=False, **ssl_kwargs)
