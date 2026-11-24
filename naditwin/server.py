"""NadiTwin demo server — pure standard library (http.server), multi-river,
real-calendar time model, and live KML upload.

Endpoints (all JSON unless noted). Every GET endpoint below (except `/`,
`/api/rivers`) accepts an optional `?river=<name>` query param to pick
which river's engine to use (defaults to the first configured river), and
an optional `?at=<ISO8601 UTC datetime>` (any hour since 1 Jan 2021, e.g. `2021-07-22T13:00:00Z`) to
view a HISTORIC hour instead of live/current. Omitting `at` = live mode:
values are interpolated to the current minute and the endpoints intended
to auto-refresh should be polled roughly once a minute by the client.

  GET  /                       -> dashboard (HTML)
  GET  /api/rivers             -> list of loaded rivers [{name, reach_km}]
  GET  /api/timerange          -> {epoch_date, today_date, today_hour, now_iso}
                                   for populating the Live/Historic date picker
  GET  /api/meta               -> reach geometry, assets, landmarks, disclaimer
  GET  /api/state              -> discharge + WSE profile at `at` (or live)
  GET  /api/forecast/profile?lead=24
  GET  /api/hydrograph?cell=520&hours=72   -> discharge + stage (+ rain) hydrograph: `hours` back (72, 168, 720,
                                   8760 = 1 year, or "all" = since 1 Jan 2021) + 72 h forecast, zone levels, danger discharge
  GET  /api/hydrograph?cell=520&year=2021  -> one calendar year (1 Jan - 31 Dec; this year: up to now + 72 h forecast)
                                   + year summary: peak discharge / stage and when, rain total, hours above
                                   watch / warning / danger
  GET  /api/rating?cell=520      -> stage-discharge rating curve at that station + Q at watch/warning/danger
  GET  /api/stations?from_km=&to_km=   -> every chainage station (10 m): discharge, velocity, depth, stage, margin
  GET  /api/stations.csv?from_km=&to_km=  -> same as a CSV download
  GET  /api/stations.xlsx?loc=L3|all      -> Excel: one location stretch, or all (Summary + All chainages + per stretch)
  GET  /api/locations            -> location areas (from the localities Excel), snapped to the chainage
  GET  /api/margins_locations    -> margin board per location area
  POST /api/upload_localities?river=  -> JSON {"filename", "data_b64"} of a .xlsx/.csv (Location, Longitude, Latitude)
  GET  /api/margins            -> margin-to-threshold board (per real bridge/
                                   locality asset)
  GET  /api/margins_chainage?step_km=2
                                -> margin-to-threshold board, per fixed
                                   step_km-wide chainage segment instead of
                                   per bridge/locality (used by the
                                   dashboard's Margin board + map marker
                                   colouring uses /api/margins separately)
  GET  /api/alerts             -> active alerts (per real bridge/locality asset)
  GET  /api/alerts_chainage?step_km=2
                                -> active alerts, per chainage segment
  GET  /api/live               -> live feed status, weather now, rain totals, per-1-km discharge table
  GET  /api/discharge_profile  -> chainage-wise (every 1 km) discharge now + 6/12/24/48/72 h forecast
  GET  /api/refresh_live       -> force a re-fetch of rainfall/weather/GloFAS now
  GET  /api/scorecard          -> hindcast-scored metrics (synthetic)
  GET  /api/today?cell=&river=&at=
                                -> today's calendar day at one chainage cell:
                                   hours already passed = observed, hours
                                   still to come today = forecast (median +
                                   P10-P90), for both discharge and stage
  POST /api/upload_kml?name=<display name>
                                -> body = raw KML/XML text, OR JSON {"polygon": <kml>, "chainage": <kml, optional>,
                                   "depth": <water-depth kml, optional>}. Derives chainage
                                   (100 m stations), width, and bridge/locality
                                   landmarks from the KML, registers a brand
                                   new river, and returns an ingest summary.
                                   The new river is immediately selectable via
                                   `?river=<display name>` on every endpoint
                                   above, and survives a server restart.
                                   Single-slot: any PREVIOUSLY uploaded river
                                   (built-ins are never touched) is dropped
                                   and its on-disk folder deleted — so a new
                                   upload always REPLACES the last uploaded
                                   river rather than piling up in the list.
  GET  /api/endpoints?river=      -> every endpoint above, filled in for that river (the dashboard's
                                   "Endpoints" panel lists these as clickable links)
  GET  /api/sources?river=        -> which files in  Digital _twin_kml/<River>_river/  feed the river
                                   (polygon / chainage / depth KML + locations Excel) and when it was built
  POST /api/rebuild_river?river=  -> re-build a built-in river (Mula-Mutha, Mithi, Mandovi) from its
                                   Digital _twin_kml folder and reload it (e.g. after replacing a KML)
  POST /api/remove_river?name=<display name>
                                -> drops an uploaded river from the running
                                   server AND deletes its on-disk folder, so
                                   it's gone from the river list for good.
                                   Refuses on the built-in rivers.
"""

