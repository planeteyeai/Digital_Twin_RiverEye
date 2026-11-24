"""
kml_ingest.py — turn ANY uploaded river KML (polygon trace or centerline
LineString, optionally with named Placemark points for bridges/localities)
into the same on-disk shape the rest of NadiTwin already expects:

    naditwin/rivers/<slug>/chainage_profile.json   [{chainage_m,lon,lat,width_m}, ...]
    naditwin/rivers/<slug>/reach_polygon.json      [[lon,lat], ...]   (best-effort; may
                                                     just be a thin buffer around the
                                                     centerline if the KML was a bare
                                                     LineString with no polygon)
    naditwin/rivers/<slug>/landmarks.json          [{name,chainage_m,chainage_km,lon,lat,
                                                      type,source,snap_distance_m}, ...]

Standard library only — no shapely/pyproj/scipy/networkx — so this can run
inside the live server process on any upload, not just as an offline dev
script.

WHAT'S REAL vs BEST-EFFORT for an uploaded river, honestly:
  - If the KML has a LineString: that IS the real centerline, used as-is.
  - If the KML only has a Polygon (hand-traced river banks, the common
    Google Earth / QGIS style): the centerline is DERIVED by pairing the
    two bank arcs point-for-point and taking their midpoints. This is a
    fast, dependency-free approximation of the true medial axis — good
    for a reasonably straight/elongated reach, weaker on tight meanders
    or braided channels. Width is the real bank-to-bank distance from the
    KML at each paired station, not synthetic.
  - Named Point placemarks in the KML (e.g. "XYZ Bridge", "Sangam") are
    used as real, KML-sourced landmarks, snapped to the nearest chainage
    station.
  - If the KML has NO named points at all, illustrative bridge/locality
    markers are auto-placed at a plausible spacing so the dashboard has
    something to show margins/alerts against — these are clearly tagged
    "source": "auto" (both in the JSON and in the dashboard UI) and are
    NOT real bridge locations. Replace them by adding named Point
    placemarks to your KML and re-uploading.
"""

import json
import math
import os
import random
import re
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
RIVERS_DIR = os.path.join(HERE, "rivers")

DEFAULT_CHAINAGE_STEP_M = 100.0   # user-requested: chainage-wise change every 100 m
DEFAULT_WIDTH_M = 25.0            # fallback when KML gives no width info (LineString-only)
MAX_RING_POINTS = 1600            # decimate absurdly dense polygons before the O(n) diameter walk

BRIDGE_WORDS = ("bridge", "pul", "setu", "causeway", "crossing", "flyover", "culvert")
CONFLUENCE_WORDS = ("sangam", "confluence", "meets", "mouth", "creek mouth")


class KmlIngestError(ValueError):
    pass


# --------------------------------- parsing ---------------------------------

def _local(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def _find_coords_text(el):
    for node in el.iter():
        if _local(node.tag) == "coordinates" and node.text and node.text.strip():
            return node.text.strip()
    return None


def _parse_coords(text):
    pts = []
    for tok in re.split(r"\s+", text.strip()):
        if not tok:
            continue
        parts = tok.split(",")
        if len(parts) < 2:
            continue
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        pts.append((lon, lat))
    return pts


def parse_kml(kml_text):
    """Returns (linestrings, polygons, points):
       linestrings = [(name, [(lon,lat), ...]), ...]
       polygons    = [(name, [(lon,lat), ...]), ...]   (outer boundary ring)
       points      = [(name, lon, lat), ...]
    """
    try:
        root = ET.fromstring(kml_text)
    except ET.ParseError as e:
        raise KmlIngestError(f"Could not parse KML/XML: {e}")

    linestrings, polygons, points = [], [], []

    placemarks = [el for el in root.iter() if _local(el.tag) == "Placemark"]
    if not placemarks:
        # some exports skip <Placemark> and put geometry straight under the doc;
        # treat the whole document as one unnamed placemark
        placemarks = [root]

    for pm in placemarks:
        pname = ""
        for child in pm:
            if _local(child.tag) == "name" and child.text:
                pname = child.text.strip()
                break

        for geom in pm.iter():
            gt = _local(geom.tag)
            if gt == "Point":
                ct = _find_coords_text(geom)
                if ct:
                    pts = _parse_coords(ct)
                    if pts:
                        points.append((pname, pts[0][0], pts[0][1]))
            elif gt == "LineString":
                ct = _find_coords_text(geom)
                if ct:
                    pts = _parse_coords(ct)
                    if len(pts) >= 2:
                        linestrings.append((pname, pts))
            elif gt == "Polygon":
                outer = None
                for node in geom.iter():
                    if _local(node.tag) == "outerBoundaryIs":
                        outer = node
                        break
                ct = _find_coords_text(outer if outer is not None else geom)
                if ct:
                    pts = _parse_coords(ct)
                    if len(pts) >= 3:
                        polygons.append((pname, pts))

    if not linestrings and not polygons:
        raise KmlIngestError(
            "No LineString or Polygon geometry found in this KML — "
            "need at least one river centerline or traced river-bank polygon."
        )
    return linestrings, polygons, points


# ------------------------------- geometry math -------------------------------

def haversine_m(lon1, lat1, lon2, lat2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def _cum_dist(pts):
    """pts: [(lon,lat), ...] -> cumulative haversine distance at each point."""
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + haversine_m(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1]))
    return cum


def _resample_by_arclength(pts, n_out):
    """Resample a polyline to n_out evenly-arclength-spaced points."""
    if len(pts) == n_out:
        return list(pts)
    cum = _cum_dist(pts)
    total = cum[-1]
    if total <= 0:
        return [pts[0]] * n_out
    out = []
    j = 0
    for k in range(n_out):
        d = total * k / (n_out - 1) if n_out > 1 else 0.0
        while j < len(cum) - 2 and cum[j + 1] < d:
            j += 1
        seg = cum[j + 1] - cum[j]
        t = 0.0 if seg <= 0 else (d - cum[j]) / seg
        lon = pts[j][0] + (pts[j + 1][0] - pts[j][0]) * t
        lat = pts[j][1] + (pts[j + 1][1] - pts[j][1]) * t
        out.append((lon, lat))
    return out


def _decimate(pts, max_n):
    if len(pts) <= max_n:
        return pts
    step = len(pts) / max_n
    idx = sorted(set(int(i * step) for i in range(max_n)))
    return [pts[i] for i in idx]


def _point_in_polygon(x, y, poly):
    """Ray-casting point-in-polygon test. poly: [(lon,lat), ...], unclosed."""
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-15) + xi):
            inside = not inside
        j = i
    return inside


def _closest_point_on_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ax, ay
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return ax + t * dx, ay + t * dy


def _nearest_point_on_ring(x, y, poly):
    best, best_d = None, float("inf")
    n = len(poly)
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        cx, cy = _closest_point_on_segment(x, y, ax, ay, bx, by)
        d = (cx - x) ** 2 + (cy - y) ** 2
        if d < best_d:
            best_d, best = d, (cx, cy)
    return best


