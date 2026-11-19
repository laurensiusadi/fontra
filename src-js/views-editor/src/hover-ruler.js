// Hover ruler: while editing, the span under the pointer is measured across the nearest outline (perpendicular to it), or along the nearer axis (horizontal or vertical) while Shift is held, like FontLab's hover measurement. Toggled in View > Glyph editor appearance.
import {
  glyphSelector,
  registerVisualizationLayerDefinition,
} from "./visualization-layer-definitions.js";

export const HOVER_RULER_LAYER = "fontra.hover.ruler";

export function hoverRulerOn(editor) {
  return !!editor.visualizationLayersSettings.model[HOVER_RULER_LAYER];
}

// The stretch of the line through `point` along `dir` between the outline crossings on either side of it, and whether it is ink.
function span(hitTester, point, dir) {
  const hits = hitTester.rayIntersections(point, dir) || [];
  const along = hits
    .map((h) => ({ h, s: (h.x - point.x) * dir.x + (h.y - point.y) * dir.y }))
    .sort((a, b) => a.s - b.s);
  let before = null;
  let after = null;
  let winding = 0;
  for (const a of along) {
    if (a.s < 0) {
      before = a;
      winding += a.h.winding;
    } else if (!after) {
      after = a;
    }
  }
  if (!before || !after) {
    return null;
  }
  return { a: before.h, b: after.h, inside: !!winding };
}

function drawSpan(context, sp, parameters) {
  const { a, b } = sp;
  const d = Math.hypot(b.x - a.x, b.y - a.y);
  if (d < 0.1) {
    return;
  }
  context.save();
  context.strokeStyle = parameters.lineColor;
  context.lineWidth = parameters.strokeWidth;
  context.setLineDash([3, 3]);
  context.beginPath();
  context.moveTo(a.x, a.y);
  context.lineTo(b.x, b.y);
  context.stroke();
  context.setLineDash([]);
  context.fillStyle = parameters.lineColor;
  for (const p of [a, b]) {
    context.beginPath();
    context.arc(p.x, p.y, parameters.dotRadius, 0, 2 * Math.PI);
    context.fill();
  }
  // the label, upright on the flipped canvas and turned with the line (never upside down)
  const text = `${Math.round(d * 10) / 10}`;
  let angle = Math.atan2(b.y - a.y, b.x - a.x);
  if (angle > Math.PI / 2 || angle < -Math.PI / 2) {
    angle += Math.PI;
  }
  const fs = parameters.fontSize;
  context.translate((a.x + b.x) / 2, (a.y + b.y) / 2);
  context.scale(1, -1);
  context.rotate(-angle);
  context.font = `${fs}px fontra-ui-regular, sans-serif`;
  context.textAlign = "center";
  context.textBaseline = "middle";
  const w = context.measureText(text).width + fs;
  context.fillStyle = sp.inside ? parameters.insideBox : parameters.outsideBox;
  context.beginPath();
  context.roundRect(-w / 2, -fs * 0.75, w, fs * 1.5, fs * 0.4);
  context.fill();
  context.fillStyle = sp.inside ? parameters.insideText : parameters.outsideText;
  context.fillText(text, 0, 0);
  context.restore();
}

registerVisualizationLayerDefinition({
  identifier: HOVER_RULER_LAYER,
  name: "Hover ruler",
  selectionFunc: glyphSelector("editing"),
  userSwitchable: true,
  defaultOn: true,
  zIndex: 610,
  screenParameters: { strokeWidth: 1, dotRadius: 2.5, fontSize: 11 },
  colors: {
    lineColor: "#555A",
    insideBox: "#FFFE",
    insideText: "#333",
    outsideBox: "#555E",
    outsideText: "#FFF",
  },
  colorsDarkMode: {
    lineColor: "#CCCA",
    insideBox: "#333E",
    insideText: "#EEE",
    outsideBox: "#DDDE",
    outsideText: "#222",
  },
  draw: ({ context, positionedGlyph, parameters, model }) => {
    const point = model.hoverRulerPoint;
    const hitTester = positionedGlyph.glyph.flattenedPathHitTester;
    if (!point || !hitTester || model.hoverTunni) {
      return;
    }
    const nearest = hitTester.findNearest(point);
    if (nearest) {
      const dv = nearest.segment.bezier.derivative(nearest.t);
      const len = Math.hypot(dv.x, dv.y);
      if (len > 1e-9) {
        let normal = { x: -dv.y / len, y: dv.x / len };
        if (model.hoverRulerShift) {
          // Shift: snapped to the nearer axis, horizontal or vertical
          normal =
            Math.abs(normal.x) >= Math.abs(normal.y) ? { x: 1, y: 0 } : { x: 0, y: 1 };
        }
        const sp = span(hitTester, point, normal);
        if (sp?.inside) {
          // ink only: counters and the space around the glyph are not measured
          drawSpan(context, sp, parameters);
        }
      }
    }
  },
});