import base64
import json
import os
import shutil
import socket
import sys
from http.server import ThreadingHTTPServer as HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

import threading
import time

from .engine import TwinEngine, load_all_rivers, DEFAULT_RIVER, BUILTIN_RIVERS, parse_iso, EPOCH, utc_now, LIVE_REFRESH_S, SEG_KM
from . import kml_ingest
from . import river_sources
from concurrent.futures import ThreadPoolExecutor

STATIC = os.path.join(os.path.dirname(__file__), "static")

ENGINES = {}        # river display name -> TwinEngine, filled in serve()
_SEED = 42
_GAUGE_CSV = None

MAX_UPLOAD_BYTES = 25 * 1024 * 1024   # 25 MB — generous for a hand-traced KML


class UnknownRiver(KeyError):
    pass


def _pick_engine(q):
    """?river= accepts the display name ("Mandovi"), any letter case ("mandovi") or the folder key ("mula_mutha_live").
    No ?river= -> the default river. An UNKNOWN ?river= is an error (404) — it is never silently answered with
    Mula-Mutha data, so one river's locations can't show up under another river's name."""
    raw = q.get("river", [""])[0]
    name = unquote(raw or "").strip()
    if not name:
        return ENGINES.get(DEFAULT_RIVER) or next(iter(ENGINES.values()))
    eng = ENGINES.get(name)
    if eng is None:
        low = name.lower()
        eng = next((e for n, e in ENGINES.items() if n.lower() == low or e.river.key.lower() == low), None)
    if eng is None:
        raise UnknownRiver(name)
    return eng


SERVER_ID = f"{os.getpid()}-{int(time.time())}"     # sent as X-NadiTwin-Server on every response


def port_in_use(port, host="127.0.0.1"):
    """True if something already answers on this port (e.g. an older NadiTwin window that is still running)."""
    try:
        with socket.create_connection((host, port), timeout=0.6):
            return True
    except OSError:
        return False


def port_busy_message(port):
    return (f"\n*** Port {port} is already in use — another NadiTwin server (or another program) is still running.\n"
            f"*** Close that window / press Ctrl+C in it (or end the old python.exe in Task Manager), then run\n"
            f"***     python run.py\n"
            f"*** again — or start this one on another port:  python run.py --port {port + 10}\n"
            f"*** (Two servers on one port answer requests at random: the old one shows Mula-Mutha for every river.)\n")


class NadiHTTPServer(HTTPServer):
    """On Windows, SO_REUSEADDR lets a SECOND server bind the same port and the two then share the requests.
    Use SO_EXCLUSIVEADDRUSE there instead, so a second copy fails at start-up instead of serving stale data."""
    allow_reuse_address = (os.name != "nt")
    request_queue_size = 64
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _pick_dt(q):
    """?at=<ISO8601> -> aware UTC datetime (historic). Missing/empty -> None (live)."""
    raw = q.get("at", [None])[0]
    if not raw:
        return None
    try:
        return parse_iso(unquote(raw))
    except Exception:
        return None


