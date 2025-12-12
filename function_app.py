# function_app.py — Azure Functions Python v2 (single file)
# HTTP API + Timer pre-warm, API key auth, per-minute rate limit,
# Azure Table Storage cache, retry + circuit breaker, Open-Meteo provider.

import json
import os
import datetime
import hashlib
import requests
import pybreaker
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# ✅ add this so we can use AuthLevel enum (prevents 404 routing confusion)
import azure.functions as func

from azure.functions import FunctionApp, HttpRequest, HttpResponse, TimerRequest
from azure.data.tables import TableServiceClient, UpdateMode

# -------------------------
# App / Config
# -------------------------
app = FunctionApp()

INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "dev-1234")
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "600"))  # seconds
TABLE_CONN = os.getenv("AzureWebJobsStorage")  # "UseDevelopmentStorage=true" for Azurite
TABLE_NAME_CACHE = "WeatherCache"
TABLE_NAME_RATELIMIT = "RateLimit"
PROVIDER_BASE = os.getenv("WEATHER_PROVIDER_BASE", "https://api.open-meteo.com/v1/forecast")
FUNC_PORT = os.getenv("FUNC_PORT", "7071")  # for local pre-warm calls

# -------------------------
# Storage helpers (lazy init)
# -------------------------
_table_service = None

def table_service() -> TableServiceClient:
    global _table_service
    if _table_service is None:
        _table_service = TableServiceClient.from_connection_string(TABLE_CONN)
        for name in (TABLE_NAME_CACHE, TABLE_NAME_RATELIMIT):
            try:
                _table_service.create_table_if_not_exists(name)
            except Exception:
                pass
    return _table_service

def get_table(name: str):
    return table_service().get_table_client(name)

# -------------------------
# Auth + Rate limit
# -------------------------
def check_api_key(req: HttpRequest) -> bool:
    supplied = req.headers.get("x-api-key") or req.params.get("key")
    return bool(supplied) and supplied == INTERNAL_API_KEY

def rate_limit(key: str, limit: int = 30):
    """Simple per-key per-minute counter in Table Storage."""
    tb = get_table(TABLE_NAME_RATELIMIT)
    window = datetime.datetime.utcnow().replace(second=0, microsecond=0)
    row_key = window.strftime("%Y%m%d%H%M")
    try:
        ent = tb.get_entity(partition_key=key, row_key=row_key)
        count = int(ent.get("count", 0)) + 1
        ent["count"] = count
        tb.update_entity(mode=UpdateMode.REPLACE, entity=ent)
    except Exception:
        tb.upsert_entity(
            mode=UpdateMode.MERGE,
            entity={"PartitionKey": key, "RowKey": row_key, "count": 1}
        )
        count = 1
    return (count <= limit, count)

# -------------------------
# Resilience (retry + circuit breaker)
# -------------------------
breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=30)

@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, max=3),
    retry=retry_if_exception_type((requests.RequestException,))
)
@breaker
def fetch_provider(lat: float, lon: float) -> dict:
    """
    Use Open-Meteo current + hourly. If 'current' is absent, build a snapshot from hourly.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": "auto",
        "current": ",".join([
            "temperature_2m",
            "relative_humidity_2m",
            "apparent_temperature",
            "precipitation",
            "wind_speed_10m",
            "wind_direction_10m",
        ]),
        "hourly": ",".join([
            "temperature_2m",
            "relative_humidity_2m",
            "apparent_temperature",
            "precipitation",
            "wind_speed_10m",
            "wind_direction_10m",
        ]),
        "forecast_days": 1,
    }
    r = requests.get(PROVIDER_BASE, params=params, timeout=8)
    r.raise_for_status()
    return r.json()

def build_current_from_hourly(data: dict) -> dict:
    """Safely build a 'current' snapshot from hourly arrays."""
    hourly = data.get("hourly", {})
    times = hourly.get("time") or []
    if not times:
        return {}
    idx = -1  # latest sample
    def pick(key):
        arr = hourly.get(key)
        return arr[idx] if isinstance(arr, list) and arr else None
    return {
        "time": times[idx],
        "temperature_2m": pick("temperature_2m"),
        "relative_humidity_2m": pick("relative_humidity_2m"),
        "apparent_temperature": pick("apparent_temperature"),
        "precipitation": pick("precipitation"),
        "wind_speed_10m": pick("wind_speed_10m"),
        "wind_direction_10m": pick("wind_direction_10m"),
    }

# -------------------------
# Demo city → lat/lon map
# -------------------------
CITY_LATLON = {
    "jaipur": (26.9124, 75.7873),
    "mumbai": (19.0760, 72.8777),
    "delhi": (28.6139, 77.2090),
    "ahmedabad": (23.0225, 72.5714),
}

# -------------------------
# Cache helpers (Table Storage)
# -------------------------
def cache_key(city: str) -> str:
    return hashlib.sha1(city.lower().encode()).hexdigest()

def get_cached(city: str):
    tb = get_table(TABLE_NAME_CACHE)
    pk = cache_key(city)
    try:
        ent = tb.get_entity(partition_key=pk, row_key="latest")
    except Exception:
        return None
    ts = ent.get("timestampUtc")
    if not ts:
        return None
    age = (datetime.datetime.utcnow() - datetime.datetime.fromisoformat(ts)).total_seconds()
    if age <= CACHE_TTL:
        return json.loads(ent["payloadJson"])
    return None

def set_cache(city: str, payload: dict):
    tb = get_table(TABLE_NAME_CACHE)
    pk = cache_key(city)
    tb.upsert_entity({
        "PartitionKey": pk,
        "RowKey": "latest",
        "city": city,
        "payloadJson": json.dumps(payload),
        "timestampUtc": datetime.datetime.utcnow().isoformat()
    })

def clear_cache(city: str):
    """Optional helper to clear a city's cached row."""
    tb = get_table(TABLE_NAME_CACHE)
    pk = cache_key(city)
    try:
        tb.delete_entity(partition_key=pk, row_key="latest")
    except Exception:
        pass

