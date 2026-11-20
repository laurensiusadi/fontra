"""MCP server (stdio) that joins a running `fontra filesystem` project as a second client, so any MCP agent (Claude Code, Codex, Cursor, …) and the user edit the same glyphs live.

    <fontra-venv>/bin/python -m fontra.mcp.live DIR [PROJECT]       # starts `fontra filesystem DIR` on FONTRA_PORT (8769) if nothing answers there
    <fontra-venv>/bin/python -m fontra.mcp.live URL [PROJECT]       # only connects; default http://localhost:8769 OvernightSans-Regular.ufo
    <fontra-venv>/bin/python -m fontra.mcp.live --selftest

Tools: get_glyph, put_glyph, changes_since, check, diff, render, measure, status. Points are [x, y, type] with type "on", "smooth", "off" (cubic) or "qoff" (quadratic); point numbers are Fontra's (one count across contours). Checks, diffs and drawings come from glyphcheck.py.

A status page (http://127.0.0.1:8780, next free port up to 8785) shows the link, the Fontra server and recent activity, and turns the link off and Fontra on or off. Runs with the Fontra environment's python (aiohttp, numpy, Pillow, fontTools).
"""
import asyncio
import base64
import collections
import io
import itertools
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from urllib.parse import quote, urlparse

import aiohttp
import numpy as np
from aiohttp import web

from fontra.mcp import glyphcheck as gc

FONTRA_PORT = os.environ.get("FONTRA_PORT", "8769")   # not Fontra's stock 8000, which other dev servers often hold
ARG = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else f"http://localhost:{FONTRA_PORT}"
SERVE_DIR = os.path.abspath(ARG) if os.path.isdir(ARG) else None    # a folder: start `fontra filesystem` on it if nothing answers
URL = f"http://localhost:{FONTRA_PORT}" if SERVE_DIR else ARG
PROJECT = sys.argv[2] if len(sys.argv) > 2 else "OvernightSans-Regular.ufo"
OUT = os.environ.get("FONTRA_LIVE_OUT", "/tmp/fontra-live")
TYPES = {0: "on", 8: "smooth", 2: "off", 1: "qoff"}
CODES = {v: k for k, v in TYPES.items()}
STATE = dict(client="Agent", link=True, stopped_by_user=False, log=collections.deque(maxlen=60), image=None, ui=None, since=time.time())


def log(source, title, summary=""):
    STATE["log"].appendleft(dict(t=time.strftime("%H:%M:%S"), source=source, title=title, summary=summary))


# ---- outlines -------------------------------------------------------------------

def unpack(path):
    """Fontra packed path dict -> list of contours of [x, y, type]."""
    out, start, c = [], 0, path["coordinates"]
    for info in path["contourInfo"]:
        end = info["endPoint"] + 1
        out.append([[c[2 * i], c[2 * i + 1], TYPES[path["pointTypes"][i]]] for i in range(start, end)])
        start = end
    return out


def pack(contours):
    coords, types, info = [], [], []
    for con in contours:
        for x, y, t in con:
            coords += [x, y]
            types.append(CODES[t])
        info.append({"endPoint": len(types) - 1, "isClosed": True})   # ponytail: open contours not supported
    return {"coordinates": coords, "pointTypes": types, "contourInfo": info}


def mask(polys, k, x0, ymin, W, H):
    from PIL import Image, ImageDraw
    m = np.zeros((H, W), bool)
    for c in polys:
        im = Image.new("1", (W, H), 0)
        ImageDraw.Draw(im).polygon([((x - x0) * k + 2, H - 1 - (y - ymin) * k) for x, y in c], fill=1)
        m ^= np.array(im, bool)
    return m


# ---- measurements -------------------------------------------------------------

