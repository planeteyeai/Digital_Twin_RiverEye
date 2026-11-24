#!/usr/bin/env python3
"""Build the Mula-Mutha 'live' river folder from the user's THREE KMLs.

  --chainage  Mula-Mutha River - 10 m ch kml.kml   (1681 Point placemarks, 10 m apart; 0 = upstream)
  --polygon   mula_mutha_river.kml                  (hand-traced river polygon -> bank-to-bank width)
  --depth     water_depth_5.kml                     (Sentinel-2 GroundOverlay PNG, light blue = shallow,
                                                     dark navy = deep, range stated in the KML description)

Output: naditwin/rivers/mula_mutha_live/
    chainage_profile.json  [{chainage_m, lon, lat, width_m}]  (10 m stations)
    reach_polygon.json     [[lon, lat], ...]
    depth_profile.json     [{chainage_m, depth_m, flagged}]    (relative depth at survey date)
    landmarks.json
    survey.json            (depth image date + range, used by the engine to calibrate stage)

Dev tool: needs numpy + Pillow (see requirements.txt). The running app itself is stdlib-only.
"""
import argparse, base64, io, json, math, os, re
import xml.etree.ElementTree as ET
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))


def _t(tag):
    return tag.split("}")[-1]


def parse_chainage(path):
    root = ET.parse(path).getroot()
    pts = []
    for pm in root.iter():
        if _t(pm.tag) != "Placemark":
            continue
        ch = lon = lat = None
        for d in pm.iter():
            if _t(d.tag) == "Data" and d.get("name") == "chainage_m":
                ch = float(d[0].text)
            if _t(d.tag) == "coordinates":
                x, y = d.text.strip().split(",")[:2]
                lon, lat = float(x), float(y)
        if ch is not None and lon is not None:
            pts.append((ch, lon, lat))
    pts.sort()
    return pts


def parse_polygon(path):
    root = ET.parse(path).getroot()
    for el in root.iter():
        if _t(el.tag) == "coordinates":
            ring = []
            for tok in el.text.split():
                p = tok.split(",")
                if len(p) >= 2:
                    ring.append((float(p[0]), float(p[1])))
            if len(ring) > 10:
                return ring
    raise SystemExit("no polygon found")


def parse_depth_overlay(path):
    txt = open(path, encoding="utf-8").read()
    b64 = re.search(r"base64,([A-Za-z0-9+/=]+)", txt).group(1)
    img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGBA")
    box = {k: float(re.search(rf"<(?:\w+:)?{k}>([^<]+)<", txt).group(1)) for k in ("north", "south", "east", "west")}
    desc = re.search(r"<(?:\w+:)?description>([^<]*)<", txt).group(1)
    m = re.search(r"Depth range:\s*([\d.]+)-([\d.]+)\s*m", desc)
    dmin, dmax = (float(m.group(1)), float(m.group(2))) if m else (0.25, 2.6)
    di = re.search(r"Image date:\s*([\d-]+)", desc)
    return img, box, dmin, dmax, (di.group(1) if di else None), desc


def to_xy(lon, lat, lon0, lat0):
    k = 111320.0
    return ((lon - lon0) * k * math.cos(math.radians(lat0)), (lat - lat0) * k)


