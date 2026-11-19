// Smart nodes, after FontLab: Genius (G2) and X/Y servant nodes.
//
// The kind is stored in the on-curve point's `name` attribute, which round-trips through the UFO
// point pen: "genius", "xservant", "yservant" or "xyservant".
//
// Genius: the node sits on the line between its two handles at the point where the curvature on
// both sides is equal (G2). With the handles fixed, a cubic's end curvature is
// k = (2/3) h / a^2, where a is the handle length and h the distance of the segment's far handle
// from the tangent line; equal curvature gives a_in / a_out = sqrt(h_in / h_out) = r, so the node
// divides the handle line at s = r / (1 + r) from the incoming handle (checked numerically: the
// two end curvatures agree to 1e-13 over 200 random curves). When the
// two far handles lie on opposite sides of the tangent line (an inflection) no G2 position exists
// and the node is left alone.
//
// Servants: an X-servant keeps the ratio its x had, at the start of the edit, between the previous
// and next non-servant on-curve nodes of its contour; a Y-servant does the same in y. Its handles
// move with it. A servant moved directly by the user is not overridden.

import { VarPackedPath } from "@fontra/core/var-path.js";
import {
  glyphSelector,
  registerVisualizationLayerDefinition,
} from "./visualization-layer-definitions.js";

export const SMART_KINDS = ["genius", "xservant", "yservant", "xyservant"];

export function nodeKind(path, index) {
  const name = path.pointAttributes?.[index]?.name;
  return SMART_KINDS.includes(name) ? name : null;
}

export function hasSmartNodes(path) {
  return !!path.pointAttributes?.some((a) => a && SMART_KINDS.includes(a.name));
}

function isOnCurve(path, index) {
  return (
    (path.pointTypes[index] & VarPackedPath.POINT_TYPE_MASK) === VarPackedPath.ON_CURVE
  );
}

function isCubicOff(path, index) {
  return (
    (path.pointTypes[index] & VarPackedPath.POINT_TYPE_MASK) ===
    VarPackedPath.OFF_CURVE_CUBIC
  );
}

function pos(path, index) {
  const [x, y] = path.getPointPosition(index);
  return { x, y };
}

// Absolute indices of a contour's points, and a cyclic neighbour lookup.
function contourIndices(path, contourIndex) {
  const n = path.getNumPointsOfContour(contourIndex);
  const indices = [];
  for (let k = 0; k < n; k++) {
    indices.push(path.getAbsolutePointIndex(contourIndex, k));
  }
  return { indices, closed: path.contourInfo[contourIndex].isClosed };
}

function neighbour(ci, k, step) {
  const n = ci.indices.length;
  const j = k + step;
  if (ci.closed) {
    return ci.indices[((j % n) + n) % n];
  }
  return j >= 0 && j < n ? ci.indices[j] : undefined;
}

function moveNodeWithHandles(path, ci, k, dx, dy) {
  if (Math.abs(dx) < 1e-9 && Math.abs(dy) < 1e-9) {
    return [];
  }
  const touched = [];
  for (const step of [0, -1, 1]) {
    const index = step === 0 ? ci.indices[k] : neighbour(ci, k, step);
    if (index === undefined || (step !== 0 && isOnCurve(path, index))) {
      continue;
    }
    const p = pos(path, index);
    path.setPointPosition(index, p.x + dx, p.y + dy);
    touched.push(index);
  }
  return touched;
}

function enforceServants(path, initialPath, moved) {
  const touched = [];
  for (let c = 0; c < path.numContours; c++) {
    const ci = contourIndices(path, c);
    const ons = ci.indices
      .map((index, k) => ({ index, k }))
      .filter(({ index }) => isOnCurve(path, index));
    const isServant = (index, axis) => {
      const kind = nodeKind(path, index);
      return kind === "xyservant" || kind === axis + "servant";
    };
    for (let m = 0; m < ons.length; m++) {
      const { index, k } = ons[m];
      if (moved.has(index)) {
        continue;
      }
      for (const axis of ["x", "y"]) {
        if (!isServant(index, axis)) {
          continue;
        }
        // previous and next non-servant (on this axis) on-curve nodes
        const find = (dir) => {
          for (let s = 1; s < ons.length; s++) {
            let mm = m + dir * s;
            if (ci.closed) {
              mm = ((mm % ons.length) + ons.length) % ons.length;
            } else if (mm < 0 || mm >= ons.length) {
              return undefined;
            }
            if (!isServant(ons[mm].index, axis)) {
              return ons[mm].index;
            }
          }
          return undefined;
        };
        const a = find(-1);
        const b = find(1);
        if (a === undefined || b === undefined || a === b) {
          continue;
        }
        const v0 = pos(initialPath, index)[axis];
        const a0 = pos(initialPath, a)[axis];
        const b0 = pos(initialPath, b)[axis];
        const a1 = pos(path, a)[axis];
        const b1 = pos(path, b)[axis];
        let target;
        if (Math.abs(b0 - a0) > 1e-6) {
          target = a1 + ((v0 - a0) / (b0 - a0)) * (b1 - a1);
        } else {
          target = v0 + (a1 - a0); // neighbours level on this axis: keep the offset from them
        }
        const cur = pos(path, index)[axis];
        touched.push(
          ...moveNodeWithHandles(
            path,
            ci,
            k,
            axis === "x" ? target - cur : 0,
            axis === "y" ? target - cur : 0
          )
        );
      }
    }
  }
  return touched;
}

