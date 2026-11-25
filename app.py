"""
Indonesia Air Quality + Health-Meter dashboard — Flask backend.

Data source: Open-Meteo Air Quality API (free, no key, CAMS-based forecasts).
  https://air-quality-api.open-meteo.com/v1/air-quality

Endpoints
  GET /                       dashboard UI
  GET /api/dashboard          all cities: current + health meter + forecast
  GET /api/city/<slug>        detail for one city (hourly history + forecast)
  GET /api/health             uptime / data-freshness probe
"""
from __future__ import annotations

import datetime as dt
import os
import threading
import time

import requests
from flask import Flask, jsonify, render_template, request
from zoneinfo import ZoneInfo

from health import CATEGORY_COLORS, assess, health_score, us_aqi
from model import train_and_forecast

API_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
POLLUTANTS = ["pm2_5", "pm10", "nitrogen_dioxide", "sulphur_dioxide", "carbon_monoxide", "ozone"]
CACHE_TTL = 600  # 10 minutes
WIB = ZoneInfo("Asia/Jakarta")  # every city in the dataset is UTC+7

CITIES = [
    {"slug": "jakarta", "name": "Jakarta", "province": "DKI Jakarta", "lat": -6.2088, "lon": 106.8456},
    {"slug": "surabaya", "name": "Surabaya", "province": "Jawa Timur", "lat": -7.2575, "lon": 112.7521},
    {"slug": "bandung", "name": "Bandung", "province": "Jawa Barat", "lat": -6.9175, "lon": 107.6191},
    {"slug": "medan", "name": "Medan", "province": "Sumatera Utara", "lat": 3.5952, "lon": 98.6722},
    {"slug": "semarang", "name": "Semarang", "province": "Jawa Tengah", "lat": -6.9932, "lon": 110.4203},
    {"slug": "yogyakarta", "name": "Yogyakarta", "province": "DI Yogyakarta", "lat": -7.7956, "lon": 110.3695},
    {"slug": "denpasar", "name": "Denpasar", "province": "Bali", "lat": -8.6705, "lon": 115.2126},
    {"slug": "balikpapan", "name": "Balikpapan", "province": "Kalimantan Timur", "lat": -1.2379, "lon": 116.8529},
    {"slug": "makassar", "name": "Makassar", "province": "Sulawesi Selatan", "lat": -5.1477, "lon": 119.4327},
    {"slug": "palangkaraya", "name": "Palangka Raya", "province": "Kalimantan Tengah", "lat": -2.2089, "lon": 113.9166},
    {"slug": "pekanbaru", "name": "Pekanbaru", "province": "Riau", "lat": 0.5071, "lon": 101.4478},
    {"slug": "manado", "name": "Manado", "province": "Sulawesi Utara", "lat": 1.4748, "lon": 124.8421},
]
CITY_BY_SLUG = {c["slug"]: c for c in CITIES}

app = Flask(__name__)
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()
STARTED_AT = time.time()


def fetch_city(city: dict) -> dict:
    """Pull 7 days of hourly history + current reading for one city."""
    params = {
        "latitude": city["lat"],
        "longitude": city["lon"],
        "current": ",".join(POLLUTANTS) + ",us_aqi,uv_index",
        "hourly": ",".join(POLLUTANTS) + ",us_aqi",
        "past_days": 7,
        "forecast_days": 2,
        "timezone": "Asia/Jakarta",
    }
    for attempt in range(3):
        try:
            r = requests.get(API_URL, params=params, timeout=25)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            if attempt == 2:
                raise RuntimeError(f"air quality API failed for {city['name']}: {exc}") from exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("unreachable")


def build_city_payload(city: dict) -> dict:
    raw = fetch_city(city)
    hourly = raw.get("hourly", {})
    times = hourly.get("time", [])
    current = raw.get("current", {})

    pollutants = {k: current.get(k) for k in POLLUTANTS}
    now_wib = dt.datetime.now(WIB)
    stamp = now_wib.strftime("%H:%M")

    assessment = assess(pollutants)

    # "now" index inside the hourly series. The API stamps hours in local time,
    # so anchor on the reading's own timestamp rather than the server clock
    # (the server runs in UTC; guessing from it would shift the horizon by 7h).
    anchor = None
    for cand in (current.get("time"), now_wib.strftime("%Y-%m-%dT%H:00")):
        if cand and cand in times:
            anchor = times.index(cand)
            break
    if anchor is None:
        anchor = max(len(times) - 1, 0)

    hist_hourly = {k: hourly.get(k, []) for k in POLLUTANTS}
    forecast = train_and_forecast(hist_hourly, times, anchor, current.get("us_aqi"))

    # 24h of observed history for the sparkline / chart
    lo = max(0, anchor - 23)
    history = {
        "time": times[lo:anchor + 1],
        "pm2_5": (hourly.get("pm2_5") or [])[lo:anchor + 1],
        "us_aqi": (hourly.get("us_aqi") or [])[lo:anchor + 1],
    }
    # API's own CAMS forecast for comparison
    api_fc = {
        "time": times[anchor + 1:anchor + 25],
        "pm2_5": (hourly.get("pm2_5") or [])[anchor + 1:anchor + 25],
        "us_aqi": (hourly.get("us_aqi") or [])[anchor + 1:anchor + 25],
    }

    fc_times = times[anchor + 1:anchor + 25]
    if forecast.get("ok"):
        forecast["times"] = fc_times
        forecast["categories"] = [health_score(a)["category_label"] for a in forecast["aqi"]]
        forecast["colors"] = [CATEGORY_COLORS[health_score(a)["category"]] for a in forecast["aqi"]]

    return {
        "slug": city["slug"],
        "name": city["name"],
        "province": city["province"],
        "lat": city["lat"],
        "lon": city["lon"],
        "updated_at": stamp,
        "reading_time": current.get("time"),
        "uv_index": current.get("uv_index"),
        "api_aqi": current.get("us_aqi"),
        "pollutants": pollutants,
        "assessment": assessment,
        "history": history,
        "api_forecast": api_fc,
        "forecast": forecast,
        "elevation": raw.get("elevation"),
    }


