"""
NadiTwin engine — multi-river, real-calendar, LIVE-data river twin.

Each river is a self-contained data folder under naditwin/rivers/<key>/:
    chainage_profile.json   centerline stations + bank-to-bank width (from the river polygon / chainage KML)
    reach_polygon.json      traced polygon (lon/lat) for the map
    landmarks.json          localities/bridges/confluences snapped onto the chainage
    depth_profile.json      OPTIONAL per-station water depth (e.g. from the Sentinel-2 water-depth KML)
    survey.json             OPTIONAL date/range of that depth survey (used to calibrate stage)
    params.json             OPTIONAL model overrides (manning_n, dwf_m3s_per_km, bank_height_m, flow_dir, ...)
    meta.json               present for rivers added via KML upload
    dem_cache.json / live_cache_*.json   written automatically (terrain + live feed caches)

WHAT IS LIVE (needs internet; see live_data.py):
    rainfall, temperature, humidity, wind, ET0       -> Open-Meteo (ERA5 archive since 1 Jan 2021 + 16-day forecast)
    upstream river discharge + forecast uncertainty   -> GloFAS v4 via Open-Meteo flood API (+ outlet cross-check)
    terrain: water-surface slope, datum, bank height  -> Copernicus GLO-90 DEM via Open-Meteo elevation API
    chainage-wise discharge (every 1 km)              -> rainfall-runoff + urban inflow + evaporation + routing model
    river stage / depth / hydrograph / rating curve / margin board -> Manning hydraulics on that discharge, using the
                                                         real width of every station and the surveyed depth

STILL ASSUMED (flagged everywhere it surfaces): Manning roughness, urban dry-weather inflow per km, runoff parameters,
dam releases (Khadakwasla etc. are not seen by GloFAS), and — where the DEM is unreachable — slope and bank height.
Bank heights from a 90 m DSM are approximate (+-several m); replace them with surveyed embankment levels via params.json.

TIME MODEL: every hourly value (past AND forecast) is indexed against a real UTC clock starting at EPOCH.
Every query takes an explicit `dt` (None = now, interpolated to the current minute) — the engine is stateless per request.
"""

import json
import math
import random
import csv
import os
import threading
from array import array
from datetime import datetime, timezone, timedelta

from . import live_data

HERE = os.path.dirname(__file__)
RIVERS_DIR = os.path.join(HERE, "rivers")

# Start of the historic record: every hourly series (rain, GloFAS inflow, discharge, stage, margins) runs from here.
# The ERA5 + GloFAS history before the current year is downloaded once (one year per request) and cached per year;
# override with the environment variable NADITWIN_HISTORY_START=YYYY-MM-DD.
def _history_start():
    raw = os.environ.get("NADITWIN_HISTORY_START", "").strip()
    try:
        d = datetime.strptime(raw, "%Y-%m-%d") if raw else datetime(2021, 1, 1)
    except ValueError:
        d = datetime(2021, 1, 1)
    return d.replace(tzinfo=timezone.utc)


EPOCH = _history_start()
PAST_WINDOW_H = 72
FORECAST_HOURS = 72
N_MEMBERS = 50
FUTURE_BUFFER_H = 24 * 45
MAX_HYDRO_POINTS = 1500

SEG_KM = 1.0                # chainage segment used for discharge + margin board

# --- hydraulics / geometry fallbacks (used only when the DEM / params.json do not provide a value) ---
DEFAULT_SLOPE = 0.0009          # bed slope m/m if the DEM is unreachable
SLOPE_MIN, SLOPE_MAX = 0.0002, 0.02
BANK_ABOVE_SURVEY_WSE_M = 4.5   # bank crest above the survey-day water surface if the DEM is unreachable
BANK_MIN_M, BANK_MAX_M = 3.0, 7.0   # clamp for DEM-estimated bank heights (90 m DSM noise, buildings)
BANK_PCTL = 0.15                    # low percentile of the bank samples = open ground / roads, not rooftops
Q_REF_DEFAULT_M3S = 15.0        # flow on the depth-survey day if no live series covers it
SENTINEL2_UTC_HOUR = 5          # Sentinel-2 overpass over India ~10:30 IST
FREEBOARD_M = 0.8               # danger level = bank crest - freeboard
HYD_WIDTH_WINDOW_M = 500.0      # hydraulic width = running mean of the bank-to-bank width over this window
FLOODPLAIN_FACTOR = 3.0         # above the bank crest the flow spreads over a floodplain this many channel-widths wide

# --- margin board zones, measured on  margin = (crest - 0.8 m) - stage  ---
ZONE_WARNING_M = 0.7        # margin <= 0.7 m  -> WARNING   (<= 0 m -> DANGER)
ZONE_WATCH_M = 1.7          # margin <= 1.7 m  -> WATCH     (else SAFE)
ZONE_RANK = {"SAFE": 0, "WATCH": 1, "WARNING": 2, "DANGER": 3}
ZONE_NAME = ["SAFE", "WATCH", "WARNING", "DANGER"]

LIVE_REFRESH_S = 25 * 60
Q_MIN_FC = 0.5


def utc_now():
    return datetime.now(timezone.utc)