# -------------------------
# HTTP API
# -------------------------
@app.route(route="WeatherFunction", auth_level=func.AuthLevel.ANONYMOUS)
def weather(req: HttpRequest) -> HttpResponse:
    # 1) auth
    if not check_api_key(req):
        return HttpResponse(
            json.dumps({"error": "Unauthorized. Provide x-api-key header."}),
            status_code=401, mimetype="application/json"
        )

    city = (req.params.get("city") or "").strip()
    refresh = (req.params.get("refresh") == "1")
    wipe = (req.params.get("clear") == "1")

    if not city:
        return HttpResponse(
            json.dumps({"error": "city is required"}),
            status_code=400, mimetype="application/json"
        )

    if wipe:
        clear_cache(city)

    # 2) rate limit (per INTERNAL_API_KEY for demo)
    ok, _ = rate_limit(INTERNAL_API_KEY)
    if not ok:
        return HttpResponse(
            json.dumps({"error": "rate_limit_exceeded", "limit_per_minute": 30}),
            status_code=429, mimetype="application/json"
        )

    # 3) cache
    if not refresh:
        cached = get_cached(city)
        if cached:
            return HttpResponse(
                json.dumps({"source": "cache", "city": city, "data": cached}),
                mimetype="application/json"
            )

    # 4) geocode (demo map)
    coords = CITY_LATLON.get(city.lower())
    if not coords:
        return HttpResponse(
            json.dumps({"error": "city_not_supported_in_demo"}),
            status_code=400, mimetype="application/json"
        )

    # 5) provider call with resilience, plus hourly → current fallback
    try:
        lat, lon = coords
        raw = fetch_provider(lat, lon)
        current = raw.get("current") or raw.get("current_weather") or build_current_from_hourly(raw)
        payload = {"lat": lat, "lon": lon, "current_weather": current}
        set_cache(city, payload)
        return HttpResponse(
            json.dumps({"source": "provider", "city": city, "data": payload}),
            mimetype="application/json"
        )
    except Exception as e:
        return HttpResponse(
            json.dumps({"error": "provider_failed", "details": str(e)}),
            status_code=502, mimetype="application/json"
        )

# -------------------------
# Timer trigger (pre-warm)
# -------------------------
@app.timer_trigger(schedule="0 */15 * * * *", arg_name="mytimer")  # every 15 minutes
def warm_cache(mytimer: TimerRequest):
    base = f"http://localhost:{FUNC_PORT}/api/WeatherFunction"
    top_cities = ["Jaipur", "Mumbai", "Delhi", "Ahmedabad"]
    key = INTERNAL_API_KEY
    for c in top_cities:
        try:
            requests.get(base, params={"city": c}, headers={"x-api-key": key}, timeout=5)
        except Exception:
            pass

# -------------------------
# Health Check Endpoint (for warmup + probes)
# -------------------------
@app.route(route="health", auth_level=func.AuthLevel.ANONYMOUS)
def health(req: HttpRequest) -> HttpResponse:
    return HttpResponse(
        json.dumps({
            "status": "ok",
            "time": str(datetime.datetime.utcnow()),
            "service": "weather-api"
        }),
        status_code=200,
        mimetype="application/json"
    )
