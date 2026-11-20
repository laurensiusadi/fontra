"""Numbers for flaws too small to see in a render: extra nodes, bent smooth nodes, near-miss alignments, crooked handles, missing extremes, curvature jumps, and outline diffs in font units. Plus a drawing that zooms, labels points, draws a curvature comb and amplifies differences.

    python -m fontra.mcp.glyphcheck FONT GLYPH [--against FONT2] [--png OUT.png] [--zoom POINT[:RADIUS]] [--amplify K]
    python -m fontra.mcp.glyphcheck --selftest

FONT is an .otf/.ttf or a .ufo; GLYPH a glyph name or a single character. Contours are lists of [x, y, type], type "on", "smooth", "off" (cubic) or "qoff" (quadratic); point numbers count across contours, as in Fontra.
"""
import math
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

OFF = ("off", "qoff")
TOL = dict(
    smooth_deg=0.05,    # a smooth node's handles may bend this much
    kink_deg=4.0,       # a corner that turns less than this is a kink, not a corner
    axis_deg=4.0,       # a handle or line this close to horizontal/vertical is meant to be on it
    axis_min=0.2,       # ...unless it is off by less than this many units
    near=4.0,           # nodes on vertical/horizontal tangents this close in x/y are a near miss
    metric_near=4.0,    # flat nodes this close to a metric line are a near miss
    extreme=0.5,        # a segment bulging this far past its ends needs an extreme node
    removable=1.0,      # a node is extra when one curve replaces its two within this many units
    tiny=3.0, dup=0.5, g2_ratio=1.5,
)
SEVERITY = {"error": 0, "warn": 1, "info": 2}


# ---- geometry -------------------------------------------------------------------

def P(pt):
    return np.array(pt[:2], float)


def segments(con, base=0):
    """Segments of one contour as dicts: kind line/cubic, p (2 or 4 control points), i0/i1 (global node numbers), offs."""
    n = len(con)
    ons = [i for i, p in enumerate(con) if p[2] not in OFF]
    segs = []
    for k, i in enumerate(ons):
        j = ons[(k + 1) % len(ons)]
        offs = [(i + m) % n for m in range(1, (j - i) % n or n)]
        p0, p3 = P(con[i]), P(con[j])
        if not offs:
            segs.append(dict(kind="line", p=np.array([p0, p3]), i0=base + i, i1=base + j, offs=[]))
        elif len(offs) == 2 and con[offs[0]][2] == "off":
            segs.append(dict(kind="cubic", p=np.array([p0, P(con[offs[0]]), P(con[offs[1]]), p3]), i0=base + i, i1=base + j, offs=[base + o for o in offs]))
        else:   # quadratic run (or a lone off point): elevate each piece between implied on-curve points
            qs, start = [P(con[o]) for o in offs], p0
            for m, q in enumerate(qs):
                end = p3 if m == len(qs) - 1 else (q + qs[m + 1]) / 2
                segs.append(dict(kind="cubic", quad=True, p=np.array([start, start + 2 / 3 * (q - start), end + 2 / 3 * (q - end), end]),
                                 i0=base + i, i1=base + j, offs=[base + o for o in offs]))
                start = end
    return segs


def cubic(seg):
    p = seg["p"]
    return p if len(p) == 4 else np.array([p[0], p[0] + (p[1] - p[0]) / 3, p[0] + 2 * (p[1] - p[0]) / 3, p[1]])


def bez(p, t):
    t = np.asarray(t, float)[..., None]
    mt = 1 - t
    return mt ** 3 * p[0] + 3 * mt * mt * t * p[1] + 3 * mt * t * t * p[2] + t ** 3 * p[3]


def d1(p, t):
    t = np.asarray(t, float)[..., None]
    mt = 1 - t
    return 3 * (mt * mt * (p[1] - p[0]) + 2 * mt * t * (p[2] - p[1]) + t * t * (p[3] - p[2]))


def d2(p, t):
    t = np.asarray(t, float)[..., None]
    return 6 * ((1 - t) * (p[2] - 2 * p[1] + p[0]) + t * (p[3] - 2 * p[2] + p[1]))


def curvature(p, t):
    a, b = d1(p, t), d2(p, t)
    cr = a[..., 0] * b[..., 1] - a[..., 1] * b[..., 0]
    return cr / np.maximum(np.linalg.norm(a, axis=-1) ** 3, 1e-12)


def tangent_out(seg):
    p = seg["p"]
    for q in p[1:]:
        if np.linalg.norm(q - p[0]) > 1e-9:
            return q - p[0]
    return np.zeros(2)