def _keep_inside_polygon(centerline, poly):
    """Safety net so the drawn centerline NEVER steps outside the traced
    river-bank polygon: the bank-pairing midpoint can occasionally land
    just outside on a tight meander or a non-convex hand-trace. Any point
    found outside is nudged toward the polygon centroid at increasing
    strength until it lands inside; if the shape is winding enough that
    even that fails, it's pulled toward the previous already-inside
    centerline point instead (chainage stations are ~100 m apart, so a
    neighbor is always a safe, very-close substitute) — so the reach line
    stays within the KML polygon end to end, no exceptions."""
    if len(poly) < 3:
        return centerline
    ring = poly[:-1] if (len(poly) > 2 and poly[0] == poly[-1]) else list(poly)
    cx = sum(p[0] for p in ring) / len(ring)
    cy = sum(p[1] for p in ring) / len(ring)
    out = []
    for (x, y) in centerline:
        if _point_in_polygon(x, y, ring):
            out.append((x, y))
            continue
        nx, ny = _nearest_point_on_ring(x, y, ring)
        fixed = None
        for frac in (0.01, 0.05, 0.15, 0.35, 0.6, 0.85):
            tx = nx + (cx - nx) * frac
            ty = ny + (cy - ny) * frac
            if _point_in_polygon(tx, ty, ring):
                fixed = (tx, ty)
                break
        if fixed is None and out:
            px, py = out[-1]
            mid = ((nx + px) / 2.0, (ny + py) / 2.0)
            fixed = mid if _point_in_polygon(mid[0], mid[1], ring) else (px, py)
        if fixed is None:
            fixed = (nx, ny)   # first point, worst case — right on the boundary
        out.append(fixed)
    return out


def _farthest_pair_approx(pts):
    """2-pass farthest-point heuristic: cheap O(n) approx of the polygon
    'diameter' endpoints — used as the two ends of the river reach."""
    a0 = pts[0]
    ia = max(range(len(pts)), key=lambda i: haversine_m(a0[0], a0[1], pts[i][0], pts[i][1]))
    a = pts[ia]
    ib = max(range(len(pts)), key=lambda i: haversine_m(a[0], a[1], pts[i][0], pts[i][1]))
    return (ia, ib) if ia < ib else (ib, ia)


def _centerline_from_polygon_bankpair(ring):
    """ring: closed or unclosed [(lon,lat), ...] hand-traced river-bank polygon.
    Returns (centerline_pts, width_at_each_pt) via bank-pairing midpoints."""
    pts = list(ring)
    if len(pts) > 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    pts = _decimate(pts, MAX_RING_POINTS)
    if len(pts) < 6:
        raise KmlIngestError("Traced polygon has too few vertices to derive a centerline.")

    ia, ib = _farthest_pair_approx(pts)
    if ia == ib:
        raise KmlIngestError("Could not find two distinct ends of the reach in this polygon.")

    arc1 = pts[ia:ib + 1]                                   # A -> B, one bank
    arc2_raw = pts[ib:] + pts[:ia + 1]                       # B -> ... -> A, other bank
    arc2 = list(reversed(arc2_raw))                          # A -> B, same direction as arc1

    n = max(60, min(2000, max(len(arc1), len(arc2))))
    b1 = _resample_by_arclength(arc1, n)
    b2 = _resample_by_arclength(arc2, n)

    centerline, widths = [], []
    for p1, p2 in zip(b1, b2):
        centerline.append(((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0))
        widths.append(haversine_m(p1[0], p1[1], p2[0], p2[1]))
    return centerline, widths


# ---------------------- medial-axis (raster skeleton) centerline ----------------------
#
# The bank-pairing method above walks the two banks in lockstep by resampled
# ARC-LENGTH INDEX. That assumes both banks wiggle in sync — true for a
# gently curving reach, false for a tight meander or a sharp real-world bend
# (a hand-traced polygon around a river with a kink, an S-curve, or a bend
# near a bridge/road crossing): one bank's arc-length races ahead of the
# other's through the bend, same-index points on each bank stop being
# laterally opposite each other, and the midpoint "centerline" starts
# jumping sideways and doubling back on itself — exactly the tangled,
# self-crossing line this rewrite fixes.
#
# This replacement computes a real medial axis instead, entirely in the
# standard library (no numpy/scipy/opencv — this runs live in the server
# process on every upload):
#   1. Project the polygon to a local metre grid (equirectangular, small
#      enough area that this is negligible error).
#   2. Rasterize the polygon with a scanline fill.
#   3. Chamfer distance-transform the raster (distance to nearest bank).
#   4. Zhang-Suen morphological thinning -> a 1-pixel-wide skeleton.
#   5. Prune short spurs (trace noise / small forks), find the two
#      farthest-apart endpoints, and take the shortest path between them
#      through the skeleton graph -> one single, ordered, non-crossing
#      centerline, however sharply the reach bends.
#   6. Width at each point = 2x the distance-transform value there (the
#      inscribed-circle radius), i.e. the real local bank-to-bank width,
#      not a placeholder.

def _shoelace_area(pts):
    s = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def _project_local_xy(pts):
    """Equirectangular lon/lat -> local metres, centered on the ring so the
    small-angle approximation stays accurate for a single reach's extent."""
    lon0 = sum(p[0] for p in pts) / len(pts)
    lat0 = sum(p[1] for p in pts) / len(pts)
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(lat0)) or 1e-6

    def to_xy(lon, lat):
        return ((lon - lon0) * m_per_deg_lon, (lat - lat0) * m_per_deg_lat)

    def to_lonlat(x, y):
        return (lon0 + x / m_per_deg_lon, lat0 + y / m_per_deg_lat)

    return to_xy, to_lonlat


def _choose_grid_cell_m(ring_xy, target_cells=200000, px_across_width=16.0):
    """Pick a grid cell size (metres) fine enough to resolve the channel
    width in ~px_across_width pixels, but coarse enough that the whole
    bounding box stays under target_cells — so a long reach doesn't blow up
    runtime/memory.

    Width is estimated as area / (perimeter/2), NOT area / bbox-diagonal:
    a bounding box collapses for any reach that folds back on itself (a
    hairpin bend, a tight S, a river that doubles back near a bridge) —
    the bbox stays small while the true reach length doesn't, which made
    this badly UNDER-estimate the true length and so badly OVER-estimate
    the width, handing back a cell far too coarse to resolve a real
    channel width once folded — the exact shape this rewrite exists to
    handle correctly. Perimeter/2 tracks the true along-channel length
    however much the reach bends or folds, so the resulting cell size
    stays right-sized for the actual channel width."""
    area = abs(_shoelace_area(ring_xy))
    n = len(ring_xy)
    perimeter = sum(
        math.hypot(ring_xy[(i + 1) % n][0] - ring_xy[i][0], ring_xy[(i + 1) % n][1] - ring_xy[i][1])
        for i in range(n)
    )
    length_est = max(perimeter / 2.0, 1.0)
    width_est = max(3.0, area / length_est)
    cell = max(1.0, width_est / px_across_width)
    xs = [p[0] for p in ring_xy]
    ys = [p[1] for p in ring_xy]
    bbox_w = max(xs) - min(xs)
    bbox_h = max(ys) - min(ys)
    nx = bbox_w / cell + 4
    ny = bbox_h / cell + 4
    tries = 0
    while nx * ny > target_cells and tries < 40:
        cell *= 1.25
        nx = bbox_w / cell + 4
        ny = bbox_h / cell + 4
        tries += 1
    return cell


def _rasterize_polygon_scanline(ring_xy, cell):
    """Scanline polygon fill onto a boolean grid (bytearray rows). Returns
    (grid, nx, ny, ox, oy) where ox/oy is the metre coordinate of cell (0,0)'s
    corner, so cell (r, c)'s center is at (ox+(c+.5)*cell, oy+(r+.5)*cell)."""
    xs = [p[0] for p in ring_xy]
    ys = [p[1] for p in ring_xy]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    ox, oy = minx - 2 * cell, miny - 2 * cell
    nx = max(3, int((maxx - minx) / cell) + 5)
    ny = max(3, int((maxy - miny) / cell) + 5)

    n = len(ring_xy)
    edges = []
    for i in range(n):
        x0, y0 = ring_xy[i]
        x1, y1 = ring_xy[(i + 1) % n]
        if y0 != y1:
            edges.append((x0, y0, x1, y1))

    grid = [bytearray(nx) for _ in range(ny)]
    for row in range(ny):
        y = oy + (row + 0.5) * cell
        hits = []
        for (x0, y0, x1, y1) in edges:
            if (y0 <= y < y1) or (y1 <= y < y0):
                t = (y - y0) / (y1 - y0)
                hits.append(x0 + t * (x1 - x0))
        hits.sort()
        grow = grid[row]
        for k in range(0, len(hits) - 1, 2):
            ca = max(0, int((hits[k] - ox) / cell))
            cb = min(nx - 1, int((hits[k + 1] - ox) / cell))
            for c in range(ca, cb + 1):
                grow[c] = 1
    return grid, nx, ny, ox, oy


