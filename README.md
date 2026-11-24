# NadiTwin / RIVER EYE — live river twin for any river KML

Upload a river polygon KML (plus, optionally, a chainage KML and a water-depth KML) and the dashboard builds a
**live** digital twin of that reach: chainage-wise discharge every 1 km, river stage at every station, hydrograph,
rating curve and a SAFE / WATCH / WARNING / DANGER margin board for every 1 km of chainage.
Three rivers are built in — **Mula-Mutha (Pune), Mithi (Mumbai) and Mandovi (Goa)** — each built from its own folder
in `Digital _twin_kml/` (see below). Click a river on the dashboard to open its digital twin.

## Built-in rivers: the `Digital _twin_kml/` folder

```
Digital _twin_kml/
  Mula_mutha_river/  Mutha_Mutha_river.kml  Mula_mutha_river_chainage.kml  water_depth_mula_mutha.kml  locations.xlsx
  Mithi_river/       Mithi river.kml        Mithi_river_chainage.kml       water_depth_mithi.kml       locations.xlsx
  Mandovi_river/     Mandovi river.kml      Mandovi_river_chainage.kml     water_depth_mandovi.kml     locations.xlsx
```

File roles are recognised by name: `*chainage*.kml` = chainage points, `*depth*.kml` = Sentinel-2 water-depth overlay,
any other `.kml` = river polygon, `locations*.xlsx|.csv` = named locations. At every start `run.py` checks each folder
(content hash, stored in `naditwin/rivers/<key>/sources.json`) and builds `naditwin/rivers/<key>/` if it is missing
or a KML changed. If only `locations.xlsx` changed, just the locations are refreshed (geometry, depth and live caches
are kept). A hand-edited `params.json` is always kept. To re-read a folder without restarting, click
**⟳ Rebuild from folder** on the dashboard (or `POST /api/rebuild_river?river=Mandovi`).

| River | Folder key | Locations (from locations.xlsx) |
|---|---|---|
| Mula-Mutha | `naditwin/rivers/mula_mutha_live` | Sangam → … → Manjari (8) |
| Mithi | `naditwin/rivers/mithi` | Kurla, Kalina, Vakola, BKC, Dharavi, N–Mahim, Mahim Creek mouth (7) |
| Mandovi | `naditwin/rivers/mandovi` | Old Goa Confluence → … → Aguada Bay (mouth) (13) |

When a river is open the dashboard shows its **source files**, a **direct link** (`/?river=Mandovi`) and every
**API endpoint** for that river as clickable links (also available as JSON at `/api/endpoints?river=Mandovi`).

```
python run.py            # opens http://localhost:8080   (Python 3.8+, standard library only)
python run.py --port 9000 --no-browser
python run.py --gauge data/your_gauge.csv     # replace the GloFAS upstream inflow with your own hourly discharge
```

Needs internet (free, **no API key**): Open-Meteo forecast + ERA5 archive + GloFAS flood API + Copernicus DEM.
Data are fetched at start, refreshed every 25 min in the background (`/api/refresh_live` forces it) and cached in
`naditwin/rivers/<river>/live_cache_*.json` / `dem_cache.json`. If the internet drops, the last good fetch is used and
the dashboard says **"STALE"**; with no cache at all it says **"synthetic (no feed)"** — it never shows made-up
numbers as live.

## History since 1 Jan 2021

Every hourly series (rain, weather, GloFAS inflow, discharge at every chainage, stage, margins) starts on
**1 Jan 2021**. The long history is downloaded once and kept on disk per river:

```
naditwin/rivers/<river>/archive_<river>_2021.json … _2026.json   ERA5 hourly rain/weather, one file per year
naditwin/rivers/<river>/glofas_history_<river>.json              GloFAS daily discharge since 1 Jan 2021
```

A finished year is downloaded once and never again; only the current year is refreshed (about once a day). The
download runs in the **background** — the dashboard opens straight away and the historic view fills in as each river
finishes (the "archive" note above the weather tiles says which years are still downloading). The first time this
takes about **12 minutes for the three rivers**, because the requests are paced to stay inside Open-Meteo's free
limits (600 calls/min, 5 000/h, 10 000/day). To start the history at another date, set
`NADITWIN_HISTORY_START=2019-01-01` (for example) before `python run.py`.

## Troubleshooting: every river shows Mula-Mutha locations

That means an **older NadiTwin server is still running** on the same port (on Windows two servers can share port
8080 and answer requests at random; the old one only knows Mula-Mutha). Close every old server window (or end the old
`python.exe` in Task Manager) and run `python run.py` **once**. `run.py` now refuses to start if the port is already
taken, the server binds the port exclusively on Windows, an unknown `?river=` gets a 404 instead of Mula-Mutha data,
and the dashboard shows a red warning bar if an old server answers.