function enforceGenius(path) {
  const touched = [];
  for (let c = 0; c < path.numContours; c++) {
    const ci = contourIndices(path, c);
    ci.indices.forEach((index, k) => {
      if (nodeKind(path, index) !== "genius" || !isOnCurve(path, index)) {
        return;
      }
      const hp = neighbour(ci, k, -1);
      const hn = neighbour(ci, k, 1);
      const qp = neighbour(ci, k, -2);
      const qn = neighbour(ci, k, 2);
      if ([hp, hn, qp, qn].some((i) => i === undefined || !isCubicOff(path, i))) {
        return; // needs a cubic segment with two handles on each side
      }
      const A = pos(path, hp);
      const B = pos(path, hn);
      const d = { x: B.x - A.x, y: B.y - A.y };
      const L = Math.hypot(d.x, d.y);
      if (L < 1e-6) {
        return;
      }
      const side = (p) => ((p.x - A.x) * d.y - (p.y - A.y) * d.x) / L; // signed distance from the handle line
      const hIn = side(pos(path, qp));
      const hOut = side(pos(path, qn));
      if (hIn * hOut <= 0) {
        return; // inflection or flat: no G2 position on this line
      }
      const r = Math.sqrt(Math.abs(hIn) / Math.abs(hOut));
      const s = r / (1 + r);
      const target = { x: A.x + d.x * s, y: A.y + d.y * s };
      const cur = pos(path, index);
      if (Math.hypot(target.x - cur.x, target.y - cur.y) > 1e-6) {
        path.setPointPosition(index, target.x, target.y);
        touched.push(index);
      }
    });
  }
  return touched;
}

// Apply every smart-node rule to `path` in place; returns the indices it moved.
export function enforceSmartNodes(path, initialPath, moved = new Set()) {
  if (!hasSmartNodes(path)) {
    return [];
  }
  return [...enforceServants(path, initialPath, moved), ...enforceGenius(path)];
}

// Indices whose position differs between two paths of the same structure.
export function changedPointIndices(pathA, pathB) {
  const out = [];
  const n = Math.min(pathA.numPoints, pathB.numPoints);
  for (let i = 0; i < n; i++) {
    const [ax, ay] = pathA.getPointPosition(i);
    const [bx, by] = pathB.getPointPosition(i);
    if (ax !== bx || ay !== by) {
      out.push(i);
    }
  }
  return out;
}

// Set (or clear, with kind = null) the smart kind of on-curve points, by replacing each point
// with a copy carrying the new `name` attribute: deletePoint and insertPoint are changes both the
// client and the server already know, so this is undoable and reaches the UFO.
export function setNodeKind(path, pointIndices, kind) {
  for (const index of pointIndices) {
    if (!isOnCurve(path, index)) {
      continue;
    }
    const [contourIndex, contourPointIndex] = path.getContourAndPointIndex(index);
    const point = path.getPoint(index);
    const attrs = { ...(point.attrs || {}) };
    if (kind) {
      attrs.name = kind;
    } else {
      delete attrs.name;
    }
    const newPoint = { ...point, attrs: Object.keys(attrs).length ? attrs : undefined };
    if (kind === "genius") {
      newPoint.smooth = true;
    }
    path.deletePoint(contourIndex, contourPointIndex);
    path.insertPoint(contourIndex, contourPointIndex, newPoint);
  }
}

// Markers for smart nodes: a ring around genius nodes (as in FontLab), and x, y or xy beside servants.
registerVisualizationLayerDefinition({
  identifier: "fontra.smart.nodes",
  name: "Smart nodes",
  selectionFunc: glyphSelector("editing"),
  zIndex: 510,
  screenParameters: { ringSize: 16, strokeWidth: 1.5, fontSize: 9 },
  colors: { color: "#2E9E6A" },
  colorsDarkMode: { color: "#5FD49A" },
  draw: ({ context, positionedGlyph, parameters }) => {
    const path = positionedGlyph.glyph.path;
    if (!path.pointAttributes) {
      return;
    }
    const marks = [];
    context.strokeStyle = parameters.color;
    context.lineWidth = parameters.strokeWidth;
    for (let i = 0; i < path.numPoints; i++) {
      const kind = nodeKind(path, i);
      if (!kind) {
        continue;
      }
      const [x, y] = path.getPointPosition(i);
      if (kind === "genius") {
        context.beginPath();
        context.arc(x, y, parameters.ringSize / 2, 0, 2 * Math.PI);
        context.stroke();
      } else {
        marks.push([x, y, kind === "xyservant" ? "xy" : kind[0]]);
      }
    }
    if (marks.length) {
      const fs = parameters.fontSize;
      context.font = `bold ${fs}px fontra-ui-regular, sans-serif`;
      context.fillStyle = parameters.color;
      context.textAlign = "left";
      context.textBaseline = "bottom";
      context.scale(1, -1);
      for (const [x, y, label] of marks) {
        context.fillText(label, x + fs * 0.6, -y - fs * 0.3);
      }
    }
  },
});