def _chamfer_distance_m(grid, nx, ny, cell):
    """Two-pass chamfer (3-4 style, using true diagonal weight) distance
    transform: metres from each inside cell to the nearest outside cell."""
    INF = float("inf")
    dist = [[INF] * nx for _ in range(ny)]
    for r in range(ny):
        grow = grid[r]
        drow = dist[r]
        for c in range(nx):
            if not grow[c]:
                drow[c] = 0.0
    diag = 2.0 ** 0.5
    for r in range(ny):
        drow = dist[r]
        prow = dist[r - 1] if r > 0 else None
        for c in range(nx):
            if drow[c] == 0.0:
                continue
            best = drow[c]
            if c > 0 and drow[c - 1] + 1.0 < best:
                best = drow[c - 1] + 1.0
            if prow is not None:
                if prow[c] + 1.0 < best:
                    best = prow[c] + 1.0
                if c > 0 and prow[c - 1] + diag < best:
                    best = prow[c - 1] + diag
                if c + 1 < nx and prow[c + 1] + diag < best:
                    best = prow[c + 1] + diag
            drow[c] = best
    for r in range(ny - 1, -1, -1):
        drow = dist[r]
        nrow = dist[r + 1] if r + 1 < ny else None
        for c in range(nx - 1, -1, -1):
            if drow[c] == 0.0:
                continue
            best = drow[c]
            if c + 1 < nx and drow[c + 1] + 1.0 < best:
                best = drow[c + 1] + 1.0
            if nrow is not None:
                if nrow[c] + 1.0 < best:
                    best = nrow[c] + 1.0
                if c > 0 and nrow[c - 1] + diag < best:
                    best = nrow[c - 1] + diag
                if c + 1 < nx and nrow[c + 1] + diag < best:
                    best = nrow[c + 1] + diag
            drow[c] = best
    for r in range(ny):
        drow = dist[r]
        for c in range(nx):
            if drow[c] != INF:
                drow[c] *= cell
    return dist


def _zhang_suen_thin(grid, nx, ny, max_iter=300):
    """Standard Zhang-Suen thinning -> 1-pixel-wide skeleton. Pure stdlib,
    operates directly on 0/1 rows."""
    img = [bytearray(row) for row in grid]

    def neighbors(r, c):
        return (
            img[r - 1][c], img[r - 1][c + 1], img[r][c + 1], img[r + 1][c + 1],
            img[r + 1][c], img[r + 1][c - 1], img[r][c - 1], img[r - 1][c - 1],
        )

    it = 0
    changed = True
    while changed and it < max_iter:
        changed = False
        it += 1
        for step in (0, 1):
            to_clear = []
            for r in range(1, ny - 1):
                row = img[r]
                for c in range(1, nx - 1):
                    if not row[c]:
                        continue
                    P = neighbors(r, c)
                    B = sum(P)
                    if B < 2 or B > 6:
                        continue
                    A = sum(1 for i in range(8) if not P[i] and P[(i + 1) % 8])
                    if A != 1:
                        continue
                    if step == 0:
                        if P[0] and P[2] and P[4]:
                            continue
                        if P[2] and P[4] and P[6]:
                            continue
                    else:
                        if P[0] and P[2] and P[6]:
                            continue
                        if P[0] and P[4] and P[6]:
                            continue
                    to_clear.append((r, c))
            if to_clear:
                changed = True
                for (r, c) in to_clear:
                    img[r][c] = 0
    return img