def n_anatomy(polys, xh):
    """n's anatomy in font units (port of the scratch nmeasure.py, which normalized to x-height 508)."""
    k = 2
    xs = [x for c in polys for x, _ in c]
    ys = [y for c in polys for _, y in c]
    x0, ymin = min(xs), min(ys)
    W, H = int((max(xs) - x0) * k) + 4, int((max(ys) - ymin) * k) + 4
    m = mask(polys, k, x0, ymin, W, H)
    row = lambda y: m[int(H - 1 - (y - ymin) * k)]  # noqa: E731
    col = lambda u: m[:, int(u * k) + 2]  # noqa: E731
    on = np.flatnonzero(row(xh * 0.3))
    runs = np.split(on, np.flatnonzero(np.diff(on) > 1) + 1)
    sl, sr = (runs[0][-1] - runs[0][0]) / k, (runs[-1][-1] - runs[-1][0]) / k
    Wu = (max(xs) - x0)
    cl, cr = runs[0][-1] / k, runs[-1][0] / k
    c = col((cl + cr) / 2)
    ys_ = np.flatnonzero(c)
    top_run = ys_[:np.flatnonzero(np.diff(ys_) > 1)[0] + 1] if (np.diff(ys_) > 1).any() else ys_
    c2 = col(cl + sl * 0.08)
    y2 = np.flatnonzero(c2)
    first_white = y2[0] + np.flatnonzero(~c2[y2[0]:])[0]
    top_y = np.flatnonzero(m.any(1))[0]
    r85 = np.flatnonzero(row(xh * 0.85))
    band = np.flatnonzero(m[top_y:top_y + 6].any(0))
    ox = (band[0] + band[-1]) / (2 * k)
    best = None
    for X in range(int(cl * k) + 2, int(cr * k) - 2):
        cc = m[:, X + 2]
        ink = np.flatnonzero(cc)
        wh = ink[0] + np.flatnonzero(~cc[ink[0]:])[0]
        if best is None or wh < best[0] - 0.5:
            best = (wh, [X])
        elif abs(wh - best[0]) <= 0.5:
            best[1].append(X)
    ix = np.mean(best[1]) / k
    r72 = row(xh * 0.72)
    wh72 = np.flatnonzero(~r72[int(cl * k):int(cr * k) + 1])
    r = lambda v: round(float(v), 1)  # noqa: E731
    return {
        "x_height": r(xh), "ink_width": r(Wu), "stem_left": r(sl), "stem_right": r(sr),
        "arch_top": r(len(top_run) / k),                     # arch thickness at the counter's centre
        "crotch": r((first_white - y2[0]) / k),              # arch thickness just right of the left stem
        "notch_depth": r((y2[0] - top_y) / k),               # how far under the ink top the arch leaves the stem
        "counter_width": r(cr - cl),
        "shoulder": r(r85[-1] / k - (Wu - sr)),              # outer right edge at 0.85 xh past the right stem's outer edge
        "outer_crest": round(float((ox - cl) / (cr - cl)), 3),   # share of the way from left stem inner edge to right stem inner edge
        "counter_crest": round(float((ix - cl) / (cr - cl)), 3),
        "side72_left": r((cl * k + wh72[0]) / k - cl), "side72_right": r(cr - (cl * k + wh72[-1]) / k),
    }


MEASURES = {"n_anatomy": n_anatomy}


# ---- Fontra connection --------------------------------------------------------