## Using the dashboard

1. **River polygon KML** (required) — hand-traced bank polygon or a centreline LineString.
2. **Chainage KML** (optional) — chainage points, e.g. every 10 m named `0+010` or with an ExtendedData `chainage_m`.
   Stations are then exactly your chainage (labels kept); width is measured perpendicular to the river at every station.
3. **Water-depth KML** (optional) — a GroundOverlay depth image (light blue = shallow, dark navy = deep, range in the
   description, e.g. the Sentinel-2 `water_depth` KML). Decoded with the standard library (Pillow used if installed).
4. **Locations Excel** (optional) — columns `Location | Longitude | Latitude` (or `Location | Chainage`), e.g.
   Sangam … Manjari. Each location is snapped to the nearest chainage point and sorted along the river; the river is
   then cut into named **stretches between consecutive locations**: "Sangam → Bund Garden", "Bund Garden → Dhanori",
   … (plus "River start → first" and "last → River end"). Points more than 1 km from the river are flagged. You can
   add/replace the Excel later for the open river with **"Set locations (Excel)…"**.
5. Click **Run** (the upload form is under **"＋ Add another river"**). Or just click one of the river buttons at the
   top — Mula-Mutha, Mithi and Mandovi are built in, with their locations from `locations.xlsx`.

The **Location (stretch)** dropdown (or a click on a location / stretch on the map, or its margin-board card) zooms
the map to that stretch; the 10 m discharge chart + table, river stage, hydrograph and rating curve follow the
selection. **⬇ Excel — this location** / **⬇ Excel — all locations** download every chainage (10 m) with Q now,
+6/+24/+72 h (P10–P90), 72 h peak, velocity, depth, stage, danger level, margin, zone and rain; the all-locations
workbook has a Summary sheet, an "All chainages" sheet and one sheet per stretch.

Flow direction is checked on the DEM: a polygon-derived chainage is re-numbered so that 0 = upstream; a chainage
KML is kept exactly as given and the model routes water in the direction the terrain falls (shown in the upload notes).

## How the live numbers are made

| Step | What | Source |
|---|---|---|
| Rain & weather | hourly rain, temperature, humidity, wind, ET0 at 6 points along the reach, interpolated to every km; **1 Jan 2021** → +16 days | Open-Meteo ERA5 archive + forecast |
| Upstream inflow | daily GloFAS v4 discharge at the upstream end (hourly-interpolated) + its ensemble p25/median/p75 for the forecast | Open-Meteo flood API |
| Discharge every 1 km | `Q(km) = route(Q(km-1)) + storm runoff + urban inflow − evaporation` | model, see below |
| Discharge every 10 m | inside each km: `Q(station) = Qin(t − f·lag) + f·[Q_km_out(t) − Qin(t − lag)]`, f = share of the km already passed — the km's lateral inflow is added evenly along it and the flood wave arrives progressively | model |
| Storm runoff | rain × runoff coefficient (rises with antecedent rain, falls with high ET0) × local catchment (5 km² per km), two linear reservoirs | model |
| Urban inflow | STP outfalls / nallas, 0.12 m³/s per km, daily sewage cycle (low at 4 am, peaks ~9 am & ~8 pm) | **assumed** |
| Evaporation | 1.05 × ET0 × water-surface area of that km | live ET0 |
| Routing | travel time per km = length / kinematic-wave celerity (5/3 × Manning velocity) → floods move faster than low flow | model |
| Terrain | water-surface slope + datum (m WSE) from a robust fit of DEM samples along the centreline; bank height per km from DEM samples beyond both water edges (15th percentile, lower bank, clamped 3–7 m) | Copernicus GLO-90 via Open-Meteo |
| River stage | Manning with the 500 m hydraulic width of every station; above the bank crest the flow spreads on a floodplain 3 × wider. With a depth KML: surveyed depth = depth at the live discharge on the image date, stage moves with live discharge from there | model + your KMLs |
| Forecast band | 50 members: GloFAS ensemble spread on the inflow, 25→60 % rain-forecast error (+-2 h timing) on storm runoff, 15 % on urban inflow | model |
| Margin board | margin = (crest − 0.8 m) − stage at the **most vulnerable station of each km**. SAFE > 1.7 m · WATCH ≤ 1.7 · WARNING ≤ 0.7 · DANGER ≤ 0; status = worst of current margin, 72 h median-forecast margin and 72 h exceedance probability (≥10 % watch, ≥30 % warning, ≥60 % danger) | |

