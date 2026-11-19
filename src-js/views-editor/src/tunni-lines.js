// Tunni lines for the glyph being edited.
//
// Tunni lines (after Eduardo Tunni, as in FontLab): for a cubic segment p0 p1 p2 p3 the two
// handle lines meet at the Tunni point T. Each handle's length over its node's distance to T
// is its percentage. The dotted line joins the two handles; dragging it changes both handle
// percentages by the same amount, so the line follows the pointer, and holding Alt changes only
// the handle nearer where the drag started. Handles keep their directions, so smooth nodes stay
// smooth. The control dot sits at 2 * mid(p1, p2) - mid(p0, p3), which is T when both handles
// are at 50 %. While a line is hovered, a target line from the nodes' midpoint through T, a tick
// at T and the ideal ring (both handles at 55.2 %, a circular arc's tension) are drawn; only the
// hovered segment shows its line, dot and percentages.

import { recordChanges } from "@fontra/core/change-recorder.js";
import { ChangeCollector } from "@fontra/core/changes.js";
import { changedPointIndices, enforceSmartNodes } from "./smart-nodes.js";
import {
  glyphSelector,
  registerVisualizationLayerDefinition,
} from "./visualization-layer-definitions.js";

export const TUNNI_LAYER = "fontra.tunni.lines";

const MIN_FRACTION = 0.02;
const MAX_FRACTION = 1.6;

function sub(a, b) {
  return { x: a.x - b.x, y: a.y - b.y };
}
function add(a, b) {
  return { x: a.x + b.x, y: a.y + b.y };
}
function mul(a, s) {
  return { x: a.x * s, y: a.y * s };
}
function mid(a, b) {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}
function dot(a, b) {
  return a.x * b.x + a.y * b.y;
}
function len(a) {
  return Math.hypot(a.x, a.y);
}

function pointAt(path, index) {
  const [x, y] = path.getPointPosition(index);
  return { x, y };
}

// Geometry of one cubic segment, or null when its handle lines do not meet ahead of both nodes.
export function tunniInfo(p0, p1, p2, p3) {
  const d1 = sub(p1, p0);
  const d2 = sub(p2, p3);
  const det = d1.x * d2.y - d1.y * d2.x;
  if (Math.abs(det) < 1e-9 || len(d1) < 1e-6 || len(d2) < 1e-6) {
    return null;
  }
  const w = sub(p3, p0);
  const s = (w.x * d2.y - w.y * d2.x) / det; // T = p0 + s * d1
  const u = (w.x * d1.y - w.y * d1.x) / det; // T = p3 + u * d2
  if (s <= 0 || u <= 0) {
    return null;
  }
  const T = add(p0, mul(d1, s));
  return {
    T,
    f1: 1 / s,
    f2: 1 / u,
    control: sub(mul(mid(p1, p2), 2), mid(p0, p3)),
  };
}

// Every cubic segment of a path, with its point indices and Tunni geometry.
export function* iterTunniSegments(path) {
  for (let ci = 0; ci < path.numContours; ci++) {
    for (const seg of path.iterContourSegmentPointIndices(ci)) {
      if (seg.type !== "cubic" || seg.pointIndices.length !== 4) {
        continue;
      }
      const [i0, i1, i2, i3] = seg.pointIndices;
      const p = [i0, i1, i2, i3].map((i) => pointAt(path, i));
      const info = tunniInfo(...p);
      if (info) {
        yield { indices: [i0, i1, i2, i3], points: p, ...info };
      }
    }
  }
}

function distanceToSegment(pt, a, b) {
  const ab = sub(b, a);
  const t = Math.max(0, Math.min(1, dot(sub(pt, a), ab) / Math.max(dot(ab, ab), 1e-9)));
  return len(sub(pt, add(a, mul(ab, t))));
}

// The tension of a circular arc's handles (4/3 * tan(pi/8)), drawn as the "ideal" ring and
// snapped to when the control dot is dragged near it.
export const IDEAL_TENSION = 0.5523;

// Where the control dot would be with both handles at the ideal tension.
function idealControl(seg) {
  const [p0, , , p3] = seg.points;
  const m0 = mid(p0, p3);
  return add(m0, mul(add(sub(seg.T, p0), sub(seg.T, p3)), IDEAL_TENSION));
}

