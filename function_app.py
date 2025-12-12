# function_app.py — Azure Functions Python v2 (single file)
# HTTP API + Timer pre-warm + API key auth + caching + circuit breaker + Open-Meteo provider.

import json
import os
import datetime
import hashlib
import requests
import pybreaker
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import azure.functions as func
from azure.functions import FunctionApp, HttpRequest, HttpResponse, TimerRequest
from azure.data.tables import TableServiceClient, UpdateMode

# -------------------------------------------------
# App / Config
# -------------------------------------------------
app = FunctionApp()

INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "dev-1234")
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "600"))
TABLE_CONN = os.getenv("AzureWebJobsStorage")
TABLE_NAME_CACHE = "WeatherCache"
TABLE_NAME_RATELIMIT = "RateLimit"
PROVIDER_BASE = "https://api.open-meteo.com/v1/forecast"
FUNC_PORT = os.getenv("FUNC_PORT", "7071")

# -------------------------------------------------
# Storage (lazy init)
# -------------------------------------------------
_table_service = None

def table_service() -> TableServiceClient:
    global _table_service
    if _table_service is None:
        _table_service = TableServiceClient.from_connection_string(TABLE_CONN)
        for name in (TABLE_NAME_CACHE, TABLE_NAME_RATELIMIT):
            try:
                _table_service.create_table_if_not_exists(name)
            except:
                pass
    return _table_service

def get_table(name: str):
    return table_service().get_table_client(name)

# -------------------------------------------------
# Auth + Rate Limit
# -------------------------------------------------
def check_api_key(req: HttpRequest) -> bool:
    supplied = req.headers.get("x-api-key") or req.params.get("key")
    return bool(supplied) and supplied == INTERNAL_API_KEY


def rate_limit(key: str, limit: int = 30):
    tb = get_table(TABLE_NAME_RATELIMIT)
    window = datetime.datetime.utcnow().replace(second=0, microsecond=0)
    row_key = window.strftime("%Y%m%d%H%M")

    try:
        ent = tb.get_entity(partition_key=key, row_key=row_key)
        count = int(ent.get("count", 0)) + 1
        ent["count"] = count
        tb.update_entity(entity=ent, mode=UpdateMode.REPLACE)
    except Exception:
        tb.upsert_entity(
            entity={"PartitionKey": key, "RowKey": row_key, "count": 1},
            mode=UpdateMode.MERGE
        )
        count = 1

    return (count <= limit, count)

# -------------------------------------------------
# Provider (Open-Meteo) with retry + circuit breaker
# -------------------------------------------------
breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=30)

@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, max=3),
    retry=retry_if_exception_type((requests.RequestException,))
)
@breaker
def fetch_provider(lat: float, lon: float) -> dict:
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

    r = requests.get(PROVIDER_BASE, params=params, timeout=10)
    r.raise_for_status()
    return r.json()

# -------------------------------------------------
# Build fallback "current" from hourly
# -------------------------------------------------
def build_current_from_hourly(data: dict) -> dict:
    hourly = data.get("hourly", {})
    times = hourly.get("time") or []

    if not times:
        return {}

    idx = -1  # latest

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

# -------------------------------------------------
# Cities
# -------------------------------------------------
CITY_LATLON = {
    "jaipur": (26.9124, 75.7873),
    "mumbai": (19.0760, 72.8777),
    "delhi": (28.6139, 77.2090),
    "ahmedabad": (23.0225, 72.5714),
}

# -------------------------------------------------
# Cache Helpers
# -------------------------------------------------
def cache_key(city: str) -> str:
    return hashlib.sha1(city.lower().encode()).hexdigest()

def get_cached(city: str):
    tb = get_table(TABLE_NAME_CACHE)
    pk = cache_key(city)

    try:
        ent = tb.get_entity(partition_key=pk, row_key="latest")
    except:
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
        "payloadJson": json.dumps(payload),
        "timestampUtc": datetime.datetime.utcnow().isoformat()
    })

def clear_cache(city: str):
    tb = get_table(TABLE_NAME_CACHE)
    pk = cache_key(city)
    try:
        tb.delete_entity(partition_key=pk, row_key="latest")
    except:
        pass

# -------------------------------------------------
# HTTP API
# -------------------------------------------------
@app.route(route="WeatherFunction", auth_level=func.AuthLevel.ANONYMOUS)
def weather(req: HttpRequest) -> HttpResponse:

    # API KEY check
    if not check_api_key(req):
        return HttpResponse(
            json.dumps({"error": "Unauthorized – missing x-api-key"}),
            status_code=401
        )

    city = (req.params.get("city") or "").strip().lower()
    refresh = (req.params.get("refresh") == "1")
    wipe = (req.params.get("clear") == "1")

    if not city:
        return HttpResponse(json.dumps({"error": "city is required"}), status_code=400)

    if wipe:
        clear_cache(city)

    ok, _ = rate_limit(INTERNAL_API_KEY)
    if not ok:
        return HttpResponse(
            json.dumps({"error": "rate_limit_exceeded"}),
            status_code=429
        )

    # Cache hit
    if not refresh:
        cached = get_cached(city)
        if cached:
            return HttpResponse(
                json.dumps({"source": "cache", "city": city, "data": cached}),
                mimetype="application/json"
            )

    # Validate supported city
    coords = CITY_LATLON.get(city)
    if not coords:
        return HttpResponse(
            json.dumps({"error": "city_not_supported"}),
            status_code=400
        )

    # Fetch provider with fallback
    try:
        lat, lon = coords
        raw = fetch_provider(lat, lon)

        # FIXED: real current → if missing → fallback to hourly
        current = raw.get("current") or raw.get("current_weather")
        if not current or "temperature_2m" not in current:
            current = build_current_from_hourly(raw)

        payload = {
            "lat": lat,
            "lon": lon,
            "current_weather": current
        }

        set_cache(city, payload)

        return HttpResponse(
            json.dumps({"source": "provider", "city": city, "data": payload}),
            mimetype="application/json"
        )

    except Exception as e:
        cached = get_cached(city)
        if cached:
            return HttpResponse(
                json.dumps({"source": "cache-fallback", "city": city, "data": cached}),
                mimetype="application/json"
            )

        return HttpResponse(
            json.dumps({"error": "provider_failed", "details": str(e)}),
            status_code=502
        )

# -------------------------------------------------
# Timer Trigger (prewarm)
# -------------------------------------------------
@app.timer_trigger(schedule="0 */15 * * * *", arg_name="mytimer")
def warm_cache(mytimer: TimerRequest):
    base = f"http://localhost:{FUNC_PORT}/api/WeatherFunction"
    key = INTERNAL_API_KEY

    for city in ["Jaipur", "Mumbai", "Delhi", "Ahmedabad"]:
        try:
            requests.get(base, params={"city": city}, headers={"x-api-key": key}, timeout=5)
        except:
            pass

# -------------------------------------------------
# Health
# -------------------------------------------------
@app.route(route="health", auth_level=func.AuthLevel.ANONYMOUS)
def health(req: HttpRequest) -> HttpResponse:
    return HttpResponse(
        json.dumps({
            "status": "ok",
            "time": str(datetime.datetime.utcnow())
        }),
        mimetype="application/json"
    )
