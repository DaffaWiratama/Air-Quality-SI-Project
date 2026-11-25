"""
Health-meter logic for the Indonesia Air Quality dashboard.

Two jobs:
1. Convert pollutant concentrations -> US EPA AQI sub-indices and a 0-100
   "AirHealth score" (100 = pristine air, 0 = hazardous).
2. Turn the score into plain-language health guidance per population group.

Unit notes: Open-Meteo returns everything in ug/m3, so gases are converted to
ppb/ppm before applying the EPA breakpoint tables.
"""
from __future__ import annotations

# --- molar-volume conversions at 25 C, 1 atm -------------------------------
# ppb = ug/m3 * 24.45 / molecular_weight
_GAS_PPB_FACTOR = {
    "ozone": 24.45 / 48.00,
    "nitrogen_dioxide": 24.45 / 46.0055,
    "sulphur_dioxide": 24.45 / 64.066,
}
_CO_UGM3_TO_PPM = 24.45 / 28.010 / 1000.0

# --- EPA breakpoints: (conc_low, conc_high, aqi_low, aqi_high) --------------
_BP = {
    "pm2_5": [
        (0.0, 12.0, 0, 50), (12.1, 35.4, 51, 100), (35.5, 55.4, 101, 150),
        (55.5, 150.4, 151, 200), (150.5, 250.4, 201, 300), (250.5, 500.4, 301, 500),
    ],
    "pm10": [
        (0, 54, 0, 50), (55, 154, 51, 100), (155, 254, 101, 150),
        (255, 354, 151, 200), (355, 424, 201, 300), (425, 604, 301, 500),
    ],
    "ozone_ppb": [
        (0, 54, 0, 50), (55, 70, 51, 100), (71, 85, 101, 150),
        (86, 105, 151, 200), (106, 200, 201, 300),
    ],
    "nitrogen_dioxide_ppb": [
        (0, 53, 0, 50), (54, 100, 51, 100), (101, 360, 101, 150),
        (361, 649, 151, 200), (650, 1249, 201, 300), (1250, 2049, 301, 500),
    ],
    "sulphur_dioxide_ppb": [
        (0, 35, 0, 50), (36, 75, 51, 100), (76, 185, 101, 150),
        (186, 304, 151, 200), (305, 604, 201, 300), (605, 1004, 301, 500),
    ],
    "carbon_monoxide_ppm": [
        (0.0, 4.4, 0, 50), (4.5, 9.4, 51, 100), (9.5, 12.4, 101, 150),
        (12.5, 15.4, 151, 200), (15.5, 30.4, 201, 300), (30.5, 50.4, 301, 500),
    ],
}

# Which pollutant is allowed to be the "responsible" driver, and its label.
_POLLUTANT_LABELS = {
    "pm2_5": "PM2.5",
    "pm10": "PM10",
    "ozone": "Ozone (O₃)",
    "nitrogen_dioxide": "Nitrogen dioxide (NO₂)",
    "sulphur_dioxide": "Sulphur dioxide (SO₂)",
    "carbon_monoxide": "Carbon monoxide (CO)",
}


def _piecewise(conc: float, table) -> float:
    """Linear interpolation inside the EPA breakpoint table."""
    if conc is None:
        return 0.0
    conc = max(0.0, float(conc))
    for c_lo, c_hi, a_lo, a_hi in table:
        if conc <= c_hi:
            span = c_hi - c_lo if c_hi != c_lo else 1.0
            return a_lo + (a_hi - a_lo) * (conc - c_lo) / span
    return 500.0


def sub_indices(pollutants: dict) -> dict:
    """US AQI sub-index for each pollutant present in the reading."""
    si = {}
    if pollutants.get("pm2_5") is not None:
        si["pm2_5"] = round(_piecewise(pollutants["pm2_5"], _BP["pm2_5"]), 1)
    if pollutants.get("pm10") is not None:
        si["pm10"] = round(_piecewise(pollutants["pm10"], _BP["pm10"]), 1)
    if pollutants.get("ozone") is not None:
        si["ozone"] = round(
            _piecewise(pollutants["ozone"] * _GAS_PPB_FACTOR["ozone"], _BP["ozone_ppb"]), 1
        )
    if pollutants.get("nitrogen_dioxide") is not None:
        si["nitrogen_dioxide"] = round(
            _piecewise(pollutants["nitrogen_dioxide"] * _GAS_PPB_FACTOR["nitrogen_dioxide"],
                       _BP["nitrogen_dioxide_ppb"]), 1
        )
    if pollutants.get("sulphur_dioxide") is not None:
        si["sulphur_dioxide"] = round(
            _piecewise(pollutants["sulphur_dioxide"] * _GAS_PPB_FACTOR["sulphur_dioxide"],
                       _BP["sulphur_dioxide_ppb"]), 1
        )
    if pollutants.get("carbon_monoxide") is not None:
        si["carbon_monoxide"] = round(
            _piecewise(pollutants["carbon_monoxide"] * _CO_UGM3_TO_PPM,
                       _BP["carbon_monoxide_ppm"]), 1
        )
    return si


def us_aqi(pollutants: dict) -> tuple[float, str]:
    """Overall AQI = worst sub-index; returns (aqi, responsible pollutant)."""
    si = sub_indices(pollutants)
    if not si:
        return 0.0, "pm2_5"
    driver = max(si, key=si.get)
    return float(si[driver]), driver