// The Tunni line or its control dot under the pointer, in glyph coordinates. part is "control"
// when the dot is grabbed (moves the two handles independently) and "line" for the dotted line.
export function tunniHitTest(path, point, margin) {
  let best = null;
  for (const seg of iterTunniSegments(path)) {
    const [, p1, p2] = seg.points;
    const dControl = len(sub(point, seg.control));
    const dLine = distanceToSegment(point, p1, p2);
    const part = dControl <= margin ? "control" : "line";
    const d = part === "control" ? dControl : dLine;
    if (d <= margin && (!best || d < best.distance)) {
      best = {
        ...seg,
        part,
        margin,
        distance: d,
        nearer: len(sub(point, p1)) <= len(sub(point, p2)) ? 1 : 2,
      };
    }
  }
  return best;
}

// Handles for a given control dot position: C = mid(p0, p3) + f1 (T - p0) + f2 (T - p3), so the
// dot fixes both handle fractions; along the diagonal both change together, across it they part.
function handlesFromControl(path, indices, control, snapMargin) {
  const [i0, i1, i2, i3] = indices;
  const p0 = pointAt(path, i0);
  const p3 = pointAt(path, i3);
  const info = tunniInfo(p0, pointAt(path, i1), pointAt(path, i2), p3);
  if (!info) {
    return null;
  }
  const a = sub(info.T, p0);
  const b = sub(info.T, p3);
  let C = control;
  const ideal = idealControl({ points: [p0, null, null, p3], T: info.T });
  if (snapMargin && len(sub(C, ideal)) <= snapMargin) {
    C = ideal;
  }
  const r = sub(C, mid(p0, p3));
  const det = a.x * b.y - a.y * b.x;
  if (Math.abs(det) < 1e-9) {
    return null;
  }
  let f1 = (r.x * b.y - r.y * b.x) / det;
  let f2 = (a.x * r.y - a.y * r.x) / det;
  f1 = Math.max(MIN_FRACTION, Math.min(MAX_FRACTION, f1));
  f2 = Math.max(MIN_FRACTION, Math.min(MAX_FRACTION, f2));
  return [add(p0, mul(a, f1)), add(p3, mul(b, f2))];
}

// A segment pinned by a click keeps its Tunni line shown after the pointer leaves it; keyed by glyph and the segment's first handle.
export function tunniPinKey(glyphName, seg) {
  return `${glyphName}/${seg.indices[1]}`;
}

export function toggleTunniPin(sceneModel, glyphName, seg) {
  sceneModel.pinnedTunni ??= new Set();
  const key = tunniPinKey(glyphName, seg);
  if (!sceneModel.pinnedTunni.delete(key)) {
    sceneModel.pinnedTunni.add(key);
  }
}

export function tunniLayerOn(editor) {
  return !!editor.visualizationLayersSettings.model[TUNNI_LAYER];
}

// New handle positions for a segment of `path` after dragging by `delta` (glyph units).
function draggedHandles(path, indices, delta, asymmetricHandle) {
  const [i0, i1, i2, i3] = indices;
  const p0 = pointAt(path, i0);
  const p1 = pointAt(path, i1);
  const p2 = pointAt(path, i2);
  const p3 = pointAt(path, i3);
  const info = tunniInfo(p0, p1, p2, p3);
  if (!info) {
    return null;
  }
  const { T } = info;
  let f1 = info.f1;
  let f2 = info.f2;
  if (asymmetricHandle) {
    // only one handle moves: the line's midpoint moves by d * (T - p_node) / 2
    const a = sub(T, asymmetricHandle === 1 ? p0 : p3);
    const d = (2 * dot(delta, a)) / Math.max(dot(a, a), 1e-9);
    if (asymmetricHandle === 1) {
      f1 += d;
    } else {
      f2 += d;
    }
  } else {
    // both move by the same fraction: the midpoint moves by d * (T - mid(p0, p3))
    const w = sub(T, mid(p0, p3));
    const d = dot(delta, w) / Math.max(dot(w, w), 1e-9);
    f1 += d;
    f2 += d;
  }
  f1 = Math.max(MIN_FRACTION, Math.min(MAX_FRACTION, f1));
  f2 = Math.max(MIN_FRACTION, Math.min(MAX_FRACTION, f2));
  return [add(p0, mul(sub(T, p0), f1)), add(p3, mul(sub(T, p3), f2))];
}