def tangent_in(seg):
    p = seg["p"]
    for q in p[-2::-1]:
        if np.linalg.norm(p[-1] - q) > 1e-9:
            return p[-1] - q
    return np.zeros(2)


def angle(u, v):
    if np.linalg.norm(u) < 1e-9 or np.linalg.norm(v) < 1e-9:
        return 0.0
    return math.degrees(math.atan2(abs(u[0] * v[1] - u[1] * v[0]), float(u @ v)))


def off_axis(v):
    """(degrees off the nearest axis, units off it over the vector's length, 'horizontal'/'vertical')."""
    L = float(np.linalg.norm(v))
    if L < 1e-9:
        return 0.0, 0.0, None
    h = math.degrees(math.atan2(abs(v[1]), abs(v[0])))
    return (h, abs(float(v[1])), "horizontal") if h <= 45 else (90 - h, abs(float(v[0])), "vertical")


def contour_segments(contours):
    base, out = 0, []
    for con in contours:
        out.append(segments(con, base))
        base += len(con)
    return out


def sample(contours, step=2.0):
    """Points along the outline about `step` units apart, with the segment's start node and the contour index of each."""
    pts, node, cid = [], [], []
    for c, segs in enumerate(contour_segments(contours)):
        for s in segs:
            p = cubic(s)
            L = float(np.linalg.norm(np.diff(bez(p, np.linspace(0, 1, 33)), axis=0), axis=1).sum())
            t = np.linspace(0, 1, max(2, math.ceil(L / step)), endpoint=False)
            pts.append(bez(p, t))
            node += [s["i0"]] * len(t)
            cid += [c] * len(t)
    if not pts:
        return np.zeros((0, 2)), np.zeros(0, int), np.zeros(0, int)
    return np.concatenate(pts), np.array(node), np.array(cid)


def polygons(contours, step=2.0):
    S, _, cid = sample(contours, step)
    return [S[cid == c] for c in range(len(contours)) if (cid == c).sum() > 2]


def area(poly):
    x, y = poly[:, 0], poly[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def inside(pts, polys):
    """Even-odd point-in-outline for many points at once."""
    res = np.zeros(len(pts), bool)
    for poly in polys:
        a, b = poly, np.roll(poly, -1, axis=0)
        px, py = pts[:, None, 0], pts[:, None, 1]
        cond = (a[None, :, 1] > py) != (b[None, :, 1] > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            xc = a[None, :, 0] + (py - a[None, :, 1]) * (b[None, :, 0] - a[None, :, 0]) / (b[None, :, 1] - a[None, :, 1])
        res ^= (np.count_nonzero(cond & (px < xc), axis=1) % 2).astype(bool)
    return res


def nearest(A, B, chunk=400):
    """For each point of A, the distance to and index of the nearest point of B."""
    d, idx = np.empty(len(A)), np.empty(len(A), int)
    for s in range(0, len(A), chunk):
        dd = ((A[s:s + chunk, None, :] - B[None]) ** 2).sum(-1)
        idx[s:s + chunk] = dd.argmin(1)
        d[s:s + chunk] = np.sqrt(dd.min(1))
    return d, idx


def fit_one(pa, pb):
    """Best single cubic from pa[0] to pb[3] keeping both end tangents; max distance from the two curves to it."""
    d0 = pa[1] - pa[0] if np.linalg.norm(pa[1] - pa[0]) > 1e-9 else pa[2] - pa[0]
    d3 = pb[3] - pb[2] if np.linalg.norm(pb[3] - pb[2]) > 1e-9 else pb[3] - pb[1]
    d0, d3 = d0 / np.linalg.norm(d0), d3 / np.linalg.norm(d3)
    S = np.concatenate([bez(pa, np.linspace(0, 1, 40, endpoint=False)), bez(pb, np.linspace(0, 1, 41))])
    u = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(S, axis=0), axis=1))])
    u /= u[-1]
    a0, a3 = pa[0], pb[3]
    for _ in range(4):
        mt = 1 - u
        basis = (mt ** 3 + 3 * mt * mt * u)[:, None] * a0 + (3 * mt * u * u + u ** 3)[:, None] * a3
        M = np.stack([(3 * mt * mt * u)[:, None] * d0, -(3 * mt * u * u)[:, None] * d3], axis=-1).reshape(-1, 2)
        (a, b), *_ = np.linalg.lstsq(M, (S - basis).reshape(-1), rcond=None)
        q = np.array([a0, a0 + a * d0, a3 - b * d3, a3])
        dense_t = np.linspace(0, 1, 600)
        _, idx = nearest(S, bez(q, dense_t))
        u = dense_t[idx]
    dist, _ = nearest(S, bez(q, np.linspace(0, 1, 2000)))
    return q, float(dist.max())


