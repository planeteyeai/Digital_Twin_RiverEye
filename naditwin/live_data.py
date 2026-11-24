"""Live rainfall / weather / discharge / terrain feed for NadiTwin  —  Python standard library only.

Sources (all free, no API key, https://open-meteo.com):
  * Forecast API   hourly rain, temperature, humidity, wind, ET0    past 10 d + 16 d forecast
  * Archive API    same variables, hourly, 1 Jan 2021 -> ~5 d ago (ERA5 re-analysis), downloaded ONE YEAR PER
                   REQUEST and cached per year (archive_<river>_<year>.json): a finished year is fetched once and
                   kept for good, only the current year is refreshed (~daily). Missing years are back-filled in
                   the background (backfill_history), paced to stay inside Open-Meteo's free-API rate limits.
  * Flood API      GloFAS v4 river discharge (daily, m3/s) at the UPSTREAM end (boundary inflow) and at the
                   OUTLET (cross-check), plus the GloFAS ensemble p25/median/p75 for the next 30 days
                   (-> real forecast uncertainty of the inflow)
  * Elevation API  Copernicus GLO-90 DEM along the centreline and on both banks
                   -> water-surface slope, absolute datum (m WSE), flow direction, bank height per km

A documented rainfall-runoff + routing model turns that into a DISCHARGE TIME SERIES FOR EVERY 1 km OF CHAINAGE,
split into components so the dashboard can show WHY discharge changes from km to km:

    Q(km, t) = route(Q(km-1), lag(Q)) + storm(km, t) + urban(km, t) - evap(km, t)

    storm  = runoff_coeff(antecedent rain, ET0) x local rain x local catchment, through two linear reservoirs
    urban  = dry-weather inflow (STP outfalls / nallas) per km with a diurnal pattern        [ASSUMED magnitude]
    evap   = open-water evaporation = 1.05 x ET0 x water-surface area of that km              [live weather]
    lag    = km length / kinematic-wave celerity (5/3 x Manning velocity), so floods travel faster than low flow

Everything is cached on disk (live_cache_*.json, dem_cache.json) so the dashboard still starts if the internet is down.
If nothing at all is reachable the engine falls back to a synthetic series and SAYS SO in /api/live.
"""
import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from array import array
from datetime import datetime, timezone, timedelta, date

API_FORECAST = "https://api.open-meteo.com/v1/forecast"
API_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
API_FLOOD = "https://flood-api.open-meteo.com/v1/flood"
API_ELEVATION = "https://api.open-meteo.com/v1/elevation"
HOURLY_VARS = "precipitation,temperature_2m,relative_humidity_2m,wind_speed_10m,et0_fao_evapotranspiration"

PAST_DAYS = 10            # forecast-API look-back (covers the archive's ~5-day lag)
FORECAST_DAYS = 16
MAX_SAMPLE_POINTS = 6     # weather is fetched at <= 6 points along the reach, interpolated to every km
FORECAST_TTL_S = 25 * 60  # re-fetch forecast + GloFAS if cache is older than this
ARCHIVE_TTL_S = 20 * 3600
HTTP_TIMEOUT_S = 20
# Open-Meteo's free API counts a request as  locations x max(1, days / 14)  "calls" and allows 600 calls/min,
# 5 000/h and 10 000/day. One year of hourly weather at 6 points = ~156 calls, so the long-history requests share a
# token bucket: a burst of up to HEAVY_BURST calls, then HEAVY_RATE_PER_MIN (headroom left for the live refreshes).
HEAVY_BURST = 200.0            # burst + one minute of refill stays <= 500 calls, leaving room for the live refreshes
HEAVY_RATE_PER_MIN = 300.0
GLOFAS_HISTORY_TTL_S = 7 * 86400   # the GloFAS daily history (epoch -> ~1 month ago) is re-fetched weekly
GLOFAS_RECENT_DAYS = 92            # each live refresh only re-fetches the last 92 days + 16-day forecast

# ---- rainfall-runoff / routing parameters (defaults; a river's params.json can override any of them) ----
DEFAULT_PARAMS = {
    "area_km2_per_km": 5.0,       # local urban catchment draining straight into each km of channel (km2 per km)
    "runoff_c_min": 0.25,         # runoff coefficient on dry ground ...
    "runoff_c_max": 0.85,         # ... and on a saturated urban surface
    "api0_mm": 40.0,              # antecedent-precipitation scale (mm) at which C is ~63% of the way to C_MAX
    "reservoir_k_h": 1.0,         # each of the two linear reservoirs (urban concentration time ~2 h)
    "dwf_m3s_per_km": 0.12,       # dry-weather urban inflow per km (STP outfalls, nallas)  [ASSUMED]
    "manning_n": 0.035,
    "upstream_area_km2": 300.0,   # only used when GloFAS is unreachable
}
API_DECAY_H = 0.0044      # base hourly drainage of the wetness store (about 10 %/day)
Q_MIN = 0.5               # m3/s floor
Q_UP_FALLBACK_BASE = 8.0  # m3/s dry-weather inflow if GloFAS is unreachable
EVAP_FACTOR = 1.05        # open-water evaporation / reference ET0
CELERITY_MIN_KMH, CELERITY_MAX_KMH = 0.5, 25.0

