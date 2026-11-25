"""
24-hour PM2.5 forecaster for each city.

Design: one direct ridge-regression model per horizon h = 1..24, trained on the
hourly history returned by the Air Quality API (past 7 days). Features are
lagged PM2.5, multi-pollutant context, short rolling means and time-of-day
cycles. Direct multi-horizon avoids recursive error compounding.

The model is deliberately small and dependency-light (numpy only) so it can be
refit on every data refresh - that keeps the dashboard honest as new API data
arrives instead of shipping a frozen, stale model.
"""
from __future__ import annotations

import numpy as np

from health import us_aqi, health_score

LAGS = (1, 2, 3, 6, 12, 24)
AUX = ("pm10", "nitrogen_dioxide", "ozone", "carbon_monoxide", "sulphur_dioxide")
ROLL = (6, 24)
HORIZONS = range(1, 25)
RIDGE_ALPHA = 6.0


def _clean(xs):
    """NaN/inf -> interpolate then fill with nearest valid value."""
    arr = np.asarray([np.nan if x is None else float(x) for x in xs], dtype=float)
    arr[~np.isfinite(arr)] = np.nan
    if np.all(np.isnan(arr)):
        return np.zeros_like(arr)
    idx = np.arange(arr.size)
    good = ~np.isnan(arr)
    arr[~good] = np.interp(idx[~good], idx[good], arr[good])
    return arr


def _rolling(arr, window):
    out = np.full(arr.size, np.nan)
    for i in range(arr.size):
        lo = max(0, i - window + 1)
        out[i] = arr[lo:i + 1].mean()
    return out


def build_features(series: dict, hours: np.ndarray) -> np.ndarray:
    """Feature matrix; row i describes the state at hour i."""
    pm = series["pm2_5"]
    cols = []
    for lag in LAGS:
        shifted = np.full(pm.size, np.nan)
        shifted[lag:] = pm[:-lag]
        cols.append(np.nan_to_num(shifted, nan=np.nanmean(pm)))
    for w in ROLL:
        cols.append(np.nan_to_num(_rolling(pm, w), nan=np.nanmean(pm)))
    for key in AUX:
        if key in series:
            v = series[key]
            lagged = np.full(v.size, np.nan)
            lagged[1:] = v[:-1]
            cols.append(np.nan_to_num(lagged, nan=np.nanmean(v)))
    # time-of-day encoding
    frac = (hours % 24) / 24.0
    cols.append(np.sin(2 * np.pi * frac))
    cols.append(np.cos(2 * np.pi * frac))
    # simple trend, scaled to a fixed one-week window so training and
    # single-step prediction rows stay comparable
    cols.append(hours / 168.0)
    return np.column_stack(cols)


def _ridge_fit(X: np.ndarray, y: np.ndarray, alpha: float = RIDGE_ALPHA):
    mu, sigma = X.mean(axis=0), X.std(axis=0)
    sigma[sigma == 0] = 1.0
    Xs = (X - mu) / sigma
    Xb = np.hstack([np.ones((Xs.shape[0], 1)), Xs])
    A = Xb.T @ Xb
    reg = alpha * np.eye(A.shape[0])
    reg[0, 0] = 0.0  # do not penalise intercept
    coef = np.linalg.solve(A + reg, Xb.T @ y)
    return {"coef": coef, "mu": mu, "sigma": sigma}


def _ridge_predict(model, X: np.ndarray) -> np.ndarray:
    Xs = (X - model["mu"]) / model["sigma"]
    Xb = np.hstack([np.ones((Xs.shape[0], 1)), Xs])
    return Xb @ model["coef"]