# ---- checks ---------------------------------------------------------------------

def check(contours, metrics=None, tol=None, outer=None):
    """Flaws as dicts: kind, severity (error/warn/info), points (node numbers), at [x, y], value (units or degrees), text. outer: "cw"/"ccw" to require a contour direction."""
    tol = {**TOL, **(tol or {})}
    flat = [p for con in contours for p in con]
    issues = []

    def add(kind, sev, pts, at, value, text):
        issues.append(dict(kind=kind, severity=sev, points=pts, at=[round(float(at[0]), 1), round(float(at[1]), 1)], value=round(float(value), 2), text=text))

    nodes = []      # (index, xy, vertical tangent, horizontal tangent)
    all_segs = contour_segments(contours)
    for segs in all_segs:
        for k, s in enumerate(segs):
            prev = segs[k - 1]
            i, p = s["i0"], s["p"][0]
            if s.get("quad") and k and segs[k - 1].get("quad") and segs[k - 1]["i0"] == i:
                continue    # implied on-curve point inside a quadratic run
            tin, tout = tangent_in(prev), tangent_out(s)
            a = angle(tin, tout)
            smooth = flat[i][2] == "smooth"
            vert = any(off_axis(t)[2] == "vertical" and off_axis(t)[0] < 1 for t in (tin, tout) if np.linalg.norm(t) > 1e-9)
            hori = any(off_axis(t)[2] == "horizontal" and off_axis(t)[0] < 1 for t in (tin, tout) if np.linalg.norm(t) > 1e-9)
            nodes.append((i, p, vert, hori))
            if len(segs) > 1 and np.linalg.norm(p - prev["p"][0]) < tol["dup"]:
                add("duplicate", "error", [prev["i0"], i], p, np.linalg.norm(p - prev["p"][0]), f"points {prev['i0']} and {i} sit on top of each other")
            if smooth and a > tol["smooth_deg"]:
                lat = min(np.linalg.norm(tin), np.linalg.norm(tout)) * math.sin(math.radians(a))
                add("smooth-bent", "error", [i], p, lat, f"point {i} is smooth but its handles bend {a:.2f}°, {lat:.1f} u off a straight line")
            elif not smooth and tol["smooth_deg"] < a < tol["kink_deg"]:
                add("kink", "warn", [i], p, a, f"corner at point {i} turns only {a:.2f}°: make it smooth or a real corner")
            if len(segs) > 2 and a < 0.5:
                if prev["kind"] == "line" and s["kind"] == "line":
                    add("extra-node", "error", [i], p, 0, f"point {i} lies on a straight line: remove it")
                elif prev["kind"] == "cubic" and s["kind"] == "cubic" and not s.get("quad") and not (vert or hori):   # extremes stay
                    _, dev = fit_one(prev["p"], s["p"])
                    if dev < tol["removable"]:
                        add("extra-node", "error", [i], p, dev, f"point {i} is extra: one curve from {prev['i0']} to {s['i1']} fits within {dev:.2f} u")
            if prev["kind"] == "cubic" and s["kind"] == "cubic" and a < 1:
                kl, kr = float(curvature(prev["p"], 1.0)), float(curvature(s["p"], 0.0))
                big, small = max(abs(kl), abs(kr)), min(abs(kl), abs(kr))
                if big > 1 / 5000:
                    if kl * kr < 0 and small > 1 / 20000:
                        add("curvature-flip", "warn", [i], p, 0, f"curvature reverses at point {i}: radius {1 / kl:+.0f} u in, {1 / kr:+.0f} u out")
                    elif small < 1e-9 or big / small > tol["g2_ratio"]:
                        r = big / max(small, 1e-9)
                        add("curvature-jump", "warn", [i], p, r, f"curvature jumps x{min(r, 999):.1f} at point {i}: radius {1 / max(abs(kl), 1e-9):.0f} u in, {1 / max(abs(kr), 1e-9):.0f} u out")
        for s in segs:
            p = s["p"]
            if s["kind"] == "line":
                deg, off, ax = off_axis(p[1] - p[0])
                if tol["smooth_deg"] < deg < tol["axis_deg"] and off > tol["axis_min"]:
                    add("crooked-line", "warn", [s["i0"], s["i1"]], (p[0] + p[1]) / 2, off, f"line {s['i0']}-{s['i1']} is {off:.1f} u off {ax} ({deg:.2f}°)")
                continue
            if not s.get("quad"):
                for node, h, o in ((s["i0"], p[1] - p[0], s["offs"][0]), (s["i1"], p[2] - p[3], s["offs"][-1])):
                    deg, off, ax = off_axis(h)
                    if np.linalg.norm(h) > 1 and tol["smooth_deg"] < deg < tol["axis_deg"] and off > tol["axis_min"]:
                        add("crooked-handle", "warn", [node, o], P(flat[o]), off, f"handle {o} of point {node} is {off:.1f} u off {ax} ({deg:.2f}°)")
            for axis, name in ((0, "x"), (1, "y")):
                a_ = -p[0] + 3 * p[1] - 3 * p[2] + p[3]
                b_ = 2 * (p[0] - 2 * p[1] + p[2])
                c_ = p[1] - p[0]
                roots = np.roots([a_[axis], b_[axis], c_[axis]]) if abs(a_[axis]) > 1e-12 else ([-c_[axis] / b_[axis]] if abs(b_[axis]) > 1e-12 else [])
                lo, hi = sorted((p[0][axis], p[3][axis]))
                for t in roots:
                    if np.isreal(t) and 0.01 < t.real < 0.99:
                        v = bez(p, t.real)
                        ex = max(v[axis] - hi, lo - v[axis])
                        if ex > tol["extreme"]:
                            add("missing-extreme", "error", [s["i0"], s["i1"]], v, ex, f"segment {s['i0']}-{s['i1']} bulges {ex:.1f} u past its ends in {name}: add an extreme point")
            t = np.linspace(0.03, 0.97, 160)
            k = curvature(p, t)
            sig = np.abs(k) > 1e-6
            flips = np.flatnonzero((np.sign(k[:-1]) != np.sign(k[1:])) & sig[:-1] & sig[1:])
            for f in flips:
                add("inflection", "info", [s["i0"], s["i1"]], bez(p, t[f]), 0, f"segment {s['i0']}-{s['i1']} inflects (S-bend) inside itself")
            ch = np.linalg.norm(p[-1] - p[0])
            if ch < tol["tiny"]:
                add("tiny-segment", "warn", [s["i0"], s["i1"]], p[0], ch, f"segment {s['i0']}-{s['i1']} is only {ch:.1f} u long")
            h0, h1 = p[1] - p[0], p[2] - p[3]
            den = h0[0] * (-h1[1]) - h0[1] * (-h1[0])
            ch_v = p[3] - p[0]
            same_side = (ch_v[0] * h0[1] - ch_v[1] * h0[0]) * (ch_v[0] * (p[2] - p[0])[1] - ch_v[1] * (p[2] - p[0])[0]) > 0   # not an S-curve
            if same_side and not len(flips) and abs(den) > 1e-9 and np.linalg.norm(h0) > 1e-9 and np.linalg.norm(h1) > 1e-9:
                r = p[3] - p[0]
                sa = (r[0] * (-h1[1]) - r[1] * (-h1[0])) / den     # p0 + sa*h0 = p3 + sb*h1
                sb = (h0[0] * r[1] - h0[1] * r[0]) / den
                if sa > 0 and sb > 0 and (sa < 1 or sb < 1):
                    add("handle-past-crossing", "warn", [s["i0"], s["i1"]], p[1] if sa < 1 else p[2], max(1 / sa, 1 / sb),
                        f"segment {s['i0']}-{s['i1']}: a handle reaches past where the two handle lines cross ({max(1 / sa, 1 / sb) * 100:.0f}%)")

    for key, axis, label in (("vert", 0, "x"), ("hori", 1, "y")):
        pick = [(i, p) for i, p, v, h in nodes if (v if key == "vert" else h)]
        for m, (i, p) in enumerate(pick):
            for j, q in pick[m + 1:]:
                d = abs(p[axis] - q[axis])
                if 0.1 < d <= tol["near"]:
                    add("near-miss", "warn", [i, j], p, d, f"points {i} and {j} almost share {label} ({p[axis]:.1f} vs {q[axis]:.1f}, {d:.1f} u apart)")
    for name, v in (metrics or {}).items():
        for i, p, _, h in nodes:
            d = p[1] - v
            if h and 0.1 < abs(d) <= tol["metric_near"]:
                add("near-metric", "warn", [i], p, abs(d), f"point {i} is {d:+.1f} u off {name} ({v:g})")

    polys = polygons(contours)
    if polys:   # outer contours one way, counters the other; which way comes from `outer` ("cw"/"ccw") or else the biggest contour
        biggest = max(range(len(polys)), key=lambda c: abs(area(polys[c])))
        outer_ccw = {"ccw": True, "cw": False}.get(outer, area(polys[biggest]) > 0)
        for c, poly in enumerate(polys):
            depth = sum(1 for o, other in enumerate(polys) if o != c and inside(poly[:1], [other])[0])
            ccw = area(poly) > 0
            if ccw != (outer_ccw == (depth % 2 == 0)):
                first = sum(len(con) for con in contours[:c])
                add("direction", "error", [first], poly[0], 0, f"contour {c} runs {'counter-' if ccw else ''}clockwise, the same way as {'its outer contour' if depth % 2 else 'the counters'}: reverse it")

    seen, out = set(), []
    for it in sorted(issues, key=lambda x: (SEVERITY[x["severity"]], x["kind"], x["points"])):
        key = (it["kind"], tuple(it["points"]), tuple(it["at"]))
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


