// A curvature comb for the glyph being edited, as in FontLab: hairs along each cubic's normal, as
// long as the curvature times a fixed gain, with the envelope through their tips. A dent or a jump
// in curvature shows as a spike or a step in the envelope. Toggled in View > Glyph editor appearance.

import {
  glyphSelector,
  registerVisualizationLayerDefinition,
} from "./visualization-layer-definitions.js";

export const COMB_LAYER = "fontra.curvature.comb";

const COMB_GAIN = 2000; // font units of hair per unit of curvature (radius 250 -> 8 units)
const COMB_STEPS = 24;

function sub(a, b) {
  return { x: a.x - b.x, y: a.y - b.y };
}
function add(a, b) {
  return { x: a.x + b.x, y: a.y + b.y };
}
function mul(a, s) {
  return { x: a.x * s, y: a.y * s };
}

function pointAt(path, index) {
  const [x, y] = path.getPointPosition(index);
  return { x, y };
}

registerVisualizationLayerDefinition({
  identifier: COMB_LAYER,
  name: "Curvature comb",
  selectionFunc: glyphSelector("editing"),
  userSwitchable: true,
  defaultOn: true,
  zIndex: 480,
  screenParameters: { hairWidth: 0.5, envelopeWidth: 0.9 },
  colors: { hair: "#E0307020", envelope: "#E0307060" },
  colorsDarkMode: { hair: "#FF5C9A20", envelope: "#FF5C9A60" },
  draw: ({ context, positionedGlyph, parameters }) => {
    const path = positionedGlyph.glyph.path;
    for (let ci = 0; ci < path.numContours; ci++) {
      for (const seg of path.iterContourSegmentPointIndices(ci)) {
        if (seg.type !== "cubic" || seg.pointIndices.length !== 4) {
          continue;
        }
        const [p0, p1, p2, p3] = seg.pointIndices.map((i) => pointAt(path, i));
        const tips = [];
        context.strokeStyle = parameters.hair;
        context.lineWidth = parameters.hairWidth;
        context.beginPath();
        for (let k = 0; k <= COMB_STEPS; k++) {
          const t = k / COMB_STEPS;
          const mt = 1 - t;
          const pos = add(
            add(mul(p0, mt * mt * mt), mul(p1, 3 * mt * mt * t)),
            add(mul(p2, 3 * mt * t * t), mul(p3, t * t * t))
          );
          const d1 = add(
            add(mul(sub(p1, p0), 3 * mt * mt), mul(sub(p2, p1), 6 * mt * t)),
            mul(sub(p3, p2), 3 * t * t)
          );
          const d2 = add(
            mul(add(sub(p2, mul(p1, 2)), p0), 6 * mt),
            mul(add(sub(p3, mul(p2, 2)), p1), 6 * t)
          );
          const speed = Math.hypot(d1.x, d1.y);
          if (speed < 1e-9) {
            continue;
          }
          const curvature = (d1.x * d2.y - d1.y * d2.x) / (speed * speed * speed);
          const normal = { x: -d1.y / speed, y: d1.x / speed };
          const tip = sub(pos, mul(normal, curvature * COMB_GAIN));
          context.moveTo(pos.x, pos.y);
          context.lineTo(tip.x, tip.y);
          tips.push(tip);
        }
        context.stroke();
        if (tips.length > 1) {
          context.strokeStyle = parameters.envelope;
          context.lineWidth = parameters.envelopeWidth;
          context.beginPath();
          context.moveTo(tips[0].x, tips[0].y);
          for (const tip of tips.slice(1)) {
            context.lineTo(tip.x, tip.y);
          }
          context.stroke();
        }
      }
    }
  },
});
