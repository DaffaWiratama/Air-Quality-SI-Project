# Nafas — Indonesia Air Quality & Health Meter

A small dashboard that shows **live air quality in 12 Indonesian cities** and turns it
into a **health meter (0–100)** plus a **24-hour forecast** of that meter.

Built on the free [Open-Meteo Air Quality API](https://open-meteo.com/en/docs/air-quality-api)
(CAMS-based), so **no API key is needed**.

![dashboard](docs/preview.png)

---

## Run it

```bash
cd aqi-indonesia
pip install -r requirements.txt
python app.py            # http://localhost:8000
```

The server warms its cache on boot (~1 API call per city), so the first page paint is instant.

## What's on the dashboard

| Section | What it shows |
|---|---|
| **KPI strip** | National average AQI, average health score, cleanest / most polluted city, count of cities above "Good" |
| **Air quality map** | Real Indonesia coastline (simplified GeoJSON → SVG path), one marker per city, coloured + sized by current AQI. Click any marker to inspect it |
| **City ranking** | Cities sorted worst→cleanest with AQI chip, health score bar, and 24h forecast trend |
| **City detail** | Health-score gauge, US AQI, main pollutant driver, exposure limit, plain-language guidance for the general public *and* sensitive groups, plus a per-pollutant breakdown with each gas's own AQI sub-index |
| **Forecast & history** | 24h observed + 24h predicted curve for US AQI / PM2.5 / health score, with an 80% interval band and an AQI colour-band background; below it, a 16-hour forecast strip |

Cities: Jakarta, Surabaya, Bandung, Medan, Semarang, Yogyakarta, Denpasar,
Balikpapan, Makassar, Palangka Raya, Pekanbaru, Manado.

---

## How the health meter is computed

1. **Sub-indices** — each pollutant gets a US EPA AQI sub-index. Open-Meteo returns
   µg/m³ for everything, so gases are converted to ppb/ppm first
   (`ppb = µg/m³ × 24.45 / MW`).
2. **Overall AQI** = the worst sub-index; that pollutant is reported as the driver.
3. **Health score** = AQI rescaled to 0–100 *inverse-linearly within its EPA band*
   (Good → 88–100, Moderate → 68–87, Unhealthy for sensitive groups → 48–67,
   Unhealthy → 28–47, Very unhealthy → 8–27, Hazardous → 0–7).
4. **Guidance** comes from the band: a headline, a general-population line,
   a sensitive-group line, and 3 concrete actions.
5. **Exposure limit** for sensitive groups is derived from the WHO 24h PM2.5
   guideline, scaled by `(15 / PM2.5)^0.85` and capped at 8 hours.

## How the forecast works

`model.py` fits **24 separate ridge regressions**, one per lead time *h = 1…24 h*
(direct multi-horizon — no recursive error compounding), refit on every data
refresh against the 7 days of hourly history returned by the API.

Features: PM2.5 lags (1, 2, 3, 6, 12, 24 h), 6 h and 24 h rolling means, one-hour
lags of PM10/NO₂/O₃/CO/SO₂, sin/cos hour-of-day, and a trend term.

Then four post-processing guards, which matter a lot in practice:

- blend with the 7-day **diurnal climatology**, with the weight ramping in by lead time
  (hour 1 stays continuous with the current reading)
- 3-point smoothing to remove single-hour spikes
- a **physical floor/ceiling** anchored on the last 24 h of observation (no
  cleaner-than-possible values, no absurd spikes)
- a small anchor-bias correction that decays to zero by hour 12

Model diagnostics (MAE on the 6 h horizon, in-sample R²) are reported per city in
`/api/dashboard` — typical values here are **R² 0.28–0.73** and **MAE 2–20 µg/m³**,
worse in cities with flat, low-pollution series and better where PM2.5 has a
strong daily cycle.

---

## API endpoints

| Endpoint | Purpose |
|---|---|
| `GET /` | Dashboard UI |
| `GET /api/dashboard[?refresh=1]` | All cities: current reading, health assessment, history, forecast, ranking, summary |
| `GET /api/city/<slug>` | One city's full payload |
| `GET /api/methodology` | Machine-readable description of the scoring + model |
| `GET /api/health` | Uptime and cache-freshness probe |

Data is cached server-side for **10 minutes**; the browser re-polls every 5 minutes.
A failure for one city does not break the page — it is reported in `errors[]` and the
last good cached payload is served if available.

## Files

```
app.py                        Flask app, caching, Open-Meteo client, API routes
health.py                     AQI sub-indices, health score, guidance text, colours
model.py                      24-horizon ridge forecaster + post-processing
templates/index.html          Dashboard UI (vanilla JS, no CDN/build step)
static/data/indonesia_path.json  Simplified Indonesia outline + projection params
```

## Notes & limitations

- Pollutant values are **modelled** (CAMS reanalysis/forecast at ~11 km), not from
  ground sensors at each address — treat them as city-scale, not street-level.
- The AQI shown is the **US EPA** scale (as reported by Open-Meteo), not Indonesia's
  ISPU index; the bands are similar in spirit but the numbers differ.
- The 24 h model is deliberately lightweight. Verified against its own history, it
  captures the daily cycle well but will miss abrupt events (rain washout, fires,
  traffic changes).
- The health meter is an informational aid, **not medical advice**.