# ---- diff -----------------------------------------------------------------------

def _pattern(con):
    return [p[2] in OFF for p in con]


def align(A, B):
    """A's contours rotated to B's start points when the structures match, else None."""
    if [len(c) for c in A] != [len(c) for c in B]:
        return None
    out = []
    for ca, cb in zip(A, B):
        best = None
        for r in range(len(ca)):
            rot = ca[r:] + ca[:r]
            if _pattern(rot) != _pattern(cb):
                continue
            d = sum((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 for a, b in zip(rot, cb))
            if best is None or d < best[0]:
                best = (d, rot)
        if best is None:
            return None
        out.append(best[1])
    return out


def diff(A, B, thr=0.5, step=2.0):
    """How outline B differs from baseline A, in font units: moved points (when the structures match), regions where the outline moved (signed: + ink added, - removed), ink area change."""
    res = {}
    Ar = align(A, B)
    if Ar is not None:
        moves, i = [], 0
        for ca, cb in zip(Ar, B):
            for a, b in zip(ca, cb):
                dx, dy = b[0] - a[0], b[1] - a[1]
                if abs(dx) > 0.01 or abs(dy) > 0.01 or a[2] != b[2]:
                    kind = "" if a[2] == b[2] else f" type {a[2]} -> {b[2]}"
                    moves.append(f"point {i} ({b[2]}) ({a[0]:g}, {a[1]:g}) -> ({b[0]:g}, {b[1]:g})  d({dx:+.2f}, {dy:+.2f}){kind}")
                i += 1
        res["points"] = moves
    else:
        res["structure"] = f"{len(A)} contours {[len(c) for c in A]} points -> {len(B)} contours {[len(c) for c in B]} points"
    SA, _, _ = sample(A, step)
    SB, nB, cB = sample(B, step)
    if not len(SA) or not len(SB):
        res["note"] = "one side is empty"
        return res
    dBA, _ = nearest(SB, SA)
    dAB, _ = nearest(SA, SB)
    sign = np.where(inside(SB, polygons(A, step)), -1.0, 1.0)
    dev = dBA * sign
    regions = []
    seg_end = {s["i0"]: s["i1"] for ss in contour_segments(B) for s in ss}
    hot = np.abs(dev) > thr
    for c in np.unique(cB):
        idx = np.flatnonzero(cB == c)
        h = hot[idx]
        if not h.any():
            continue
        runs = np.split(idx[h], np.flatnonzero(np.diff(idx[h]) > 1) + 1)
        if len(runs) > 1 and runs[0][0] == idx[0] and runs[-1][-1] == idx[-1]:     # wraps past the contour start
            runs = [np.concatenate([runs[-1], runs[0]])] + runs[1:-1]
        for r in runs:
            k = r[np.argmax(np.abs(dev[r]))]
            regions.append(dict(segment=f"{nB[k]}-{seg_end[nB[k]]}", at=[round(float(SB[k][0]), 1), round(float(SB[k][1]), 1)], max=round(float(dev[k]), 2), length=round(len(r) * step)))
    regions.sort(key=lambda r: -abs(r["max"]))
    ia, ib = ink(A), ink(B)
    res.update(max_deviation=round(float(dBA.max()), 2), max_back=round(float(dAB.max()), 2), regions=regions,
               ink_change=round(ib - ia), ink_change_pct=round(100 * (ib - ia) / ia, 2) if ia else None)
    return res


def ink(contours):
    """Ink area in square units (outer contours positive, counters negative, whatever their direction)."""
    polys = polygons(contours)
    tot = 0.0
    for c, poly in enumerate(polys):
        depth = sum(1 for o, other in enumerate(polys) if o != c and inside(poly[:1], [other])[0])
        tot += abs(area(poly)) * (1 if depth % 2 == 0 else -1)
    return tot


# ---- drawing --------------------------------------------------------------------

def _font(size):
    try:
        return ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size)
    except OSError:
        return ImageFont.load_default(size=size)