**Still ASSUMED (please calibrate):** Manning n (0.035), urban inflow per km, runoff parameters, the 90 m-DEM bank
heights (a surface model — buildings and trees creep in), and dam releases (Khadakwasla etc. are not seen by GloFAS).
Override any of them per river in `naditwin/rivers/<river>/params.json`:

```json
{ "manning_n": 0.035, "dwf_m3s_per_km": 0.12, "area_km2_per_km": 5.0, "slope": 0.0004,
  "bank_height_m": [4.5, 4.5, 3.8, ...one value per km...], "floodplain_width_factor": 3.0, "flow_dir": 1 }
```

## Dashboard panels

- **Map** — polygon, centreline, 1 km / 100 m chainage ticks, and every km coloured by its margin-board zone.
- **Live rainfall, weather & chainage-wise discharge** — weather tiles, and for the selected stretch a chart of Q now /
  +24 h / +72 h / rain **at every 10 m chainage** plus one merged table per chainage (width, Q now, Q +24 h and +72 h
  with P10–P90, 72 h peak, velocity, depth, stage, danger level, margin, zone, rain) and the Excel downloads.
  Per-km components (upstream inflow, storm runoff, urban inflow, evaporation) stay available at `/api/live`.
- **River stage** — water surface along the selected km (now and at any forecast lead), bed and bank crest (m WSE).
- **Hydrograph** — stage + discharge + rainfall bars at the selected km, 72 h / 7 d / 30 d / 90 d / 1 year / all history
  since 1 Jan 2021 + 72 h, or one calendar year (**Year 2021 … Year 2025**, and the current year up to now + 72 h
  forecast) with a summary of that year: peak discharge and when, peak stage, rain total, hours above
  watch / warning / danger
  forecast, with the watch / warning / danger levels.
- **Rating curve** — stage ↔ discharge at that station and the discharge needed to reach each zone.
- **Margin board** — per location (from your Excel) or every 1 km of chainage (toggle).
- **Live / Historic** — any hour since **1 Jan 2021** (e.g. the July 2021 floods) re-renders every panel.

## API

`/api/rivers`, `/api/endpoints?river=`, `/api/sources?river=`, `POST /api/rebuild_river?river=`, `/api/timerange`, `/api/meta`, `/api/state`, `/api/live`, `/api/discharge_profile`,
`/api/hydrograph?cell=&hours=72|168|720|2160|8760|all` or `?cell=&year=2021`, `/api/rating?cell=`, `/api/today?cell=&tz=`,
`/api/forecast/profile?lead=`, `/api/stations?loc=L2|from_km=&to_km=` (+ `/api/stations.csv`, `/api/stations.xlsx?loc=L2|all`), `/api/locations`,
`/api/margins_locations`, `POST /api/upload_localities?river=` (JSON `{"filename","data_b64"}`), `/api/margins_chainage?step_km=1`, `/api/alerts_chainage`, `/api/margins`,
`/api/alerts`, `/api/scorecard`, `/api/refresh_live`,
`POST /api/upload_kml?name=` (body = raw KML, or JSON `{"polygon":…, "chainage":…, "depth":…}`),
`POST /api/remove_river?name=`. Every GET takes `?river=<name>` (`Mula-Mutha`, `Mithi`, `Mandovi`; any letter case or the
folder key also works) and optional `?at=2021-07-22T09:00:00Z` (historic, any hour since 1 Jan 2021).

## Layout

```
run.py                       launcher
naditwin/engine.py           river registry + twin engine (terrain, stage, ensemble, hydrograph, rating, margins)
naditwin/live_data.py        Open-Meteo / GloFAS / DEM fetchers + rainfall-runoff-routing model (stdlib only)
naditwin/kml_ingest.py       polygon / chainage / depth KML -> river folder (stdlib only)
naditwin/river_sources.py    builds the 3 built-in rivers from Digital _twin_kml/<River>_river/ (auto, at start)
naditwin/server.py           HTTP server + API
naditwin/xlsx_writer.py      Excel (.xlsx) export, standard library only
naditwin/static/dashboard.html
naditwin/rivers/mula_mutha_live/   Mula-Mutha built from the 10 m chainage + polygon + Sentinel-2 depth KMLs
naditwin/rivers/mithi/             Mithi   (built automatically from Digital _twin_kml/Mithi_river)
naditwin/rivers/mandovi/           Mandovi (built automatically from Digital _twin_kml/Mandovi_river)
Digital _twin_kml/                 source KMLs + locations.xlsx for the three built-in rivers
build_mula_mutha_live.py     offline rebuild of that folder (numpy + Pillow); the dashboard upload does the same
```