class Fontra:
    def __init__(self, url, project):
        self.url, self.project = url, project
        self.ws = self.session = None
        self.ids = itertools.count()
        self.futures = {}
        self.events = []        # (glyph names, isLive) for every externalChange the server pushed us
        self.snap = {}          # (glyph, layer) -> (contours, advance) as last seen by the agent
        self.glyph_map = self.sources = None

    @property
    def connected(self):
        return self.ws is not None and not self.ws.closed

    async def ensure(self):
        if not STATE["link"]:
            raise ConnectionError(f"the link to Fontra is off; turn it on in the status page {STATE['ui']}")
        if self.connected:
            return
        self.session = self.session or aiohttp.ClientSession()
        ws_url = self.url.replace("http", "ws", 1) + "/websocket?project=" + quote(self.project)
        try:
            self.ws = await self.session.ws_connect(ws_url, max_msg_size=0)
        except aiohttp.ClientConnectorError:
            if not SERVE_DIR or STATE["stopped_by_user"]:
                raise ConnectionError(f"Fontra is not running at {self.url}" + (f"; start it in the status page {STATE['ui']}" if SERVE_DIR else ""))
            await self.serve()
            self.ws = await self.session.ws_connect(ws_url, max_msg_size=0)
        await self.ws.send_json({"client-uuid": str(uuid.uuid4())})
        asyncio.create_task(self.reader(self.ws))
        self.glyph_map = self.sources = None
        await self.call("subscribeChanges", {"glyphs": None}, False)
        log("Agent","Connected to Fontra", self.project)

    async def disconnect(self):
        if self.connected:
            await self.ws.close()
            log("Agent","Disconnected from Fontra")

    async def up(self):
        self.session = self.session or aiohttp.ClientSession()
        try:
            async with self.session.get(self.url, timeout=aiohttp.ClientTimeout(total=1)) as r:
                return r.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False

    async def serve(self):
        """Start `fontra filesystem SERVE_DIR` in its own session (it outlives this MCP server, so the user's tab keeps working) and wait for it."""
        self.session = self.session or aiohttp.ClientSession()
        port = self.url.rsplit(":", 1)[1]
        fontra = os.path.join(os.path.dirname(sys.executable), "fontra")
        os.makedirs(OUT, exist_ok=True)
        lg = open(os.path.join(OUT, "fontra.log"), "a")
        subprocess.Popen([fontra, "--http-port", port, "filesystem", SERVE_DIR], stdout=lg, stderr=lg, stdin=subprocess.DEVNULL, start_new_session=True)
        for _ in range(60):
            await asyncio.sleep(0.25)
            if await self.up():
                log("Agent","Started Fontra", f"{SERVE_DIR} on port {port}")
                return
        raise ConnectionError(f"fontra did not start on {self.url}; see {OUT}/fontra.log")

    async def reader(self, ws):
        async for msg in ws:
            d = msg.json()
            if "client-call-id" in d:
                fut = self.futures.pop(d["client-call-id"])
                if "exception" in d or "error" in d:
                    fut.set_exception(RuntimeError(d.get("exception") or d.get("error")))
                else:
                    fut.set_result(d.get("return-value"))
            elif "server-call-id" in d:
                name, args = d["method-name"], d.get("arguments", [])
                if name == "externalChange":
                    gl = glyphs_in(args[0])
                    self.events.append((gl, args[1]))
                    log("Fontra", "Edited " + ", ".join(gl or ["font data"]))
                elif name == "reloadData":
                    self.events.append((list((args[0] or {}).get("glyphs") or []) or ["*"], False))
                    log("Fontra", "Reloaded from disk")
                await ws.send_json({"server-call-id": d["server-call-id"], "return-value": None})
            elif "initialization-error" in d:
                print("fontra:", d, file=sys.stderr)
        for fut in self.futures.values():
            fut.set_exception(ConnectionError("Fontra websocket closed"))
        self.futures.clear()

    async def call(self, method, *args):
        await self.ensure()
        cid = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.futures[cid] = fut
        await self.ws.send_json({"client-call-id": cid, "method-name": method, "arguments": list(args)})
        return await asyncio.wait_for(fut, 30)

    async def glyph(self, name, layer=None):
        g = await self.call("getGlyph", name)
        if g is None:
            raise KeyError(f"no glyph {name!r}")
        layer = layer or g["sources"][0]["layerName"]
        lg = g["layers"][layer]["glyph"]
        return g, layer, unpack(lg["path"]), lg.get("xAdvance")

    async def unicode_of(self, name):
        if self.glyph_map is None:
            self.glyph_map = await self.call("getGlyphMap")
        cps = self.glyph_map.get(name) or []
        return cps[0] if cps else None

    async def metrics(self, g, layer):
        if self.sources is None:
            self.sources = await self.call("getSources")
        src = next((s for s in g["sources"] if s["layerName"] == layer), g["sources"][0])
        fs = self.sources.get(src.get("locationBase") or "") or next(iter(self.sources.values()), {})
        return {k: v["value"] for k, v in (fs.get("lineMetricsHorizontalLayout") or {}).items()}


def glyphs_in(change):
    p = change.get("p", [])
    if len(p) >= 2 and p[0] == "glyphs":
        return [p[1]]
    if p == ["glyphs"] or not p:
        return sorted({n for c in change.get("c", []) for n in glyphs_in({**c, "p": p + c.get("p", [])})})
    return []


def issue_lines(issues):
    return [f"{n}. [{it['severity']}] {it['text']}" for n, it in enumerate(issues, 1)]


def image(img, name):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    img.save(path)
    STATE["image"] = name
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return path, {"type": "image", "data": base64.b64encode(buf.getvalue()).decode(), "mimeType": "image/png"}


# ---- tools --------------------------------------------------------------------

F = Fontra(URL, PROJECT)


async def t_get_glyph(glyph, layer=None):
    g, layer, contours, adv = await F.glyph(glyph, layer)
    F.snap[(glyph, layer)] = (contours, adv)
    return {"glyph": glyph, "layer": layer, "layers": list(g["layers"]), "advance": adv, "contours": contours}