def draw(contours, *, base=None, ref=None, amplify=10.0, comb=False, labels=False, issues=None, zoom=None, metrics=None, size=1000):
    """PNG image of a glyph. base: baseline contours, drawn grey, with the live outline's difference from it amplified (magenta). ref: reference contours already in this glyph's units (black). zoom: (x, y, radius in units); adds a 1-unit grid. issues: from check(), circled and numbered."""
    every = [c for c in (contours, base, ref) if c]
    pts = np.array([p[:2] for cs in every for con in cs for p in con] or [[0, 0]], float)
    if zoom:
        cx, cy, r = zoom
        x0, x1, y0, y1 = cx - r, cx + r, cy - r, cy + r
    else:
        x0, x1, y0, y1 = pts[:, 0].min(), pts[:, 0].max(), pts[:, 1].min(), pts[:, 1].max()
        pad = 0.12 * max(x1 - x0, y1 - y0)
        x0, x1, y0, y1 = x0 - pad, x1 + pad, y0 - pad, y1 + pad
    k = size / max(x1 - x0, y1 - y0)
    W, H = int((x1 - x0) * k), int((y1 - y0) * k)
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    T = lambda x, y: ((x - x0) * k, (y1 - y) * k)  # noqa: E731
    small = _font(15)
    if zoom:
        for step, col in ((1, (238, 238, 238)), (10, (205, 205, 205))):
            if step * k < 4:
                continue
            for gx in range(math.ceil(x0 / step) * step, int(x1) + 1, step):
                d.line([T(gx, y0), T(gx, y1)], fill=col)
            for gy in range(math.ceil(y0 / step) * step, int(y1) + 1, step):
                d.line([T(x0, gy), T(x1, gy)], fill=col)
        d.text((8, H - 24), f"grid 1 u (light), 10 u (dark); {k:.1f} px per unit", fill=(120, 120, 120), font=small)
    for name, v in (metrics or {}).items():
        if y0 <= v <= y1:
            d.line([T(x0, v), T(x1, v)], fill=(190, 190, 190), width=1)
            d.text((T(x0, v)[0] + 4, T(x0, v)[1] - 18), f"{name} {v:g}", fill=(150, 150, 150), font=small)

    def outline(cs, color, width):
        for poly in polygons(cs, step=max(0.5, 2 / k)):
            pl = [T(*q) for q in poly]
            d.line(pl + pl[:1], fill=color, width=width)

    if ref:
        outline(ref, (0, 0, 0), 2)
    if base:
        outline(base, (170, 170, 170), 3)
    outline(contours, (220, 40, 40), 2 if (base or ref) else 3)
    if base:
        SB, _, cB = sample(contours, max(0.5, 2 / k))
        SA, _, _ = sample(base, 0.5)
        _, idx = nearest(SB, SA)
        amp = SA[idx] + amplify * (SB - SA[idx])
        for c in np.unique(cB):
            pl = [T(*q) for q in amp[cB == c]]
            d.line(pl + pl[:1], fill=(200, 0, 200), width=2)
        d.text((8, 8), f"grey: baseline, magenta: difference x{amplify:g}, red: live", fill=(120, 120, 120), font=small)
    if comb:
        segs = [s for ss in contour_segments(contours) for s in ss if s["kind"] == "cubic"]
        ks = np.concatenate([np.abs(curvature(s["p"], np.linspace(0, 1, 50))) for s in segs]) if segs else np.array([1e-9])
        reach = 0.07 * max(x1 - x0, y1 - y0) if not zoom else 0.5 * zoom[2]
        scale = reach / max(float(np.percentile(ks, 75)), 1e-9)       # ponytail: robust scale; tight corners clip at 2x reach
        for s in segs:
            t = np.linspace(0, 1, 40)
            q, dv, kv = bez(s["p"], t), d1(s["p"], t), curvature(s["p"], t)
            nrm = np.stack([dv[:, 1], -dv[:, 0]], 1) / np.maximum(np.linalg.norm(dv, axis=1), 1e-9)[:, None]
            tip = q + nrm * np.clip(kv * scale, -2 * reach, 2 * reach)[:, None]     # away from the centre of curvature
            for a, b in zip(q, tip):
                d.line([T(*a), T(*b)], fill=(120, 160, 230), width=1)
            d.line([T(*b) for b in tip], fill=(60, 110, 220), width=2)
    flat = [p for con in contours for p in con]
    for segs in contour_segments(contours):
        for s in segs:
            if s["kind"] == "cubic" and not s.get("quad"):
                d.line([T(*s["p"][0]), T(*s["p"][1])], fill=(150, 150, 150), width=1)
                d.line([T(*s["p"][3]), T(*s["p"][2])], fill=(150, 150, 150), width=1)
    for i, (x, y, t) in enumerate(flat):
        X, Y = T(x, y)
        if t in OFF:
            d.ellipse([X - 4, Y - 4, X + 4, Y + 4], outline=(110, 110, 110), width=2)
        elif t == "smooth":
            d.ellipse([X - 6, Y - 6, X + 6, Y + 6], fill=(255, 255, 255), outline=(220, 40, 40), width=2)
        else:
            d.rectangle([X - 5, Y - 5, X + 5, Y + 5], fill=(255, 255, 255), outline=(220, 40, 40), width=2)
        if labels:
            d.text((X + 7, Y + 3), str(i), fill=(60, 60, 60) if t not in OFF else (140, 140, 140), font=small)
    for n, it in enumerate(issues or [], 1):
        X, Y = T(*it["at"])
        col = (225, 60, 30) if it["severity"] == "error" else (235, 150, 0) if it["severity"] == "warn" else (120, 120, 120)
        d.ellipse([X - 16, Y - 16, X + 16, Y + 16], outline=col, width=3)
        d.text((X + 16, Y - 30), str(n), fill=col, font=_font(20))
    return img