# (method, path, extra query, what it returns) — /api/endpoints fills in ?river=<name> for the selected river
ENDPOINT_CATALOG = [
    ("GET", "/", "", "Dashboard opened straight on this river"),
    ("GET", "/api/rivers", "", "All loaded rivers (name, city, reach, stations, locations, live mode)"),
    ("GET", "/api/sources", "", "Source files in Digital _twin_kml used to build this river"),
    ("GET", "/api/meta", "", "Reach geometry, chainage, polygon, landmarks, locations, terrain, disclaimer"),
    ("GET", "/api/state", "", "Discharge + water-surface profile now"),
    ("GET", "/api/live", "", "Live feed status, weather now, rain totals, per-1-km discharge table"),
    ("GET", "/api/discharge_profile", "", "Chainage-wise (1 km) discharge now + 6/12/24/48/72 h forecast"),
    ("GET", "/api/forecast/profile", "lead=24", "Water-surface profile at a forecast lead time (h)"),
    ("GET", "/api/locations", "", "Named stretches between consecutive locations (from locations.xlsx)"),
    ("GET", "/api/margins_locations", "", "Margin board per location stretch"),
    ("GET", "/api/margins_chainage", "step_km=1", "Margin board per 1 km of chainage"),
    ("GET", "/api/alerts_chainage", "step_km=1", "Active alerts per chainage segment"),
    ("GET", "/api/margins", "", "Margin board per bridge / landmark"),
    ("GET", "/api/alerts", "", "Active alerts per bridge / landmark"),
    ("GET", "/api/stations", "loc=L1", "Every 10 m chainage of one stretch: Q, velocity, depth, stage, margin"),
    ("GET", "/api/stations.csv", "", "Every 10 m chainage of the whole reach as CSV"),
    ("GET", "/api/stations.xlsx", "loc=all", "Excel: Summary + all chainages + one sheet per stretch"),
    ("GET", "/api/hydrograph", "cell={cell}&hours=72", "Stage + discharge + rain hydrograph at a station"),
    ("GET", "/api/hydrograph", "cell={cell}&year=2021", "Hydrograph of one calendar year (2021 … this year) + year summary"),
    ("GET", "/api/rating", "cell={cell}", "Stage-discharge rating curve at a station"),
    ("GET", "/api/today", "cell={cell}&tz=330", "Today at a station: observed hours + forecast hours"),
    ("GET", "/api/scorecard", "", "Hindcast-scored metrics"),
    ("GET", "/api/timerange", "", "Live / historic date range"),
    ("GET", "/api/refresh_live", "", "Force a re-fetch of rain / weather / GloFAS now"),
    ("POST", "/api/rebuild_river", "", "Re-build this river from its Digital _twin_kml folder"),
    ("POST", "/api/upload_localities", "", "Replace the locations (JSON {filename, data_b64} of .xlsx/.csv)"),
]


def _endpoints_for(engine, host):
    from urllib.parse import quote
    name = engine.river.display_name
    cell = engine.river.n_cells // 2
    base = f"http://{host}" if host else ""
    out = []
    for method, path, extra, desc in ENDPOINT_CATALOG:
        qs = "river=" + quote(name)
        if path in ("/api/rivers", "/api/timerange"):
            qs = ""
        if extra:
            qs = (qs + "&" if qs else "") + extra.replace("{cell}", str(cell))
        rel = path + ("?" + qs if qs else "")
        out.append({"method": method, "path": path, "url": rel, "full_url": base + rel, "description": desc})
    return {"river": name, "key": engine.river.key, "base": base, "endpoints": out}


def _river_info(name, eng):
    r = eng.river
    info = {"name": name, "key": r.key, "reach_km": round(r.reach_len_m / 1000.0, 3), "n_stations": r.n_cells,
            "uploaded": name not in BUILTIN_RIVERS, "builtin": name in BUILTIN_RIVERS,
            "has_depth": r.has_real_depth, "mode": eng.live_status.get("mode"),
            "n_locations": len(r.localities or []), "product": r.product_label,
            "city": (BUILTIN_RIVERS.get(name) or {}).get("city") or r.meta_json.get("city"),
            "survey_date": (r.survey or {}).get("image_date")}
    if name in BUILTIN_RIVERS:
        info["source_folder"] = os.path.relpath(river_sources.source_folder(name), river_sources.PROJECT_DIR).replace("\\", "/")
    return info