_NBR8 = ((-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
         (-1, -1, 2.0 ** 0.5), (-1, 1, 2.0 ** 0.5), (1, -1, 2.0 ** 0.5), (1, 1, 2.0 ** 0.5))


def _skeleton_set(img, nx, ny):
    s = set()
    for r in range(ny):
        row = img[r]
        for c in range(nx):
            if row[c]:
                s.add((r, c))
    return s


def _degree(pt, skel):
    r, c = pt
    return sum(1 for dr, dc, _ in _NBR8 if (r + dr, c + dc) in skel)


def _prune_spurs(skel, min_len):
    """Iteratively trim short dead-end branches (trace noise / small forks)
    so only the main through-path's endpoints remain as leaves."""
    skel = set(skel)
    changed = True
    while changed:
        changed = False
        leaves = [p for p in skel if _degree(p, skel) == 1]
        for leaf in leaves:
            if leaf not in skel:
                continue
            branch = [leaf]
            cur = leaf
            prev = None
            for _ in range(min_len + 1):
                nbrs = [(cur[0] + dr, cur[1] + dc) for dr, dc, _ in _NBR8
                        if (cur[0] + dr, cur[1] + dc) in skel and (cur[0] + dr, cur[1] + dc) != prev]
                if len(nbrs) != 1:
                    break
                prev, cur = cur, nbrs[0]
                branch.append(cur)
                if _degree(cur, skel) != 2:
                    break
            if len(branch) <= min_len and _degree(branch[-1], skel) >= 3:
                for p in branch[:-1]:
                    skel.discard(p)
                changed = True
    return skel


def _double_sweep_farthest(skel, start):
    """BFS from `start` to find the farthest node, then BFS again from
    there — standard double-sweep heuristic for the two ends of a
    path-like graph."""
    def bfs_farthest(src):
        from collections import deque
        seen = {src: 0}
        dq = deque([src])
        far, far_d = src, 0
        while dq:
            cur = dq.popleft()
            d = seen[cur]
            if d > far_d:
                far, far_d = cur, d
            for dr, dc, _ in _NBR8:
                nb = (cur[0] + dr, cur[1] + dc)
                if nb in skel and nb not in seen:
                    seen[nb] = d + 1
                    dq.append(nb)
        return far
    a = bfs_farthest(start)
    b = bfs_farthest(a)
    return a, b


def _shortest_path(skel, start, end):
    """Dijkstra over the (possibly still slightly branchy) pruned skeleton
    graph — picks the single direct channel between the two chosen ends
    even if a minor unpruned spur still touches the path somewhere."""
    import heapq
    dist = {start: 0.0}
    prev = {}
    pq = [(0.0, start)]
    visited = set()
    while pq:
        d, cur = heapq.heappop(pq)
        if cur in visited:
            continue
        visited.add(cur)
        if cur == end:
            break
        for dr, dc, w in _NBR8:
            nb = (cur[0] + dr, cur[1] + dc)
            if nb not in skel or nb in visited:
                continue
            nd = d + w
            if nd < dist.get(nb, float("inf")):
                dist[nb] = nd
                prev[nb] = cur
                heapq.heappush(pq, (nd, nb))
    if end not in prev and end != start:
        raise KmlIngestError("Skeleton graph is disconnected between the reach ends.")
    path = [end]
    cur = end
    while cur != start:
        cur = prev[cur]
        path.append(cur)
    path.reverse()
    return path


def _centerline_from_polygon_skeleton(ring):
    """ring: closed or unclosed [(lon,lat), ...] hand-traced river-bank
    polygon. Returns (centerline_pts, width_at_each_pt) via a real
    raster medial-axis skeleton — robust to tight meanders, S-curves, and
    sharp bends, unlike the bank-pairing fallback below."""
    pts = list(ring)
    if len(pts) > 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    pts = _decimate(pts, MAX_RING_POINTS)
    if len(pts) < 6:
        raise KmlIngestError("Traced polygon has too few vertices to derive a centerline.")

    to_xy, to_lonlat = _project_local_xy(pts)
    ring_xy = [to_xy(lo, la) for (lo, la) in pts]

    cell = _choose_grid_cell_m(ring_xy)
    grid, nx, ny, ox, oy = _rasterize_polygon_scanline(ring_xy, cell)
    n_inside = sum(sum(row) for row in grid)
    if n_inside < 20:
        raise KmlIngestError("Rasterized polygon is too small/thin to skeletonize.")

    dist_m = _chamfer_distance_m(grid, nx, ny, cell)
    thinned = _zhang_suen_thin(grid, nx, ny)
    skel = _skeleton_set(thinned, nx, ny)
    if len(skel) < 4:
        raise KmlIngestError("Thinned skeleton has too few pixels to form a centerline.")

    # prune dead-end spurs shorter than ~1.5x the local channel width so
    # only the main through-path's two real ends remain as leaves
    min_len = max(3, int(2.0 * (sum(sum(row) for row in grid) / max(1, len(skel))) / max(cell, 1e-6)))
    pruned = _prune_spurs(skel, min_len)
    if len(pruned) < 4:
        pruned = skel  # pruning was too aggressive on a very short/blobby trace — use unpruned

    any_pt = next(iter(pruned))
    end_a, end_b = _double_sweep_farthest(pruned, any_pt)
    path_rc = _shortest_path(pruned, end_a, end_b)

    centerline, widths = [], []
    for (r, c) in path_rc:
        x = ox + (c + 0.5) * cell
        y = oy + (r + 0.5) * cell
        lo, la = to_lonlat(x, y)
        centerline.append((lo, la))
        widths.append(max(1.0, 2.0 * dist_m[r][c]))

    if len(centerline) < 2:
        raise KmlIngestError("Skeleton path collapsed to a single point.")
    return centerline, widths


def _centerline_from_polygon(ring):
    """Preferred entry point: try the robust raster-skeleton medial axis
    first (handles meanders/sharp bends correctly); silently fall back to
    the simpler bank-pairing method only if the skeleton approach can't
    run at all (e.g. a degenerate/too-small trace) — never lets a river
    upload hard-fail just because the better method needs a fallback.
    Returns (centerline_pts, widths, method_used)."""
    try:
        cl, w = _centerline_from_polygon_skeleton(ring)
        return cl, w, "kml_medial_axis_skeleton"
    except Exception:
        cl, w = _centerline_from_polygon_bankpair(ring)
        return cl, w, "kml_bank_pairing"


def _bbox(pts):
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return min(lons), min(lats), max(lons), max(lats)


def _thin_buffer_polygon(centerline, half_width_m=15.0):
    """Fabricates a thin ribbon polygon around a bare LineString so the map
    still has something to shade — clearly not a real bank trace."""
    n = len(centerline)
    left, right = [], []
    for i in range(n):
        x, y = centerline[i]
        if i == 0:
            x2, y2 = centerline[i + 1]
            dx, dy = x2 - x, y2 - y
        elif i == n - 1:
            x0, y0 = centerline[i - 1]
            dx, dy = x - x0, y - y0
        else:
            x0, y0 = centerline[i - 1]
            x2, y2 = centerline[i + 1]
            dx, dy = x2 - x0, y2 - y0
        # rough lon/lat-degree perpendicular offset for a small ribbon
        norm = math.hypot(dx, dy) or 1e-9
        px, py = -dy / norm, dx / norm
        m_per_deg_lat = 111320.0
        m_per_deg_lon = 111320.0 * math.cos(math.radians(y)) or 1e-6
        off_m = half_width_m
        left.append((x + px * off_m / m_per_deg_lon, y + py * off_m / m_per_deg_lat))
        right.append((x - px * off_m / m_per_deg_lon, y - py * off_m / m_per_deg_lat))
    return left + list(reversed(right))


# ------------------------------- landmarks -------------------------------

def _classify(name):
    low = (name or "").lower()
    if any(w in low for w in BRIDGE_WORDS):
        return "bridge"
    if any(w in low for w in CONFLUENCE_WORDS):
        return "confluence"
    return "locality"


def _snap_points_to_chainage(named_points, stations):
    out = []
    for name, lon, lat in named_points:
        if not name:
            continue
        best_i, best_d = 0, float("inf")
        for i, s in enumerate(stations):
            d = haversine_m(lon, lat, s["lon"], s["lat"])
            if d < best_d:
                best_d, best_i = d, i
        st = stations[best_i]
        out.append({
            "name": name,
            "chainage_m": st["chainage_m"],
            "chainage_km": round(st["chainage_m"] / 1000.0, 3),
            "lon": st["lon"], "lat": st["lat"],
            "type": _classify(name),
            "source": "kml",
            "snap_distance_m": round(best_d, 1),
        })
    return out


def _auto_landmarks(stations, reach_len_m, seed):
    """No named points in the KML — auto-place plausible bridge markers so
    margins/alerts/hydrograph have assets to attach to, named simply B-1,
    B-2, ... in upstream-to-downstream chainage order. Clearly tagged
    source:"auto" (both in the JSON and the dashboard UI) — these are
    illustrative placements at a plausible spacing, not surveyed bridge
    locations. Add named Point placemarks to your KML and re-upload for
    real ones."""
    rng = random.Random(seed)
    n_bridges = max(2, min(10, round(reach_len_m / 4000.0)))
    raw = []
    for k in range(n_bridges):
        frac = (k + 1) / (n_bridges + 1)
        jitter = rng.uniform(-0.35, 0.35) / (n_bridges + 1)
        x = max(0.0, min(reach_len_m, reach_len_m * (frac + jitter)))
        best_i = min(range(len(stations)), key=lambda i: abs(stations[i]["chainage_m"] - x))
        st = stations[best_i]
        raw.append(st)
    raw.sort(key=lambda st: st["chainage_m"])
    out = []
    for i, st in enumerate(raw):
        out.append({
            "name": f"B-{i + 1}",
            "chainage_m": st["chainage_m"],
            "chainage_km": round(st["chainage_m"] / 1000.0, 3),
            "lon": st["lon"], "lat": st["lat"],
            "type": "bridge",
            "source": "auto",
            "snap_distance_m": 0.0,
        })
    return out


# --------------------------------- main entry ---------------------------------

def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return s or "river"


def build_river_from_kml(kml_text, display_name, chainage_step_m=DEFAULT_CHAINAGE_STEP_M):
    """Back-compat single-file entry point (polygon or centreline KML). See build_river_from_files()."""
    return build_river_from_files(display_name, polygon_text=kml_text, chainage_step_m=chainage_step_m)


def _build_from_polygon_or_line(kml_text, chainage_step_m):
    """polygon / LineString KML -> (stations, reach_polygon, named_points, width_source)"""
    linestrings, polygons, points = parse_kml(kml_text)

    width_source = "none"
    if linestrings:
        name, cl_pts = max(linestrings, key=lambda t: len(t[1]))
        centerline = cl_pts
        widths = None
        poly_for_map = None
        # if a polygon ALSO happens to be present alongside the LineString,
        # use it for width-at-station + the map trace
        if polygons:
            _, ring = max(polygons, key=lambda t: len(t[1]))
            poly_for_map = ring
            widths = _widths_from_polygon_at_points(centerline, ring)
            if widths is not None:
                width_source = "kml_polygon"
    else:
        pname, ring = max(polygons, key=lambda t: len(t[1]))
        centerline, widths, width_source = _centerline_from_polygon(ring)
        centerline = _keep_inside_polygon(centerline, ring)   # never draw outside the traced KML polygon
        poly_for_map = ring

    if not centerline or len(centerline) < 2:
        raise KmlIngestError("Could not derive a usable centerline from this KML.")

    total_len = _cum_dist(centerline)[-1]
    if total_len < 50:
        raise KmlIngestError(f"Derived reach length is only {total_len:.0f} m — check the KML geometry.")

    n_stations = int(total_len // chainage_step_m) + 1
    station_pts = _resample_by_arclength(centerline, n_stations + 1)
    if not linestrings:
        # second safety pass: linear interpolation between two boundary-safe
        # points can still drift outside a concave/meandering polygon —
        # re-clip the final resampled chainage stations too.
        station_pts = _keep_inside_polygon(station_pts, ring)
    cum = _cum_dist(station_pts)

    if widths is not None and len(widths) == len(centerline):
        width_cum = _cum_dist(centerline)
        station_widths = _interp_series(width_cum, widths, cum)
    else:
        station_widths = [DEFAULT_WIDTH_M] * len(station_pts)
        width_source = "default_constant"

    stations = [
        {"chainage_m": round(cum[i], 1), "lon": round(station_pts[i][0], 7),
         "lat": round(station_pts[i][1], 7), "width_m": round(max(1.0, station_widths[i]), 1)}
        for i in range(len(station_pts))
    ]

    if poly_for_map is None:
        poly_for_map = _thin_buffer_polygon(centerline)
    reach_polygon = [[round(lo, 7), round(la, 7)] for lo, la in poly_for_map]
    return stations, reach_polygon, points, width_source


def _widths_from_polygon_at_points(centerline, ring):
    """Best-effort width when BOTH a LineString centerline and a separate
    Polygon were supplied: nearest-ring-point distance doubled. Cheap and
    approximate; returns None on any failure so caller falls back cleanly."""
    try:
        pts = list(ring)
        if len(pts) > 2 and pts[0] == pts[-1]:
            pts = pts[:-1]
        pts = _decimate(pts, MAX_RING_POINTS)
        widths = []
        for cx, cy in centerline:
            d = min(haversine_m(cx, cy, rx, ry) for rx, ry in pts)
            widths.append(max(2.0, d * 2.0))
        return widths
    except Exception:
        return None


def _interp_series(xs, ys, targets):
    out = []
    j = 0
    n = len(xs)
    for t in targets:
        while j < n - 2 and xs[j + 1] < t:
            j += 1
        seg = xs[j + 1] - xs[j]
        frac = 0.0 if seg <= 0 else (t - xs[j]) / seg
        frac = max(0.0, min(1.0, frac))
        out.append(ys[j] + (ys[j + 1] - ys[j]) * frac)
    return out


# =====================================================================================================
#  Multi-file upload: river polygon KML  (+ optional chainage KML)  (+ optional water-depth KML)
# =====================================================================================================

MAX_STATIONS = 6000          # a 60 km reach at 10 m spacing; longer chainage KMLs are thinned to this


def _ext_data(pm):
    out = {}
    for d in pm.iter():
        if _local(d.tag) == "Data" and d.get("name"):
            for v in d:
                if _local(v.tag) == "value" and v.text is not None:
                    out[d.get("name")] = v.text.strip()
        elif _local(d.tag) == "SimpleData" and d.get("name") and d.text is not None:
            out[d.get("name")] = d.text.strip()
    return out


_CH_LABEL = re.compile(r"^\s*(?:ch\.?\s*)?(\d+)\s*\+\s*(\d{1,3}(?:\.\d+)?)\s*$", re.I)


def parse_chainage_points(kml_text):
    """Chainage KML = many Point placemarks along the river (e.g. "0+010" every 10 m, ExtendedData chainage_m).
    Returns [(chainage_m, lon, lat, label)] sorted by chainage, or [] if this is not a chainage KML."""
    try:
        root = ET.fromstring(kml_text)
    except ET.ParseError as e:
        raise KmlIngestError(f"Could not parse chainage KML: {e}")
    pts = []
    for pm in root.iter():
        if _local(pm.tag) != "Placemark":
            continue
        name = ""
        for child in pm:
            if _local(child.tag) == "name" and child.text:
                name = child.text.strip()
        coords = None
        for g in pm.iter():
            if _local(g.tag) == "Point":
                ct = _find_coords_text(g)
                if ct:
                    p = _parse_coords(ct)
                    coords = p[0] if p else None
        if coords is None:
            continue
        ed = _ext_data(pm)
        ch = None
        for k in ("chainage_m", "chainage", "CHAINAGE", "Chainage", "ch_m"):
            if k in ed:
                try:
                    ch = float(ed[k])
                    break
                except ValueError:
                    pass
        if ch is None:
            m = _CH_LABEL.match(name)
            if m:
                ch = int(m.group(1)) * 1000.0 + float(m.group(2))
        pts.append((ch, coords[0], coords[1], name))
    if len(pts) < 10:
        return []
    if sum(1 for p in pts if p[0] is not None) >= 0.8 * len(pts):
        pts = sorted([p for p in pts if p[0] is not None])
    else:   # no chainage attribute: keep document order, chainage = distance along the points
        cum = _cum_dist([(p[1], p[2]) for p in pts])
        pts = [(cum[i], p[1], p[2], p[3]) for i, p in enumerate(pts)]
    # drop duplicates
    out = []
    for p in pts:
        if not out or p[0] - out[-1][0] > 0.01:
            out.append(p)
    if len(out) > MAX_STATIONS:
        k = int(math.ceil(len(out) / MAX_STATIONS))
        out = out[::k] + ([out[-1]] if (len(out) - 1) % k else [])
    return out


def _local_xy(lon, lat, lon0, lat0):
    k = 111320.0
    return (lon - lon0) * k * math.cos(math.radians(lat0)), (lat - lat0) * k


def widths_by_perpendicular_rays(st_lonlat, ring, max_d=1500.0):
    """bank-to-bank width at every station: chord of the river polygon perpendicular to the local centreline."""
    ring = _decimate(list(ring), 2500)
    lon0, lat0 = st_lonlat[0]
    rxy = [_local_xy(x, y, lon0, lat0) for x, y in ring]
    if rxy[0] != rxy[-1]:
        rxy.append(rxy[0])
    edges = []
    for (ax, ay), (bx, by) in zip(rxy, rxy[1:]):
        edges.append((ax, ay, bx - ax, by - ay, min(ax, bx), max(ax, bx), min(ay, by), max(ay, by)))
    xy = [_local_xy(x, y, lon0, lat0) for x, y in st_lonlat]
    n = len(xy)
    raw = []
    for i, (px, py) in enumerate(xy):
        j0, j1 = max(0, i - 3), min(n - 1, i + 3)
        tx, ty = xy[j1][0] - xy[j0][0], xy[j1][1] - xy[j0][1]
        L = math.hypot(tx, ty) or 1.0
        nx, ny = -ty / L, tx / L
        hits = []
        for dx, dy in ((nx, ny), (-nx, -ny)):
            best = None
            for ax, ay, ex, ey, x0, x1, y0, y1 in edges:
                if x1 < px - max_d or x0 > px + max_d or y1 < py - max_d or y0 > py + max_d:
                    continue
                den = dx * ey - dy * ex
                if abs(den) < 1e-12:
                    continue
                t = ((ax - px) * ey - (ay - py) * ex) / den
                u = ((ax - px) * dy - (ay - py) * dx) / den
                if 0.5 < t <= max_d and 0.0 <= u <= 1.0 and (best is None or t < best):
                    best = t
            hits.append(best)
        h1, h2 = hits
        if h1 is not None and h2 is not None:
            raw.append(h1 + h2)
        elif h1 is not None or h2 is not None:
            raw.append(2.0 * (h1 if h1 is not None else h2))
        else:
            raw.append(None)
    good = [i for i, v in enumerate(raw) if v is not None]
    if not good:
        return None
    w = []
    for i in range(n):            # fill gaps by nearest valid
        if raw[i] is not None:
            w.append(raw[i])
        else:
            j = min(good, key=lambda g: abs(g - i))
            w.append(raw[j])
    k = 5                          # running median over 11 stations against ray-grazing spikes
    out = []
    for i in range(n):
        win = sorted(w[max(0, i - k):i + k + 1])
        out.append(max(5.0, min(2000.0, win[len(win) // 2])))
    return out


# ------------------------------ water-depth overlay (Sentinel-2 GroundOverlay PNG) ------------------------------

def _png_decode(b):
    """minimal stdlib PNG decoder (8-bit, non-interlaced) -> (w, h, rgba bytearray). ~3 s for a 4600x900 image."""
    import zlib
    import struct
    if b[:8] != b"\x89PNG\r\n\x1a\n":
        raise KmlIngestError("depth overlay is not a PNG image")
    pos, idat, plte, trns = 8, [], None, None
    w = h = bd = ct = il = None
    while pos < len(b):
        ln, = struct.unpack(">I", b[pos:pos + 4])
        typ = b[pos + 4:pos + 8]
        data = b[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            w, h, bd, ct, _, _, il = struct.unpack(">IIBBBBB", data)
        elif typ == b"IDAT":
            idat.append(data)
        elif typ == b"PLTE":
            plte = data
        elif typ == b"tRNS":
            trns = data
        elif typ == b"IEND":
            break
    if bd != 8 or il != 0:
        raise KmlIngestError("depth PNG must be 8-bit, non-interlaced (or install Pillow)")
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ct]
    raw = zlib.decompress(b"".join(idat))
    stride = w * ch
    px = bytearray(h * stride)
    prev = bytes(stride)
    p = 0
    for y in range(h):
        ft = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if ft == 1:
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 255
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                line[i] = (line[i] + (((line[i - ch] if i >= ch else 0) + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(ch):
                line[i] = (line[i] + prev[i]) & 255
            for i in range(ch, stride):
                a = line[i - ch]
                bb = prev[i]
                c = prev[i - ch]
                pa = bb - c
                pb = a - c
                pc = pa + pb
                if pa < 0:
                    pa = -pa
                if pb < 0:
                    pb = -pb
                if pc < 0:
                    pc = -pc
                line[i] = (line[i] + (a if (pa <= pb and pa <= pc) else (bb if pb <= pc else c))) & 255
        px[y * stride:(y + 1) * stride] = line
        prev = line
    if ch == 4:
        return w, h, px
    rgba = bytearray(w * h * 4)
    for k in range(w * h):
        if ch == 3:
            r, g, bl, a = px[3 * k], px[3 * k + 1], px[3 * k + 2], 255
        elif ch == 2:
            r = g = bl = px[2 * k]
            a = px[2 * k + 1]
        elif ct == 3:
            idx = px[k]
            r, g, bl = plte[3 * idx], plte[3 * idx + 1], plte[3 * idx + 2]
            a = trns[idx] if trns and idx < len(trns) else 255
        else:
            r = g = bl = px[k]
            a = 255
        rgba[4 * k:4 * k + 4] = bytes((r, g, bl, a))
    return w, h, rgba


def parse_depth_overlay(kml_text):
    import base64
    m = re.search(r"base64,([A-Za-z0-9+/=\s]+)", kml_text)
    if not m:
        raise KmlIngestError("water-depth KML has no embedded (base64) image overlay")
    png = base64.b64decode(re.sub(r"\s+", "", m.group(1)))
    box = {}
    for k in ("north", "south", "east", "west"):
        mm = re.search(rf"<(?:\w+:)?{k}>\s*([-\d.eE+]+)\s*<", kml_text)
        if not mm:
            raise KmlIngestError("water-depth KML has no LatLonBox")
        box[k] = float(mm.group(1))
    dm = re.search(r"<(?:\w+:)?description>([^<]*)<", kml_text)
    desc = dm.group(1) if dm else ""
    r = re.search(r"Depth range:\s*([\d.]+)\s*-\s*([\d.]+)\s*m", desc)
    dmin, dmax = (float(r.group(1)), float(r.group(2))) if r else (0.25, 2.6)
    di = re.search(r"Image date:\s*([\d-]+)", desc)
    try:
        from PIL import Image     # optional fast path
        import io
        im = Image.open(io.BytesIO(png)).convert("RGBA")
        w, h = im.size
        px = bytearray(im.tobytes())
        decoder = "Pillow"
    except Exception:
        w, h, px = _png_decode(png)
        decoder = "stdlib"
    return {"w": w, "h": h, "px": px, "box": box, "dmin": dmin, "dmax": dmax,
            "image_date": di.group(1) if di else None, "desc": desc, "decoder": decoder}


def sample_depth(ov, stations, radius_m=12.0):
    """light blue = shallow ... dark navy = deep, scaled to the range stated in the KML (luminance, 2-98 percentile).
    Returns (depth_m list, flagged list, coverage fraction)."""
    w, h, px, box = ov["w"], ov["h"], ov["px"], ov["box"]
    dmin, dmax = ov["dmin"], ov["dmax"]

    def valid_lum(k):
        r, g, b, a = px[4 * k], px[4 * k + 1], px[4 * k + 2], px[4 * k + 3]
        if a == 0 or (r == 0 and g == 0 and b == 255) or (40 < r < 60 and g > 240 and b > 240):
            return None
        return 0.30 * r + 0.59 * g + 0.11 * b
    hist = [0] * 256
    for k in range(0, w * h, 3):
        v = valid_lum(k)
        if v is not None:
            hist[min(255, int(v))] += 1
    tot = sum(hist)
    if tot == 0:
        raise KmlIngestError("water-depth image has no water pixels")

    def pctl(q):
        c = 0
        for i, n in enumerate(hist):
            c += n
            if c >= q * tot:
                return i + 0.5
        return 255.0
    lo, hi = pctl(0.02), pctl(0.98)
    span = max(1.0, hi - lo)
    lat0 = (box["north"] + box["south"]) / 2.0
    px_m_x = (box["east"] - box["west"]) / w * 111320.0 * math.cos(math.radians(lat0))
    px_m_y = (box["north"] - box["south"]) / h * 111320.0
    rx, ry = max(2, int(round(radius_m / px_m_x))), max(2, int(round(radius_m / px_m_y)))
    depth, flagged = [], []
    for s in stations:
        c = (s["lon"] - box["west"]) / (box["east"] - box["west"]) * w
        r = (box["north"] - s["lat"]) / (box["north"] - box["south"]) * h
        c0, r0 = int(round(c)), int(round(r))
        vals = []
        for yy in range(max(0, r0 - ry), min(h, r0 + ry + 1)):
            base = yy * w
            for xx in range(max(0, c0 - rx), min(w, c0 + rx + 1)):
                v = valid_lum(base + xx)
                if v is not None:
                    vals.append(dmin + (1.0 - min(1.0, max(0.0, (v - lo) / span))) * (dmax - dmin))
        if len(vals) >= 6:
            vals.sort()
            depth.append(vals[len(vals) // 2])
            flagged.append(False)
        else:
            depth.append(None)
            flagged.append(True)
    good = [i for i, v in enumerate(depth) if v is not None]
    cov = len(good) / max(1, len(stations))
    if not good:
        return None, flagged, 0.0
    # interpolate gaps linearly between valid neighbours, then a 7-station moving average
    filled = list(depth)
    for i in range(len(filled)):
        if filled[i] is None:
            prv = max((g for g in good if g < i), default=None)
            nxt = min((g for g in good if g > i), default=None)
            if prv is None:
                filled[i] = depth[nxt]
            elif nxt is None:
                filled[i] = depth[prv]
            else:
                f = (i - prv) / (nxt - prv)
                filled[i] = depth[prv] * (1 - f) + depth[nxt] * f
    k = 3
    sm = []
    for i in range(len(filled)):
        win = filled[max(0, i - k):i + k + 1]
        win = [filled[0]] * max(0, k - i) + win + [filled[-1]] * max(0, i + k + 1 - len(filled))
        sm.append(sum(win) / len(win))
    return sm, flagged, cov


# ------------------------------ flow direction from the DEM ------------------------------

def dem_flow_check(stations):
    """Elevation along the reach (Copernicus GLO-90 via Open-Meteo). Returns (reversed?, note).
    reversed=True means elevation RISES with chainage, i.e. chainage 0 is the downstream end."""
    try:
        from . import live_data
    except Exception:
        return None, "DEM check unavailable"
    n = len(stations)
    idx = sorted(set([round(i * (n - 1) / 39) for i in range(40)]))
    try:
        el = live_data.fetch_elevations([(stations[i]["lon"], stations[i]["lat"]) for i in idx])
    except Exception as e:
        return None, f"DEM unreachable ({str(e)[:80]}) — assumed chainage 0 = upstream"
    pts = [(stations[i]["chainage_m"], v) for i, v in zip(idx, el) if v is not None]
    if len(pts) < 8:
        return None, "DEM returned too few points — assumed chainage 0 = upstream"
    a, b, rmse, _ = live_data.robust_line([p[0] for p in pts], [p[1] for p in pts])
    drop = -b * stations[-1]["chainage_m"]
    if b > 5e-5 and -drop > 1.0:
        return True, f"DEM: ground RISES {-drop:.1f} m from chainage 0 to the end -> chainage 0 is DOWNSTREAM"
    if drop > 1.0:
        return False, f"DEM: water surface falls {drop:.1f} m from chainage 0 to the end -> chainage 0 is upstream"
    return None, f"DEM: reach is nearly flat ({drop:+.1f} m) — assumed chainage 0 = upstream"


def _rechainage(stations):
    pts = [(s["lon"], s["lat"]) for s in stations]
    cum = _cum_dist(pts)
    return [dict(s, chainage_m=round(cum[i], 1)) for i, s in enumerate(stations)]


def build_river_from_files(display_name, polygon_text=None, chainage_text=None, depth_text=None,
                           chainage_step_m=DEFAULT_CHAINAGE_STEP_M, localities=None,
                           slug=None, uploaded=True, keep_files=("params.json",), extra_meta=None):
    """Ingest any river from up to three KMLs and write naditwin/rivers/<slug>/:
         polygon_text   river-bank polygon (or a centreline LineString)           -> width, map trace, named landmarks
         chainage_text  OPTIONAL chainage points (e.g. every 10 m, "0+010")      -> stations exactly at your chainage
         depth_text     OPTIONAL water-depth GroundOverlay (Sentinel-2 style)    -> per-station surveyed depth
    Flow direction is checked against the DEM: polygon-derived chainage is flipped so 0 = upstream; a user chainage
    is kept as given and the model is told which way the water flows (meta.flow_dir)."""
    if not polygon_text and not chainage_text:
        raise KmlIngestError("Upload at least a river polygon KML (or a chainage KML).")
    notes = []
    ring, named_points, reach_polygon, width_source = None, [], None, "none"
    if polygon_text:
        try:
            ls, polys, named_points = parse_kml(polygon_text)
            if polys:
                ring = max(polys, key=lambda t: len(t[1]))[1]
        except KmlIngestError:
            if not chainage_text:
                raise
    chainage_pts = parse_chainage_points(chainage_text) if chainage_text else []
    if chainage_text and not chainage_pts:
        notes.append("chainage KML had no usable chainage points — derived chainage from the polygon instead")

    flow_dir = 1
    if chainage_pts:
        off = chainage_pts[0][0]
        if off > 0:
            notes.append(f"chainage starts at {off:.0f} m — shifted to 0 (original labels kept)")
        stations = [{"chainage_m": round(c - off, 1), "lon": round(lo, 7), "lat": round(la, 7),
                     "label": lab or None} for c, lo, la, lab in chainage_pts]
        if ring:
            ws = widths_by_perpendicular_rays([(s["lon"], s["lat"]) for s in stations], ring)
            width_source = "polygon_perpendicular" if ws else "default_constant"
        else:
            ws = None
            width_source = "default_constant"
        for s, wv in zip(stations, ws or [DEFAULT_WIDTH_M] * len(stations)):
            s["width_m"] = round(wv, 1)
        reach_polygon = [[round(lo, 7), round(la, 7)] for lo, la in ring] if ring else \
            [[round(lo, 7), round(la, 7)] for lo, la in _thin_buffer_polygon([(s["lon"], s["lat"]) for s in stations])]
        rev, note = dem_flow_check(stations)
        notes.append(note)
        if rev:
            flow_dir = -1
            notes.append("your chainage is kept as-is; the model routes water from the END of the chainage towards 0")
        chainage_source = "chainage KML"
        named_points = [p for p in named_points if not _CH_LABEL.match(p[0] or "")]
    else:
        stations, reach_polygon, named_points, width_source = _build_from_polygon_or_line(polygon_text, chainage_step_m)
        rev, note = dem_flow_check(stations)
        notes.append(note)
        if rev:
            stations = _rechainage(list(reversed(stations)))
            notes.append("chainage re-numbered so that 0 = upstream end")
        chainage_source = f"derived from polygon every {chainage_step_m:g} m"

    total_len = stations[-1]["chainage_m"]
    if total_len < 50:
        raise KmlIngestError(f"Reach length is only {total_len:.0f} m — check the KML geometry.")

    depth_info = None
    depth_rows = None
    if depth_text:
        ov = parse_depth_overlay(depth_text)
        d, fl, cov = sample_depth(ov, stations)
        if d is None or cov < 0.2:
            notes.append(f"depth KML covers only {cov*100:.0f}% of the stations — depth not used")
        else:
            depth_rows = [{"chainage_m": s["chainage_m"], "depth_m": round(v, 3), "flagged": bool(f)}
                          for s, v, f in zip(stations, d, fl)]
            depth_info = {"image_date": ov["image_date"], "depth_min_m": ov["dmin"], "depth_max_m": ov["dmax"],
                          "note": ov["desc"], "coverage": round(cov, 3), "decoder": ov["decoder"],
                          "source": "water-depth KML overlay (light = shallow, dark = deep)"}
            notes.append(f"depth sampled at {cov*100:.0f}% of stations (image {ov['image_date']}, "
                         f"{ov['dmin']}-{ov['dmax']} m)")

    kml_landmarks = _snap_points_to_chainage(named_points, stations)
    kml_landmarks = [lm for lm in kml_landmarks if lm["snap_distance_m"] <= 500]
    if kml_landmarks:
        landmarks = sorted(kml_landmarks, key=lambda lm: lm["chainage_m"])
    else:
        landmarks = _auto_landmarks(stations, total_len, seed=sum(map(ord, display_name)))

    slug = slug or slugify(display_name)
    out_dir = os.path.join(RIVERS_DIR, slug)
    if os.path.isdir(out_dir):
        for fn in os.listdir(out_dir):     # replace everything (old depth/caches belong to the old geometry)
            if fn in (keep_files or ()):   # ...except hand-calibrated model overrides (params.json)
                continue
            try:
                os.remove(os.path.join(out_dir, fn))
            except OSError:
                pass
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "chainage_profile.json"), "w") as f:
        json.dump(stations, f)
    with open(os.path.join(out_dir, "reach_polygon.json"), "w") as f:
        json.dump(reach_polygon, f)
    with open(os.path.join(out_dir, "landmarks.json"), "w") as f:
        json.dump(landmarks, f, indent=1)
    if localities:
        save_localities(out_dir, localities)
        notes.append(f"{len(localities)} locations loaded from the locations file")
    if depth_rows:
        with open(os.path.join(out_dir, "depth_profile.json"), "w") as f:
            json.dump(depth_rows, f)
        with open(os.path.join(out_dir, "survey.json"), "w") as f:
            json.dump(depth_info, f, indent=1)

    n_bridges = sum(1 for lm in landmarks if lm["type"] == "bridge")
    n_confluences = sum(1 for lm in landmarks if lm["type"] == "confluence")
    span = f"{landmarks[0]['name']} → {landmarks[-1]['name']}" if landmarks else ""
    step = round(total_len / max(1, len(stations) - 1), 1)
    meta_out = {
        "display_name": display_name, "slug": slug, "reach_km": round(total_len / 1000.0, 3),
        "n_stations": len(stations), "n_bridges": n_bridges, "n_confluences": n_confluences, "span": span,
        "landmarks_source": "kml" if kml_landmarks else "auto", "width_source": width_source,
        "chainage_source": chainage_source, "chainage_step_m": step, "flow_dir": flow_dir,
        "has_depth": bool(depth_rows), "notes": notes, "uploaded": bool(uploaded),
    }
    if extra_meta:
        meta_out.update(extra_meta)
    with open(os.path.join(out_dir, "meta.json"), "w") as f:
        json.dump(meta_out, f, indent=2)
    return dict(meta_out, key=slug)


# ------------------------------ localities (Excel / CSV): "Sangam ... Manjari" ------------------------------

def _xlsx_rows(data):
    """first worksheet of an .xlsx as a list of rows (strings / numbers) — zipfile + XML, no openpyxl needed."""
    import io
    import zipfile
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise KmlIngestError("locations file is not a valid .xlsx")
    names = z.namelist()
    shared = []
    if "xl/sharedStrings.xml" in names:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root:
            if _local(si.tag) == "si":
                shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
    sheet = "xl/worksheets/sheet1.xml"
    try:   # the FIRST sheet of the workbook, whatever its file name
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        first = next(el for el in wb.iter() if _local(el.tag) == "sheet")
        rid = next(v for k, v in first.attrib.items() if k.endswith("}id") or k == "r:id")
        tgt = next(el.get("Target") for el in rels if el.get("Id") == rid)
        sheet = "xl/" + tgt.lstrip("/").replace("xl/", "", 1) if not tgt.startswith("/xl/") else tgt.lstrip("/")
    except Exception:
        pass
    if sheet not in names:
        sheet = next((n for n in names if n.startswith("xl/worksheets/") and n.endswith(".xml")), None)
        if sheet is None:
            raise KmlIngestError("no worksheet found in the .xlsx")
    root = ET.fromstring(z.read(sheet))
    rows = []
    for row in root.iter():
        if _local(row.tag) != "row":
            continue
        vals = {}
        for c in row:
            if _local(c.tag) != "c":
                continue
            ref = c.get("r") or ""
            col = 0
            for ch in ref:
                if ch.isalpha():
                    col = col * 26 + (ord(ch.upper()) - 64)
            col = max(1, col) if col else len(vals) + 1
            t = c.get("t")
            v = None
            for sub in c:
                if _local(sub.tag) == "v":
                    v = sub.text
                elif _local(sub.tag) == "is":
                    v = "".join(x.text or "" for x in sub.iter() if _local(x.tag) == "t")
            if t == "s" and v is not None:
                v = shared[int(v)] if int(v) < len(shared) else ""
            elif t not in ("s", "str", "inlineStr") and v is not None:
                try:
                    v = float(v)
                except ValueError:
                    pass
            vals[col] = v
        if vals:
            rows.append([vals.get(i) for i in range(1, max(vals) + 1)])
    return rows


def parse_localities(data, filename=""):
    """Excel (.xlsx) or CSV with columns  Location | Longitude | Latitude  (optional: Chainage, in m or as 1+250).
    Header names are matched loosely (name/location/place/area, lon/lng/longitude/x, lat/latitude/y, chainage/ch).
    Returns [{"name", "lon", "lat", "chainage_m"?}] in file order."""
    import csv
    import io
    fn = (filename or "").lower()
    if isinstance(data, str):
        data = data.encode("utf-8")
    if data[:2] == b"PK" or fn.endswith((".xlsx", ".xlsm")):
        rows = _xlsx_rows(data)
    elif fn.endswith(".xls"):
        raise KmlIngestError("old .xls format — please save the sheet as .xlsx or .csv")
    else:
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace"))))
    rows = [r for r in rows if r and any(v not in (None, "") for v in r)]
    if not rows:
        raise KmlIngestError("locations file is empty")

    def low(v):
        return str(v or "").strip().lower()
    hdr = [low(v) for v in rows[0]]
    has_hdr = any(any(w in h for w in ("lon", "lng", "lat", "location", "name", "place", "chainage")) for h in hdr)
    col = {}
    if has_hdr:
        for i, h in enumerate(hdr):
            if "name" not in col and any(w in h for w in ("location", "name", "place", "locality", "area", "village")):
                col["name"] = i
            elif "lon" not in col and (h.startswith("lon") or "lng" in h or "longitude" in h or h in ("x", "long")):
                col["lon"] = i
            elif "lat" not in col and (h.startswith("lat") or "latitude" in h or h == "y"):
                col["lat"] = i
            elif "ch" not in col and ("chainage" in h or h in ("ch", "ch_m", "km")):
                col["ch"] = i
        body = rows[1:]
    else:
        col = {"name": 0, "lon": 1, "lat": 2}
        body = rows
    col.setdefault("name", 0)
    out = []
    for r in body:
        def g(k):
            i = col.get(k)
            return r[i] if i is not None and i < len(r) else None
        name = str(g("name") or "").strip()
        if not name:
            continue
        item = {"name": name}
        try:
            lon, lat = float(g("lon")), float(g("lat"))
            if (abs(lat) > 90 and abs(lon) <= 90) or (68 <= lat <= 98 and 6 <= lon <= 37.5):   # columns swapped
                lon, lat = lat, lon
            item["lon"], item["lat"] = lon, lat
        except (TypeError, ValueError):
            pass
        chv = g("ch")
        if chv not in (None, ""):
            m = _CH_LABEL.match(str(chv))
            try:
                item["chainage_m"] = int(m.group(1)) * 1000.0 + float(m.group(2)) if m else float(chv) * (
                    1000.0 if "km" in hdr[col["ch"]] else 1.0)
            except (TypeError, ValueError):
                pass
        if "lon" in item or "chainage_m" in item:
            out.append(item)
    if not out:
        raise KmlIngestError("no usable rows — need columns Location, Longitude, Latitude (or Chainage)")
    return out


def save_localities(river_dir, localities):
    with open(os.path.join(river_dir, "localities.json"), "w") as f:
        json.dump(localities, f, indent=1)