# ---- loading fonts --------------------------------------------------------------

class _Collect:
    """Point pen -> contours of [x, y, type]."""
    def __init__(self):
        self.contours, self.cur = [], None

    def beginPath(self, identifier=None, **kw):
        self.cur = []

    def addPoint(self, pt, segmentType=None, smooth=False, name=None, identifier=None, **kw):
        self.cur.append([float(pt[0]), float(pt[1]), segmentType, smooth])

    def endPath(self):
        out, n = [], len(self.cur)
        for i, (x, y, seg, sm) in enumerate(self.cur):
            if seg is None:
                nxt = next((self.cur[(i + m) % n][2] for m in range(1, n) if self.cur[(i + m) % n][2] is not None), "curve")
                out.append([x, y, "qoff" if nxt == "qcurve" else "off"])
            else:
                out.append([x, y, "smooth" if sm else "on"])
        self.contours.append(out)

    def addComponent(self, *a, **kw):
        pass


def load_glyph(path, glyph=None, unicode=None):
    """(contours, advance, metrics) of a glyph in an OTF/TTF or UFO, found by name, else by code point."""
    pen = _Collect()
    if path.rstrip("/").endswith(".ufo"):
        import ufoLib2
        f = ufoLib2.Font.open(path)
        name = glyph if glyph in f else next((g.name for g in f if unicode in (g.unicodes or [])), None)
        f[name].drawPoints(pen)
        info = f.info
        m = dict(baseline=0, xHeight=info.xHeight, capHeight=info.capHeight, ascender=info.ascender, descender=info.descender)
        return pen.contours, f[name].width, {k: v for k, v in m.items() if v is not None}
    from fontTools.pens.pointPen import SegmentToPointPen
    from fontTools.ttLib import TTFont
    f = TTFont(path)
    gs, cm = f.getGlyphSet(), f.getBestCmap()
    name = glyph if glyph in gs else cm.get(unicode)
    gs[name].draw(SegmentToPointPen(pen))
    os2 = f["OS/2"] if "OS/2" in f else None
    m = dict(baseline=0, xHeight=getattr(os2, "sXHeight", None), capHeight=getattr(os2, "sCapHeight", None),
             ascender=f["hhea"].ascent, descender=f["hhea"].descent)
    return pen.contours, f["hmtx"][name][0], {k: v for k, v in m.items() if v}