// Drag a Tunni line: edits every editing layer, sends incremental changes, records one undo step.
export async function handleTunniDrag(tool, eventStream, initialEvent, hit) {
  const sceneController = tool.sceneController;
  // Alt is read on every move, so pressing or releasing it mid-drag switches between moving
  // both handles and moving the nearer one; each move is computed from the drag's start.
  let asymmetric = initialEvent.altKey ? hit.nearer : 0;
  await sceneController.editGlyph(async (sendIncrementalChange, glyph) => {
    const initialPoint = sceneController.selectedGlyphPoint(initialEvent);
    const layers = Object.entries(
      sceneController.getEditingLayerFromGlyphLayers(glyph.layers)
    ).map(([layerName, layerGlyph]) => ({
      layerGlyph,
      changePath: ["layers", layerName, "glyph"],
      initial: [hit.indices[1], hit.indices[2]].map((i) =>
        layerGlyph.path.getPointPosition(i)
      ),
      initialPath: layerGlyph.path.copy(),
    }));
    let finalTargets = null;
    for await (const event of eventStream) {
      const point = sceneController.selectedGlyphPoint(event);
      if (!point) {
        continue;
      }
      const delta = sub(point, initialPoint);
      asymmetric = event.altKey ? hit.nearer : 0;
      const changes = [];
      finalTargets = [];
      for (const layer of layers) {
        const target =
          hit.part === "control"
            ? handlesFromControl(
                layer.initialPath,
                hit.indices,
                add(hit.control, delta),
                hit.margin * 0.6
              )
            : draggedHandles(layer.initialPath, hit.indices, delta, asymmetric);
        finalTargets.push(target);
        if (!target) {
          continue;
        }
        const ch = recordChanges(layer.layerGlyph, (g) => {
          g.path.setPointPosition(hit.indices[1], target[0].x, target[0].y);
          g.path.setPointPosition(hit.indices[2], target[1].x, target[1].y);
          enforceSmartNodes(g.path, layer.initialPath); // a genius node beside the handles re-solves
        });
        changes.push(ch.prefixed(layer.changePath));
      }
      if (changes.length) {
        await sendIncrementalChange(changes.reduce((a, b) => a.concat(b)).change, true);
      }
    }
    if (!finalTargets) {
      return; // a click without a drag
    }
    // one undoable change from the initial points to the final ones (handles, and any node a
    // smart-node rule moved)
    let forward = null;
    for (const layer of layers) {
      const touched = changedPointIndices(layer.layerGlyph.path, layer.initialPath);
      const finals = touched.map((i) => layer.layerGlyph.path.getPointPosition(i));
      recordChanges(layer.layerGlyph, (g) =>
        touched.forEach((i) =>
          g.path.setPointPosition(i, ...layer.initialPath.getPointPosition(i))
        )
      );
      const ch = recordChanges(layer.layerGlyph, (g) =>
        touched.forEach((i, k) => g.path.setPointPosition(i, ...finals[k]))
      ).prefixed(layer.changePath);
      forward = forward ? forward.concat(ch) : ch;
    }
    if (!forward || !forward.hasChange) {
      return; // nothing moved: an empty undo record fails Fontra's glyph-name check
    }
    return {
      undoLabel:
        hit.part === "control"
          ? "Drag Tunni control"
          : asymmetric
            ? "Drag Tunni line (one handle)"
            : "Drag Tunni line",
      changes: forward || new ChangeCollector(),
      broadcast: true,
    };
  });
}

function strokeDashed(context, a, b, dash) {
  context.save();
  context.setLineDash(dash);
  context.beginPath();
  context.moveTo(a.x, a.y);
  context.lineTo(b.x, b.y);
  context.stroke();
  context.restore();
}