async def t_put_glyph(glyph, contours=None, moves=None, advance=None, layer=None):
    g, layer, old, adv = await F.glyph(glyph, layer)
    new = json.loads(json.dumps(contours)) if contours is not None else json.loads(json.dumps(old))
    if moves:
        flat_pts = [p for c in new for p in c]
        for i, dx, dy in moves:
            flat_pts[i][0] += dx
            flat_pts[i][1] += dy
    base = ["glyphs", glyph, "layers", layer, "glyph"]
    fwd, back = [{"f": "=", "a": ["path", pack(new)]}], [{"f": "=", "a": ["path", pack(old)]}]
    if advance is not None:
        fwd.append({"f": "=", "a": ["xAdvance", advance]})
        back.append({"f": "=", "a": ["xAdvance", adv]})
    await F.call("editFinal", {"p": base, "c": fwd}, {"p": base, "c": back}, f"{STATE['client']}: put_glyph", True)
    F.snap[(glyph, layer)] = (new, advance if advance is not None else adv)
    m = await F.metrics(g, layer)
    key = lambda it: (it["kind"], tuple(it["points"]))  # noqa: E731
    before = {key(it) for it in gc.check(old, m)}
    introduced = [it for it in gc.check(new, m) if key(it) not in before and it["severity"] != "info"]
    d = gc.diff(old, new)
    log("Agent",f"Edited {glyph}", f"{len(d.get('points', []))} points moved, outline moved up to {d.get('max_deviation', 0)} u" + (f", {len(introduced)} new issues" if introduced else ""))
    return {"glyph": glyph, "layer": layer, "changed": d.get("points") or d.get("structure"), "max_deviation": d.get("max_deviation"),
            "new_issues": issue_lines(introduced) or ["none"]}


async def t_changes_since(glyph, layer=None):
    _, layer, cur, adv = await F.glyph(glyph, layer)
    key = (glyph, layer)
    events = [e for e in F.events if glyph in e[0] or "*" in e[0]]
    F.events = [e for e in F.events if e not in events]
    if key not in F.snap:
        F.snap[key] = (cur, adv)
        return {"glyph": glyph, "layer": layer, "note": "no earlier snapshot; baseline taken now"}
    old, adv_old = F.snap[key]
    F.snap[key] = (cur, adv)
    d = gc.diff(old, cur)
    if adv_old != adv:
        d["advance"] = f"{adv_old} -> {adv}"
    return {"glyph": glyph, "layer": layer, "user_edits_received": len(events), **d}


async def t_check(glyph, layer=None, outer=None, png=False):
    g, layer, contours, _ = await F.glyph(glyph, layer)
    m = await F.metrics(g, layer)
    issues = gc.check(contours, m, outer=outer)
    counts = collections.Counter(it["severity"] for it in issues)
    log("Agent",f"Checked {glyph}", ", ".join(f"{v} {k}" for k, v in counts.items()) or "clean")
    out = {"glyph": glyph, "layer": layer, "counts": dict(counts), "issues": issue_lines(issues) or ["clean"]}
    if not png:
        return out
    path, img = image(gc.draw(contours, issues=issues, labels=True, comb=True, metrics=m), f"{glyph}-check.png")
    return [{"type": "text", "text": json.dumps(out)}, {"type": "text", "text": path}, img]


def baseline_path(against):
    if against in ("base", "previous") and not SERVE_DIR:
        raise ValueError(f"'{against}' needs the server started on a folder; pass a font path instead")
    stem = os.path.splitext(PROJECT)[0]
    if against == "base":
        return os.path.join(SERVE_DIR.rstrip("/") + "-base", stem + ".otf")
    if against == "previous":
        hist = os.path.join(os.path.dirname(SERVE_DIR.rstrip("/")), "edit-history")
        saves = sorted(f for f in os.listdir(hist) if re.fullmatch(re.escape(stem) + r"-\d{8}-\d{6}\.ufo", f))
        if not saves:
            raise FileNotFoundError(f"no saves of {stem} in {hist}")
        return os.path.join(hist, saves[-1])
    return against


async def baseline(glyph, layer, against):
    """Contours to compare the live glyph with: 'snapshot' (what the agent last saw), 'base' (EDIT_DIR-base/<font>.otf), 'previous' (newest save in edit-history), 'layer:NAME', or a font path."""
    if against == "snapshot":
        if (glyph, layer) not in F.snap:
            raise KeyError("no snapshot yet; call get_glyph first")
        return F.snap[(glyph, layer)][0], "the agent's last snapshot"
    if against.startswith("layer:"):
        _, _, cons, _ = await F.glyph(glyph, against[6:])
        return cons, against
    path = baseline_path(against)
    cons, _, _ = gc.load_glyph(path, glyph, await F.unicode_of(glyph))
    return cons, path