def get_dashboard(force: bool = False) -> dict:
    with _lock:
        hit = _cache.get("dashboard")
        if hit and not force and time.time() - hit[0] < CACHE_TTL:
            return hit[1]

    payloads, errors = [], []
    for city in CITIES:
        with _lock:
            cached_city = _cache.get(f"city:{city['slug']}")
            if cached_city and not force and time.time() - cached_city[0] < CACHE_TTL:
                payloads.append(cached_city[1])
                continue
        try:
            data = build_city_payload(city)
            with _lock:
                _cache[f"city:{city['slug']}"] = (time.time(), data)
            payloads.append(data)
        except Exception as exc:  # keep the dashboard alive if one city fails
            errors.append({"city": city["name"], "error": str(exc)})
            stale = _cache.get(f"city:{city['slug']}")
            if stale:
                payloads.append({**stale[1], "stale": True})

    payloads.sort(key=lambda c: c["assessment"]["aqi"], reverse=True)
    ranking = [
        {
            "rank": i + 1,
            "slug": c["slug"],
            "name": c["name"],
            "aqi": c["assessment"]["aqi"],
            "score": c["assessment"]["score"],
            "category": c["assessment"]["category"],
            "color": CATEGORY_COLORS[c["assessment"]["category"]],
        }
        for i, c in enumerate(payloads)
    ]

    aqis = [c["assessment"]["aqi"] for c in payloads]
    scores = [c["assessment"]["score"] for c in payloads]
    avg_aqi = round(sum(aqis) / len(aqis)) if aqis else 0
    worst = payloads[0] if payloads else None
    best = min(payloads, key=lambda c: c["assessment"]["aqi"]) if payloads else None
    dirty = [c for c in payloads if c["assessment"]["category"] not in ("good", "moderate")]

    summary = {
        "city_count": len(payloads),
        "avg_aqi": avg_aqi,
        "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
        "worst": {"name": worst["name"], "slug": worst["slug"], "aqi": worst["assessment"]["aqi"]} if worst else None,
        "best": {"name": best["name"], "slug": best["slug"], "aqi": best["assessment"]["aqi"]} if best else None,
        "cities_over_threshold": len(dirty),
        "threshold_label": "not Good/Moderate",
        "national_category": health_score(avg_aqi)["category_label"],
        "national_color": CATEGORY_COLORS[health_score(avg_aqi)["category"]],
        "national_score": health_score(avg_aqi)["score"],
    }

    result = {
        "generated_at": dt.datetime.now(WIB).strftime("%d %b %Y %H:%M WIB"),
        "generated_iso": dt.datetime.now(WIB).isoformat(timespec="seconds"),
        "source": "Open-Meteo Air Quality API (CAMS Europe/global ensemble)",
        "cities": payloads,
        "ranking": ranking,
        "summary": summary,
        "errors": errors,
    }
    with _lock:
        _cache["dashboard"] = (time.time(), result)
    return result


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/dashboard")
def api_dashboard():
    force = request.args.get("refresh") in ("1", "true", "yes")
    return jsonify(get_dashboard(force=force))


@app.route("/api/city/<slug>")
def api_city(slug):
    city = CITY_BY_SLUG.get(slug)
    if not city:
        return jsonify({"error": "unknown city"}), 404
    data = get_dashboard()
    for c in data["cities"]:
        if c["slug"] == slug:
            return jsonify(c)
    return jsonify({"error": "city unavailable"}), 503


@app.route("/api/health")
def api_health():
    with _lock:
        cached = "dashboard" in _cache
        age = round(time.time() - _cache["dashboard"][0], 1) if cached else None
    return jsonify({"status": "ok", "uptime_s": round(time.time() - STARTED_AT, 1),
                    "cache_warm": cached, "cache_age_s": age, "cache_ttl_s": CACHE_TTL})


@app.route("/api/methodology")
def api_methodology():
    return jsonify({
        "data_source": API_URL,
        "pollutants": POLLUTANTS,
        "aqi_standard": "US EPA AQI breakpoints (as reported by Open-Meteo)",
        "health_meter": "0-100 inverse-linear rescale of AQI within each EPA category band",
        "forecast_model": "Direct multi-horizon ridge regression, 24 models (h=1..24), "
                          "features: PM2.5 lags 1/2/3/6/12/24h, 6h & 24h rolling means, "
                          "NO2/O3/CO/SO2/PM10 lags, sin/cos hour-of-day, linear trend. "
                          "80% interval from residual sigma (1.28x).",
        "refresh": "client polls every 5 min; server cache 10 min",
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    get_dashboard()  # warm cache so first paint is instant
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