# --- category bands --------------------------------------------------------
# (upper AQI bound, key, label, health-score low, health-score high)
_BANDS = [
    (50, "good", "Good", 88, 100),
    (100, "moderate", "Moderate", 68, 87),
    (150, "unhealthy_sensitive", "Unhealthy for sensitive groups", 48, 67),
    (200, "unhealthy", "Unhealthy", 28, 47),
    (300, "very_unhealthy", "Very unhealthy", 8, 27),
    (500, "hazardous", "Hazardous", 0, 7),
]

GUIDANCE = {
    "good": {
        "headline": "Air is clean — good day to be outside.",
        "general": "Air quality poses little or no risk. Enjoy normal outdoor activity.",
        "sensitive": "No precautions needed for children, the elderly, or people with asthma.",
        "actions": ["Open windows to ventilate", "Outdoor exercise is fine", "Masks not needed"],
    },
    "moderate": {
        "headline": "Acceptable air, but a sensitive few may notice it.",
        "general": "Unusually sensitive people should consider reducing prolonged or heavy exertion outdoors.",
        "sensitive": "Asthma or heart-condition sufferers: keep reliever inhaler handy on long walks.",
        "actions": ["Sensitive groups: shorten intense outdoor exercise",
                    "Ventilate in the early morning when air is cleanest",
                    "Watch for cough or eye irritation"],
    },
    "unhealthy_sensitive": {
        "headline": "Sensitive groups should cut back on outdoor exertion.",
        "general": "Children, the elderly and people with heart or lung disease should limit prolonged outdoor exertion.",
        "sensitive": "Move exercise indoors or to a low-traffic park; keep windows closed during rush hour.",
        "actions": ["Use an N95/KN95 mask if you must be outdoors for long",
                    "Skip outdoor cardio, prefer indoor workouts",
                    "Run a HEPA purifier if you have one"],
    },
    "unhealthy": {
        "headline": "Everyone may feel effects — reduce outdoor time.",
        "general": "Everyone should limit prolonged outdoor exertion; sensitive groups should avoid it.",
        "sensitive": "Stay indoors with filtered air; keep medication accessible and avoid rush-hour streets.",
        "actions": ["Wear a fitted N95 outdoors",
                    "Close windows and doors, seal gaps",
                    "Avoid burning waste or mosquito coils indoors"],
    },
    "very_unhealthy": {
        "headline": "Health alert — avoid outdoor activity.",
        "general": "Everyone should avoid prolonged or heavy outdoor exertion.",
        "sensitive": "Remain indoors in a filtered room; seek medical advice if you feel breathless.",
        "actions": ["Stay indoors, ideally in one filtered room",
                    "Run air purifier on high",
                    "N95 mandatory for any outdoor trip"],
    },
    "hazardous": {
        "headline": "Emergency conditions — stay inside.",
        "general": "Everyone should avoid all outdoor physical activity.",
        "sensitive": "High risk of serious health effects. Follow local emergency guidance.",
        "actions": ["Do not go outdoors unless essential",
                    "Seal windows, run purifier continuously",
                    "Seek medical help for chest pain or severe breathlessness"],
    },
}


def health_score(aqi: float) -> dict:
    """Map AQI to a 0-100 AirHealth score (100 = cleanest)."""
    aqi = max(0.0, min(500.0, float(aqi)))
    lower = 0.0
    for upper, key, label, h_lo, h_hi in _BANDS:
        if aqi <= upper:
            span = upper - lower if upper != lower else 1.0
            score = h_hi - (h_hi - h_lo) * (aqi - lower) / span
            return {
                "score": round(score, 1),
                "category": key,
                "category_label": label,
                "band": (lower, upper),
            }
        lower = upper
    return {"score": 0.0, "category": "hazardous", "category_label": "Hazardous", "band": (300, 500)}


def assess(pollutants: dict) -> dict:
    """Full health assessment for one current reading."""
    si = sub_indices(pollutants)
    aqi, driver = us_aqi(pollutants)
    hs = health_score(aqi)
    guide = GUIDANCE[hs["category"]]

    # exposure minutes before a sensitive adult reaches a "noticeable
    # irritation" threshold - rough but useful, scaled from WHO 24h guidance.
    pm = pollutants.get("pm2_5") or 0.0
    if pm <= 15:
        safe_minutes = 480
    else:
        safe_minutes = int(max(0, 480 * (15.0 / pm) ** 0.85))

    return {
        "aqi": round(aqi),
        "responsible_pollutant": driver,
        "responsible_label": _POLLUTANT_LABELS.get(driver, driver),
        "sub_indices": si,
        **hs,
        "guidance": guide,
        "sensitive_safe_minutes": min(safe_minutes, 480),
    }


def category_for_aqi(aqi: float) -> str:
    return health_score(aqi)["category_label"]


# --- colour palette shared with the frontend ------------------------------
CATEGORY_COLORS = {
    "good": "#22c55e",
    "moderate": "#eab308",
    "unhealthy_sensitive": "#f97316",
    "unhealthy": "#ef4444",
    "very_unhealthy": "#a855f7",
    "hazardous": "#7f1d1d",
}