async def t_diff(glyph, against="base", layer=None, amplify=10, png=True):
    g, layer, cur, _ = await F.glyph(glyph, layer)
    base, label = await baseline(glyph, layer, against)
    d = gc.diff(base, cur)
    log("Agent",f"Diffed {glyph}", f"against {against}: outline moved up to {d.get('max_deviation')} u")
    out = {"glyph": glyph, "layer": layer, "against": label, **d}
    if not png:
        return out
    path, img = image(gc.draw(cur, base=base, amplify=amplify, labels=True, metrics=await F.metrics(g, layer)), f"{glyph}-diff.png")
    return [{"type": "text", "text": json.dumps(out)}, {"type": "text", "text": path}, img]


async def t_measure(glyph="n", name="n_anatomy", layer=None):
    g, layer, contours, _ = await F.glyph(glyph, layer)
    xh = (await F.metrics(g, layer)).get("xHeight")
    if not xh:
        _, _, xc, _ = await F.glyph("x")
        xh = max(p[1] for p in gc.sample(xc)[0])
    log("Agent",f"Measured {glyph}", name)
    return {"glyph": glyph, "layer": layer, name: MEASURES[name](gc.polygons(contours, 0.5), xh)}


async def t_render(glyph, layer=None, reference=None, against=None, amplify=10, comb=False, labels=True, issues=False, zoom_point=None, zoom_radius=40, size=1000):
    g, layer, contours, _ = await F.glyph(glyph, layer)
    m = await F.metrics(g, layer)
    ref = base = None
    if reference:
        rc, _, rm = gc.load_glyph(reference, glyph, await F.unicode_of(glyph))
        s = (m.get("xHeight") or 500) / (rm.get("xHeight") or 500)
        S, _, _ = gc.sample(contours)
        R, _, _ = gc.sample(rc)
        dx = (S[:, 0].min() + S[:, 0].max()) / 2 - s * (R[:, 0].min() + R[:, 0].max()) / 2
        ref = [[[s * x + dx, s * y, t] for x, y, t in con] for con in rc]
    if against:
        base, _ = await baseline(glyph, layer, against)
    iss = gc.check(contours, m) if issues else None
    zoom = None
    if zoom_point is not None:
        x, y, _ = [p for con in contours for p in con][zoom_point]
        zoom = (x, y, zoom_radius)
    img = gc.draw(contours, base=base, ref=ref, amplify=amplify, comb=comb, labels=labels, issues=iss, zoom=zoom, metrics=m, size=size)
    path, im = image(img, f"{glyph}.png")
    log("Agent",f"Rendered {glyph}", ("zoom on point %s" % zoom_point) if zoom else "")
    text = path + ("  (black: reference at the same x-height)" if reference else "")
    if iss:
        text += "\n" + "\n".join(issue_lines(iss))
    return [{"type": "text", "text": text}, im]


async def t_status():
    return {"status_page": STATE["ui"], "fontra": F.url, "project": PROJECT, "fontra_up": await F.up(), "connected": F.connected, "link": STATE["link"]}


