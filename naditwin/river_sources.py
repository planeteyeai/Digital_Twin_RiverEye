"""Built-in rivers fed straight from the  ``Digital _twin_kml/``  folder.

Each built-in river has one sub-folder with up to four source files:

    Digital _twin_kml/
        Mula_mutha_river/   Mutha_Mutha_river.kml   Mula_mutha_river_chainage.kml   water_depth_mula_mutha.kml   locations.xlsx
        Mithi_river/        Mithi river.kml         Mithi_river_chainage.kml        water_depth_mithi.kml        locations.xlsx
        Mandovi_river/      Mandovi river.kml       Mandovi_river_chainage.kml      water_depth_mandovi.kml      locations.xlsx

File roles are recognised by name:  *chainage*.kml -> chainage points,  *depth*.kml -> Sentinel-2 water-depth overlay,
any other .kml -> river polygon,  locations*.xlsx / .csv -> named locations (Location | Longitude | Latitude [| Chainage]).

At server start ``ensure_all()`` builds  naditwin/rivers/<key>/  for every river whose folder is missing, and
re-builds it when a source file changed (content hash, stored in sources.json). If only the locations Excel changed
the geometry, depth and live caches are kept and just the locations are refreshed. A hand-edited params.json
(Manning n, bank heights, ...) is always kept.
"""

import glob
import hashlib
import json
import os
import time

from . import kml_ingest

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(HERE)
RIVERS_DIR = os.path.join(HERE, "rivers")
SOURCES_DIR = os.environ.get("NADITWIN_KML_DIR") or os.path.join(PROJECT_DIR, "Digital _twin_kml")

# display name -> config.  Order = order of the river buttons in the dashboard.
BUILTIN_SOURCES = {
    "Mula-Mutha": dict(
        key="mula_mutha_live", folder="Mula_mutha_river", city="Pune, Maharashtra",
        product_label="NadiTwin — Mula-Mutha, Pune (10 m chainage KML + river polygon + Sentinel-2 depth KML, LIVE rainfall/discharge)",
    ),
    "Mithi": dict(
        key="mithi", folder="Mithi_river", city="Mumbai, Maharashtra",
        product_label="NadiTwin — Mithi, Mumbai (10 m chainage KML + river polygon + Sentinel-2 depth KML, LIVE rainfall/discharge)",
    ),
    "Mandovi": dict(
        key="mandovi", folder="Mandovi_river", city="Panaji, Goa",
        product_label="NadiTwin — Mandovi estuary, Goa (10 m chainage KML + river polygon + Sentinel-2 depth KML, LIVE rainfall/discharge)",
    ),
}

ROLES = ("polygon", "chainage", "depth", "locations")
SOURCES_JSON = "sources.json"


# ------------------------------------------------------------------ files ------------------------------------------------------------------

def source_folder(name):
    cfg = BUILTIN_SOURCES[name]
    return os.path.join(SOURCES_DIR, cfg["folder"])


def find_source_files(name):
    """-> {role: absolute path or None} for one built-in river's source folder."""
    d = source_folder(name)
    out = {r: None for r in ROLES}
    if not os.path.isdir(d):
        return out
    kmls = sorted(glob.glob(os.path.join(d, "*.kml")) + glob.glob(os.path.join(d, "*.KML")))
    polys = []
    for p in kmls:
        low = os.path.basename(p).lower()
        if "chainage" in low or "chain" in low:
            out["chainage"] = out["chainage"] or p
        elif "depth" in low:
            out["depth"] = out["depth"] or p
        else:
            polys.append(p)
    if polys:      # the river polygon: prefer a file with "river" in the name, else the first one
        out["polygon"] = next((p for p in polys if "river" in os.path.basename(p).lower()), polys[0])
    tabs = sorted(glob.glob(os.path.join(d, "*.xlsx")) + glob.glob(os.path.join(d, "*.csv")))
    tabs = [p for p in tabs if not os.path.basename(p).startswith("~$")]   # Excel lock files
    if tabs:
        out["locations"] = next((p for p in tabs if "locat" in os.path.basename(p).lower()), tabs[0])
    return out


def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def signature(files):
    sig = {}
    for role in ROLES:
        p = files.get(role)
        if p and os.path.isfile(p):
            sig[role] = {"file": os.path.basename(p), "bytes": os.path.getsize(p), "sha1": _sha1(p)}
        else:
            sig[role] = None
    return sig


def _read_text(path):
    if not path:
        return None
    with open(path, "rb") as f:
        raw = f.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _read_localities(path):
    if not path:
        return None
    with open(path, "rb") as f:
        return kml_ingest.parse_localities(f.read(), os.path.basename(path))


def _river_dir(name):
    return os.path.join(RIVERS_DIR, BUILTIN_SOURCES[name]["key"])