# ---- self-check and CLI -----------------------------------------------------------

def _circle(cx, cy, r, extra=False):
    c = 0.5523 * r
    con = [[cx + r, cy, "smooth"], [cx + r, cy + c, "off"], [cx + c, cy + r, "off"], [cx, cy + r, "smooth"], [cx - c, cy + r, "off"], [cx - r, cy + c, "off"],
           [cx - r, cy, "smooth"], [cx - r, cy - c, "off"], [cx - c, cy - r, "off"], [cx, cy - r, "smooth"], [cx + c, cy - r, "off"], [cx + r, cy - c, "off"]]
    if extra:   # split the first quarter at t=0.5: the same shape, one node too many
        p = np.array([con[0][:2], con[1][:2], con[2][:2], con[3][:2]], float)
        m = bez(p, 0.5)
        a1, b1 = (p[0] + p[1]) / 2, (p[1] + p[2]) / 2
        c1 = (p[2] + p[3]) / 2
        a2, b2 = (a1 + b1) / 2, (b1 + c1) / 2
        con = [con[0], [*a1, "off"], [*a2, "off"], [*m, "smooth"], [*b2, "off"], [*c1, "off"]] + con[3:]
        con = [[float(x), float(y), t] for x, y, t in con]
    return con


def selftest():
    kinds = lambda iss: {i["kind"] for i in iss if i["severity"] != "info"}  # noqa: E731
    assert kinds(check([_circle(0, 0, 100)])) == set(), check([_circle(0, 0, 100)])
    assert "extra-node" in kinds(check([_circle(0, 0, 100, extra=True)]))
    sq = [[0, 0, "on"], [100, 0, "on"], [103, 200, "on"], [0, 200, "on"]]           # right side leans 3 u
    assert "crooked-line" in kinds(check([sq]))
    bent = _circle(0, 0, 100)
    bent[1][0] += 2                                                                     # handle of a smooth node pulled off its line
    assert {"smooth-bent", "crooked-handle"} <= kinds(check([bent]))
    lean = [[0, 0, "on"], [100, 0, "on"], [100, 100, "on"], [30, 140, "off"], [-30, 130, "off"], [0, 100, "on"]]   # the top curve rises past both ends
    assert "missing-extreme" in kinds(check([lean]))
    assert "near-metric" in kinds(check([[[0, 0, "on"], [100, 0, "on"], [100, 497, "on"], [0, 497, "on"]]], metrics={"xHeight": 500}))
    cw = list(reversed(_circle(0, 0, 100)))
    assert "direction" in kinds(check([cw], outer="ccw")) and "direction" not in kinds(check([cw]))
    assert "direction" in kinds(check([_circle(0, 0, 100), _circle(0, 0, 50)]))                  # counter runs the same way as its outer
    r = diff([sq], [[[0, 0, "on"], [100, 0, "on"], [100, 200, "on"], [0, 200, "on"]]])
    assert r["points"] == ["point 2 (on) (103, 200) -> (100, 200)  d(-3.00, +0.00)"], r
    assert 2.5 < r["max_back"] <= 3.01 and r["ink_change"] < 0, r
    rot = diff([_circle(0, 0, 100)], [_circle(0, 0, 100)[3:] + _circle(0, 0, 100)[:3]])
    assert rot["points"] == [] and rot["max_deviation"] < 0.5, rot
    print("ok")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("font")
    ap.add_argument("glyph")
    ap.add_argument("--against")
    ap.add_argument("--png")
    ap.add_argument("--zoom")
    ap.add_argument("--amplify", type=float, default=10)
    a = ap.parse_args()
    g, u = (a.glyph, None) if len(a.glyph) > 1 else (None, ord(a.glyph))
    cons, _, metrics = load_glyph(a.font, g, u)
    iss = check(cons, metrics)
    for n, it in enumerate(iss, 1):
        print(f"{n:3} {it['severity']:5} {it['text']}")
    base = None
    if a.against:
        base, _, _ = load_glyph(a.against, g, u)
        r = diff(base, cons)
        print({k: v for k, v in r.items() if k != "regions"})
        for reg in r["regions"]:
            print("  region", reg)
    if a.png:
        z = None
        if a.zoom:
            pi, _, rad = a.zoom.partition(":")
            x, y, _ = [p for con in cons for p in con][int(pi)]
            z = (x, y, float(rad or 40))
        draw(cons, base=base, amplify=a.amplify, comb=True, labels=True, issues=iss, zoom=z, metrics=metrics).save(a.png)


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