S = {"type": "string"}
N = {"type": "number"}
B = {"type": "boolean"}
LAYER = {"type": "string", "description": "layer name; default the glyph's first source layer"}
AGAINST = {"type": "string", "description": "'base' (the frozen baseline OTF), 'previous' (newest save in edit-history), 'snapshot' (what you last read or wrote), 'layer:NAME', or a font path"}
TOOLS = {
    "get_glyph": (t_get_glyph, "Points, contours and advance of a glyph as Fontra has it now (including edits from the open tab).",
                  {"glyph": S, "layer": LAYER}, ["glyph"]),
    "put_glyph": (t_put_glyph, "Replace a glyph's outline (and optionally advance) through Fontra; the open browser tab updates live. Give `contours` ([[ [x,y,type], ...], ...]) to replace, or `moves` ([[pointIndex, dx, dy], ...]) to nudge points. Returns the moved points and any check issues the edit introduced.",
                  {"glyph": S, "layer": LAYER, "contours": {"type": "array"}, "moves": {"type": "array"}, "advance": N}, ["glyph"]),
    "changes_since": (t_changes_since, "What changed in a glyph since you last read or wrote it: moved points, regions where the outline moved (signed units, + is ink added), ink change, and how many edits the user's tab pushed.",
                      {"glyph": S, "layer": LAYER}, ["glyph"]),
    "check": (t_check, "Find flaws too small to see: extra nodes, smooth nodes with bent handles, almost-smooth kinks, crooked lines and handles (units off axis), near-miss alignments between nodes and to metric lines, missing extremes, curvature jumps and reversals, S-bends, tiny segments, handles past their crossing, contour direction. png=true adds a labelled drawing with a curvature comb.",
              {"glyph": S, "layer": LAYER, "outer": {"type": "string", "enum": ["cw", "ccw"], "description": "require this outer-contour direction; default: consistent within the glyph"}, "png": B}, ["glyph"]),
    "diff": (t_diff, "Compare the live glyph with a baseline in font units: moved points, regions where the outline moved (max signed distance, segment), ink change. The image shows the baseline grey and the difference amplified (default x10) in magenta.",
             {"glyph": S, "against": AGAINST, "layer": LAYER, "amplify": N, "png": B}, ["glyph"]),
    "render": (t_render, "PNG of the live glyph: red outline, nodes (square corner, circle smooth), handles, point numbers. Options: reference font (black, same x-height), against (baseline + amplified difference), comb (curvature comb), issues (circle check findings), zoom_point/zoom_radius (crop around a point with a 1-unit grid, to see a few units).",
               {"glyph": S, "layer": LAYER, "reference": {"type": "string", "description": "path to an OTF/TTF/UFO"}, "against": AGAINST, "amplify": N, "comb": B, "labels": B, "issues": B,
                "zoom_point": {"type": "integer"}, "zoom_radius": N, "size": N}, ["glyph"]),
    "measure": (t_measure, "Run a named measurement on the live outline. Names: " + ", ".join(MEASURES) + ".",
                {"glyph": S, "name": {"type": "string", "enum": list(MEASURES)}, "layer": LAYER}, []),
    "status": (t_status, "The status page URL, whether Fontra is up, and whether the link is on.", {}, []),
}