def load_sources_json(name):
    p = os.path.join(_river_dir(name), SOURCES_JSON)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_sources_json(name, files, sig, action, notes=None):
    cfg = BUILTIN_SOURCES[name]
    rel = lambda p: os.path.relpath(p, PROJECT_DIR).replace("\\", "/") if p else None
    doc = {
        "river": name, "key": cfg["key"], "city": cfg.get("city"),
        "source_folder": os.path.relpath(source_folder(name), PROJECT_DIR).replace("\\", "/"),
        "files": {r: rel(files.get(r)) for r in ROLES},
        "signature": sig, "last_action": action,
        "built_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "notes": notes or [],
    }
    os.makedirs(_river_dir(name), exist_ok=True)
    with open(os.path.join(_river_dir(name), SOURCES_JSON), "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, ensure_ascii=False)
    return doc


# ------------------------------------------------------------------ build ------------------------------------------------------------------

def build(name, log=print):
    """(Re)build naditwin/rivers/<key>/ for one built-in river from its Digital _twin_kml folder."""
    cfg = BUILTIN_SOURCES[name]
    files = find_source_files(name)
    if not files["polygon"] and not files["chainage"]:
        raise kml_ingest.KmlIngestError(
            f"{name}: no river KML found in {source_folder(name)} (need a polygon KML and/or a *chainage*.kml)")
    t0 = time.time()
    log(f"  building {name} from {os.path.relpath(source_folder(name), PROJECT_DIR)} "
        f"({', '.join(os.path.basename(p) for p in files.values() if p)}) ...")
    localities = _read_localities(files["locations"])
    summary = kml_ingest.build_river_from_files(
        name,
        polygon_text=_read_text(files["polygon"]),
        chainage_text=_read_text(files["chainage"]),
        depth_text=_read_text(files["depth"]),
        localities=localities,
        slug=cfg["key"], uploaded=False,
        extra_meta={"builtin": True, "city": cfg.get("city"),
                    "source_folder": os.path.relpath(source_folder(name), PROJECT_DIR).replace("\\", "/")},
    )
    _write_sources_json(name, files, signature(files), "built", summary.get("notes"))
    log(f"  {name}: {summary['reach_km']} km, {summary['n_stations']} stations, "
        f"depth={'yes' if summary['has_depth'] else 'no'}, {len(localities or [])} locations "
        f"({time.time() - t0:.1f} s)")
    return summary


def refresh_locations(name, files=None, sig=None):
    """Only the locations Excel changed -> rewrite localities.json, keep geometry / depth / caches."""
    files = files or find_source_files(name)
    locs = _read_localities(files["locations"]) or []
    kml_ingest.save_localities(_river_dir(name), locs)
    prev = load_sources_json(name) or {}
    return _write_sources_json(name, files, sig or signature(files), "locations refreshed", prev.get("notes"))


def status(name):
    """-> (action, reason): action in {"ok", "build", "locations", "adopt", "missing"}."""
    files = find_source_files(name)
    d = _river_dir(name)
    built = os.path.isfile(os.path.join(d, "chainage_profile.json"))
    if not files["polygon"] and not files["chainage"]:
        return ("ok" if built else "missing"), f"no source KMLs in {source_folder(name)}"
    if not built:
        return "build", "not built yet"
    sig = signature(files)
    prev = load_sources_json(name)
    if prev is None:
        # folder was built before sources.json existed (e.g. by build_mula_mutha_live.py) -> keep it, stamp it
        return "adopt", "existing build adopted"
    old = prev.get("signature") or {}
    changed = [r for r in ROLES if (old.get(r) or {}).get("sha1") != (sig.get(r) or {}).get("sha1")]
    if not changed:
        return "ok", "up to date"
    if changed == ["locations"]:
        return "locations", "locations file changed"
    return "build", "source changed: " + ", ".join(changed)


def ensure_all(log=print):
    """Called at server start: make sure every built-in river folder exists and matches its Digital _twin_kml files."""
    report = {}
    for name in BUILTIN_SOURCES:
        try:
            action, reason = status(name)
            if action == "build":
                log(f"{name}: {reason}")
                build(name, log=log)
            elif action == "locations":
                log(f"{name}: {reason} — refreshing locations only")
                refresh_locations(name)
            elif action == "adopt":
                files = find_source_files(name)
                sig = signature(files)
                # keep the existing geometry, but make sure the locations match the Excel in the folder
                if files["locations"]:
                    kml_ingest.save_localities(_river_dir(name), _read_localities(files["locations"]) or [])
                _write_sources_json(name, files, sig, "adopted existing build")
            report[name] = {"action": action, "reason": reason}
        except Exception as e:      # one bad folder must not stop the other rivers
            log(f"{name}: could not build from {source_folder(name)}: {e}")
            report[name] = {"action": "error", "reason": str(e)}
    return report


def describe(name):
    """Info for /api/sources and the dashboard: which files feed this river and when it was built."""
    cfg = BUILTIN_SOURCES[name]
    files = find_source_files(name)
    doc = load_sources_json(name) or {}
    meta = {}
    mp = os.path.join(_river_dir(name), "meta.json")
    if os.path.isfile(mp):
        try:
            with open(mp, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            pass
    rel = lambda p: os.path.relpath(p, PROJECT_DIR).replace("\\", "/") if p else None
    return {
        "river": name, "key": cfg["key"], "city": cfg.get("city"), "builtin": True,
        "source_folder": os.path.relpath(source_folder(name), PROJECT_DIR).replace("\\", "/"),
        "files": {r: rel(files.get(r)) for r in ROLES},
        "file_bytes": {r: (os.path.getsize(files[r]) if files.get(r) else None) for r in ROLES},
        "built_at_utc": doc.get("built_at_utc"), "last_action": doc.get("last_action"),
        "build_notes": doc.get("notes") or meta.get("notes") or [],
        "status": status(name)[1],
    }