class Handler(BaseHTTPRequestHandler):

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-NadiTwin-Server", SERVER_ID)
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)

    def log_message(self, fmt, *args):  # quieter console
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html", "/dashboard"):
                with open(os.path.join(STATIC, "dashboard.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")

            if u.path == "/api/rivers":
                return self._send(200, [_river_info(name, eng) for name, eng in ENGINES.items()])

            if u.path == "/api/timerange":
                now = utc_now()
                return self._send(200, {
                    "epoch_date": EPOCH.strftime("%Y-%m-%d"),
                    "today_date": now.strftime("%Y-%m-%d"),
                    "today_hour": now.hour,
                    "now_iso": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                })

            engine = _pick_engine(q)
            dt = _pick_dt(q)

            if u.path == "/api/endpoints":
                return self._send(200, _endpoints_for(engine, self.headers.get("Host", "")))
            if u.path == "/api/sources":
                nm = engine.river.display_name
                if nm in BUILTIN_RIVERS:
                    return self._send(200, river_sources.describe(nm))
                return self._send(200, {"river": nm, "key": engine.river.key, "builtin": False,
                                        "source_folder": None, "files": {}, "status": "uploaded from the dashboard",
                                        "build_notes": engine.river.meta_json.get("notes", [])})
            if u.path == "/api/meta":
                return self._send(200, engine.meta())
            if u.path == "/api/state":
                return self._send(200, engine.state_now(dt))
            if u.path == "/api/forecast/profile":
                lead = int(q.get("lead", ["24"])[0])
                return self._send(200, engine.forecast_profile(lead, dt))
            if u.path == "/api/hydrograph":
                n_cells = engine.river.n_cells
                cell = int(q.get("cell", [str(n_cells // 2)])[0])
                cell = max(0, min(n_cells - 1, cell))
                yr = (q.get("year", [""])[0] or "").strip()
                if yr:
                    from .engine import EPOCH as _EP
                    try:
                        yr = int(yr)
                    except ValueError:
                        return self._send(400, {"error": "year must be a number, e.g. year=2021"})
                    if not (_EP.year <= yr <= utc_now().year):
                        return self._send(400, {"error": f"year must be {_EP.year}..{utc_now().year}"})
                    return self._send(200, engine.hydrograph(cell, dt, year=yr))
                hours = q.get("hours", ["72"])[0]
                hours = 10 ** 6 if hours == "all" else int(float(hours or 72))
                return self._send(200, engine.hydrograph(cell, dt, hours))
            if u.path == "/api/rating":
                n_cells = engine.river.n_cells
                cell = int(q.get("cell", [str(n_cells // 2)])[0])
                cell = max(0, min(n_cells - 1, cell))
                return self._send(200, engine.rating(cell, dt))
            if u.path == "/api/margins":
                return self._send(200, engine.margins(dt))
            if u.path == "/api/margins_locations":
                return self._send(200, engine.margins_locations(dt))
            if u.path == "/api/locations":
                return self._send(200, engine.locations)
            if u.path == "/api/stations.xlsx":
                data, fname = _stations_workbook(engine, dt, q.get("loc", ["all"])[0])
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if u.path in ("/api/stations", "/api/stations.csv"):
                def km(k):
                    v = q.get(k, [""])[0]
                    return float(v) * 1000.0 if v not in ("", None) else None
                loc = q.get("loc", [""])[0]
                L = next((x for x in engine.locations if x["id"] == loc), None)
                tab = engine.stations_table(dt, cells=list(range(L["i0"], L["i1"] + 1))) if L else \
                    engine.stations_table(dt, km("from_km"), km("to_km"))
                if u.path.endswith(".csv"):
                    cols = ["chainage_m", "label", "lon", "lat", "width_m", "q", "velocity_ms", "depth_m", "stage",
                            "danger_level", "margin_m", "zone"]
                    head = ["chainage_m", "chainage", "lon", "lat", "width_m", "discharge_m3s", "velocity_ms",
                            "depth_m", "stage_" + tab["datum"].replace(" ", "_").replace(",", ""),
                            "danger_level", "margin_m", "zone"]
                    lines = [f"# {engine.river.display_name} - discharge every chainage station at {tab['at']}",
                             ",".join(head)]
                    for i in range(tab["n"]):
                        lines.append(",".join(str(tab[c][i]) for c in cols))
                    data = ("\r\n".join(lines) + "\r\n").encode("utf-8")
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/csv; charset=utf-8")
                        fname = "discharge_10m_" + engine.river.key + ".csv"
                        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                return self._send(200, tab)
            if u.path == "/api/margins_chainage":
                step_km = float(q.get("step_km", ["1"])[0] or 1.0)
                step_km = max(0.1, min(50.0, step_km))
                return self._send(200, engine.margins_chainage(dt, step_km))
            if u.path == "/api/alerts":
                return self._send(200, engine.alerts(dt))
            if u.path == "/api/alerts_chainage":
                step_km = float(q.get("step_km", ["1"])[0] or 1.0)
                step_km = max(0.1, min(50.0, step_km))
                return self._send(200, engine.alerts_chainage(dt, step_km))
            if u.path == "/api/live":
                return self._send(200, engine.live_summary(dt))
            if u.path == "/api/discharge_profile":
                return self._send(200, engine.discharge_profile(dt))
            if u.path == "/api/refresh_live":
                return self._send(200, engine.refresh_live(force=True))
            if u.path == "/api/scorecard":
                return self._send(200, engine.scorecard(dt))
            if u.path == "/api/today":
                n_cells = engine.river.n_cells
                cell = int(q.get("cell", [str(n_cells // 2)])[0])
                cell = max(0, min(n_cells - 1, cell))
                try:
                    tz_offset_min = int(q.get("tz", ["0"])[0])
                except (TypeError, ValueError):
                    tz_offset_min = 0
                tz_offset_min = max(-720, min(840, tz_offset_min))  # clamp to real UTC offset range
                return self._send(200, engine.today_series(cell, dt, tz_offset_min))
            return self._send(404, {"error": "not found"})

        except (BrokenPipeError, ConnectionResetError):
            pass
        except UnknownRiver as e:
            return self._send(404, {"error": f'unknown river "{e.args[0]}"', "rivers": list(ENGINES)})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/api/upload_kml":
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return self._send(400, {"error": "empty request body — POST the raw KML text"})
                if length > MAX_UPLOAD_BYTES:
                    return self._send(413, {"error": f"KML too large (> {MAX_UPLOAD_BYTES // (1024*1024)} MB)"})
                raw = self.rfile.read(length)
                try:
                    body = raw.decode("utf-8")
                except UnicodeDecodeError:
                    body = raw.decode("utf-8", errors="replace")
                # JSON body = multi-file upload {"polygon": kml, "chainage": kml?, "depth": kml?, "name": ...};
                # anything else = a single raw KML (polygon or centreline), as before
                files = {"polygon": body, "chainage": None, "depth": None}
                localities = None
                if body.lstrip().startswith("{"):
                    try:
                        jb = json.loads(body)
                    except ValueError:
                        return self._send(400, {"error": "bad JSON upload body"})
                    files = {k: (jb.get(k) or None) for k in ("polygon", "chainage", "depth")}
                    if jb.get("localities"):
                        try:
                            lb = jb["localities"]
                            localities = kml_ingest.parse_localities(base64.b64decode(lb["data_b64"]), lb.get("filename", ""))
                        except kml_ingest.KmlIngestError as e:
                            return self._send(400, {"error": "locations file: " + str(e)})
                        except Exception as e:
                            return self._send(400, {"error": f"locations file could not be read: {e}"})
                    if jb.get("name") and not q.get("name"):
                        q["name"] = [jb["name"]]

                display_name = unquote(q.get("name", [""])[0]).strip()
                if not display_name:
                    display_name = f"Uploaded river {len(ENGINES)+1}"
                if display_name in BUILTIN_RIVERS:
                    return self._send(400, {"error": f'"{display_name}" is a built-in river name — pick a different name.'})

                try:
                    # re-uploading the same display name REPLACES it (same
                    # on-disk slug gets overwritten) instead of piling up
                    # near-duplicate rivers in the list.
                    summary = kml_ingest.build_river_from_files(display_name, polygon_text=files["polygon"],
                                                                chainage_text=files["chainage"],
                                                                depth_text=files["depth"], localities=localities)
                except kml_ingest.KmlIngestError as e:
                    return self._send(400, {"error": str(e)})

                # single-slot uploads: any PREVIOUSLY uploaded river (built-ins
                # are never touched) is dropped now that the new KML has been
                # ingested successfully — so there's always at most one
                # uploaded river, and a fresh upload always replaces it in
                # place instead of piling up as a separate list entry.
                for old_name in [n for n in ENGINES if n not in BUILTIN_RIVERS and n != display_name]:
                    old_eng = ENGINES.pop(old_name, None)
                    if old_eng is not None:
                        old_dir = os.path.join(kml_ingest.RIVERS_DIR, old_eng.river.key)
                        if os.path.isdir(old_dir):
                            shutil.rmtree(old_dir, ignore_errors=True)

                from .engine import RiverData
                rd = RiverData(
                    key=summary["key"], display_name=display_name,
                    product_label=(f"NadiTwin — {display_name} (uploaded KML, "
                                    f"{summary['landmarks_source']} landmarks)"),
                )
                eng = TwinEngine(rd, seed=_SEED, gauge_csv=_GAUGE_CSV)
                ENGINES[display_name] = eng
                summary["display_name"] = display_name
                summary["live_mode"] = eng.live_status.get("mode")
                summary["terrain"] = eng.terrain_summary()
                summary["locations"] = eng.locations
                return self._send(200, summary)

            if u.path == "/api/upload_localities":
                # body: JSON {"filename": "...xlsx|.csv", "data_b64": "..."}; ?river=<name> picks the river
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > MAX_UPLOAD_BYTES:
                    return self._send(400, {"error": "empty or too large"})
                try:
                    jb = json.loads(self.rfile.read(length).decode("utf-8"))
                    locs = kml_ingest.parse_localities(base64.b64decode(jb["data_b64"]), jb.get("filename", ""))
                except kml_ingest.KmlIngestError as e:
                    return self._send(400, {"error": str(e)})
                except Exception as e:
                    return self._send(400, {"error": f"could not read the locations file: {e}"})
                eng = _pick_engine(q)
                kml_ingest.save_localities(eng.river.dir, locs)
                return self._send(200, {"river": eng.river.display_name, "locations": eng.set_localities(locs)})

            if u.path == "/api/rebuild_river":
                name = unquote(q.get("river", q.get("name", [""]))[0]).strip()
                if name not in BUILTIN_RIVERS:
                    return self._send(400, {"error": f'"{name}" is not a built-in river (built-ins: {", ".join(BUILTIN_RIVERS)})'})
                try:
                    summary = river_sources.build(name)
                except kml_ingest.KmlIngestError as e:
                    return self._send(400, {"error": str(e)})
                from .engine import RiverData
                rd = RiverData(key=BUILTIN_RIVERS[name]["key"], display_name=name,
                               product_label=BUILTIN_RIVERS[name]["product_label"])
                ENGINES[name] = TwinEngine(rd, seed=_SEED, gauge_csv=_GAUGE_CSV)
                summary["display_name"] = name
                summary["live_mode"] = ENGINES[name].live_status.get("mode")
                summary["sources"] = river_sources.describe(name)
                return self._send(200, summary)

            if u.path == "/api/remove_river":
                name = unquote(q.get("name", [""])[0]).strip()
                if not name:
                    return self._send(400, {"error": "missing ?name="})
                if name in BUILTIN_RIVERS:
                    return self._send(400, {"error": "cannot remove a built-in river"})
                eng = ENGINES.pop(name, None)
                if eng is None:
                    return self._send(404, {"error": f'river "{name}" not found'})
                slug = eng.river.key
                d = os.path.join(kml_ingest.RIVERS_DIR, slug)
                if os.path.isdir(d):
                    shutil.rmtree(d, ignore_errors=True)
                return self._send(200, {"removed": name})

            return self._send(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except UnknownRiver as e:
            return self._send(404, {"error": f'unknown river "{e.args[0]}"', "rivers": list(ENGINES)})
        except Exception as e:
            return self._send(500, {"error": str(e)})


XLSX_COLS = [("location", "Location (stretch)", 24), ("label", "Chainage", 10), ("chainage_m", "Chainage (m)", 11),
             ("lon", "Longitude", 11), ("lat", "Latitude", 11), ("width_m", "Width (m)", 9),
             ("q", "Q now (m³/s)", 11), ("q6", "Q +6 h (m³/s)", 11), ("q24_p10", "Q +24 h P10", 10),
             ("q24", "Q +24 h median", 11), ("q24_p90", "Q +24 h P90", 10), ("q72_p10", "Q +72 h P10", 10),
             ("q72", "Q +72 h median", 11), ("q72_p90", "Q +72 h P90", 10), ("q_peak72", "Peak Q next 72 h", 11),
             ("peak_in_h", "Peak in (h)", 9), ("velocity_ms", "Velocity (m/s)", 10),
             ("depth_m", "Water depth (m)", 10), ("stage", "River stage = water level", 13),
             ("danger_level", "Danger level = bank crest - 0.8 m", 14), ("margin_m", "Margin below danger (m)", 11),
             ("zone", "Zone", 10), ("rain_now_mmh", "Rain now (mm/h)", 10), ("rain_past24_mm", "Rain past 24 h (mm)", 11),
             ("rain_next24_mm", "Rain next 24 h (mm)", 11), ("runoff_coeff", "Runoff coeff.", 9)]


def _stations_workbook(engine, dt, loc):
    """.xlsx of every chainage station: one stretch (loc=L3) or all locations (Summary + All chainages + one sheet
    per stretch). Built with the standard library (xlsx_writer)."""
    from .xlsx_writer import write_xlsx
    river = engine.river.display_name
    full = engine.stations_table(dt)
    at = full["at"]
    hdr = [h if k not in ("stage", "danger_level") else f"{h} ({full['datum']})" for k, h, _ in XLSX_COLS]
    widths = [w for _, _, w in XLSX_COLS]
    zc = [k for k, _, _ in XLSX_COLS].index("zone")
    mode = engine.live_status.get("mode")
    title = (f"{river} — discharge at every chainage · {at} UTC · "
             + ("LIVE (Open-Meteo rain/weather + GloFAS discharge)" if mode == "live" else "SYNTHETIC — no live feed"))

    def rows_for(cells_set):
        idx = [j for j, c in enumerate(full["cell"]) if c in cells_set]
        return [[full[k][j] for k, _, _ in XLSX_COLS] for j in idx]
    locs = engine.locations
    L = next((x for x in locs if x["id"] == loc), None)
    if L is not None:
        cells = set(range(L["i0"], L["i1"] + 1))
        sheets = [{"name": L["name"], "title": title + " · " + L["name"], "header": hdr, "rows": rows_for(cells),
                   "widths": widths, "zone_col": zc}]
        fname = f"discharge_10m_{engine.river.key}_{L['from_name']}_to_{L['to_name']}.xlsx"
    else:
        sheets = []
        if locs:
            ms = engine.margins_locations(dt)
            summ = []
            for x, m in zip(locs, ms):
                qs = [full["q"][i] for i in range(full["n"]) if x["i0"] <= full["cell"][i] <= x["i1"]]
                summ.append([x["name"], x["from_label"], x["to_label"], round((x["to_m"] - x["from_m"]) / 1000.0, 3),
                             x["n_chainages"], m["status"], m["margin_now_m"], m["label"], round(min(qs), 3),
                             round(max(qs), 3), m["peak_q_72h"], m["q_danger"], m["levels"]["danger"],
                             m["wse_now"], m["depth_now_m"]])
            sheets.append({"name": "Summary", "title": title + " · margin board by location",
                           "header": ["Location (stretch)", "From chainage", "To chainage", "Length (km)",
                                      "Chainage points", "Status", "Margin now (m)", "Weakest chainage",
                                      "Q min (m³/s)", "Q max (m³/s)", "Peak Q next 72 h", "Q at danger level",
                                      f"Danger level ({full['datum']})", f"River stage now at weakest point ({full['datum']})",
                                      "Water depth now (m)"],
                           "rows": summ, "widths": [26, 11, 11, 10, 10, 10, 11, 11, 11, 11, 12, 12, 13, 15, 11], "zone_col": 5})
        sheets.append({"name": "All chainages", "title": title, "header": hdr,
                       "rows": rows_for(set(full["cell"])), "widths": widths, "zone_col": zc})
        for x in locs:
            sheets.append({"name": x["name"], "title": title + " · " + x["name"], "header": hdr,
                           "rows": rows_for(set(range(x["i0"], x["i1"] + 1))), "widths": widths, "zone_col": zc})
        fname = f"discharge_10m_{engine.river.key}_all_locations.xlsx"
    fname = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in fname)
    return write_xlsx(sheets), fname


def serve(port=8080, seed=42, gauge_csv=None):
    global ENGINES, _SEED, _GAUGE_CSV
    _SEED = seed
    _GAUGE_CSV = gauge_csv
    if port_in_use(port):
        print(port_busy_message(port), flush=True)
        raise SystemExit(1)
    try:
        # bind now: the port is ours while the rivers load (the browser just waits), and a second copy can't share it
        httpd = NadiHTTPServer(("0.0.0.0", port), Handler)
    except OSError as e:
        print(port_busy_message(port) + f"*** ({e})", flush=True)
        raise SystemExit(1)
    print(f"Checking built-in rivers against {river_sources.SOURCES_DIR} ...", flush=True)
    river_sources.ensure_all(log=lambda m: print(m, flush=True))
    rivers = load_all_rivers()
    print("Loading " + ", ".join(rivers) + " (fetching live rainfall / discharge...)", flush=True)

    def _load(item):
        name, river = item
        eng = TwinEngine(river, seed=seed, gauge_csv=gauge_csv)
        print(f"  {name}: {eng.live_status.get('mode')}  {eng.live_status.get('note', '')}", flush=True)
        return name, eng
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(rivers)))) as ex:
        loaded = dict(ex.map(_load, rivers.items()))
    ENGINES = {name: loaded[name] for name in rivers}     # keep the order Mula-Mutha, Mithi, Mandovi, uploads

    def _backfill(only_missing):
        # history since EPOCH (1 Jan 2021): past ERA5 years + GloFAS, one river at a time, paced for the free API
        for nm, eng in list(ENGINES.items()):
            if only_missing and not eng.history_missing():
                continue
            try:
                got = eng.backfill_history(log=lambda m: print(m, flush=True))
                if got:
                    print(f"  {nm}: history since {EPOCH:%d %b %Y} updated ({got} download(s))", flush=True)
            except Exception as e:
                print("history download failed for", nm, e, flush=True)

    def _refresher():
        print(f"Downloading any missing history since {EPOCH:%d %b %Y} in the background "
              "(first run: a few minutes; the dashboard works meanwhile) ...", flush=True)
        _backfill(only_missing=False)
        print("History check finished.", flush=True)
        while True:
            time.sleep(LIVE_REFRESH_S)
            for nm, eng in list(ENGINES.items()):
                try:
                    eng.refresh_live(force=True)
                except Exception as e:
                    print("live refresh failed for", nm, e, flush=True)
            _backfill(only_missing=True)
    threading.Thread(target=_refresher, daemon=True).start()
    print(f"NadiTwin demo running at http://localhost:{port}  (Ctrl+C to stop)")
    print("Rivers loaded:", ", ".join(ENGINES.keys()))
    print("Live rain/weather + GloFAS discharge + DEM terrain via Open-Meteo. Roughness, urban inflow and DEM bank heights are estimates — calibrate before real decisions.")
    httpd.serve_forever()