# ---- status page --------------------------------------------------------------

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fontra live</title><style>
:root{--bg:#f6f6f6;--card:#fff;--ink:#161616;--mute:#666;--line:#e3e3e3;--green:#1f8a4c;--red:#c4372b;--grey:#8a8a8a;--blue:#2f6fd0}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--card:#1e1e1e;--ink:#ededed;--mute:#a0a0a0;--line:#333;--grey:#777}}
body{margin:0;background:var(--bg);color:var(--ink);font:18px/1.45 system-ui,-apple-system,sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px}h1{font-size:26px;margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:12px}
.row{display:flex;justify-content:space-between;align-items:center;gap:12px}.name{font-weight:600}
.tag{font-size:15px;padding:2px 10px;border-radius:99px;color:#fff;white-space:nowrap}
.on{background:var(--green)}.off{background:var(--grey)}.down{background:var(--red)}
.detail{color:var(--mute);font-size:16px;margin-top:4px}
button{font:inherit;font-size:16px;padding:6px 14px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink);cursor:pointer}
button:disabled{opacity:.4;cursor:default}.actions{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
a{color:var(--blue)}img{max-width:100%;border-radius:8px;border:1px solid var(--line);background:#fff;margin-top:8px}
.log{list-style:none;margin:0;padding:0}.log li{padding:8px 0;border-top:1px solid var(--line)}.log li:first-child{border-top:0}
.src{font-size:14px;padding:1px 8px;border-radius:99px;margin-right:8px;border:1px solid}
.Agent{color:var(--blue);border-color:var(--blue)}.Fontra{color:var(--green);border-color:var(--green)}
.time{color:var(--mute);font-size:15px;font-variant-numeric:tabular-nums}
</style></head><body><main><h1>Fontra live</h1>
<div class="card"><div class="row"><span class="name" id="linkName">Agent link</span><span id="linkTag" class="tag off">…</span></div>
<div class="detail" id="linkDetail"></div><div class="actions"><button id="linkBtn"></button></div></div>
<div class="card"><div class="row"><span class="name">Fontra server</span><span id="fTag" class="tag off">…</span></div>
<div class="detail" id="fDetail"></div><div class="actions"><button id="fStart">Start</button><button id="fStop">Stop</button></div></div>
<div class="card"><div class="row"><span class="name">MCP server</span><span class="tag on" id="mTag">Running</span></div>
<div class="detail" id="mDetail"></div></div>
<div class="card"><div class="row"><span class="name">Activity</span><span class="detail"><span class="src Agent">Agent</span><span class="src Fontra">You in Fontra</span></span></div><ul class="log" id="log"></ul></div>
<div class="card" id="imgCard" hidden><div class="row"><span class="name">Last image</span><span class="time" id="imgName"></span></div><img id="img" alt="Last image the agent drew"></div>
</main><script>
const $=id=>document.getElementById(id);let S=null,imgStamp="";
async function post(p,b){await fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})});tick()}
$("linkBtn").onclick=()=>post("/api/link",{on:!S.link});$("fStart").onclick=()=>post("/api/fontra/start");$("fStop").onclick=()=>post("/api/fontra/stop");
function tag(el,cls,text){el.className="tag "+cls;el.textContent=text}
async function tick(){let r;try{r=await fetch("/api/status");S=await r.json()}catch(e){tag($("mTag"),"down","Not running");$("mDetail").textContent="This page stops when the MCP server stops. Start it from your agent's MCP settings.";return}
tag($("mTag"),"on","Running");$("mDetail").textContent=`Process ${S.pid} for ${S.client}, running since ${S.since}. The agent starts and stops it.`;$("linkName").textContent=`${S.client} link`;
tag($("linkTag"),S.link?(S.connected?"on":"off"):"off",S.link?(S.connected?"Connected":"On, idle"):"Off");
$("linkDetail").textContent=S.link?(S.connected?`${S.client} is a second client on ${S.project}.`:`${S.client} connects on its next tool call.`):"The agent's tools refuse to read or edit until you turn the link on.";
$("linkBtn").textContent=S.link?"Turn off":"Turn on";
tag($("fTag"),S.fontra_up?"on":"down",S.fontra_up?"Running":"Stopped");
$("fDetail").innerHTML=S.fontra_up?`<a href="${S.editor}" target="_blank">Open ${S.project} in Fontra</a> · ${S.fontra}`:(S.serve_dir?`Start serves ${S.serve_dir}.`:"Started outside this server.");
$("fStart").disabled=S.fontra_up||!S.serve_dir;$("fStop").disabled=!S.fontra_up;
if(S.image){$("imgCard").hidden=false;$("imgName").textContent=S.image;const st=S.image+S.image_mtime;if(st!==imgStamp){imgStamp=st;$("img").src="/img/"+encodeURIComponent(S.image)+"?t="+S.image_mtime}}
$("log").innerHTML=S.log.map(e=>`<li><div class="row"><span><span class="src ${e.source=="Fontra"?"Fontra":"Agent"}">${e.source=="Fontra"?"You in Fontra":esc(S.client)}</span>${esc(e.title)}</span><span class="time">${e.t}</span></div>${e.summary?`<div class="detail">${esc(e.summary)}</div>`:""}</li>`).join("")||'<li class="detail">Nothing yet.</li>'}
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
tick();setInterval(tick,1000);
</script></body></html>"""


def fontra_pid():
    """PID of the fontra process listening on Fontra's port, or None (only processes whose command line is fontra's)."""
    port = urlparse(F.url).port
    out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split()
    for pid in out:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", pid], capture_output=True, text=True).stdout
        if "fontra" in cmd:
            return int(pid)
    return None


def local(url):
    return urlparse(url).hostname in ("127.0.0.1", "localhost")


@web.middleware
async def same_origin(request, handler):
    """Only local pages (this one, and Fontra's MCP panel) may press the buttons; CORS lets the panel read and post."""
    origin = request.headers.get("Origin")
    if origin and not local(origin):
        if request.method != "GET":
            raise web.HTTPForbidden()
        return await handler(request)
    resp = web.Response() if request.method == "OPTIONS" else await handler(request)
    if origin:
        resp.headers.update({"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Headers": "content-type", "Access-Control-Allow-Methods": "GET, POST"})
    return resp


async def ui_status(request):
    img = STATE["image"]
    mtime = os.path.getmtime(os.path.join(OUT, img)) if img and os.path.exists(os.path.join(OUT, img)) else 0
    return web.json_response(dict(client=STATE["client"], pid=os.getpid(), since=time.strftime("%H:%M", time.localtime(STATE["since"])), link=STATE["link"], connected=F.connected,
                                  fontra=F.url, fontra_up=await F.up(), project=PROJECT, serve_dir=SERVE_DIR,
                                  editor=f"{F.url}/editor.html?project={quote(PROJECT)}", log=list(STATE["log"]), image=img, image_mtime=mtime))


async def ui_link(request):
    on = bool((await request.json()).get("on"))
    STATE["link"] = on
    log("Fontra", "You turned the link " + ("on" if on else "off"))
    if not on:
        await F.disconnect()
    return web.json_response({"link": on})


async def ui_start(request):
    STATE["stopped_by_user"] = False
    if SERVE_DIR and not await F.up():
        await F.serve()
    return web.json_response({"ok": True})


async def ui_stop(request):
    pid = fontra_pid()
    if pid:
        STATE["stopped_by_user"] = True
        await F.disconnect()
        os.kill(pid, signal.SIGTERM)
        log("Fontra", "You stopped Fontra")
    return web.json_response({"ok": bool(pid)})


async def ui_target(request):
    """Point the agent at another Fontra (the panel sends its own origin and project)."""
    global PROJECT
    d = await request.json()
    url, project = d.get("url", "").rstrip("/"), d.get("project")
    if not local(url) or not project:
        raise web.HTTPBadRequest(text="url must be a localhost Fontra and project is required")
    await F.disconnect()
    F.url, F.project, PROJECT = url, project, project
    F.snap.clear()
    F.events.clear()
    log("Fontra", "You pointed the agent at this Fontra", f"{url} {project}")
    return web.json_response({"fontra": url, "project": project})


async def ui_img(request):
    name = os.path.basename(request.match_info["name"])
    path = os.path.join(OUT, name)
    if not name.endswith(".png") or not os.path.exists(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def start_ui():
    app = web.Application(middlewares=[same_origin])
    app.add_routes([web.get("/", lambda r: web.Response(text=PAGE, content_type="text/html")), web.get("/api/status", ui_status),
                    web.post("/api/link", ui_link), web.post("/api/fontra/start", ui_start), web.post("/api/fontra/stop", ui_stop),
                    web.post("/api/target", ui_target), web.get("/img/{name}", ui_img)])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    first = int(os.environ.get("FONTRA_LIVE_UI_PORT", "8780"))
    for port in range(first, first + 6):     # one page per running MCP server (one per agent session)
        try:
            await web.TCPSite(runner, "127.0.0.1", port).start()
            STATE["ui"] = f"http://127.0.0.1:{port}"
            return
        except OSError:
            continue
    print("fontra-live: no free port for the status page", file=sys.stderr)


# ---- MCP stdio ----------------------------------------------------------------

async def handle(req):
    m, p = req.get("method"), req.get("params") or {}
    if m == "initialize":
        STATE["client"] = (p.get("clientInfo") or {}).get("name") or "Agent"
        return {"protocolVersion": p.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                "serverInfo": {"name": "fontra-live", "version": "0.2"}}
    if m == "tools/list":
        return {"tools": [{"name": n, "description": desc, "inputSchema": {"type": "object", "properties": props, "required": req_}}
                          for n, (_, desc, props, req_) in TOOLS.items()]}
    if m == "tools/call":
        fn = TOOLS[p["name"]][0]
        try:
            out = await fn(**(p.get("arguments") or {}))
            content = out if isinstance(out, list) else [{"type": "text", "text": json.dumps(out)}]
            return {"content": content}
        except Exception as e:  # tool errors go back to the model, not the transport
            return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
    if m == "ping":
        return {}
    raise ValueError(f"unknown method {m}")


async def main():
    loop = asyncio.get_running_loop()
    await start_ui()
    reader = asyncio.StreamReader(limit=2 ** 26)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

    async def respond(req):
        try:
            msg = {"jsonrpc": "2.0", "id": req["id"], "result": await handle(req)}
        except Exception as e:
            msg = {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32603, "message": str(e)}}
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()

    while line := await reader.readline():
        req = json.loads(line)
        if "id" in req:       # notifications (no id) need no answer
            asyncio.create_task(respond(req))
    if F.session:
        await F.session.close()


def selftest():
    cons = [[[0, 0, "on"], [10, 0, "on"], [10, 5, "off"], [5, 10, "off"], [0, 10, "smooth"]]]
    assert unpack(pack(cons)) == cons
    # a square n: stems 0-80 and 300-380, top bar 420-500
    n = [[[0, 0, "on"], [80, 0, "on"], [80, 420, "on"], [300, 420, "on"], [300, 0, "on"], [380, 0, "on"], [380, 500, "on"], [0, 500, "on"]]]
    a = n_anatomy(gc.polygons(n, 0.5), 500)
    assert abs(a["stem_left"] - 80) <= 1 and abs(a["stem_right"] - 80) <= 1, a
    assert abs(a["arch_top"] - 80) <= 1 and abs(a["counter_width"] - 220) <= 1, a
    assert glyphs_in({"p": ["glyphs", "n", "layers"], "f": "="}) == ["n"]
    assert glyphs_in({"c": [{"p": ["glyphs", "a"]}, {"p": ["glyphs", "b"]}]}) == ["a", "b"]
    gc.selftest()


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else asyncio.run(main())