# typical Indian-city sewage diurnal pattern, local clock hours 0..23 (normalised to mean 1 below)
_DWF_SHAPE = [0.62, 0.55, 0.50, 0.48, 0.50, 0.62, 0.85, 1.15, 1.38, 1.42, 1.32, 1.20,
              1.12, 1.05, 1.00, 0.98, 1.00, 1.08, 1.20, 1.30, 1.28, 1.15, 0.95, 0.78]
_m = sum(_DWF_SHAPE) / 24.0
DWF_SHAPE = [v / _m for v in _DWF_SHAPE]


def merged_params(overrides=None):
    p = dict(DEFAULT_PARAMS)
    for k, v in (overrides or {}).items():
        if k in p and isinstance(v, (int, float)):
            p[k] = float(v)
    return p


# ------------------------------------------------------------------ HTTP ------------------------------------------------------------------

def _get_json(url, params, timeout=HTTP_TIMEOUT_S, tries=2):
    full = url + "?" + urllib.parse.urlencode(params, safe=",")
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": "NadiTwin/1.1"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:   # network, 4xx/5xx, JSON — all "unavailable"
            last = e
            time.sleep(0.4 + 0.4 * k)
    raise RuntimeError(f"{url.split('/')[2]}: {last}")


def _as_list(obj):
    return obj if isinstance(obj, list) else [obj]