def ray_hit(px, py, dx, dy, ring_xy, max_d=800.0):
    """nearest distance along (dx,dy) from (px,py) to the polygon boundary; None if no hit."""
    best = None
    n = len(ring_xy)
    for i in range(n - 1):
        ax, ay = ring_xy[i]; bx, by = ring_xy[i + 1]
        ex, ey = bx - ax, by - ay
        den = dx * ey - dy * ex
        if abs(den) < 1e-12:
            continue
        t = ((ax - px) * ey - (ay - py) * ex) / den
        u = ((ax - px) * dy - (ay - py) * dx) / den
        if t > 0.5 and 0 <= u <= 1 and t <= max_d:
            if best is None or t < best:
                best = t
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chainage", required=True)
    ap.add_argument("--polygon", required=True)
    ap.add_argument("--depth", required=True)
    ap.add_argument("--out", default=os.path.join(HERE, "naditwin", "rivers", "mula_mutha_live"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    st = parse_chainage(a.chainage)
    ring = parse_polygon(a.polygon)
    img, box, dmin, dmax, img_date, desc = parse_depth_overlay(a.depth)
    print(f"{len(st)} stations, polygon {len(ring)} pts, depth image {img.size} date={img_date} range {dmin}-{dmax}")

    lon0, lat0 = st[0][1], st[0][2]
    ring_xy = [to_xy(x, y, lon0, lat0) for x, y in ring]
    xy = [to_xy(lo, la, lon0, lat0) for _, lo, la in st]

    # ---- width: perpendicular chord through polygon ----
    raw_w = []
    for i, (cx, cy) in enumerate(xy):
        j0, j1 = max(0, i - 3), min(len(xy) - 1, i + 3)
        tx, ty = xy[j1][0] - xy[j0][0], xy[j1][1] - xy[j0][1]
        L = math.hypot(tx, ty) or 1.0
        nx, ny = -ty / L, tx / L
        h1 = ray_hit(cx, cy, nx, ny, ring_xy)
        h2 = ray_hit(cx, cy, -nx, -ny, ring_xy)
        if h1 is not None and h2 is not None:
            raw_w.append(h1 + h2)
        elif h1 is not None or h2 is not None:
            raw_w.append(2 * (h1 if h1 is not None else h2))
        else:
            raw_w.append(float("nan"))
    w = np.array(raw_w)
    good = ~np.isnan(w)
    w = np.interp(np.arange(len(w)), np.where(good)[0], w[good])
    # median-smooth over ~100 m to kill ray-grazing spikes
    k = 5
    wp = np.pad(w, k, mode="edge")
    w = np.array([np.median(wp[i:i + 2 * k + 1]) for i in range(len(w))])
    w = np.clip(w, 15.0, 600.0)

    # ---- depth: sample the Sentinel-2 raster around each station ----
    a_ = np.array(img)
    rgb = a_[..., :3].astype(float)
    alpha = a_[..., 3]
    edge = (a_[..., 0] == 0) & (a_[..., 1] == 0) & (a_[..., 2] == 255)           # resampling halo
    marker = (a_[..., 0] > 40) & (a_[..., 1] > 240) & (a_[..., 2] > 240) & (a_[..., 0] < 60)  # cyan pin
    valid = (alpha > 0) & ~edge & ~marker
    lum = 0.30 * rgb[..., 0] + 0.59 * rgb[..., 1] + 0.11 * rgb[..., 2]
    lo, hi = np.percentile(lum[valid], [2, 98])
    depth_img = np.where(valid, dmin + (1 - np.clip((lum - lo) / (hi - lo), 0, 1)) * (dmax - dmin), np.nan)
    H, W = depth_img.shape
    px_m_x = (box["east"] - box["west"]) / W * 111320.0 * math.cos(math.radians(lat0))
    px_m_y = (box["north"] - box["south"]) / H * 111320.0
    rad_px_x = max(2, int(round(12.0 / px_m_x))); rad_px_y = max(2, int(round(12.0 / px_m_y)))
    print(f"raster pixel ~{px_m_x:.1f} x {px_m_y:.1f} m, sampling radius {rad_px_x}px")

    depth = []
    flagged = []
    for ch, lon, lat in st:
        c = (lon - box["west"]) / (box["east"] - box["west"]) * W
        r = (box["north"] - lat) / (box["north"] - box["south"]) * H
        c0, r0 = int(round(c)), int(round(r))
        win = depth_img[max(0, r0 - rad_px_y):r0 + rad_px_y + 1, max(0, c0 - rad_px_x):c0 + rad_px_x + 1]
        v = win[~np.isnan(win)]
        if v.size >= 6:
            depth.append(float(np.median(v)))
            flagged.append(False)
        else:
            depth.append(float("nan"))
            flagged.append(True)
    d = np.array(depth)
    g = ~np.isnan(d)
    d = np.interp(np.arange(len(d)), np.where(g)[0], d[g])
    kk = 3
    dp = np.pad(d, kk, mode="edge")
    d = np.array([np.mean(dp[i:i + 2 * kk + 1]) for i in range(len(d))])
    print(f"depth: min {d.min():.2f} mean {d.mean():.2f} max {d.max():.2f}; {sum(flagged)} stations interpolated")

    json.dump([{"chainage_m": round(ch, 1), "lon": round(lo_, 7), "lat": round(la_, 7), "width_m": round(float(wi), 1)}
               for (ch, lo_, la_), wi in zip(st, w)], open(os.path.join(a.out, "chainage_profile.json"), "w"))
    json.dump([[round(x, 7), round(y, 7)] for x, y in ring], open(os.path.join(a.out, "reach_polygon.json"), "w"))
    json.dump([{"chainage_m": round(ch, 1), "depth_m": round(float(di), 3), "flagged": bool(fl)}
               for (ch, _, _), di, fl in zip(st, d, flagged)], open(os.path.join(a.out, "depth_profile.json"), "w"))
    json.dump({"image_date": img_date, "depth_min_m": dmin, "depth_max_m": dmax, "note": desc,
               "source": "Sentinel-2 relative depth overlay (water_depth KML)"},
              open(os.path.join(a.out, "survey.json"), "w"), indent=1)

    # landmarks: only well-known localities that actually sit on the traced reach (<300 m from it)
    cands = [("Sangam (Mula-Mutha confluence)", 73.8602, 18.5312, "confluence"),
             ("Bund Garden", 73.8831, 18.5430, "bridge")]
    lms = []
    for name, lo_, la_, typ in cands:
        best = min(st, key=lambda s: (s[1] - lo_) ** 2 + (s[2] - la_) ** 2)
        dist = math.hypot(*to_xy(best[1], best[2], lo_, la_))
        if dist <= 300:
            lms.append({"name": name, "chainage_m": round(best[0], 1), "chainage_km": round(best[0] / 1000, 3),
                        "lon": best[1], "lat": best[2], "type": typ, "source": "snapped", "snap_distance_m": round(dist, 1)})
    json.dump(lms, open(os.path.join(a.out, "landmarks.json"), "w"), indent=1)
    print("landmarks:", [l["name"] for l in lms], " reach", st[-1][0], "m ->", a.out)


if __name__ == "__main__":
    main()