def parse_iso(s):
    if s is None:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def hours_since_epoch(dt):
    return int((dt - EPOCH).total_seconds() // 3600)


def iso_h(idx):
    return (EPOCH + timedelta(hours=idx)).strftime("%Y-%m-%dT%H:%MZ")


def zone_for_margin(m):
    if m <= 0:
        return 3
    if m <= ZONE_WARNING_M:
        return 2
    if m <= ZONE_WATCH_M:
        return 1
    return 0


def _pct(vals, p):
    v = sorted(vals)
    if not v:
        return None
    pos = p * (len(v) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def _haversine(lon1, lat1, lon2, lat2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _ch_label(m):
    km = int(m // 1000)
    return f"{km}+{int(round(m - km * 1000)):03d}"


def _utc_offset_h(lon, lat):
    if 68.0 <= lon <= 98.0 and 6.0 <= lat <= 37.5:
        return 5.5                      # India Standard Time
    return round(lon / 7.5) / 2.0


# ------------------------------ river registry ------------------------------

class RiverData:
    def __init__(self, key, display_name, product_label):
        self.key = key
        self.display_name = display_name
        self.product_label = product_label
        d = os.path.join(RIVERS_DIR, key)
        self.dir = d

        with open(os.path.join(d, "chainage_profile.json")) as f:
            stations = json.load(f)
        with open(os.path.join(d, "reach_polygon.json")) as f:
            self.reach_polygon_lonlat = json.load(f)
        lm_path = os.path.join(d, "landmarks.json")
        self.landmarks = json.load(open(lm_path)) if os.path.exists(lm_path) else []

        self.n_cells = len(stations)
        self.chainage_m = [s["chainage_m"] for s in stations]
        self.chainage_label = [s.get("label") or _ch_label(s["chainage_m"]) for s in stations]
        self.lon = [s["lon"] for s in stations]
        self.lat = [s["lat"] for s in stations]
        self.real_width_m = [s["width_m"] for s in stations]
        self.reach_len_m = self.chainage_m[-1]
        self.mean_width = sum(self.real_width_m) / len(self.real_width_m)

        depth_path = os.path.join(d, "depth_profile.json")
        if os.path.exists(depth_path):
            with open(depth_path) as f:
                depth_stations = json.load(f)
            self.has_real_depth = len(depth_stations) == self.n_cells
        else:
            self.has_real_depth = False
        if self.has_real_depth:
            self.real_depth_m = [ds["depth_m"] for ds in depth_stations]
            self.depth_flagged = [bool(ds["flagged"]) for ds in depth_stations]
            self.mean_depth = sum(self.real_depth_m) / len(self.real_depth_m)
        else:
            self.real_depth_m = [0.0] * self.n_cells
            self.depth_flagged = [False] * self.n_cells
            self.mean_depth = 0.0

        self.survey = None
        sp = os.path.join(d, "survey.json")
        if os.path.exists(sp) and self.has_real_depth:
            with open(sp) as f:
                self.survey = json.load(f)
        self.params = {}
        pp = os.path.join(d, "params.json")
        if os.path.exists(pp):
            try:
                with open(pp) as f:
                    self.params = json.load(f)
            except Exception:
                self.params = {}
        self.localities = []
        lp = os.path.join(d, "localities.json")
        if os.path.exists(lp):
            try:
                with open(lp) as f:
                    self.localities = json.load(f)
            except Exception:
                self.localities = []
        self.meta_json = {}
        mp = os.path.join(d, "meta.json")
        if os.path.exists(mp):
            try:
                with open(mp) as f:
                    self.meta_json = json.load(f)
            except Exception:
                pass


# The three built-in rivers (Mula-Mutha, Mithi, Mandovi) are fed from the  Digital _twin_kml/<River>_river/  folders
# (river polygon KML, chainage KML, water-depth KML, locations Excel) — see river_sources.py, which builds/refreshes
# naditwin/rivers/<key>/ from those files at server start.
from .river_sources import BUILTIN_SOURCES as _BUILTIN_SOURCES

BUILTIN_RIVERS = {name: dict(key=cfg["key"], product_label=cfg["product_label"], city=cfg.get("city"),
                             folder=cfg.get("folder"))
                  for name, cfg in _BUILTIN_SOURCES.items()}
DEFAULT_RIVER = "Mula-Mutha"


def load_builtin_rivers():
    out = {}
    for name, cfg in BUILTIN_RIVERS.items():
        d = os.path.join(RIVERS_DIR, cfg["key"])
        if os.path.isfile(os.path.join(d, "chainage_profile.json")):
            out[name] = RiverData(key=cfg["key"], display_name=name, product_label=cfg["product_label"])
    return out


def _geometry_fingerprint(stations):
    """n stations + first/last point: two folders with the same fingerprint are the same river geometry."""
    if not stations:
        return None
    a, b = stations[0], stations[-1]
    return (len(stations), round(a["lon"], 5), round(a["lat"], 5), round(b["lon"], 5), round(b["lat"], 5))


def discover_uploaded_rivers(existing_keys, builtin_rivers=None):
    out = {}
    if not os.path.isdir(RIVERS_DIR):
        return out
    builtin_fp = set()
    for rd in (builtin_rivers or {}).values():
        builtin_fp.add((rd.n_cells, round(rd.lon[0], 5), round(rd.lat[0], 5), round(rd.lon[-1], 5), round(rd.lat[-1], 5)))
    for slug in sorted(os.listdir(RIVERS_DIR)):
        d = os.path.join(RIVERS_DIR, slug)
        meta_path = os.path.join(d, "meta.json")
        if slug in existing_keys or not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path) as f:
                m = json.load(f)
            if m.get("builtin"):
                continue
            # an earlier upload of a river that is now built in (e.g. "mithi river (2)" = the Mithi folder) is
            # skipped, so the same river is not listed twice
            try:
                with open(os.path.join(d, "chainage_profile.json")) as f:
                    if _geometry_fingerprint(json.load(f)) in builtin_fp:
                        continue
            except Exception:
                pass
            display_name = m.get("display_name") or slug.replace("_", " ").title()
            if display_name in BUILTIN_RIVERS:
                continue
            out[display_name] = RiverData(
                key=slug, display_name=display_name,
                product_label=f"NadiTwin — {display_name} (uploaded KML, {m.get('landmarks_source', 'auto')} landmarks)",
            )
        except Exception:
            continue
    return out


def load_all_rivers():
    rivers = load_builtin_rivers()
    existing_keys = {cfg["key"] for cfg in BUILTIN_RIVERS.values()}
    rivers.update(discover_uploaded_rivers(existing_keys, rivers))
    return rivers


# --------------------------------- engine ---------------------------------

class TwinEngine:
    def __init__(self, river: RiverData, seed=42, gauge_csv=None, live=True):
        self.river = river
        self.seed = seed
        self.gauge_csv = gauge_csv
        self.use_live = live
        self._lock = threading.Lock()
        self._mq_cache = {}
        self._qq_cache = {}
        self.live_status = {"mode": "synthetic", "note": "not fetched yet"}
        self.params = live_data.merged_params(river.params)
        self._build_segments()
        self._load_terrain()
        self._build_discharge(initial=True)
        self._build_assets()
        self._build_locations()

    # ------------------------- 1 km segmentation -------------------------

    def _build_segments(self):
        r = self.river
        step_m = SEG_KM * 1000.0
        self.seg_bounds_m = []
        start = 0.0
        while start < r.reach_len_m - 1e-6:
            self.seg_bounds_m.append((start, min(start + step_m, r.reach_len_m)))
            start += step_m
        if not self.seg_bounds_m:
            self.seg_bounds_m = [(0.0, r.reach_len_m)]
        # a last sliver shorter than 150 m is merged into the previous km
        if len(self.seg_bounds_m) > 1 and self.seg_bounds_m[-1][1] - self.seg_bounds_m[-1][0] < 150.0:
            a, _ = self.seg_bounds_m[-2]
            self.seg_bounds_m = self.seg_bounds_m[:-2] + [(a, r.reach_len_m)]
        self.n_seg = len(self.seg_bounds_m)
        self.seg_of_cell = [min(self.n_seg - 1, int(c // step_m)) for c in r.chainage_m]
        self.seg_len_km = [(b - a) / 1000.0 for a, b in self.seg_bounds_m]
        self.seg_mid_frac = [((a + b) / 2.0) / max(1.0, r.reach_len_m) for a, b in self.seg_bounds_m]
        self.seg_cells = [[] for _ in range(self.n_seg)]
        for i, s in enumerate(self.seg_of_cell):
            self.seg_cells[s].append(i)
        for s in range(self.n_seg):          # guarantee non-empty
            if not self.seg_cells[s]:
                mid = (self.seg_bounds_m[s][0] + self.seg_bounds_m[s][1]) / 2.0
                self.seg_cells[s] = [self._cell_for_chainage(mid)]
        self.seg_width = [sum(r.real_width_m[c] for c in cells) / len(cells) for cells in self.seg_cells]

    # ------------------------- terrain: slope, datum, flow direction, bank height -------------------------

    def _load_terrain(self):
        r = self.river
        self.dem = None
        try:
            self.dem = live_data.fetch_dem(r.chainage_m, r.lon, r.lat, r.real_width_m, r.dir, SEG_KM) if self.use_live \
                else None
        except Exception:
            self.dem = None
        fit = None
        if self.dem and len(self.dem.get("center", [])) >= 5:
            xs = [p[0] for p in self.dem["center"]]
            ys = [p[1] for p in self.dem["center"]]
            a, b, rmse, n_used = live_data.robust_line(xs, ys)
            fit = {"a": a, "b": b, "rmse": rmse, "n": n_used, "x_mid": sum(xs) / len(xs)}
        self.dem_fit = fit

        # flow direction: params/meta > DEM (water runs downhill) > assume chainage 0 = upstream
        fd = r.params.get("flow_dir", r.meta_json.get("flow_dir"))
        if fd in (1, -1):
            self.flow_dir, self.flow_dir_source = int(fd), "params/meta"
        elif fit and fit["b"] * r.reach_len_m > 1.0 and fit["b"] > 5e-5:
            self.flow_dir, self.flow_dir_source = -1, "DEM (elevation rises with chainage -> chainage 0 is downstream)"
        else:
            self.flow_dir, self.flow_dir_source = 1, ("DEM" if fit else "assumed (chainage 0 = upstream)")

        # slope
        if isinstance(r.params.get("slope"), (int, float)):
            self.slope, self.slope_source = float(r.params["slope"]), "params.json"
        elif fit and -self.flow_dir * fit["b"] > 0:
            self.slope = max(SLOPE_MIN, min(SLOPE_MAX, -self.flow_dir * fit["b"]))
            self.slope_source = f"DEM water-surface fit ({fit['n']} pts, rmse {fit['rmse']:.1f} m)"
        else:
            self.slope, self.slope_source = DEFAULT_SLOPE, "ASSUMED (no DEM)"

        # datum: survey-day / low-flow water surface line
        if fit:
            y_mid = fit["a"] + fit["b"] * fit["x_mid"]
            b_eff = -self.flow_dir * self.slope
            self._wse_line = lambda x, y=y_mid, xm=fit["x_mid"], be=b_eff: y + be * (x - xm)
            self.datum = "m WSE (water surface elevation; Copernicus GLO-90 DEM, +-2 m)"
            self.datum_short = "m WSE"
        else:
            L = r.reach_len_m
            self._wse_line = lambda x: -self.slope * (x if self.flow_dir > 0 else L - x)
            self.datum = "m, relative (0 = water surface at the upstream end)"
            self.datum_short = "m, relative"

        # bank height above that water surface, per km
        bh = r.params.get("bank_height_m")
        if isinstance(bh, (int, float)):
            self.bank_h_seg, self.bank_source = [float(bh)] * self.n_seg, "params.json"
        elif isinstance(bh, list) and len(bh) >= self.n_seg:
            self.bank_h_seg, self.bank_source = [float(v) for v in bh[:self.n_seg]], "params.json (per km)"
        elif self.dem and self.dem.get("banks"):
            raw = []
            for s in range(self.n_seg):
                b = self.dem["banks"][s] if s < len(self.dem["banks"]) else {"left": [], "right": []}
                sides = [_pct(b[k], BANK_PCTL) for k in ("left", "right") if b.get(k)]
                mid = (self.seg_bounds_m[s][0] + self.seg_bounds_m[s][1]) / 2.0
                raw.append(None if not sides else min(sides) - self._wse_line(mid))
            good = [v for v in raw if v is not None]
            fill = sorted(good)[len(good) // 2] if good else BANK_ABOVE_SURVEY_WSE_M
            raw = [max(BANK_MIN_M, min(BANK_MAX_M, v if v is not None else fill)) for v in raw]
            sm = []
            for s in range(self.n_seg):           # 3-km running median against DSM speckle (buildings/trees)
                w = sorted(raw[max(0, s - 1):s + 2])
                sm.append(w[len(w) // 2])
            self.bank_h_seg = sm
            self.bank_source = (f"DEM-estimated (lower bank, {BANK_PCTL*100:.0f}th percentile of GLO-90 samples beyond each water edge, "
                                f"clamped {BANK_MIN_M:g}-{BANK_MAX_M:g} m) — replace with surveyed embankment levels")
        else:
            self.bank_h_seg = [BANK_ABOVE_SURVEY_WSE_M] * self.n_seg
            self.bank_source = f"ASSUMED {BANK_ABOVE_SURVEY_WSE_M:g} m (no DEM)"

    # ------------------------- discharge (live or synthetic) -------------------------

    def _n_hours(self):
        now_idx = hours_since_epoch(utc_now())
        return max(now_idx + FUTURE_BUFFER_H, PAST_WINDOW_H + FORECAST_HOURS + 4)

    def _gauge_series(self, n_h):
        if not (self.gauge_csv and os.path.exists(self.gauge_csv)):
            return None
        try:
            vals = []
            with open(self.gauge_csv, newline="") as f:
                for row in csv.reader(f):
                    if not row:
                        continue
                    try:
                        vals.append(float(row[-1]))
                    except ValueError:
                        continue
            if len(vals) < 24:
                return None
            vals = vals[:n_h]
            while len(vals) < n_h:
                vals.append(vals[-1])
            return vals
        except Exception:
            return None

    def _synthetic_upstream(self, n_h):
        """offline fallback only — dry-season ~9 m3/s, monsoon-seasoned storm pulses (shape of a GloFAS year).
        Each pulse is added only within +-5 sigma of its peak, so years of hourly data build in well under a second."""
        rng = random.Random(self.seed + 2)
        pulses, t = [], 20
        while t < n_h:
            pulses.append((t, rng.uniform(40, 700), rng.uniform(10, 30)))
            t += int(rng.uniform(60, 160))
        q = array("d", bytes(8 * n_h))
        day0 = EPOCH
        for d in range(n_h // 24 + 1):
            month = (day0 + timedelta(days=d)).month
            base = 9.0 if month not in (6, 7, 8, 9, 10) else (60.0 if month in (7, 8) else 25.0)
            for h in range(d * 24, min(n_h, d * 24 + 24)):
                q[h] = base
        for (pt, amp, w) in pulses:
            pm = (EPOCH + timedelta(hours=pt)).month
            a = amp * (1.0 if pm in (6, 7, 8, 9) else 0.02)
            span = int(5 * w) + 1
            for h in range(max(0, pt - span), min(n_h, pt + span + 1)):
                q[h] += a * math.exp(-0.5 * ((h - pt) / w) ** 2)
        return [max(2.0, v) for v in q]

    def _flow_segments(self):
        order = range(self.n_seg) if self.flow_dir > 0 else range(self.n_seg - 1, -1, -1)
        return [{"i": s, "mid_frac": self.seg_mid_frac[s], "len_km": self.seg_len_km[s], "width_m": self.seg_width[s]}
                for s in order]

    def _build_discharge(self, initial=False, force=False):
        """(Re)build the chainage-wise discharge components. Tries live data first; falls back to synthetic inflow."""
        r = self.river
        n_h = self._n_hours()
        gauge = self._gauge_series(n_h)
        stations = list(zip(r.lon, r.lat))
        ld, err = None, None
        if self.use_live:
            try:
                ld = live_data.fetch_live(stations, EPOCH, n_h, r.dir, r.key, force=force, flow_dir=self.flow_dir)
            except Exception as e:
                err = str(e)
        live_ok = ld is not None
        if not live_ok:
            ld = live_data.synthetic_live(stations, EPOCH, n_h, self._synthetic_upstream(n_h))
        mid = r.n_cells // 2
        comp, info = live_data.compute_discharge(ld, self._flow_segments(), self.slope, r.params, gauge_up=gauge,
                                                 utc_offset_h=_utc_offset_h(r.lon[mid], r.lat[mid]))
        if live_ok:
            live = {"mode": "live", "fetched_at": ld.fetched_at, "sources": ld.status,
                    "q_up_source": info["q_up_source"], "glofas_cells": ld.glofas_cells,
                    "model": {k: v for k, v in info["params"].items()}}
            if "note" in ld.status:
                live["note"] = ld.status["note"]
            q_source = ("LIVE: rain/weather = Open-Meteo (ERA5 archive + forecast); upstream inflow = "
                        + info["q_up_source"] + f"; discharge every {SEG_KM:g} km = routed inflow + storm runoff + "
                        "urban dry-weather inflow - evaporation")
        else:
            live = {"mode": "synthetic", "note": ("no internet / Open-Meteo unreachable" + (f" ({err})" if err else "")
                                                   + " — upstream inflow is SYNTHETIC, rain = 0. NOT real data.")}
            q_source = ("user_csv:" + os.path.basename(self.gauge_csv)) if gauge is not None else \
                "synthetic (live feed unreachable)"
        # inflow series of every km (= outflow of the km upstream of it, or the boundary inflow) and each 10 m
        # station's position along its km in the FLOW direction -> discharge at every station (see _q_cell_h)
        order = [sg["i"] for sg in self._flow_segments()]
        seg_qin = [None] * self.n_seg
        prev = array("d", info["q_up"])
        for s_ in order:
            seg_qin[s_] = prev
            prev = comp["q"][s_]
        cell_frac = []
        for i, c in enumerate(r.chainage_m):
            a, b = self.seg_bounds_m[self.seg_of_cell[i]]
            f = (c - a) / max(1e-6, b - a) if self.flow_dir > 0 else (b - c) / max(1e-6, b - a)
            cell_frac.append(max(0.0, min(1.0, f)))
        with self._lock:
            self.n_hours = n_h
            self.comp = comp
            self.seg_qin = seg_qin
            self.lag_seg = info["lag_h_seg"]
            self.cell_frac = cell_frac
            self.q_seg = comp["q"]
            self.live_info = info
            self.live_raw = ld if live_ok else None
            self.ld = ld
            self.live_status = live
            self.q_source = q_source
            self._mq_cache = {}
            self._qq_cache = {}
            self._calibrate_reference()
            self._build_geometry()
        return live

    def refresh_live(self, force=True):
        """Called by the server's background thread (and /api/refresh_live)."""
        if not self.use_live:
            return self.live_status
        return self._build_discharge(force=force)

    def history_missing(self):
        """True while part of the history since EPOCH (ERA5 years / GloFAS history) is not downloaded yet."""
        src = (self.live_status or {}).get("sources") or {}
        return bool(src.get("history_years_missing")) or bool(src.get("archive_refresh_due")) \
            or "still downloading" in str(src.get("glofas", "")) or self.live_status.get("mode") != "live"

    def backfill_history(self, log=None):
        """Download the missing history (past ERA5 years + GloFAS since EPOCH), then rebuild the discharge so the
        historic view covers it. Returns the number of downloads (0 = complete already, or offline)."""
        if not self.use_live:
            return 0
        r = self.river
        got = live_data.backfill_history(list(zip(r.lon, r.lat)), EPOCH, r.dir, r.key, flow_dir=self.flow_dir,
                                         log=log)
        if got:
            self.refresh_live(force=True)
        return got

    def _calibrate_reference(self):
        """discharge in each km on the depth-survey day (Sentinel-2 overpass) -> Manning baseline for the stage
        calibration. Without a survey: the low-flow (20th percentile, past year) discharge, which is the flow the DEM
        water surface most likely shows."""
        r = self.river
        idx = None
        if r.survey and r.survey.get("image_date"):
            try:
                d = datetime.strptime(r.survey["image_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc) + \
                    timedelta(hours=SENTINEL2_UTC_HOUR)
                idx = hours_since_epoch(d)
            except Exception:
                idx = None
        live = self.live_status.get("mode") == "live"
        if idx is not None and 0 <= idx < self.n_hours and live:
            self.q_ref_seg = [max(0.5, self.q_seg[s][idx]) for s in range(self.n_seg)]
            self.q_ref_note = (f"surveyed depth (Sentinel-2, {r.survey['image_date']}) = water depth at the live discharge "
                               f"of that day ({self.q_ref_seg[0]:.1f}–{self.q_ref_seg[-1]:.1f} m³/s); stage moves with "
                               f"live discharge from there")
        elif r.has_real_depth:
            self.q_ref_seg = [Q_REF_DEFAULT_M3S] * self.n_seg
            self.q_ref_note = f"assumed {Q_REF_DEFAULT_M3S:g} m³/s on the survey day (no live series for that date)"
        else:
            now_i = min(self.n_hours - 1, hours_since_epoch(utc_now()))
            lo = max(0, now_i - 365 * 24)
            self.q_ref_seg = [max(0.5, _pct(list(self.q_seg[s][lo:now_i + 1]) or [Q_REF_DEFAULT_M3S], 0.2))
                              for s in range(self.n_seg)]
            self.q_ref_note = ("no depth survey: depth = Manning depth of the live discharge with the real width; "
                               "DEM water surface taken at low flow (20th percentile of the past year)")

    # ------------------------- geometry / bathymetry -------------------------

    def _mdepth(self, q, width_m):
        return live_data.manning_depth(q, width_m, self.slope, self.params["manning_n"])

    def _mdepth_cell(self, q, i):
        """Manning depth at station i with a compound section: rectangular main channel (hydraulic width) up to the
        bank crest, then the extra flow spreads over a floodplain FLOODPLAIN_FACTOR x wider (stage rises slower)."""
        w = self.hyd_w[i]
        h = self._mdepth(q, w)
        hb = self.hb_m[i]
        if h <= hb:
            return h
        k = math.sqrt(self.slope) / self.params["manning_n"]
        qb = k * w * hb ** (5.0 / 3.0)
        return hb + ((q - qb) / (k * w * self.fp_factor)) ** 0.6

    def _build_geometry(self):
        """water surface on the reference day (DEM line, or relative datum) = wse_ref
           bed   = wse_ref - reference depth (surveyed Sentinel-2 depth, or Manning depth of the reference flow)
           stage = bed + reference depth + [Manning depth(Q) - Manning depth(Q_ref)]  -> equals wse_ref at Q_ref
           crest = wse_ref + bank height (DEM per km, params.json, or assumed)"""
        r = self.river
        self.fp_factor = float(r.params.get("floodplain_width_factor", FLOODPLAIN_FACTOR))
        # hydraulic width: the water surface cannot jump at every local pinch of a hand-traced polygon
        med_w = sorted(r.real_width_m)[r.n_cells // 2]
        half = HYD_WIDTH_WINDOW_M / 2.0
        ch, W = r.chainage_m, r.real_width_m
        pre = [0.0]
        for v in W:
            pre.append(pre[-1] + v)
        self.hyd_w = []
        lo = hi = 0
        for i in range(r.n_cells):
            while ch[lo] < ch[i] - half:
                lo += 1
            while hi + 1 < r.n_cells and ch[hi + 1] <= ch[i] + half:
                hi += 1
            self.hyd_w.append(max(0.5 * med_w, (pre[hi + 1] - pre[lo]) / (hi + 1 - lo)))
        self.wse_ref, self.bed, self.crest, self.d_ref, self.h_ref, self.sigma_bed, self.hb_m = [], [], [], [], [], [], []
        for i in range(r.n_cells):
            x = r.chainage_m[i]
            s = self.seg_of_cell[i]
            w = self.hyd_w[i]
            wr = self._wse_line(x)
            h_ref = self._mdepth(self.q_ref_seg[s], w)
            self.hb_m.append(self.bank_h_seg[s] + h_ref)          # Manning depth that puts the water at the crest
            d_ref = r.real_depth_m[i] if r.has_real_depth else h_ref
            self.wse_ref.append(wr)
            self.h_ref.append(h_ref)
            self.d_ref.append(d_ref)
            self.bed.append(wr - d_ref)
            self.crest.append(wr + self.bank_h_seg[s])
            edge_frac = min(x, r.reach_len_m - x) / max(1.0, r.reach_len_m / 2.0)
            sg = 0.6 - 0.4 * edge_frac if not r.has_real_depth else 0.25
            if r.has_real_depth and r.depth_flagged[i]:
                sg = 0.5
            self.sigma_bed.append(round(max(0.12, sg), 3))

    def _depth_from_q(self, q, cell):
        return max(0.05, self.d_ref[cell] + self._mdepth_cell(q, cell) - self.h_ref[cell])

    def wse_at(self, q, cell):
        return self.bed[cell] + self._depth_from_q(q, cell)

    def threshold(self, cell):
        return self.crest[cell] - FREEBOARD_M

    def levels(self, cell):
        t = self.threshold(cell)
        return {"watch": round(t - ZONE_WATCH_M, 2), "warning": round(t - ZONE_WARNING_M, 2), "danger": round(t, 2),
                "crest": round(self.crest[cell], 2), "bed": round(self.bed[cell], 2),
                "survey_wse": round(self.wse_ref[cell], 2)}

    def q_for_stage(self, cell, stage):
        """invert the (monotonic) rating curve: discharge that puts the water surface at `stage`."""
        lo, hi = 0.0, 1.0
        while self.wse_at(hi, cell) < stage and hi < 1e6:
            hi *= 2.0
        if hi >= 1e6:
            return None
        for _ in range(50):
            m = (lo + hi) / 2.0
            if self.wse_at(m, cell) < stage:
                lo = m
            else:
                hi = m
        return (lo + hi) / 2.0

    # ------------------------------- assets -------------------------------

    def _build_assets(self):
        r = self.river
        self.assets = []
        if r.landmarks:
            ordered = sorted(r.landmarks, key=lambda lm: lm["chainage_m"])
            for i, lm in enumerate(ordered):
                x = lm["chainage_m"]
                cell = self._cell_for_chainage(x)
                self.assets.append({
                    "id": f"A{i+1}", "name": lm["name"], "locality": lm["name"],
                    "type": lm.get("type", "locality"), "source": lm.get("source", "auto"),
                    "chainage_m": round(x, 1), "lon": r.lon[cell], "lat": r.lat[cell],
                    "threshold": round(self.threshold(cell), 2),
                })
        else:
            for aid, name, f in [("A1", "Upstream reach", 0.10), ("A2", "Mid reach", 0.50), ("A3", "Downstream reach", 0.90)]:
                x = r.reach_len_m * f
                cell = self._cell_for_chainage(x)
                self.assets.append({"id": aid, "name": name, "type": "locality", "source": "auto",
                                    "chainage_m": round(x, 1), "lon": r.lon[cell], "lat": r.lat[cell],
                                    "threshold": round(self.threshold(cell), 2)})

    def _cell_for_chainage(self, x_m):
        ch = self.river.chainage_m
        n = len(ch)
        if n < 2:
            return 0
        step = (ch[-1] - ch[0]) / (n - 1)
        i = int(round((x_m - ch[0]) / step)) if step > 0 else 0
        i = max(0, min(n - 1, i))
        lo, hi = max(0, i - 3), min(n - 1, i + 3)
        return min(range(lo, hi + 1), key=lambda k: abs(ch[k] - x_m))

    # -------------------------------- time index --------------------------------

    def _max_idx(self):
        return self.n_hours - FORECAST_HOURS - 2

    def idx_for(self, dt=None):
        if dt is None:
            dt = utc_now()
        return max(0, min(self._max_idx(), hours_since_epoch(dt)))

    def live_idx_and_frac(self):
        now = utc_now()
        total_h = (now - EPOCH).total_seconds() / 3600.0
        idx = max(0, min(self._max_idx(), int(total_h)))
        frac = 0.0 if idx == self._max_idx() else max(0.0, min(1.0, total_h - int(total_h)))
        return idx, frac

    def q_seg_at(self, s, dt=None):
        """discharge in segment s. dt=None -> live, interpolated to the current minute."""
        if dt is None:
            idx, frac = self.live_idx_and_frac()
            a = self.q_seg[s][idx]
            b = self.q_seg[s][min(idx + 1, self.n_hours - 1)]
            return a + (b - a) * frac, idx
        idx = self.idx_for(dt)
        return self.q_seg[s][idx], idx

    # ------------------------- discharge at every chainage station (10 m) -------------------------
    # Inside one km the lateral inflow (storm runoff + urban inflow - evaporation) is spread evenly along the channel and
    # the inflow wave needs a fraction f of the km's travel time to reach a station at fraction f of the km:
    #     Q(station, t) = Qin(t - f*lag) + f * [Q_km_out(t) - Qin(t - lag)]
    # -> equals the km's inflow at its upstream boundary and the km's outflow at its downstream boundary, and changes
    #    smoothly at every station in between.

    def _q_cell_h(self, cell, t):
        s = self.seg_of_cell[cell]
        f = self.cell_frac[cell]
        qin = self.seg_qin[s]
        t = max(0, min(self.n_hours - 1, t))
        lag = self.lag_seg[s][t]
        a = self._interp(qin, t - f * lag)
        b = self._interp(qin, t - lag)
        return max(Q_MIN_FC, a + f * (self.q_seg[s][t] - b))

    def q_cell_at(self, cell, dt=None):
        """discharge at one station; dt=None -> live, interpolated to the current minute."""
        if dt is None:
            idx, frac = self.live_idx_and_frac()
            a = self._q_cell_h(cell, idx)
            b = self._q_cell_h(cell, min(idx + 1, self.n_hours - 1))
            return a + (b - a) * frac, idx
        idx = self.idx_for(dt)
        return self._q_cell_h(cell, idx), idx

    def q_stations(self, dt=None):
        idx = None
        out = []
        for c in range(self.river.n_cells):
            q, idx = self.q_cell_at(c, dt)
            out.append(q)
        return out, idx

    def _ratio(self, cell, t):
        """station discharge / km-outflow discharge at hour t (used to carry the km forecast to a station)."""
        s = self.seg_of_cell[cell]
        t = max(0, min(self.n_hours - 1, t))
        return self._q_cell_h(cell, t) / max(1e-6, self.q_seg[s][t])

    @staticmethod
    def _interp(arr, pos):
        n = len(arr)
        i0 = max(0, min(n - 2, int(pos)))
        f = min(1.0, max(0.0, pos - i0))
        return arr[i0] * (1 - f) + arr[i0 + 1] * f

    # ------------------------------- forecasting -------------------------------
    # Forecast = the live rainfall + GloFAS forecast already baked into the discharge components for future hours,
    # plus a 50-member ensemble that perturbs each component by its own uncertainty:
    #   upstream inflow  : GloFAS ensemble spread ((p75-p25)/1.349/median) when available, else 2 % -> 12 % with lead
    #   storm runoff     : rain-forecast error 25 % -> 60 % with lead, plus +-2 h timing error
    #   urban inflow     : 15 %
    # so the P10-P90 band is narrow in dry weather and widens when rain or a GloFAS flood is forecast.

    def _member_q(self, idx):
        c = self._mq_cache.get(idx)
        if c is not None:
            return c
        spread = getattr(self.ld, "glofas_spread", None) or []
        fetched_idx = self.ld.now_idx if self.live_raw is not None else 10 ** 9
        mem = []
        for m in range(N_MEMBERS):
            rng = random.Random(self.seed * 1000 + idx * 7 + m)
            mem.append((rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 2.0),
                        [rng.gauss(0, 0.004) for _ in range(FORECAST_HOURS)]))
        out = []
        for s in range(self.n_seg):
            up, st, dw, ev = self.comp["up"][s], self.comp["storm"][s], self.comp["dwf"][s], self.comp["evap"][s]
            rows = []
            for (z_up, z_rain, z_dwf, dt_h, rw) in mem:
                walk, row = 0.0, []
                for k in range(FORECAST_HOURS):
                    t = idx + k + 1
                    lead = (k + 1) / FORECAST_HOURS
                    sp = spread[t] if (t < len(spread) and spread[t] is not None and t >= fetched_idx - 24) else None
                    sd_up = max(0.01, sp) if sp is not None else 0.02 + 0.10 * lead
                    sd_rain = 0.25 + 0.35 * lead
                    walk += rw[k]
                    q = (up[t] * (1 + z_up * sd_up)
                         + self._interp(st, t + dt_h) * max(0.0, 1 + z_rain * sd_rain)
                         + dw[t] * (1 + 0.15 * z_dwf) - ev[t]) * (1 + walk)
                    row.append(max(Q_MIN_FC, q))
                rows.append(row)
            out.append(rows)
        if len(self._mq_cache) > 8:
            self._mq_cache.clear()
        self._mq_cache[idx] = out
        return out

    def _qq(self, idx):
        """quantiles per segment per lead: qq[s][k] = (p10, p50, p90)"""
        c = self._qq_cache.get(idx)
        if c is not None:
            return c
        mq = self._member_q(idx)
        out = []
        for s in range(self.n_seg):
            row = []
            for k in range(FORECAST_HOURS):
                vals = [mq[s][m][k] for m in range(N_MEMBERS)]
                row.append((_pct(vals, 0.1), _pct(vals, 0.5), _pct(vals, 0.9)))
            out.append(row)
        if len(self._qq_cache) > 8:
            self._qq_cache.clear()
        self._qq_cache[idx] = out
        return out

    def q_quantiles(self, idx, k, cell):
        return self._qq(idx)[self.seg_of_cell[cell]][k]

    # ------------------------------- profiles -------------------------------

    def wse_profile_seg(self, qseg):
        return [self.wse_at(qseg[self.seg_of_cell[i]], i) for i in range(self.river.n_cells)]

    def state_now(self, dt=None):
        at = dt or utc_now()
        qs = [self.q_seg_at(s, dt)[0] for s in range(self.n_seg)]
        qst, idx = self.q_stations(dt)
        wse = [self.wse_at(qst[i], i) for i in range(self.river.n_cells)]
        depth = [round(wse[i] - self.bed[i], 2) for i in range(self.river.n_cells)]
        up_c = 0 if self.flow_dir > 0 else self.river.n_cells - 1
        out_c = self.river.n_cells - 1 - up_c
        return {
            "q_now": round(qst[up_c], 1),
            "q_up_now": round(qst[up_c], 1),
            "q_out_now": round(qst[out_c], 1),
            "q_seg": [round(v, 2) for v in qs],
            "q_station": [round(v, 3) for v in qst],
            "seg_bounds_km": [[round(a / 1000, 3), round(b / 1000, 3)] for a, b in self.seg_bounds_m],
            "wse": [round(v, 3) for v in wse],
            "depth": depth,
            "datum": self.datum,
            "flow_dir": self.flow_dir,
            "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hour_index": idx,
            "live": dt is None,
            "mode": self.live_status.get("mode"),
            "note": self.live_status.get("note"),
        }

    def observed_hydrograph(self, cell, dt=None, window=PAST_WINDOW_H):
        idx = self.idx_for(dt)
        lo = max(0, idx - window + 1)
        q = [self._q_cell_h(cell, h) for h in range(lo, idx + 1)]
        return [round(self.wse_at(v, cell), 3) for v in q], [round(v, 2) for v in q]

    def forecast_hydrograph(self, cell, dt=None):
        idx = self.idx_for(dt)
        s = self.seg_of_cell[cell]
        qq = self._qq(idx)[s]
        out = {"median": [], "p10": [], "p90": [], "q_median": [], "q_p10": [], "q_p90": []}
        for k in range(FORECAST_HOURS):
            r = self._ratio(cell, idx + k + 1)
            q10, q50, q90 = (v * r for v in qq[k])
            out["p10"].append(round(self.wse_at(q10, cell), 3))
            out["median"].append(round(self.wse_at(q50, cell), 3))
            out["p90"].append(round(self.wse_at(q90, cell), 3))
            out["q_p10"].append(round(q10, 2)); out["q_median"].append(round(q50, 2)); out["q_p90"].append(round(q90, 2))
        return out

    def hydrograph(self, cell, dt=None, hours=PAST_WINDOW_H, year=None):
        """Discharge + stage hydrograph (with the rainfall hyetograph) at one station:
        observed for `hours` back (downsampled to <= MAX_HYDRO_POINTS bins, keeping each bin's PEAK) + 72 h forecast.
        year=2021 instead shows that whole calendar year (1 Jan 00:00 -> 31 Dec 23:00 UTC; the running year up to now
        + the 72 h forecast) and adds a summary of the year (peak discharge, peak stage, rain, hours above each level)."""
        if year is not None:
            return self._year_hydrograph(cell, int(year))
        idx = self.idx_for(dt)
        s = self.seg_of_cell[cell]
        hours = max(6, min(int(hours), idx + 1))
        lo = idx - hours + 1
        step = max(1, int(math.ceil(hours / MAX_HYDRO_POINTS)))
        rain = (self.live_info or {}).get("rain_seg")
        rain = rain[s] if rain and self.live_raw is not None else None
        times, q_obs, st_obs, r_obs = [], [], [], []
        for b0 in range(lo, idx + 1, step):
            b1 = min(idx + 1, b0 + step)
            qs = [self._q_cell_h(cell, h) for h in range(b0, b1)]
            qmax = max(qs)
            times.append(iso_h(b0))
            q_obs.append(round(qmax, 2))
            st_obs.append(round(self.wse_at(qmax, cell), 3))
            r_obs.append(round(sum(rain[b0:b1]), 2) if rain else None)
        fc = self.forecast_hydrograph(cell, dt)
        fc["times"] = [iso_h(idx + k + 1) for k in range(FORECAST_HOURS)]
        fc["rain"] = [round(rain[idx + k + 1], 2) for k in range(FORECAST_HOURS)] if rain else None
        return {"cell": cell, "chainage_km": round(self.river.chainage_m[cell] / 1000.0, 3), "segment": s + 1,
                "label": self.river.chainage_label[cell], "step_h": step, "hours": hours, "times": times, "observed": st_obs, "observed_q": q_obs,
                "observed_rain": r_obs, "forecast": fc, "levels": self.levels(cell), "datum": self.datum_short,
                "q_danger": self._r(self.q_for_stage(cell, self.threshold(cell)), 1)}

    def _year_hydrograph(self, cell, year):
        now_idx = self.idx_for(None)
        y0 = hours_since_epoch(datetime(year, 1, 1, tzinfo=timezone.utc))
        y1 = hours_since_epoch(datetime(year + 1, 1, 1, tzinfo=timezone.utc)) - 1
        lo, idx = max(0, y0), min(y1, now_idx)
        if idx < lo:
            raise ValueError(f"no data for {year}: the record runs from {EPOCH:%d %b %Y} to today")
        running = idx == now_idx            # the current year: up to now, followed by the 72 h forecast
        s = self.seg_of_cell[cell]
        hours = idx - lo + 1
        step = max(1, int(math.ceil(hours / MAX_HYDRO_POINTS)))
        rain = (self.live_info or {}).get("rain_seg")
        rain = rain[s] if rain and self.live_raw is not None else None
        lv = self.levels(cell)
        q_lv = {k: self.q_for_stage(cell, lv[k]) for k in ("watch", "warning", "danger")}
        times, q_obs, st_obs, r_obs = [], [], [], []
        peak_q, peak_h, rain_tot = -1.0, lo, 0.0
        above = {"watch": 0, "warning": 0, "danger": 0}
        for b0 in range(lo, idx + 1, step):
            b1 = min(idx + 1, b0 + step)
            qs = [self._q_cell_h(cell, h) for h in range(b0, b1)]
            for k, qv in enumerate(qs):
                if qv > peak_q:
                    peak_q, peak_h = qv, b0 + k
                for name in above:
                    if q_lv[name] is not None and qv >= q_lv[name]:
                        above[name] += 1
            qmax = max(qs)
            times.append(iso_h(b0))
            q_obs.append(round(qmax, 2))
            st_obs.append(round(self.wse_at(qmax, cell), 3))
            if rain:
                rs = sum(rain[b0:b1])
                rain_tot += rs
                r_obs.append(round(rs, 2))
            else:
                r_obs.append(None)
        if running:
            fc = self.forecast_hydrograph(cell, None)
            fc["times"] = [iso_h(idx + k + 1) for k in range(FORECAST_HOURS)]
            fc["rain"] = [round(rain[idx + k + 1], 2) for k in range(FORECAST_HOURS)] if rain else None
        else:
            fc = {k: [] for k in ("median", "p10", "p90", "q_median", "q_p10", "q_p90", "times")}
            fc["rain"] = []
        summary = {"year": year, "from": iso_h(lo), "to": iso_h(idx), "complete": not running,
                   "peak_q_m3s": round(peak_q, 2), "peak_at": iso_h(peak_h),
                   "peak_stage": round(self.wse_at(peak_q, cell), 3),
                   "rain_total_mm": round(rain_tot, 1) if rain else None,
                   "hours_above": above, "q_at_level": {k: (round(v, 1) if v is not None else None) for k, v in q_lv.items()}}
        return {"cell": cell, "chainage_km": round(self.river.chainage_m[cell] / 1000.0, 3), "segment": s + 1,
                "label": self.river.chainage_label[cell], "step_h": step, "hours": hours, "year": year,
                "times": times, "observed": st_obs, "observed_q": q_obs, "observed_rain": r_obs, "forecast": fc,
                "levels": lv, "datum": self.datum_short, "year_summary": summary,
                "q_danger": self._r(q_lv["danger"], 1)}

    @staticmethod
    def _r(v, nd):
        return None if v is None else round(v, nd)

    def rating(self, cell, dt=None):
        """stage-discharge (rating) curve at one station, with the zone levels and the discharge that reaches each."""
        s = self.seg_of_cell[cell]
        q_now, idx = self.q_cell_at(cell, dt)
        qmax = max(3000.0, 1.3 * max(self.q_seg[s]))
        qmin = 0.5
        n = 70
        qs = [qmin * (qmax / qmin) ** (k / (n - 1)) for k in range(n)]
        lv = self.levels(cell)
        qq = self._qq(idx)[s]
        q_pk = max(v[1] * self._ratio(cell, idx + k + 1) for k, v in enumerate(qq))
        return {"cell": cell, "label": self.river.chainage_label[cell], "chainage_km": round(self.river.chainage_m[cell] / 1000.0, 3), "segment": s + 1,
                "width_m": self.river.real_width_m[cell], "hydraulic_width_m": round(self.hyd_w[cell], 1),
                "floodplain_factor": self.fp_factor, "q": [round(v, 2) for v in qs],
                "stage": [round(self.wse_at(v, cell), 3) for v in qs],
                "depth": [round(self._depth_from_q(v, cell), 3) for v in qs],
                "levels": lv, "datum": self.datum_short,
                "q_at": {k: self._r(self.q_for_stage(cell, lv[k]), 1) for k in ("watch", "warning", "danger", "crest")},
                "now": {"q": round(q_now, 2), "stage": round(self.wse_at(q_now, cell), 3)},
                "peak72": {"q": round(q_pk, 2), "stage": round(self.wse_at(q_pk, cell), 3)},
                "manning_n": self.params["manning_n"], "slope": self.slope}

    def forecast_profile(self, lead_h, dt=None):
        idx = self.idx_for(dt)
        k = max(0, min(FORECAST_HOURS - 1, lead_h - 1))
        qq = self._qq(idx)
        t = idx + k + 1
        med, p10, p90, qm = [], [], [], []
        for i in range(self.river.n_cells):
            s = self.seg_of_cell[i]
            r = self._ratio(i, t)
            q10, q50, q90 = (v * r for v in qq[s][k])
            p10.append(round(self.wse_at(q10, i), 3))
            med.append(round(self.wse_at(q50, i), 3))
            p90.append(round(self.wse_at(q90, i), 3))
            qm.append(round(q50, 3))
        return {"lead_h": lead_h, "median": med, "p10": p10, "p90": p90, "q_median_station": qm,
                "q_median_seg": [round(qq[s][k][1], 2) for s in range(self.n_seg)]}

    def discharge_profile(self, dt=None):
        """Chainage-wise discharge every 1 km: now (with its components) and +6/+12/+24/+48/+72 h (P10/median/P90)."""
        idx = self.idx_for(dt)
        qq = self._qq(idx)
        leads = [6, 12, 24, 48, 72]
        rows = []
        for s in range(self.n_seg):
            q_now, _ = self.q_seg_at(s, dt)
            c = {k: round(self.comp[k][s][idx], 3) for k in ("up", "storm", "dwf", "evap")}
            lag = (self.live_info or {}).get("lag_h_seg")
            row = {"segment": s + 1, "from_km": round(self.seg_bounds_m[s][0] / 1000, 3),
                   "to_km": round(self.seg_bounds_m[s][1] / 1000, 3), "q_now": round(q_now, 2),
                   "components": {"upstream_inflow": c["up"], "storm_runoff": c["storm"], "urban_inflow": c["dwf"],
                                  "evaporation": -c["evap"]},
                   "travel_time_h": round(lag[s][idx], 2) if lag else None,
                   "forecast": {str(L): {"p10": round(qq[s][L - 1][0], 2), "p50": round(qq[s][L - 1][1], 2),
                                         "p90": round(qq[s][L - 1][2], 2)} for L in leads},
                   "q_peak_72h_median": round(max(qq[s][k][1] for k in range(FORECAST_HOURS)), 2)}
            rs = self._rain_stats(s, idx)
            if rs:
                row.update(rs)
            rows.append(row)
        return rows

    def _rain_stats(self, s, idx):
        rain = (self.live_info or {}).get("rain_seg")
        if not rain or self.live_raw is None:
            return None
        r = rain[s]
        n = len(r)

        def tot(a, b):
            return round(sum(r[max(0, a):min(n, b)]), 1)
        cr = self.live_info["runoff_coeff_seg"][s][min(n - 1, idx)]
        return {"rain_now_mmh": round(r[min(n - 1, idx)], 2), "rain_past_24h_mm": tot(idx - 23, idx + 1),
                "rain_next_24h_mm": tot(idx + 1, idx + 25), "rain_next_72h_mm": tot(idx + 1, idx + 73),
                "runoff_coeff": round(cr, 2)}

    def terrain_summary(self):
        return {"datum": self.datum, "slope_m_per_m": round(self.slope, 6), "slope_source": self.slope_source,
                "flow_dir": self.flow_dir, "flow_dir_source": self.flow_dir_source,
                "upstream_end": "chainage 0" if self.flow_dir > 0 else f"chainage {self.river.reach_len_m/1000:.2f} km",
                "bank_height_m_per_km": [round(v, 2) for v in self.bank_h_seg], "bank_source": self.bank_source,
                "dem": (self.dem or {}).get("source") if self.dem else "unreachable (assumed values used)",
                "dem_fit_rmse_m": round(self.dem_fit["rmse"], 2) if self.dem_fit else None}

    def live_summary(self, dt=None):
        idx = self.idx_for(dt)
        out = {"status": self.live_status, "q_source": self.q_source, "calibration": self.q_ref_note,
               "terrain": self.terrain_summary(),
               "assumptions": {"manning_n": self.params["manning_n"], "bed_slope_m_per_m": round(self.slope, 6),
                               "slope_source": self.slope_source,
                               "bank_source": self.bank_source,
                               "urban_dwf_m3s_per_km": self.params["dwf_m3s_per_km"],
                               "local_catchment_km2_per_km": self.params["area_km2_per_km"],
                               "note": "Dam releases (e.g. Khadakwasla) are NOT modelled; GloFAS does not see them."},
               "at": (dt or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")}
        ld = self.live_raw
        if ld is not None:
            n = ld.n_h

            def tot(a, b):
                return round(sum(sum(r[h] for r in ld.rain) / len(ld.rain) for h in range(max(0, a), min(n, b))), 1)
            i = min(n - 1, idx)
            et = sum(r[i] for r in ld.et0) / len(ld.et0)
            out["weather"] = {"temperature_c": round(ld.temp[i], 1), "humidity_pct": round(ld.rh[i]),
                              "wind_kmh": round(ld.wind[i], 1), "et0_mmh": round(et, 3),
                              "rain_now_mmh": round(sum(r[i] for r in ld.rain) / len(ld.rain), 2),
                              "rain_past_24h_mm": tot(i - 23, i + 1), "rain_next_24h_mm": tot(i + 1, i + 25),
                              "rain_next_72h_mm": tot(i + 1, i + 73), "rain_past_7d_mm": tot(i - 167, i + 1),
                              "rain_next_7d_mm": tot(i + 1, i + 169)}
            if ld.q_out_glofas is not None and ld.q_out_glofas[i] is not None:
                out_s = self.n_seg - 1 if self.flow_dir > 0 else 0
                out["outlet_check"] = {"glofas_outlet_m3s": round(ld.q_out_glofas[i], 2),
                                       "model_outlet_m3s": round(self.q_seg[out_s][i], 2),
                                       "glofas_cell": ld.glofas_cells.get("outlet")}
        out["segments"] = self.discharge_profile(dt)
        return out

    def today_series(self, cell, dt=None, tz_offset_min=0):
        at = dt or utc_now()
        tz_delta = timedelta(minutes=tz_offset_min)
        at_local = at + tz_delta
        day_start_local = at_local.replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_utc = day_start_local - tz_delta
        idx0 = self.idx_for(day_start_utc)
        now_idx = self.idx_for(at)
        cur_hour = max(0, min(23, at_local.hour))
        s = self.seg_of_cell[cell]
        qq = self._qq(now_idx)[s]
        hours = list(range(24))
        q_obs, q_med, q_p10, q_p90 = [], [], [], []
        wse_obs, wse_med, wse_p10, wse_p90 = [], [], [], []
        for h in hours:
            idx = now_idx if h == cur_hour else idx0 + h
            if h <= cur_hour:
                q = self._q_cell_h(cell, idx)
                q_obs.append(round(q, 2)); q_med.append(None); q_p10.append(None); q_p90.append(None)
                wse_obs.append(round(self.wse_at(q, cell), 3))
                wse_med.append(None); wse_p10.append(None); wse_p90.append(None)
            else:
                k = h - cur_hour - 1
                r = self._ratio(cell, now_idx + k + 1)
                q10, q50, q90 = (v * r for v in qq[k])
                q_obs.append(None); q_med.append(round(q50, 2)); q_p10.append(round(q10, 2)); q_p90.append(round(q90, 2))
                wse_obs.append(None)
                wse_med.append(round(self.wse_at(q50, cell), 3))
                wse_p10.append(round(self.wse_at(q10, cell), 3))
                wse_p90.append(round(self.wse_at(q90, cell), 3))
        return {
            "date": day_start_local.strftime("%Y-%m-%d"), "hours": hours, "current_hour": cur_hour,
            "tz_offset_min": tz_offset_min,
            "q_observed": q_obs, "q_forecast_median": q_med, "q_forecast_p10": q_p10, "q_forecast_p90": q_p90,
            "wse_observed": wse_obs, "wse_forecast_median": wse_med, "wse_forecast_p10": wse_p10, "wse_forecast_p90": wse_p90,
        }

    # ------------------------------- margin board -------------------------------

    def _margins_for_points(self, points, dt=None):
        """Per point: current + 72 h forecast margin to the danger level (crest - 0.8 m), zone
        (SAFE/WATCH/WARNING/DANGER), exceedance probability. Status = worst of (current-margin zone,
        forecast-median-margin zone, exceedance-probability zone). For an AREA (a 1 km segment or a location's stretch,
        given as "cells") the station checked is the one with the LEAST margin at its own 72 h peak flow, re-chosen on
        every request. All discharges are at that 10 m station."""
        idx = self.idx_for(dt)
        qq = self._qq(idx)
        mq = self._member_q(idx)
        out = []
        r_ = self.river
        for a in points:
            cells = a.get("cells")
            if cells is None and a.get("seg") is not None:
                cells = self.seg_cells[a["seg"]]
            if cells:
                def peak_margin(c):
                    s_c = self.seg_of_cell[c]
                    rr = self._ratio(c, idx)
                    q_pk = max(self._q_cell_h(c, idx), rr * max(v[1] for v in qq[s_c]))
                    return self.threshold(c) - self.wse_at(q_pk, c)
                step = max(1, len(cells) // 400)          # very long areas: test every n-th station
                cell = min(cells[::step], key=peak_margin)
            else:
                cell = a["cell"]
            s = self.seg_of_cell[cell]
            q_now, _ = self.q_cell_at(cell, dt)
            thr = self.threshold(cell)
            wse_now = self.wse_at(q_now, cell)
            margin_now = thr - wse_now
            min_margin_med, tth, peak_wse, peak_q, peak_k = margin_now, None, wse_now, q_now, 0
            ratios = [self._ratio(cell, idx + k + 1) for k in range(FORECAST_HOURS)]
            for k in range(FORECAST_HOURS):
                q50 = qq[s][k][1] * ratios[k]
                w50 = self.wse_at(q50, cell)
                m_med = thr - w50
                if m_med < min_margin_med:
                    min_margin_med = m_med
                if w50 > peak_wse:
                    peak_wse, peak_q, peak_k = w50, q50, k + 1
                if tth is None and m_med <= 0:
                    tth = k + 1
            q_thr = self.q_for_stage(cell, thr)
            exceed = sum(1 for row in mq[s] if q_thr is not None and
                         max(v * ratios[k] for k, v in enumerate(row)) >= q_thr)
            p = exceed / N_MEMBERS
            z_now = zone_for_margin(margin_now)
            z_fc = zone_for_margin(min_margin_med)
            z_p = 3 if p >= 0.6 else 2 if p >= 0.3 else 1 if p >= 0.1 else 0
            z = max(z_now, z_fc, z_p)
            basis = "current level" if z == z_now and z_now >= max(z_fc, z_p) else \
                    "72 h median forecast" if z == z_fc and z_fc >= z_p else "exceedance probability"
            row = {
                "id": a["id"], "name": a["name"], "type": a.get("type", "locality"),
                "segment": s + 1,
                "from_km": round(a.get("from_m", self.seg_bounds_m[s][0]) / 1000, 3),
                "to_km": round(a.get("to_m", self.seg_bounds_m[s][1]) / 1000, 3),
                "chainage_m": round(r_.chainage_m[cell], 1), "label": r_.chainage_label[cell],
                "cell": cell, "lon": r_.lon[cell], "lat": r_.lat[cell],
                "width_m": r_.real_width_m[cell],
                "threshold": round(thr, 2), "crest": round(self.crest[cell], 2),
                "bank_height_m": round(self.bank_h_seg[s], 2), "levels": self.levels(cell),
                "q_now": round(q_now, 2), "wse_now": round(wse_now, 2),
                "depth_now_m": round(wse_now - self.bed[cell], 2),
                "margin_now_m": round(margin_now, 2),
                "min_margin_median_m": round(min_margin_med, 2),
                "q_danger": self._r(q_thr, 1),
                "peak_q_72h": round(peak_q, 2), "peak_stage_72h": round(peak_wse, 2), "peak_in_h": peak_k,
                "p_exceed_72h": round(p, 2), "time_to_threshold_h": tth,
                "status": ZONE_NAME[z], "basis": basis,
            }
            if cells:
                row["n_chainages"] = len(cells)
            rs = self._rain_stats(s, idx)
            if rs:
                row.update(rs)
            out.append(row)
        return out

    def chainage_segments(self, step_km=SEG_KM):
        r = self.river
        if abs(step_km - SEG_KM) < 1e-9:
            return [{"id": f"C{s+1}", "name": f"{a/1000:.1f}–{b/1000:.1f} km" + (" (end)" if s == self.n_seg - 1 else ""),
                     "type": "chainage_segment", "seg": s} for s, (a, b) in enumerate(self.seg_bounds_m)]
        # any other step: fixed station = least freeboard at a flood-ish reference flow in that window
        step_m = step_km * 1000.0
        segs, start, i = [], 0.0, 1
        while start < r.reach_len_m - 1e-6:
            end = min(start + step_m, r.reach_len_m)
            cells = [c for c in range(r.n_cells) if start - 1e-6 <= r.chainage_m[c] <= end + 1e-6] or \
                    [self._cell_for_chainage((start + end) / 2.0)]
            crit = min(cells, key=lambda c: self.threshold(c) - self.wse_at(400.0, c))
            segs.append({"id": f"C{i}", "name": f"{start/1000:.1f}–{end/1000:.1f} km" +
                         (" (end)" if end >= r.reach_len_m - 1e-6 else ""), "type": "chainage_segment", "cell": crit})
            start += step_m
            i += 1
        return segs

    def margins(self, dt=None):
        points = [dict(a, cell=self._cell_for_chainage(a["chainage_m"])) for a in self.assets]
        return self._margins_for_points(points, dt)

    # ------------------------------- location areas (from the localities Excel) -------------------------------

    def _build_locations(self):
        """Locations from localities.json (name + lon/lat, or name + chainage) are snapped to the nearest chainage
        station and sorted along the river. The reach is then cut into STRETCHES between consecutive locations:
        "Sangam -> Bund Garden", "Bund Garden -> Dhanori", ... The first stretch starts at chainage 0+000 and the last
        ends at the river end, so every chainage belongs to exactly one named stretch."""
        r = self.river
        pts = []
        for k, L in enumerate(r.localities or []):
            try:
                if L.get("chainage_m") is not None:
                    cell = self._cell_for_chainage(float(L["chainage_m"]))
                    dist = 0.0 if L.get("lon") is None else _haversine(L["lon"], L["lat"], r.lon[cell], r.lat[cell])
                else:
                    lon, lat = float(L["lon"]), float(L["lat"])
                    cell = min(range(r.n_cells), key=lambda i: (r.lon[i] - lon) ** 2 + ((r.lat[i] - lat) * 1.05) ** 2)
                    dist = _haversine(lon, lat, r.lon[cell], r.lat[cell])
            except (TypeError, ValueError, KeyError):
                continue
            pts.append((cell, k, dist, L))
        pts.sort(key=lambda t: (t[0], t[1]))
        self.location_points = [{"name": str(L.get("name") or f"Location {j+1}"), "cell": cell,
                                 "chainage_m": round(r.chainage_m[cell], 1), "label": r.chainage_label[cell],
                                 "lon": r.lon[cell], "lat": r.lat[cell], "input_lon": L.get("lon"),
                                 "input_lat": L.get("lat"), "snap_distance_m": round(dist, 0),
                                 "far_from_river": dist > 1000.0}
                                for j, (cell, k, dist, L) in enumerate(pts)]
        P = self.location_points
        stretches = []

        def add(c0, c1, a_name, b_name, a_pt, b_pt):
            if c1 <= c0:
                return
            stretches.append({"id": f"L{len(stretches)+1}", "name": f"{a_name} \u2192 {b_name}",
                              "from_name": a_name, "to_name": b_name, "from_point": a_pt, "to_point": b_pt,
                              "from_m": r.chainage_m[c0], "to_m": r.chainage_m[c1], "i0": c0, "i1": c1,
                              "cell": (c0 + c1) // 2, "n_chainages": c1 - c0 + 1,
                              "from_label": r.chainage_label[c0], "to_label": r.chainage_label[c1]})
        if P:
            # the first location starts at chainage 0+000 and the last one ends at the river end, so the stretches
            # are exactly "Sangam -> Bund Garden", ..., "Kharadi -> Manjari" and still cover every chainage
            n = r.n_cells
            if len(P) == 1:
                add(0, n - 1, P[0]["name"], f"River end ({r.chainage_label[-1]})", 0, None)
            for j in range(len(P) - 1):
                c0 = 0 if j == 0 else P[j]["cell"]
                c1 = n - 1 if j == len(P) - 2 else P[j + 1]["cell"]
                add(c0, c1, P[j]["name"], P[j + 1]["name"], j, j + 1)
            # a stretch shares its end chainage with the next one's start; give that point to the downstream stretch
            for k in range(len(stretches) - 1):
                if stretches[k]["i1"] == stretches[k + 1]["i0"] and stretches[k]["i1"] > stretches[k]["i0"]:
                    stretches[k]["i1"] -= 1
                    stretches[k]["n_chainages"] -= 1
                    stretches[k]["to_m"] = r.chainage_m[stretches[k]["i1"]]
                    stretches[k]["to_label"] = r.chainage_label[stretches[k]["i1"]]
        self.locations = stretches
        self.loc_of_cell = [None] * r.n_cells
        for k, L in enumerate(stretches):
            for i in range(L["i0"], L["i1"] + 1):
                self.loc_of_cell[i] = k

    def set_localities(self, localities):
        self.river.localities = localities
        self._build_locations()
        return self.locations

    def margins_locations(self, dt=None):
        pts = [{"id": L["id"], "name": L["name"], "type": "stretch", "from_m": L["from_m"], "to_m": L["to_m"],
                "cells": list(range(L["i0"], L["i1"] + 1))} for L in self.locations]
        return self._margins_for_points(pts, dt)

    STATION_COLS = ("cell", "location", "chainage_m", "label", "lon", "lat", "width_m", "q", "q6",
                    "q24_p10", "q24", "q24_p90", "q72_p10", "q72", "q72_p90", "q_peak72", "peak_in_h",
                    "velocity_ms", "depth_m", "stage", "danger_level", "margin_m", "zone",
                    "rain_now_mmh", "rain_past24_mm", "rain_next24_mm", "runoff_coeff")

    def _fc_mult(self, i, idx, k):
        """forecast factor at station i for lead k+1: (km quantile / km deterministic discharge) blended linearly
        between the km and its neighbour (by position past the km's middle) so 10 m forecasts have no step at km
        boundaries. Returns (m_p10, m_p50, m_p90)."""
        qq = self._qq(idx)
        t = max(0, min(self.n_hours - 1, idx + k + 1))
        s = self.seg_of_cell[i]
        a, b = self.seg_bounds_m[s]
        p = (self.river.chainage_m[i] - a) / max(1e-6, b - a)
        nb = s - 1 if p < 0.5 else s + 1
        w = abs(p - 0.5) if 0 <= nb < self.n_seg else 0.0

        def m(sg):
            d = max(1e-6, self.q_seg[sg][t])
            return [v / d for v in qq[sg][k]]
        ms = m(s)
        if w <= 0:
            return ms
        mn = m(nb)
        return [x * (1 - w) + y * w for x, y in zip(ms, mn)]

    def stations_table(self, dt=None, from_m=None, to_m=None, cells=None):
        """every chainage station (10 m) in [from_m, to_m] (or the given cells): discharge now and forecast
        (+6/+24/+72 h, 72 h peak), velocity, depth, stage, danger level, margin, zone and the rain on that km."""
        r = self.river
        if cells is None:
            lo = -1e9 if from_m is None else from_m - 1e-6
            hi = 1e18 if to_m is None else to_m + 1e-6
            cells = [i for i in range(r.n_cells) if lo <= r.chainage_m[i] <= hi]
        idx = self.idx_for(dt)
        qq = self._qq(idx)
        pk = [max(range(FORECAST_HOURS), key=lambda k, s=s: qq[s][k][1]) for s in range(self.n_seg)]
        rs = [self._rain_stats(s, idx) or {} for s in range(self.n_seg)]
        rows = {k: [] for k in self.STATION_COLS}
        locs = getattr(self, "locations", [])
        for i in cells:
            s = self.seg_of_cell[i]
            q, _ = self.q_cell_at(i, dt)
            st = self.wse_at(q, i)
            dep = st - self.bed[i]
            thr = self.threshold(i)
            det = {L: self._q_cell_h(i, idx + L) for L in (6, 24, 72)}
            m6, m24, m72 = (self._fc_mult(i, idx, L - 1) for L in (6, 24, 72))
            k_pk = pk[s]
            q_pk = max(q, self._q_cell_h(i, idx + k_pk + 1) * self._fc_mult(i, idx, k_pk)[1])
            li = self.loc_of_cell[i] if getattr(self, "loc_of_cell", None) else None
            vals = (i, locs[li]["name"] if li is not None else "", r.chainage_m[i], r.chainage_label[i], r.lon[i],
                    r.lat[i], r.real_width_m[i], round(q, 3), round(det[6] * m6[1], 3),
                    round(det[24] * m24[0], 3), round(det[24] * m24[1], 3), round(det[24] * m24[2], 3),
                    round(det[72] * m72[0], 3), round(det[72] * m72[1], 3), round(det[72] * m72[2], 3),
                    round(q_pk, 3), (k_pk + 1) if q_pk > q else 0,
                    round(q / max(1.0, self.hyd_w[i] * max(0.05, dep)), 3), round(dep, 3), round(st, 3),
                    round(thr, 2), round(thr - st, 2), ZONE_NAME[zone_for_margin(thr - st)],
                    rs[s].get("rain_now_mmh"), rs[s].get("rain_past_24h_mm"), rs[s].get("rain_next_24h_mm"),
                    rs[s].get("runoff_coeff"))
            for k, v in zip(self.STATION_COLS, vals):
                rows[k].append(v)
        rows["at"] = (dt or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows["datum"] = self.datum_short
        rows["n"] = len(cells)
        return rows

    def margins_chainage(self, dt=None, step_km=SEG_KM):
        return self._margins_for_points(self.chainage_segments(step_km), dt)

    def _alerts_from(self, rows):
        out = []
        for m in rows:
            if m["status"] in ("WATCH", "WARNING", "DANGER"):
                msg = (f"{m['name']}: {m['status']} — stage {m['wse_now']} m (depth {m['depth_now_m']} m), "
                       f"Q {m['q_now']} m³/s, margin {m['margin_now_m']} m. Forecast peak {m['peak_stage_72h']} m "
                       f"in ~{m['peak_in_h']} h; 72 h exceedance probability {int(m['p_exceed_72h']*100)}%.")
                if m["time_to_threshold_h"]:
                    msg += f" Median forecast crosses threshold in ~{m['time_to_threshold_h']} h."
                out.append({"severity": m["status"], "asset": m["id"], "message": msg})
        return out

    def alerts(self, dt=None):
        return self._alerts_from(self.margins(dt))

    def alerts_chainage(self, dt=None, step_km=SEG_KM):
        return self._alerts_from(self.margins_chainage(dt, step_km))

    def scorecard(self, dt=None):
        idx = self.idx_for(dt)
        if idx - 24 < 0:
            return {"note": "insufficient history yet", "rows": []}
        past_idx = idx - 24
        a0 = self.assets[0]
        cell = self._cell_for_chainage(a0["chainage_m"])
        s = self.seg_of_cell[cell]
        qq = self._qq(past_idx)[s]
        errs, hits, misses, fa, thr = [], 0, 0, 0, self.threshold(cell)
        for k in range(24):
            pred = self.wse_at(qq[k][1], cell)
            truth = self.wse_at(self.q_seg[s][past_idx + 1 + k], cell)
            errs.append((pred - truth) ** 2)
            pe, te = pred >= thr, truth >= thr
            if pe and te: hits += 1
            elif te and not pe: misses += 1
            elif pe and not te: fa += 1
        rmse = math.sqrt(sum(errs) / len(errs))
        return {"note": "", "rows": [
            {"metric": f"Stage 24 h forecast RMSE at {a0['name']}", "value": f"{rmse:.2f} m"},
            {"metric": "Threshold-exceedance hits (24 h)", "value": str(hits)},
            {"metric": "Misses", "value": str(misses)},
            {"metric": "False alarms", "value": str(fa)},
            {"metric": "Ensemble size", "value": str(N_MEMBERS)},
            {"metric": "Data source", "value": self.q_source},
        ]}

    def meta(self):
        r = self.river
        now = utc_now()
        n_bridges = sum(1 for lm in r.landmarks if lm.get("type") == "bridge")
        n_confluences = sum(1 for lm in r.landmarks if lm.get("type") == "confluence")
        span = f"{r.landmarks[0]['name']} → {r.landmarks[-1]['name']}" if r.landmarks else ""
        live = self.live_status.get("mode") == "live"
        return {
            "product": r.product_label, "river": r.display_name,
            "disclaimer": (
                ("LIVE: rainfall/weather (Open-Meteo), upstream discharge + forecast spread (GloFAS), terrain (GLO-90 DEM) "
                 "and a per-km rainfall-runoff + routing model. " if live else
                 "NO LIVE FEED REACHED — upstream inflow is synthetic. ")
                + "REAL geometry from your KMLs (chainage, polygon width"
                + (", Sentinel-2 relative depth" if r.has_real_depth else "") + "). "
                "ASSUMED: Manning roughness, urban inflow per km, runoff parameters; dam releases are not modelled; "
                "bank heights are DEM estimates. Calibrate with a gauge CSV and surveyed bank levels before real decisions."),
            "has_real_depth": r.has_real_depth, "survey": r.survey,
            "reach_km": round(r.reach_len_m / 1000.0, 3), "n_cells": r.n_cells,
            "chainage_step_m": round((r.reach_len_m / max(1, r.n_cells - 1)), 1),
            "past_window_hours": PAST_WINDOW_H, "forecast_hours": FORECAST_HOURS, "members": N_MEMBERS,
            "q_source": self.q_source, "live_status": self.live_status, "seg_km": SEG_KM, "n_seg": self.n_seg,
            "seg_bounds_km": [[round(a / 1000, 3), round(b / 1000, 3)] for a, b in self.seg_bounds_m],
            "seg_mid_cell": [cells[len(cells) // 2] for cells in self.seg_cells],
            "zones": {"danger": "margin <= 0 m", "warning": f"margin <= {ZONE_WARNING_M} m",
                      "watch": f"margin <= {ZONE_WATCH_M} m", "safe": f"margin > {ZONE_WATCH_M} m",
                      "margin_definition": f"(crest - {FREEBOARD_M} m) - stage"},
            "freeboard_m": FREEBOARD_M, "zone_watch_m": ZONE_WATCH_M, "zone_warning_m": ZONE_WARNING_M,
            "terrain": self.terrain_summary(), "datum": self.datum, "datum_short": self.datum_short,
            "flow_dir": self.flow_dir,
            "assets": self.assets, "n_bridges": n_bridges, "n_confluences": n_confluences,
            "n_landmarks": len(r.landmarks), "reach_span": span, "landmarks": r.landmarks,
            "chainage_km": [round(v / 1000.0, 4) for v in r.chainage_m],
            "chainage_label": r.chainage_label,
            "locations": self.locations, "location_points": getattr(self, "location_points", []),
            "lon": r.lon, "lat": r.lat, "real_width_m": r.real_width_m,
            "real_depth_m": r.real_depth_m, "depth_flagged": r.depth_flagged,
            "reach_polygon_lonlat": r.reach_polygon_lonlat,
            "bed": [round(v, 3) for v in self.bed], "crest": [round(v, 3) for v in self.crest],
            "sigma_bed": self.sigma_bed,
            "epoch_date": EPOCH.strftime("%Y-%m-%d"),
            "now_iso": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "today_date": now.strftime("%Y-%m-%d"),
            "today_hour": now.hour,
            "max_index_date": (EPOCH + timedelta(hours=self._max_idx())).strftime("%Y-%m-%d"),
        }