registerVisualizationLayerDefinition({
  identifier: TUNNI_LAYER,
  name: "Tunni lines",
  selectionFunc: glyphSelector("editing"),
  userSwitchable: true,
  defaultOn: true,
  zIndex: 490,
  screenParameters: {
    strokeWidth: 1.5,
    hoverWidth: 3,
    dotSize: 7,
    hoverDotSize: 10,
    guideWidth: 1,
    tickSize: 12,
    idealSize: 9,
    fontSize: 10,
  },
  colors: {
    color: "#4A8AF4",
    guide: "#4A8AF488",
    labelBox: "#FFFD",
    labelText: "#3A6FC4",
  },
  colorsDarkMode: {
    color: "#6FA3FF",
    guide: "#6FA3FF88",
    labelBox: "#111D",
    labelText: "#9CC0FF",
  },
  draw: ({ context, positionedGlyph, parameters, model }) => {
    const path = positionedGlyph.glyph.path;
    const hover = model.hoverTunni;
    const dash = [parameters.strokeWidth * 2, parameters.strokeWidth * 2];
    context.strokeStyle = parameters.color;
    context.fillStyle = parameters.color;
    const labels = [];
    for (const seg of iterTunniSegments(path)) {
      const [p0, p1, p2, p3] = seg.points;
      const hovered = hover && hover.indices[1] === seg.indices[1];
      const pinned = model.pinnedTunni?.has(
        tunniPinKey(positionedGlyph.glyphName, seg)
      );
      if (!hovered && !pinned) {
        continue; // only the segment under the pointer, or one pinned by a click, shows its Tunni line, dot and labels
      }
      if (hovered || pinned) {
        // guides for the line in hand: the target line from the nodes' midpoint through the Tunni
        // point (fixed by the nodes and handle directions, so it never turns while dragging), a
        // tick across it at that point, and the ideal ring (both handles at 55.2 %, on the line)
        const curveMid = mid(p0, p3);
        const ideal = idealControl(seg);
        const axis = sub(seg.T, curveMid);
        const axisLen = Math.max(len(axis), 1e-9);
        const u = mul(axis, 1 / axisLen);
        const reach = Math.max(
          axisLen,
          dot(sub(seg.control, curveMid), u),
          dot(sub(ideal, curveMid), u)
        );
        context.save();
        context.strokeStyle = parameters.guide;
        context.lineWidth = parameters.guideWidth;
        strokeDashed(context, curveMid, add(curveMid, mul(u, reach * 1.15)), [2, 3]);
        const n = { x: -u.y, y: u.x };
        const t = parameters.tickSize / 2;
        context.beginPath();
        context.moveTo(seg.T.x - n.x * t, seg.T.y - n.y * t);
        context.lineTo(seg.T.x + n.x * t, seg.T.y + n.y * t);
        context.stroke();
        context.beginPath();
        context.arc(ideal.x, ideal.y, parameters.idealSize / 2, 0, 2 * Math.PI);
        context.stroke();
        context.restore();
      }
      context.lineWidth =
        hovered || pinned ? parameters.hoverWidth : parameters.strokeWidth;
      strokeDashed(context, p1, p2, dash);
      const r =
        (hovered && hover.part === "control"
          ? parameters.hoverDotSize
          : parameters.dotSize) / 2;
      context.beginPath();
      context.arc(seg.control.x, seg.control.y, r, 0, 2 * Math.PI);
      context.fill();
      labels.push([mid(p0, p1), seg.f1], [mid(p3, p2), seg.f2]);
    }
    // percentages on the handles, upright text on the flipped canvas
    const fs = parameters.fontSize;
    context.font = `${fs}px fontra-ui-regular, sans-serif`;
    context.textAlign = "center";
    context.textBaseline = "middle";
    context.scale(1, -1);
    for (const [pt, f] of labels) {
      const text = `${Math.round(f * 100)}%`;
      const w = context.measureText(text).width + fs * 0.6;
      context.fillStyle = parameters.labelBox;
      context.fillRect(pt.x - w / 2, -pt.y - fs * 0.75, w, fs * 1.5);
      context.fillStyle = parameters.labelText;
      context.fillText(text, pt.x, -pt.y);
    }
  },
});