def train_and_forecast(
    hourly: dict,
    times: list[str],
    anchor_idx: int,
    current_aqi_hint: float | None = None,
) -> dict:
    """
    hourly: dict of lists keyed by pollutant name (full series)
    times:  ISO timestamps aligned to the series
    anchor_idx: index of "now" inside the series
    """
    series = {k: _clean(v) for k, v in hourly.items() if v and any(x is not None for x in v)}
    if "pm2_5" not in series or series["pm2_5"].size < 60:
        return {"ok": False, "reason": "not enough history to train"}

    n = series["pm2_5"].size
    hours = np.arange(n)
    X_all = build_features(series, hours)
    max_h = max(HORIZONS)

    # Sliding-window training set: state at t -> pm2_5 at t+h
    train_rows = np.arange(24, n - max_h)
    if train_rows.size < 40:
        return {"ok": False, "reason": "history too short"}

    models, resid_std = {}, {}
    for h in HORIZONS:
        y = series["pm2_5"][train_rows + h]
        model = _ridge_fit(X_all[train_rows], y)
        fitted = _ridge_predict(model, X_all[train_rows])
        models[h] = model
        resid_std[h] = float(np.clip(np.std(y - fitted), 2.0, 60.0))

    # Forecast from the anchor state, stepping the feature state forward by
    # appending predicted values so lags stay coherent for later horizons.
    ext = {k: v[: anchor_idx + 1].copy() for k, v in series.items()}
    ext_hours = hours[: anchor_idx + 1].copy()
    preds, spread = [], []
    for h in HORIZONS:
        ext_hours = np.append(ext_hours, ext_hours[-1] + 1)
        for key in ext:  # carry the non-PM state forward unchanged
            if key != "pm2_5":
                ext[key] = np.append(ext[key], ext[key][-1])
        ext["pm2_5"] = np.append(ext["pm2_5"], ext["pm2_5"][-1])  # placeholder, replaced below
        X = build_features(ext, ext_hours)
        p = float(_ridge_predict(models[h], X[-1:])[0])
        p = float(np.clip(p, 0.0, 600.0))
        preds.append(p)
        spread.append(resid_std[h])
        ext["pm2_5"][-1] = p  # let the prediction feed the next step's lags

    # --- post-processing -------------------------------------------------
    # 1) blend with the 7-day diurnal climatology: the ridge term captures the
    #    autocorrelation signal, the climatology keeps the daily cycle sane.
    por_hours = np.array([int(t[11:13]) for t in times[: series["pm2_5"].size]])
    clim = {}
    for hh in range(24):
        sel = series["pm2_5"][por_hours == hh]
        clim[hh] = float(np.median(sel)) if sel.size else float(np.median(series["pm2_5"]))
    fc_hours = [int(t[11:13]) for t in times[anchor_idx + 1: anchor_idx + 25]]
    # blend weight ramps in with lead time: hour 1 must stay continuous with
    # the current observation, later hours lean on the daily cycle instead
    blended = []
    for p, hh, h in zip(preds, fc_hours, HORIZONS):
        w = 0.35 * min(1.0, (h - 1) / 6.0)
        blended.append((1 - w) * p + w * clim.get(hh, p))
    preds = blended

    # 2) smooth with a 3-point weighted kernel to kill single-hour spikes
    sm = []
    for i in range(len(preds)):
        lo, hi = max(0, i - 1), min(len(preds) - 1, i + 1)
        w = [preds[j] for j in range(lo, hi + 1)]
        sm.append(float(np.mean(w)))
    preds = sm

    # 3) physical floor/ceiling anchored on the last 24 h of observation, so we
    #    never forecast a cleaner-than-possible or absurd value
    recent = series["pm2_5"][max(0, anchor_idx - 23): anchor_idx + 1]
    floor = max(1.0, 0.45 * float(np.min(recent)))
    ceiling = max(40.0, 2.2 * float(np.max(recent)))
    preds = [float(np.clip(p, floor, ceiling)) for p in preds]

    # 4) light anchor-bias correction on the first hours (models drift on a
    #    single point); decays to zero by hour 12
    bias = float(np.clip(series["pm2_5"][anchor_idx] - preds[0], -15, 15)) * 0.35
    preds = [float(np.clip(p + bias * max(0.0, 1 - (h - 1) / 12.0), 0, 600))
             for p, h in zip(preds, HORIZONS)]

    aqi_curve = [us_aqi({"pm2_5": p})[0] for p in preds]
    scores = [health_score(a)["score"] for a in aqi_curve]

    peak_h = int(np.argmax(aqi_curve))
    mean_resid = float(np.mean(list(resid_std.values())))
    # in-sample R^2 on horizon 6 as a quick quality signal
    h_ref = 6
    y_ref = series["pm2_5"][train_rows + h_ref]
    fit_ref = _ridge_predict(models[h_ref], X_all[train_rows])
    ss_res = float(np.sum((y_ref - fit_ref) ** 2))
    ss_tot = float(np.sum((y_ref - y_ref.mean()) ** 2)) or 1.0

    return {
        "ok": True,
        "pm2_5": [round(p, 1) for p in preds],
        "aqi": [round(a) for a in aqi_curve],
        "health_score": [round(s, 1) for s in scores],
        "lower_pm2_5": [round(max(0.0, p - 1.28 * s), 1) for p, s in zip(preds, spread)],
        "upper_pm2_5": [round(p + 1.28 * s, 1) for p, s in zip(preds, spread)],
        "peak_hour_index": peak_h,
        "peak_aqi": round(aqi_curve[peak_h]),
        "peak_time": times[anchor_idx + 1 + peak_h] if anchor_idx + 1 + peak_h < len(times) else None,
        "end_aqi": round(aqi_curve[-1]),
        "trend": ("improving" if aqi_curve[-1] < aqi_curve[0] - 4
                  else "worsening" if aqi_curve[-1] > aqi_curve[0] + 4 else "steady"),
        "mae": round(mean_resid, 1),
        "r2_h6": round(max(0.0, 1 - ss_res / ss_tot), 3),
        "train_samples": int(train_rows.size),
        "models": len(list(HORIZONS)),
    }