def _hour_idx(epoch, iso):
    t = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
    return int((t - epoch).total_seconds() // 3600)


def _fill(arr, n, epoch, times, values):
    if not times:
        return
    i0 = _hour_idx(epoch, times[0])
    if _hour_idx(epoch, times[-1]) == i0 + len(times) - 1:
        # Open-Meteo hourly arrays are consecutive hours: index by offset (years of data, no per-item parsing)
        lo, hi = max(0, -i0), min(len(values), n - i0)
        for k in range(lo, hi):
            v = values[k]
            if v is not None:
                arr[i0 + k] = float(v)
        return
    for t, v in zip(times, values):
        if v is None:
            continue
        i = _hour_idx(epoch, t)
        if 0 <= i < n:
            arr[i] = float(v)


def _ffill(arr, default=0.0):
    last = None
    for i, v in enumerate(arr):
        if v is None:
            arr[i] = last if last is not None else default
        else:
            last = v
    # back-fill the leading gap with the first real value
    first = next((v for v in arr if v is not None), default)
    return [first if v is None else v for v in arr]


def _daily_to_hourly(times, values, epoch, n_h):
    """daily means (timestamped at 12:00 UTC) -> hourly list by linear interpolation (None outside the data)."""
    arr = [None] * n_h
    pts = [(_hour_idx(epoch, t + "T12:00"), float(v)) for t, v in zip(times, values) if v is not None]
    if len(pts) < 2:
        return arr, len(pts)
    for (i0, v0), (i1, v1) in zip(pts, pts[1:]):
        for i in range(max(0, i0), min(n_h, i1 + 1)):
            arr[i] = v0 + (v1 - v0) * (i - i0) / max(1, i1 - i0)
    for i in range(max(0, pts[0][0] - 12), max(0, min(n_h, pts[0][0]))):
        arr[i] = pts[0][1]
    for i in range(max(0, pts[-1][0]), min(n_h, pts[-1][0] + 12)):
        arr[i] = pts[-1][1]
    return arr, len(pts)


# ------------------------------------------------------------------ terrain (DEM) ------------------------------------------------------------------

def _xy(lon, lat, lon0, lat0):
    k = 111320.0
    return (lon - lon0) * k * math.cos(math.radians(lat0)), (lat - lat0) * k


def _lonlat(x, y, lon0, lat0):
    k = 111320.0
    return lon0 + x / (k * math.cos(math.radians(lat0))), lat0 + y / k


def fetch_elevations(points, status=None):
    """points [(lon, lat)] -> [elev m or None]. Copernicus GLO-90 via Open-Meteo (100 points per request)."""
    out = []
    for a in range(0, len(points), 100):
        chunk = points[a:a + 100]
        js = _get_json(API_ELEVATION, {"latitude": ",".join(f"{p[1]:.5f}" for p in chunk),
                                       "longitude": ",".join(f"{p[0]:.5f}" for p in chunk)}, timeout=25)
        el = js.get("elevation") or []
        out.extend([(float(v) if v is not None else None) for v in el] + [None] * (len(chunk) - len(el)))
    return out


def fetch_dem(chainage_m, lon, lat, width_m, cache_dir, seg_km=1.0, force=False):
    """Centreline + bank terrain, cached forever in <river>/dem_cache.json (terrain does not change).
    Returns {"center": [[chainage_m, elev], ...], "banks": [{"km": k, "left": [...], "right": [...]}, ...],
             "source": ...} or None if the elevation API is unreachable and nothing is cached."""
    n = len(chainage_m)
    sig = [n, round(lon[0], 5), round(lat[0], 5), round(lon[-1], 5), round(lat[-1], 5)]
    cp = os.path.join(cache_dir, "dem_cache.json")
    if not force:
        try:
            with open(cp) as f:
                c = json.load(f)
            if c.get("sig") == sig:
                return c
        except Exception:
            pass
    reach = chainage_m[-1]
    # centreline: one sample every ~150 m (max 300)
    step = max(1, int(round(n * 150.0 / max(1.0, reach))))
    ci = list(range(0, n, step))
    if len(ci) > 300:
        ci = ci[::int(math.ceil(len(ci) / 300.0))]
    if ci[-1] != n - 1:
        ci.append(n - 1)
    # banks: 5 stations per km, 2 offsets beyond the water edge, both sides
    lon0, lat0 = lon[0], lat[0]
    bank_pts, bank_tag = [], []
    n_km = max(1, int(math.ceil(reach / (seg_km * 1000.0) - 1e-9)))
    for k in range(n_km):
        a, b = k * seg_km * 1000.0, min(reach, (k + 1) * seg_km * 1000.0)
        for f in (0.1, 0.3, 0.5, 0.7, 0.9):
            x = a + (b - a) * f
            i = min(range(n), key=lambda j: abs(chainage_m[j] - x)) if n < 400 else \
                max(0, min(n - 1, int(round(x / max(1e-6, reach) * (n - 1)))))
            j0, j1 = max(0, i - 3), min(n - 1, i + 3)
            x0, y0 = _xy(lon[j0], lat[j0], lon0, lat0)
            x1, y1 = _xy(lon[j1], lat[j1], lon0, lat0)
            L = math.hypot(x1 - x0, y1 - y0) or 1.0
            nx, ny = -(y1 - y0) / L, (x1 - x0) / L
            cx, cy = _xy(lon[i], lat[i], lon0, lat0)
            half = max(5.0, width_m[i] / 2.0)
            for side, sgn in (("left", 1), ("right", -1)):
                for off in (half + 30.0, half + 80.0):
                    bank_pts.append(_lonlat(cx + sgn * nx * off, cy + sgn * ny * off, lon0, lat0))
                    bank_tag.append((k, side))
    try:
        ce = fetch_elevations([(lon[i], lat[i]) for i in ci])
        be = fetch_elevations(bank_pts)
    except Exception:
        return None          # offline: engine falls back to the assumed slope / relative datum / bank height
    banks = [{"km": k, "left": [], "right": []} for k in range(n_km)]
    for (k, side), v in zip(bank_tag, be):
        if v is not None:
            banks[k][side].append(v)
    out = {"sig": sig, "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "source": "Copernicus GLO-90 DEM via Open-Meteo elevation API",
           "center": [[round(chainage_m[i], 1), v] for i, v in zip(ci, ce) if v is not None], "banks": banks}
    try:
        with open(cp, "w") as f:
            json.dump(out, f)
    except Exception:
        pass
    return out


def robust_line(xs, ys):
    """least-squares line y = a + b x, refitted after dropping points > 2.5 m (or 2.5 MAD) off the first fit."""
    pts = list(zip(xs, ys))
    for _ in range(3):
        n = len(pts)
        if n < 3:
            break
        mx = sum(p[0] for p in pts) / n
        my = sum(p[1] for p in pts) / n
        sxx = sum((p[0] - mx) ** 2 for p in pts) or 1e-9
        b = sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx
        a = my - b * mx
        res = sorted(abs(p[1] - (a + b * p[0])) for p in pts)
        mad = res[len(res) // 2] or 0.5
        lim = max(2.5, 2.5 * mad)
        keep = [p for p in pts if abs(p[1] - (a + b * p[0])) <= lim]
        if len(keep) == len(pts):
            break
        pts = keep
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts) or 1e-9
    b = sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx
    a = my - b * mx
    resid = [p[1] - (a + b * p[0]) for p in pts]
    rmse = math.sqrt(sum(r * r for r in resid) / n)
    return a, b, rmse, n


# ------------------------------------------------------------------ history caches ------------------------------------------------------------------

_HEAVY_LOCK = threading.Lock()
_BUCKET = {"tokens": None, "t": 0.0}


def _api_weight(params):
    """Open-Meteo's own count of a request: locations x max(1, days / 14) x max(1, variables / 10)."""
    n_loc = len(str(params.get("latitude", "0")).split(","))
    if params.get("start_date") and params.get("end_date"):
        days = (date.fromisoformat(params["end_date"]) - date.fromisoformat(params["start_date"])).days + 1
    else:
        days = int(params.get("past_days", 0)) + int(params.get("forecast_days", 7))
    n_var = len(str(params.get("hourly") or params.get("daily") or "x").split(","))
    return n_loc * max(1.0, days / 14.0) * max(1.0, n_var / 10.0)


def _heavy_get(url, params, timeout):
    """Long-history requests go one at a time (all rivers) through a token bucket sized to the free-API limits."""
    w = _api_weight(params)
    with _HEAVY_LOCK:
        now = time.time()
        b = _BUCKET
        if b["tokens"] is None:
            b["tokens"], b["t"] = HEAVY_BURST, now
        b["tokens"] = min(HEAVY_BURST, b["tokens"] + (now - b["t"]) * HEAVY_RATE_PER_MIN / 60.0)
        b["t"] = now
        need = min(w, HEAVY_BURST)
        if b["tokens"] < need:
            time.sleep((need - b["tokens"]) * 60.0 / HEAVY_RATE_PER_MIN)
            b["tokens"], b["t"] = need, time.time()
        b["tokens"] -= need
        try:
            return _get_json(url, params, timeout=timeout, tries=3)
        except Exception:
            b["tokens"] += need        # nothing was served (offline): don't let failures slow the next request
            raise


def sample_points(stations_lonlat):
    """The <= MAX_SAMPLE_POINTS stations where weather is fetched (same for live refreshes and history back-fill)."""
    n = len(stations_lonlat)
    ns = max(1, min(MAX_SAMPLE_POINTS, n))
    sample_i = [round(i * (n - 1) / max(1, ns - 1)) for i in range(ns)] if ns > 1 else [0]
    return sample_i, [stations_lonlat[i][0] for i in sample_i], [stations_lonlat[i][1] for i in sample_i]


def _archive_end(now):
    """last day taken from the ERA5 archive; the forecast API's 10-day look-back covers the days after it."""
    return (now - timedelta(days=PAST_DAYS - 1)).date()


def _year_span(year, epoch, now):
    a = max(date(year, 1, 1), epoch.date())
    b = min(date(year, 12, 31), _archive_end(now))
    return a, b


def _archive_path(cache_dir, key, year):
    return os.path.join(cache_dir, f"archive_{key}_{year}.json")


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _archive_chunk_ok(c, lat_s, lon_s, a, b, max_age_s=None):
    if not c or c.get("lat") != lat_s or c.get("lon") != lon_s:
        return False
    if c.get("start", "9999") > a.isoformat() or c.get("end", "0000") < b.isoformat():
        return False
    return max_age_s is None or time.time() - c.get("ts", 0) < max_age_s


def _fetch_archive_year(year, lon_s, lat_s, epoch, now, cache_dir, key):
    a, b = _year_span(year, epoch, now)
    js = _as_list(_heavy_get(API_ARCHIVE, {
        "latitude": lat_s, "longitude": lon_s, "start_date": a.isoformat(), "end_date": b.isoformat(),
        "hourly": HOURLY_VARS, "timezone": "UTC"}, timeout=120))
    data = [{"time": j["hourly"]["time"], **{v: j["hourly"].get(v) for v in HOURLY_VARS.split(",")}} for j in js]
    c = {"ts": time.time(), "year": year, "start": a.isoformat(), "end": b.isoformat(), "lat": lat_s, "lon": lon_s,
         "complete": b == date(year, 12, 31), "data": data}
    try:
        with open(_archive_path(cache_dir, key, year), "w") as f:
            json.dump(c, f)
    except Exception:
        pass
    return c


def _legacy_archive(cache_dir, key, lat_s, n_loc):
    """the single-file archive cache written by earlier versions (1 Jan 2026 -> ...): reused for 2026 until refetched."""
    c = _read_json(os.path.join(cache_dir, f"live_cache_archive_{key}.json"))
    if c and c.get("n") == n_loc and c.get("lat") == lat_s and c.get("data"):
        return dict(c, legacy=True)
    return None


def backfill_history(stations_lonlat, epoch, cache_dir, key, flow_dir=1, now=None, log=None):
    """Runs in the server's background thread (never blocks the dashboard). Downloads whatever ERA5 history is
    missing (one request per year), refreshes the current year's copy when it is older than ARCHIVE_TTL_S, and keeps
    the GloFAS daily history since `epoch`; all paced by the token bucket in _heavy_get.
    Returns the number of new downloads (0 = up to date, or offline)."""
    now = now or datetime.now(timezone.utc)
    log = log or (lambda m: None)
    _, lons, lats = sample_points(stations_lonlat)
    lon_s = ",".join(f"{x:.4f}" for x in lons)
    lat_s = ",".join(f"{y:.4f}" for y in lats)
    got = 0
    cur_year = _archive_end(now).year
    for y in range(epoch.year, cur_year + 1):
        a, b = _year_span(y, epoch, now)
        if b < a:
            continue
        c = _read_json(_archive_path(cache_dir, key, y))
        if y < cur_year:
            need = not _archive_chunk_ok(c, lat_s, lon_s, a, date(y, 12, 31))
        else:
            need = not _archive_chunk_ok(c, lat_s, lon_s, a, a, ARCHIVE_TTL_S)
        c = None
        if not need:
            continue
        try:
            _fetch_archive_year(y, lon_s, lat_s, epoch, now, cache_dir, key)
            got += 1
            log(f"    {key}: rain/weather history {y} downloaded")
        except Exception as e:
            log(f"    {key}: rain/weather history {y} not downloaded ({str(e)[:90]}) - will retry later")
            return got          # offline / rate-limited: stop this round, the next refresh tries again
    up = stations_lonlat[0] if flow_dir >= 0 else stations_lonlat[-1]
    dn = stations_lonlat[-1] if flow_dir >= 0 else stations_lonlat[0]
    if _glofas_history(up, dn, epoch, now, cache_dir, key, fetch=True, log=log)[1]:
        got += 1
    return got


def _glofas_history(up, dn, epoch, now, cache_dir, key, fetch=False, log=None):
    """GloFAS daily discharge from `epoch` to ~30 days ago at the upstream end + outlet, cached in
    glofas_history_<key>.json (re-fetched weekly). Returns (cache dict or None, fetched_now?)."""
    lat_s = f"{up[1]:.4f},{dn[1]:.4f}"
    lon_s = f"{up[0]:.4f},{dn[0]:.4f}"
    path = os.path.join(cache_dir, f"glofas_history_{key}.json")
    c = _read_json(path)
    ok = bool(c and c.get("lat") == lat_s and c.get("lon") == lon_s and c.get("start", "9999") <= epoch.strftime("%Y-%m-%d"))
    if ok and time.time() - c.get("ts", 0) < GLOFAS_HISTORY_TTL_S:
        return c, False
    if not fetch:
        return (c if ok else None), False
    end = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    try:
        js = _as_list(_heavy_get(API_FLOOD, {"latitude": lat_s, "longitude": lon_s, "daily": "river_discharge",
                                             "start_date": epoch.strftime("%Y-%m-%d"), "end_date": end}, timeout=90))
        c = {"ts": time.time(), "lat": lat_s, "lon": lon_s, "start": epoch.strftime("%Y-%m-%d"), "end": end,
             "time": js[0]["daily"]["time"], "q_up": js[0]["daily"]["river_discharge"],
             "q_out": js[1]["daily"]["river_discharge"] if len(js) > 1 else None,
             "cells": {"upstream": [js[0].get("latitude"), js[0].get("longitude")],
                       "outlet": ([js[1].get("latitude"), js[1].get("longitude")] if len(js) > 1 else None)}}
        with open(path, "w") as f:
            json.dump(c, f)
        if log:
            log(f"    {key}: GloFAS discharge history {c['start']} -> {end} downloaded")
        return c, True
    except Exception as e:
        if log:
            log(f"    {key}: GloFAS discharge history not downloaded ({str(e)[:90]}) - will retry later")
        return (c if ok else None), False


# ------------------------------------------------------------------ fetchers ------------------------------------------------------------------

def _fetch_weather(lons, lats, epoch, n_h, now, cache_dir, key, status):
    """Hourly weather at sample points. Returns dict var -> [loc][h] (None where missing)."""
    nl = len(lons)
    out = {v: [[None] * n_h for _ in range(nl)] for v in HOURLY_VARS.split(",")}
    lon_s = ",".join(f"{x:.4f}" for x in lons)
    lat_s = ",".join(f"{y:.4f}" for y in lats)

    # --- archive (ERA5, one cache file per year; finished years are kept for good, the current year ~daily) ---
    have, missing, notes = [], [], []
    cur_year = _archive_end(now).year
    for y in range(epoch.year, cur_year + 1):
        a, b = _year_span(y, epoch, now)
        if b < a:
            continue
        c = _read_json(_archive_path(cache_dir, key, y))
        if y < cur_year:
            # a finished year never changes: any complete cached copy is good
            if not _archive_chunk_ok(c, lat_s, lon_s, a, date(y, 12, 31)):
                c = None
        else:
            if not _archive_chunk_ok(c, lat_s, lon_s, a, a):
                c = _legacy_archive(cache_dir, key, lat_s, nl)       # copy written by the previous version
            if c is None:
                # very first run: nothing on disk for this year -> fetch it now
                try:
                    c = _fetch_archive_year(y, lon_s, lat_s, epoch, now, cache_dir, key)
                    notes.append(f"{y} live")
                except Exception as e:
                    notes.append(f"{y} error ({str(e)[:60]})")
            elif c.get("legacy") or time.time() - c.get("ts", 0) >= ARCHIVE_TTL_S:
                status["archive_refresh_due"] = True                 # refreshed by backfill_history in the background
        if c and c.get("data"):
            have.append(y)
            for l, arc in enumerate(c["data"][:nl]):
                for v in HOURLY_VARS.split(","):
                    if arc.get(v):
                        _fill(out[v][l], n_h, epoch, arc["time"], arc[v])
        else:
            missing.append(y)
    if have:
        status["archive"] = (f"ERA5 {min(have)}-{max(have)}"
                             + (f"; still downloading {', '.join(map(str, missing))} in the background" if missing else "")
                             + (f" ({'; '.join(notes)})" if notes else ""))
    else:
        status["archive"] = "error: no ERA5 history" + (f" ({'; '.join(notes)})" if notes else "")
    status["history_years_missing"] = missing

    # --- forecast (past 10 d + 16 d ahead): overrides archive where present ---
    try:
        js = _as_list(_get_json(API_FORECAST, {
            "latitude": lat_s, "longitude": lon_s, "hourly": HOURLY_VARS, "past_days": PAST_DAYS,
            "forecast_days": FORECAST_DAYS, "timezone": "UTC"}))
        for l, j in enumerate(js):
            h = j["hourly"]
            for v in HOURLY_VARS.split(","):
                if h.get(v):
                    _fill(out[v][l], n_h, epoch, h["time"], h[v])
        status["forecast"] = "live"
    except Exception as e:
        status["forecast"] = f"error: {e}"
    return out


def _fetch_glofas(up_lonlat, out_lonlat, epoch, n_h, now, status, cache_dir=None, key=None):
    """GloFAS daily discharge at the upstream end and the outlet -> hourly lists, + forecast ensemble spread.
    History since `epoch` comes from glofas_history_<key>.json (back-filled in the background); every refresh only
    re-fetches the last 92 days + the 16-day forecast."""
    q_up = q_out = None
    cells = {}
    hist = None
    if cache_dir and key:
        hist, _ = _glofas_history(up_lonlat, out_lonlat, epoch, now, cache_dir, key, fetch=False)
    try:
        lat_s = f"{up_lonlat[1]:.4f},{out_lonlat[1]:.4f}"
        lon_s = f"{up_lonlat[0]:.4f},{out_lonlat[0]:.4f}"
        js = _as_list(_get_json(API_FLOOD, {"latitude": lat_s, "longitude": lon_s, "daily": "river_discharge",
                                            "past_days": GLOFAS_RECENT_DAYS, "forecast_days": FORECAST_DAYS}, timeout=40))
        series = []
        for k in range(min(2, len(js))):
            d = js[k]["daily"]
            merged = {}
            if hist and hist.get("time"):
                hv = hist.get("q_up") if k == 0 else hist.get("q_out")
                if hv:
                    merged.update({t: v for t, v in zip(hist["time"], hv) if v is not None})
            merged.update({t: v for t, v in zip(d["time"], d["river_discharge"]) if v is not None})
            tt = sorted(merged)
            series.append(_daily_to_hourly(tt, [merged[t] for t in tt], epoch, n_h))
        q_up, n_days = series[0]
        cells["upstream"] = [js[0].get("latitude"), js[0].get("longitude")]
        if len(series) > 1:
            q_out = series[1][0]
            cells["outlet"] = [js[1].get("latitude"), js[1].get("longitude")]
        if n_days >= 2:
            status["glofas"] = (f"live ({n_days} days" + (f", history since {hist['start']}" if hist else
                                                         ", history since " + epoch.strftime("%Y-%m-%d") + " still downloading") + ")")
        else:
            status["glofas"] = "error: no discharge values returned"
            q_up = q_out = None
    except Exception as e:
        status["glofas"] = f"error: {e}"

    spread = [None] * n_h
    try:
        js = _as_list(_get_json(API_FLOOD, {"latitude": f"{up_lonlat[1]:.4f}", "longitude": f"{up_lonlat[0]:.4f}",
                                            "daily": "river_discharge_p25,river_discharge_median,river_discharge_p75",
                                            "past_days": 1, "forecast_days": 30}, timeout=30))
        d = js[0]["daily"]
        rel = []
        for t, p25, med, p75 in zip(d["time"], d["river_discharge_p25"], d["river_discharge_median"], d["river_discharge_p75"]):
            if None in (p25, med, p75) or med <= 0:
                continue
            rel.append((t, max(0.0, (p75 - p25) / 1.349 / med)))     # robust relative standard deviation
        if rel:
            hh, _ = _daily_to_hourly([t for t, _ in rel], [v for _, v in rel], epoch, n_h)
            spread = hh
            status["glofas_ensemble"] = f"live ({len(rel)} days of p25/median/p75)"
        else:
            status["glofas_ensemble"] = "no ensemble statistics returned"
    except Exception as e:
        status["glofas_ensemble"] = f"error: {e}"
    return q_up, q_out, spread, cells


# ------------------------------------------------------------------ public API ------------------------------------------------------------------

class LiveData:
    """Raw live inputs, aligned to hourly indices since `epoch`."""
    pass


def fetch_live(stations_lonlat, epoch, n_h, cache_dir, key, force=False, now=None, flow_dir=1):
    """stations_lonlat: [(lon, lat), ...] in CHAINAGE order (any density); flow_dir = +1 if chainage 0 is upstream.
    Returns LiveData or None if nothing could be fetched or restored from cache."""
    now = now or datetime.now(timezone.utc)
    cp = os.path.join(cache_dir, f"live_cache_{key}.json")
    if not force:
        try:
            with open(cp) as f:
                c = json.load(f)
            if time.time() - c["ts"] < FORECAST_TTL_S and c.get("flow_dir", 1) == flow_dir and \
                    c.get("epoch") == epoch.strftime("%Y-%m-%dT%H:%M:%SZ"):
                return _resize(_from_dict(c), n_h)
        except Exception:
            pass

    sample_i, lons, lats = sample_points(stations_lonlat)
    status = {}
    w = _fetch_weather(lons, lats, epoch, n_h, now, cache_dir, key, status)
    up = stations_lonlat[0] if flow_dir >= 0 else stations_lonlat[-1]
    dn = stations_lonlat[-1] if flow_dir >= 0 else stations_lonlat[0]
    q_up, q_out, spread, cells = _fetch_glofas(up, dn, epoch, n_h, now, status, cache_dir, key)

    got_rain = status.get("forecast") == "live" or str(status.get("archive", "")).startswith("ERA5")
    got_q = q_up is not None
    if status.get("forecast") != "live" and not got_q:
        # nothing fresh came back: the last complete fetch (any age) beats a half-rebuilt dataset
        try:
            with open(cp) as f:
                c = json.load(f)
            if c.get("epoch") == epoch.strftime("%Y-%m-%dT%H:%M:%SZ"):
                ld = _resize(_from_dict(c), n_h)
                ld.status = dict(ld.status, note="network unreachable — using STALE live data fetched " + c.get("fetched_at", "?"))
                return ld
        except Exception:
            pass
        if not got_rain:
            return None

    ld = LiveData()
    ld.epoch = epoch
    ld.n_h = n_h
    ld.flow_dir = flow_dir
    ld.sample_i = sample_i
    ld.status = status
    ld.fetched_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    ld.rain = [array("d", [(v if v is not None else 0.0) for v in row]) for row in w["precipitation"]]
    ld.temp = array("d", _mean_series(w["temperature_2m"]))
    ld.rh = array("d", _mean_series(w["relative_humidity_2m"]))
    ld.wind = array("d", _mean_series(w["wind_speed_10m"]))
    ld.et0 = [array("d", [(v if v is not None else 0.1) for v in row]) for row in w["et0_fao_evapotranspiration"]]
    del w
    ld.q_up_glofas = q_up
    ld.q_out_glofas = q_out
    ld.glofas_spread = spread
    ld.glofas_cells = cells
    ld.now_idx = int((now - epoch).total_seconds() // 3600)
    ld.rain_live = got_rain
    try:
        with open(cp, "w") as f:
            json.dump(_to_dict(ld), f)
    except Exception:
        pass
    return ld


def synthetic_live(stations_lonlat, epoch, n_h, q_up):
    """No network at all: same model, zero rain, typical ET0 — only the upstream inflow is synthetic."""
    ld = LiveData()
    ld.epoch, ld.n_h, ld.flow_dir = epoch, n_h, 1
    ld.sample_i = [0]
    ld.status = {"note": "synthetic"}
    ld.fetched_at = None
    ld.rain = [[0.0] * n_h]
    ld.et0 = [[max(0.0, 0.35 * math.sin(math.pi * (((h + 5) % 24) - 6) / 12.0)) for h in range(n_h)]]
    ld.temp = [26.0] * n_h
    ld.rh = [60.0] * n_h
    ld.wind = [8.0] * n_h
    ld.q_up_glofas = list(q_up)
    ld.q_out_glofas = None
    ld.glofas_spread = [None] * n_h
    ld.glofas_cells = {}
    ld.now_idx = 0
    ld.rain_live = False
    return ld


def _mean_series(rows):
    n = len(rows[0])
    out = []
    for h in range(n):
        vs = [r[h] for r in rows if r[h] is not None]
        out.append(sum(vs) / len(vs) if vs else None)
    return _ffill(out, 0.0)


def _rnd(a, nd):
    return None if a is None else [None if v is None else round(v, nd) for v in a]


def _to_dict(ld):
    return {"ts": time.time(), "n_h": ld.n_h, "flow_dir": ld.flow_dir, "fetched_at": ld.fetched_at,
            "sample_i": ld.sample_i, "status": ld.status,
            "rain": [[round(v, 2) for v in r] for r in ld.rain], "temp": [round(v, 1) for v in ld.temp],
            "rh": [round(v) for v in ld.rh], "wind": [round(v, 1) for v in ld.wind],
            "et0": [[round(v, 3) for v in r] for r in ld.et0],
            "q_up_glofas": _rnd(ld.q_up_glofas, 2), "q_out_glofas": _rnd(ld.q_out_glofas, 2),
            "glofas_spread": _rnd(ld.glofas_spread, 4), "glofas_cells": ld.glofas_cells, "rain_live": ld.rain_live,
            "now_idx": ld.now_idx, "epoch": ld.epoch.strftime("%Y-%m-%dT%H:%M:%SZ")}


def _from_dict(c):
    ld = LiveData()
    ld.epoch = datetime.strptime(c["epoch"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    for k in ("n_h", "fetched_at", "sample_i", "status", "rain", "temp", "rh", "wind", "et0", "q_up_glofas", "now_idx"):
        setattr(ld, k, c[k])
    ld.rain = [array("d", r) for r in ld.rain]
    ld.et0 = [array("d", r) for r in ld.et0]
    ld.temp, ld.rh, ld.wind = array("d", ld.temp), array("d", ld.rh), array("d", ld.wind)
    ld.flow_dir = c.get("flow_dir", 1)
    ld.q_out_glofas = c.get("q_out_glofas")
    ld.glofas_spread = c.get("glofas_spread") or [None] * ld.n_h
    ld.glofas_cells = c.get("glofas_cells", {})
    ld.rain_live = c.get("rain_live", True)
    return ld


def _resize(ld, n_h):
    """pad/truncate every hourly series to n_h (the clock moves on between fetches)."""
    if ld.n_h == n_h:
        return ld

    def fit(a, pad):
        if a is None:
            return None
        typed = isinstance(a, array)
        a = list(a[:n_h])
        a = a + [pad if pad is not None else (a[-1] if a else 0.0)] * (n_h - len(a))
        return array("d", a) if typed else a
    ld.rain = [fit(r, 0.0) for r in ld.rain]
    ld.et0 = [fit(r, None) for r in ld.et0]
    ld.temp, ld.rh, ld.wind = fit(ld.temp, None), fit(ld.rh, None), fit(ld.wind, None)
    ld.q_up_glofas = fit(ld.q_up_glofas, None)
    ld.q_out_glofas = fit(ld.q_out_glofas, None)
    ld.glofas_spread = (list(ld.glofas_spread[:n_h]) + [None] * max(0, n_h - len(ld.glofas_spread)))
    ld.n_h = n_h
    return ld


# ------------------------------------------------------------------ rainfall-runoff + routing ------------------------------------------------------------------

def _reservoir(x, k_h):
    a = 1.0 - math.exp(-1.0 / k_h)
    y, out = 0.0, []
    for v in x:
        y += (v - y) * a
        out.append(y)
    return out


def _shift_var(x, lag):
    """delay series x by a time-varying lag (hours, per output step), linear interpolation."""
    n = len(x)
    out = array("d", bytes(8 * n))
    x0 = x[0]
    for i in range(n):
        p = i - lag[i]
        if p <= 0:
            out[i] = x0
            continue
        k = int(p)
        f = p - k
        out[i] = x[k] * (1 - f) + x[min(k + 1, n - 1)] * f if k + 1 < n else x[k]
    return out


def manning_depth(q, width_m, slope, n_manning):
    w = max(5.0, width_m)
    return (max(q, 0.0) * n_manning / (w * math.sqrt(max(1e-5, slope)))) ** 0.6


def celerity_kmh(q, width_m, slope, n_manning):
    h = manning_depth(q, width_m, slope, n_manning)
    v = max(q, 0.01) / (max(5.0, width_m) * max(0.02, h))
    return max(CELERITY_MIN_KMH, min(CELERITY_MAX_KMH, 5.0 / 3.0 * v * 3.6))


def compute_discharge(ld, segs, slope, params=None, gauge_up=None, utc_offset_h=5.5):
    """Chainage-wise discharge, by component.

    segs: list in FLOW ORDER (upstream first) of dicts {"i": original segment index, "mid_frac": position of the
          segment along the station list (0..1, used to interpolate the weather sample points), "len_km", "width_m"}.
    Returns (comp, info): comp[name][i] = hourly array('d') for name in up/storm/dwf/evap/q (q = total);
            info = {"q_up", "q_up_source", "rain_seg", "runoff_coeff_seg", "lag_h_seg"} (indexed by original segment)."""
    p = merged_params(params)
    n_h = ld.n_h
    ns = len(segs)
    sample_frac = [i / max(1, len(ld.sample_i) - 1) if len(ld.sample_i) > 1 else 0.0 for i in range(len(ld.sample_i))]
    nm = p["manning_n"]
    c_min, c_max, api0 = p["runoff_c_min"], p["runoff_c_max"], p["api0_mm"]

    api = []   # antecedent wetness at each sample point
    for l, row in enumerate(ld.rain):
        a, series = 0.0, array("d", bytes(8 * n_h))
        et = ld.et0[l]
        for h in range(n_h):
            decay = max(0.90, 1.0 - API_DECAY_H - 0.0125 * et[h])   # hot/dry air dries the ground faster
            a = a * decay + row[h]
            series[h] = a
        api.append(series)

    def blend(rows, frac):
        if len(rows) == 1:
            return rows[0]
        j = 0
        while j < len(sample_frac) - 2 and frac > sample_frac[j + 1]:
            j += 1
        span = sample_frac[j + 1] - sample_frac[j] or 1.0
        w = min(1.0, max(0.0, (frac - sample_frac[j]) / span))
        a, b = rows[j], rows[j + 1]
        return [a[h] * (1 - w) + b[h] * w for h in range(n_h)]

    # upstream boundary
    if gauge_up is not None:
        q_up, src = list(gauge_up), "user gauge CSV"
    elif ld.q_up_glofas is not None:
        q_up = _ffill(list(ld.q_up_glofas), Q_UP_FALLBACK_BASE)
        src = "GloFAS river discharge (Open-Meteo flood API)" if ld.status.get("note") != "synthetic" else "synthetic"
    else:
        fr0 = segs[0]["mid_frac"]
        r0, a0 = blend(ld.rain, fr0), blend(api, fr0)
        lat = [r0[h] * (c_min + (c_max - c_min) * (1 - math.exp(-a0[h] / api0))) * p["upstream_area_km2"] * 0.27778
               for h in range(n_h)]
        lat = _reservoir(_reservoir(lat, 3.0), 3.0)
        q_up, src = [Q_UP_FALLBACK_BASE + v for v in lat], "rainfall-runoff estimate (GloFAS unreachable)"
    q_up = [max(Q_MIN, v) for v in q_up]

    # local clock hour for the dry-weather (sewage) pattern
    dwf_h = [DWF_SHAPE[int((h + utc_offset_h) % 24)] for h in range(n_h)]

    names = ("up", "storm", "dwf", "evap", "q")
    comp = {k: [None] * ns for k in names}
    rain_seg, cr_seg, lag_seg = [None] * ns, [None] * ns, [None] * ns
    prev = {"up": array("d", q_up), "storm": array("d", bytes(8 * n_h)), "dwf": array("d", bytes(8 * n_h)),
            "evap": array("d", bytes(8 * n_h))}
    prev_q = array("d", q_up)
    for sg in segs:
        i, L, W = sg["i"], sg["len_km"], sg["width_m"]
        fr = sg["mid_frac"]
        r = blend(ld.rain, fr)
        a = blend(api, fr)
        e = blend(ld.et0, fr)
        cr = [c_min + (c_max - c_min) * (1 - math.exp(-a[h] / api0)) for h in range(n_h)]
        storm_loc = [r[h] * cr[h] * p["area_km2_per_km"] * L * 0.27778 for h in range(n_h)]   # mm/h * km2 -> m3/s
        storm_loc = _reservoir(_reservoir(storm_loc, p["reservoir_k_h"]), p["reservoir_k_h"])
        dwf_loc = p["dwf_m3s_per_km"] * L
        evap_k = EVAP_FACTOR * W * L * 1000.0 / 1000.0 / 3600.0      # (mm/h) -> m3/s for this km's water surface
        lag = [L / celerity_kmh(prev_q[h], W, slope, nm) for h in range(n_h)]
        cur = {}
        for k in ("up", "storm", "dwf", "evap"):
            cur[k] = _shift_var(prev[k], lag)
        for h in range(n_h):
            cur["storm"][h] += storm_loc[h]
            cur["dwf"][h] += dwf_loc * dwf_h[h]
            cur["evap"][h] += evap_k * e[h]
        q = array("d", bytes(8 * n_h))
        for h in range(n_h):
            q[h] = max(Q_MIN, cur["up"][h] + cur["storm"][h] + cur["dwf"][h] - cur["evap"][h])
        for k in ("up", "storm", "dwf", "evap"):
            comp[k][i] = cur[k]
        comp["q"][i] = q
        rain_seg[i], cr_seg[i] = array("d", r), array("d", cr)
        lag_seg[i] = array("d", lag)
        prev, prev_q = cur, q
    return comp, {"q_up": q_up, "q_up_source": src, "rain_seg": rain_seg, "runoff_coeff_seg": cr_seg,
                  "lag_h_seg": lag_seg, "params": p}
