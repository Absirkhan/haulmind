/* Haulmind vehicle glyphs: the same top-down line drawings as icons.svg (veh-*), as Path2D
   data for Canvas. One unified style: 1.4px outline, 28x12 box, nose pointing +x. */
(function () {
  "use strict";
  const SHAPES = {
    semi: ["M0 1h18a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H0a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1z", "M19.5 2.5h6.5a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2h-6.5z", "M24.5 2.5v7"],
    daycab: ["M7 1.5h11a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1z", "M19.5 2.5h6.5a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2h-6.5z", "M24.5 2.5v7"],
    box: ["M3 1.5h16a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1z", "M20.5 3h5a2 2 0 0 1 2 2v2a2 2 0 0 1-2 2h-5z"],
    van: ["M6 2.5h15a4 4 0 0 1 4 3.5 4 4 0 0 1-4 3.5H6z", "M18 2.5v7"],
    pickup: ["M7 2.5h9v7H7z", "M17 2h6a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2h-6z"],
  };
  const cache = {};
  function paths(kind) {
    if (!cache[kind]) cache[kind] = (SHAPES[kind] || SHAPES.semi).map((d) => new Path2D(d));
    return cache[kind];
  }
  /* Draw a vehicle centred at (x, y), heading in radians, scale s (1 = 28px long). */
  function draw(ctx, kind, x, y, heading, s, stroke, fill) {
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(heading);
    ctx.scale(s, s);
    ctx.translate(-14, -6);
    ctx.lineWidth = 1.4;
    ctx.lineJoin = "round";
    ctx.strokeStyle = stroke;
    ctx.fillStyle = fill;
    const ps = paths(kind);
    ps.forEach((p, i) => { if (i < 2) ctx.fill(p); ctx.stroke(p); });
    ctx.restore();
  }
  window.HaulmindVehicles = { SHAPES, draw, kinds: Object.keys(SHAPES) };
})();
