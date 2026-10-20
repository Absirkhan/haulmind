/* Haulmind landing page: the living fleet map (Canvas), scroll-linked sequences, reveals,
   count-ups, the pause toggle and the transition into the app.
   Everything here animates transform/opacity or a Canvas; nothing runs while off-screen,
   hidden, paused, or under reduced motion. */
(function () {
  "use strict";
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const lerp = (a, b, t) => a + (b - a) * t;
  const root = document.documentElement;
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* ignore */ } },
  };
  const reducedQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  const hoverable = window.matchMedia("(hover: hover) and (pointer: fine)").matches;
  const mobile = window.matchMedia("(max-width: 700px)").matches;
  function reduced() { return reducedQuery.matches || root.classList.contains("reduced"); }
  function paused() { return root.classList.contains("anim-off"); }
  if (reducedQuery.matches) root.classList.add("reduced");
  reducedQuery.addEventListener("change", () => { root.classList.toggle("reduced", reducedQuery.matches); });

  /* seeded randomness so the scene is the same every visit */
  function mulberry32(seed) { let a = seed >>> 0; return function () { a |= 0; a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }

  /* ---------- theme + pause toggles ---------- */
  function applyTheme(t) {
    if (t === "light" || t === "dark") root.setAttribute("data-theme", t); else root.removeAttribute("data-theme");
    const dark = root.getAttribute("data-theme") === "dark" || (!root.getAttribute("data-theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
    $$("[data-theme-toggle]").forEach((b) => { b.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme"); const u = $("use", b); if (u) u.setAttribute("href", dark ? "/static/icons.svg#sun" : "/static/icons.svg#moon"); });
    if (window.fleetMap) window.fleetMap.recolor();
  }
  const urlTheme = (location.search.match(/[?&]theme=(dark|light)\b/) || [])[1] || null;
  applyTheme(store.get("theme", null) || urlTheme);
  document.addEventListener("click", (e) => {
    if (!e.target.closest("[data-theme-toggle]")) return;
    const dark = root.getAttribute("data-theme") === "dark" || (!root.getAttribute("data-theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
    const next = dark ? "light" : "dark"; store.set("theme", next); applyTheme(next);
  });
  const toggle = $("#anim-toggle");
  if (toggle) {
    toggle.checked = paused();
    toggle.addEventListener("change", () => { root.classList.toggle("anim-off", toggle.checked); store.set("anim-off", toggle.checked); });
  }

  /* ---------- headline: staggered masked word reveal ---------- */
  $$("[data-reveal-words]").forEach((el) => {
    const words = el.textContent.trim().split(/\s+/);
    el.textContent = "";
    words.forEach((w, i) => {
      const outer = document.createElement("span"); outer.className = "w"; outer.style.setProperty("--i", i);
      const inner = document.createElement("span"); inner.textContent = w; outer.appendChild(inner);
      el.appendChild(outer); el.appendChild(document.createTextNode(" "));
    });
  });

  /* ---------- the living fleet map ---------- */
  const data = (() => { try { return JSON.parse($("#landing-data").textContent); } catch (e) { return { cities: [] }; } })();
  const EDGES = [
    ["Dallas", "Fort Worth"], ["Dallas", "Waco"], ["Waco", "Austin"], ["Austin", "San Antonio"], ["San Antonio", "Laredo"],
    ["San Antonio", "Corpus Christi"], ["Houston", "Austin"], ["Houston", "Beaumont"], ["Beaumont", "Baton Rouge"],
    ["Houston", "Corpus Christi"], ["Dallas", "Houston"], ["Dallas", "Shreveport"], ["Shreveport", "Texarkana"],
    ["Texarkana", "Little Rock"], ["Little Rock", "Memphis"], ["Dallas", "Oklahoma City"], ["Oklahoma City", "Tulsa"],
    ["Dallas", "Wichita Falls"], ["Wichita Falls", "Amarillo"], ["Amarillo", "Lubbock"], ["Lubbock", "Midland"],
    ["Midland", "El Paso"], ["Abilene", "Midland"], ["Fort Worth", "Abilene"], ["Dallas", "Texarkana"], ["Tulsa", "Little Rock"],
    ["Shreveport", "Baton Rouge"], ["Wichita Falls", "Oklahoma City"], ["Lubbock", "Abilene"],
  ];
  const ALERTS = [
    { text: "is 40 min from the drive-time limit, 55 min from Houston.", tier: "warn", label: "Needs your OK", kind: "semi" },
    { text: "bought 206 gallons. The tank holds 150.", tier: "ok", label: "Handled for you", kind: "semi" },
    { text: "is running hot: coolant up 17°F in 6 hours.", tier: "warn", label: "Needs your OK", kind: "daycab" },
    { text: "fuel card was used 90 miles from the truck.", tier: "stop", label: "For your decision", kind: "box" },
    { text: "had 4 hard-braking events in two days.", tier: "ok", label: "Handled for you", kind: "van" },
    { text: "engine computer dropped offline twice today.", tier: "warn", label: "Needs your OK", kind: "semi" },
  ];

  function FleetMap(wrap, canvas, cardsLayer) {
    const ctx = canvas.getContext("2d", { alpha: true });
    const rng = mulberry32(42);
    let W = 0, H = 0, dpr = 1, nodes = {}, edges = [], vehicles = [], layer = null, running = false, lastT = 0;
    let pointer = { x: -1e4, y: -1e4, tx: -1e4, ty: -1e4 };
    let colors = {};
    let alertTimer = 0, alertIndex = 0, activeAlert = null;
    const cardMargin = 12;

    function recolor() {
      const cs = getComputedStyle(root);
      const get = (n) => cs.getPropertyValue(n).trim();
      colors = { accent: get("--accent"), ink: get("--ink"), ink3: get("--ink-3"), line: get("--line"), paper: get("--paper"), surface: get("--surface"), ok: get("--ok"), warn: get("--warn"), stop: get("--stop") };
      layer = null; // static layer depends on colours
    }

    function project() {
      const cs = data.cities || [];
      if (!cs.length) return;
      const lats = cs.map((c) => c.lat), lons = cs.map((c) => c.lon);
      const lat0 = Math.min(...lats) - 0.6, lat1 = Math.max(...lats) + 0.6, lon0 = Math.min(...lons) - 0.6, lon1 = Math.max(...lons) + 0.6;
      // cover the box: scale so the network fills the wider of the two dimensions
      const sx = W / (lon1 - lon0), sy = H / (lat1 - lat0);
      const s = Math.max(sx, sy) * (mobile ? 1.35 : 1.08);
      const cx = (lon0 + lon1) / 2, cy = (lat0 + lat1) / 2;
      const ox = W * (mobile ? 0.5 : 0.62), oy = H * 0.5;
      nodes = {};
      cs.forEach((c) => { nodes[c.name] = { x: ox + (c.lon - cx) * s, y: oy - (c.lat - cy) * s, name: c.name }; });
      edges = [];
      const er = mulberry32(7);
      EDGES.forEach(([a, b]) => {
        const A = nodes[a], B = nodes[b]; if (!A || !B) return;
        const mx = (A.x + B.x) / 2, my = (A.y + B.y) / 2, dx = B.x - A.x, dy = B.y - A.y, len = Math.hypot(dx, dy);
        const bend = (er() - 0.5) * 0.28 * len;
        const c = { x: mx - (dy / len) * bend, y: my + (dx / len) * bend };
        const pts = []; let total = 0;
        for (let i = 0; i <= 48; i++) {
          const t = i / 48, u = 1 - t;
          const x = u * u * A.x + 2 * u * t * c.x + t * t * B.x, y = u * u * A.y + 2 * u * t * c.y + t * t * B.y;
          if (i) total += Math.hypot(x - pts[i - 1].x, y - pts[i - 1].y);
          pts.push({ x, y, d: total });
        }
        edges.push({ a, b, pts, len: total, ctrl: c });
      });
    }
    function edgesFrom(name) { return edges.filter((e) => e.a === name || e.b === name); }
    function posOn(edge, dist, forward) {
      const d = forward ? dist : edge.len - dist;
      const pts = edge.pts; let i = 1;
      while (i < pts.length - 1 && pts[i].d < d) i++;
      const p0 = pts[i - 1], p1 = pts[i], span = Math.max(1e-6, p1.d - p0.d), t = clamp((d - p0.d) / span, 0, 1);
      const x = lerp(p0.x, p1.x, t), y = lerp(p0.y, p1.y, t);
      let ang = Math.atan2(p1.y - p0.y, p1.x - p0.x); if (!forward) ang += Math.PI;
      return { x, y, ang };
    }
    function spawn() {
      vehicles = [];
      const n = mobile ? 10 : 22;
      const kinds = ["semi", "semi", "semi", "semi", "daycab", "daycab", "box", "box", "van", "van", "pickup"];
      for (let i = 0; i < n; i++) {
        const e = edges[Math.floor(rng() * edges.length)];
        vehicles.push({ id: 1200 + i, kind: kinds[Math.floor(rng() * kinds.length)], edge: e, forward: rng() > 0.5, dist: rng() * e.len, speed: 16 + rng() * 26, trail: [], x: 0, y: 0, ang: 0, pulse: 0 });
      }
    }
    function nextEdge(v) {
      const at = v.forward ? v.edge.b : v.edge.a;
      const options = edgesFrom(at).filter((e) => e !== v.edge);
      const e = options.length ? options[Math.floor(rng() * options.length)] : v.edge;
      v.edge = e; v.forward = e.a === at; v.dist = 0;
    }
    function resize() {
      const r = wrap.getBoundingClientRect();
      W = Math.max(320, r.width); H = Math.max(320, r.height);
      dpr = Math.min(2, window.devicePixelRatio || 1);
      canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
      canvas.style.width = W + "px"; canvas.style.height = H + "px";
      project(); layer = null;
      if (!vehicles.length) spawn(); else vehicles.forEach((v) => { if (!edges.includes(v.edge)) { v.edge = edges[0]; v.dist = 0; } });
      if (reduced()) drawFrame(0);
    }
    function staticLayer() {
      const c = document.createElement("canvas"); c.width = canvas.width; c.height = canvas.height;
      const g = c.getContext("2d"); g.scale(dpr, dpr);
      // fine grid
      g.strokeStyle = colors.line; g.globalAlpha = 0.5; g.lineWidth = 1;
      const step = mobile ? 36 : 44;
      g.beginPath();
      for (let x = (W / 2) % step; x < W; x += step) { g.moveTo(x, 0); g.lineTo(x, H); }
      for (let y = (H / 2) % step; y < H; y += step) { g.moveTo(0, y); g.lineTo(W, y); }
      g.stroke();
      // routes: a soft glow pass then a crisp line
      g.globalAlpha = 1;
      const path = new Path2D();
      edges.forEach((e) => { path.moveTo(e.pts[0].x, e.pts[0].y); path.quadraticCurveTo(e.ctrl.x, e.ctrl.y, e.pts[48].x, e.pts[48].y); });
      g.lineCap = "round";
      g.strokeStyle = colors.accent; g.globalAlpha = 0.18; g.lineWidth = 7; g.stroke(path);
      g.globalAlpha = 0.55; g.lineWidth = 1.5; g.stroke(path);
      // cities, labels skipped where they would collide
      g.globalAlpha = 1;
      const labelled = [];
      Object.values(nodes).forEach((n) => {
        g.beginPath(); g.arc(n.x, n.y, 3, 0, Math.PI * 2); g.fillStyle = colors.paper; g.fill(); g.strokeStyle = colors.accent; g.lineWidth = 1.2; g.stroke();
        const crowded = labelled.some((o) => Math.abs(o.x - n.x) < 90 && Math.abs(o.y - n.y) < 14);
        if (!mobile && !crowded) { g.fillStyle = colors.ink3; g.font = "500 10px " + getComputedStyle(root).getPropertyValue("--mono"); g.fillText(n.name.toUpperCase(), n.x + 7, n.y - 6); labelled.push(n); }
      });
      return c;
    }
    function step(dt) {
      vehicles.forEach((v) => {
        v.dist += v.speed * dt;
        if (v.dist >= v.edge.len) nextEdge(v);
        const p = posOn(v.edge, v.dist, v.forward);
        v.x = p.x; v.y = p.y; v.ang = p.ang;
        v.trail.push({ x: p.x, y: p.y }); if (v.trail.length > 16) v.trail.shift();
        if (v.pulse > 0) v.pulse = Math.max(0, v.pulse - dt);
      });
      pointer.x = lerp(pointer.x, pointer.tx, 0.12); pointer.y = lerp(pointer.y, pointer.ty, 0.12);
    }
    function drawFrame(dt) {
      if (!layer) layer = staticLayer();
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      ctx.drawImage(layer, 0, 0, W, H);
      // local glow near the pointer
      if (hoverable && pointer.x > -500) {
        const g = ctx.createRadialGradient(pointer.x, pointer.y, 0, pointer.x, pointer.y, 180);
        g.addColorStop(0, colors.accent); g.addColorStop(1, "transparent");
        ctx.globalAlpha = 0.1; ctx.fillStyle = g; ctx.fillRect(pointer.x - 180, pointer.y - 180, 360, 360); ctx.globalAlpha = 1;
      }
      // trails
      ctx.lineCap = "round"; ctx.lineWidth = 2;
      vehicles.forEach((v) => {
        for (let i = 1; i < v.trail.length; i++) {
          ctx.globalAlpha = (i / v.trail.length) * 0.35; ctx.strokeStyle = colors.accent;
          ctx.beginPath(); ctx.moveTo(v.trail[i - 1].x, v.trail[i - 1].y); ctx.lineTo(v.trail[i].x, v.trail[i].y); ctx.stroke();
        }
      });
      ctx.globalAlpha = 1;
      // pulse rings
      vehicles.forEach((v) => {
        if (v.pulse > 0) {
          const k = 1 - v.pulse / 1.6;
          ctx.beginPath(); ctx.arc(v.x, v.y, 8 + k * 26, 0, Math.PI * 2);
          ctx.strokeStyle = v.pulseColor || colors.warn; ctx.globalAlpha = (1 - k) * 0.8; ctx.lineWidth = 2; ctx.stroke(); ctx.globalAlpha = 1;
        }
      });
      // vehicles
      const V = window.HaulmindVehicles;
      vehicles.forEach((v) => { if (V) V.draw(ctx, v.kind, v.x, v.y, v.ang, mobile ? 0.55 : 0.7, colors.ink, colors.surface); });
      // follow the active alert card
      if (activeAlert && activeAlert.el) {
        const v = activeAlert.vehicle;
        const el = activeAlert.el, w = el.offsetWidth || 240, h = el.offsetHeight || 60;
        let x = v.x, y = v.y;
        if (x + w + 30 > W) { x = v.x - w - 28; }
        y = clamp(y, h / 2 + cardMargin, H - h / 2 - cardMargin);
        el.style.transform = `translate(${x + 14}px, ${y - h / 2}px)`;
      }
    }
    function scheduleAlerts(dt) {
      alertTimer -= dt;
      if (alertTimer > 0) return;
      alertTimer = 6 + rng() * 2;
      const spec = ALERTS[alertIndex++ % ALERTS.length];
      const inside = vehicles.filter((v) => v.x > 60 && v.x < W - 60 && v.y > 80 && v.y < H - 60);
      const v = inside[Math.floor(rng() * inside.length)] || vehicles[0];
      showAlert(v, spec);
    }
    function showAlert(v, spec, hold) {
      if (activeAlert) removeAlert();
      v.pulse = 1.6; v.pulseColor = colors[spec.tier];
      const el = document.createElement("div"); el.className = "map-card";
      el.innerHTML = `<span class="micro mc-unit">UNIT ${v.id}</span><span class="mc-text"></span><span class="badge badge-${spec.tier}"></span>`;
      $(".mc-text", el).textContent = `Truck ${v.id} ${spec.text}`; $(".badge", el).textContent = spec.label;
      el.style.left = "0"; el.style.top = "0";
      cardsLayer.appendChild(el);
      activeAlert = { el, vehicle: v, t: 0 };
      if (hold) { el.classList.add("resolved"); return; }
      activeAlert.t1 = setTimeout(() => el.classList.add("resolved"), 2200);
      activeAlert.t2 = setTimeout(() => { el.classList.add("leaving"); setTimeout(removeAlert, 280); }, 5200);
    }
    function removeAlert() { if (!activeAlert) return; clearTimeout(activeAlert.t1); clearTimeout(activeAlert.t2); activeAlert.el.remove(); activeAlert = null; }
    function frame(t) {
      if (!running) return;
      const dt = Math.min(0.05, (t - lastT) / 1000 || 0); lastT = t;
      step(dt); scheduleAlerts(dt); drawFrame(dt);
      requestAnimationFrame(frame);
    }
    function start() { if (running || reduced() || paused()) return; running = true; lastT = performance.now(); requestAnimationFrame(frame); }
    function stop() { running = false; }
    function still() { // reduced motion: one composed frame with a resolved card
      stop(); step(0); drawFrame(0);
      if (!activeAlert) { const v = vehicles.find((x) => x.x > W * 0.35 && x.x < W * 0.8 && x.y > 100 && x.y < H - 100) || vehicles[0]; showAlert(v, ALERTS[0], true); drawFrame(0); }
    }
    // camera: scroll drift + pointer parallax, applied as one transform on the two layers
    let heroP = 0, px = 0, py = 0;
    function camera() {
      if (reduced()) return;
      const s = 1 + heroP * 0.12, tx = px * 8, ty = -heroP * 40 + py * 6;
      const tf = `translate3d(${tx.toFixed(1)}px, ${ty.toFixed(1)}px, 0) scale(${s.toFixed(3)})`;
      canvas.style.transform = tf; cardsLayer.style.transform = tf;
    }
    if (hoverable) {
      wrap.parentElement.addEventListener("pointermove", (e) => {
        const r = wrap.getBoundingClientRect();
        pointer.tx = e.clientX - r.left; pointer.ty = e.clientY - r.top;
        px = (e.clientX / window.innerWidth - 0.5) * 2; py = (e.clientY / window.innerHeight - 0.5) * 2; camera();
      });
      wrap.parentElement.addEventListener("pointerleave", () => { pointer.tx = -1e4; pointer.ty = -1e4; px = 0; py = 0; camera(); });
    }
    recolor(); resize();
    new ResizeObserver(resize).observe(wrap);
    return {
      start, stop, still, recolor,
      setScroll(p) { heroP = p; camera(); },
      visible(v) { if (v) start(); else stop(); },
    };
  }

  const heroEl = $("#hero"), mapCanvas = $("#fleet-map"), cardsLayer = $("#map-cards");
  if (heroEl && mapCanvas && window.HaulmindVehicles) {
    const map = FleetMap($(".map-wrap", heroEl), mapCanvas, cardsLayer);
    window.fleetMap = map;
    let heroVisible = true;
    new IntersectionObserver((entries) => { heroVisible = entries[0].isIntersecting; map.visible(heroVisible && !document.hidden); }, { threshold: 0.05 }).observe(heroEl);
    document.addEventListener("visibilitychange", () => map.visible(heroVisible && !document.hidden));
    if (toggle) toggle.addEventListener("change", () => map.visible(heroVisible && !document.hidden));
    if (reduced()) map.still(); else map.start();
    reducedQuery.addEventListener("change", () => { if (reducedQuery.matches) map.still(); else map.start(); });
  }

  /* ---------- scroll-linked sections ---------- */
  const problem = $("#problem"), how = $("#how");
  const chipField = $("#chip-field");
  const CHIP_TEXTS = ["Location · Truck 1203", "Coolant 190°F · Truck 1201", "Speeding 71 mph · Truck 1210", "Fuel 42 gal · Truck 1208", "Hours update · Jordan", "Location · Truck 1211", "Hard brake · Truck 1207", "Battery 13.9 V · Truck 1204", "Trip started · Truck 1209", "Location · Truck 1200", "Idling 22 min · Truck 1206", "Fault code P0401 · Truck 1202", "Location · Truck 1205", "Hours update · Avery", "Fuel 61 gal · Truck 1203", "Coolant 201°F · Truck 1205", "Location · Truck 1212", "Trip ended · Truck 1201", "Hours update · Riley", "Location · Truck 1207", "Speed 64 mph · Truck 1200", "Engine reading · Truck 1210", "Location · Truck 1209", "Hard brake · Truck 1207"];
  const KEEP_TEXTS = ["Drive time 55 min short · Logan", "Coolant up 17°F · Truck 1205", "Fill 206 gal, tank 150 · Truck 1202", "4 hard brakes in 2 days · Sawyer"];
  function buildChips() {
    if (!chipField) return;
    chipField.innerHTML = "";
    const r = mulberry32(99);
    const fw = chipField.clientWidth, fh = chipField.clientHeight;
    const all = CHIP_TEXTS.concat(KEEP_TEXTS);
    const slots = $$("#calm-list .calm-item");
    const fieldRect = chipField.getBoundingClientRect();
    all.forEach((text, i) => {
      const keep = i >= CHIP_TEXTS.length;
      const el = document.createElement("div"); el.className = "chip" + (keep ? " keep" : "");
      const inner = document.createElement("span"); inner.textContent = text; el.appendChild(inner);
      const x0 = r() * Math.max(40, fw - 200), y0 = r() * Math.max(20, fh - 36);
      el.style.setProperty("--x0", x0.toFixed(0) + "px"); el.style.setProperty("--y0", y0.toFixed(0) + "px");
      el.style.setProperty("--j", (r() * 1.4).toFixed(2));
      if (keep) {
        const slot = slots[i - CHIP_TEXTS.length];
        const sr = slot ? slot.getBoundingClientRect() : fieldRect;
        el.style.setProperty("--x1", (sr.left - fieldRect.left + 14).toFixed(0) + "px");
        el.style.setProperty("--y1", (sr.top - fieldRect.top + 14).toFixed(0) + "px");
      } else {
        el.style.setProperty("--x1", (fw * 0.5 + (r() - 0.5) * 60).toFixed(0) + "px");
        el.style.setProperty("--y1", (fh * 0.5 + (r() - 0.5) * 40).toFixed(0) + "px");
      }
      chipField.appendChild(el);
    });
  }
  function sectionProgress(el) {
    const r = el.getBoundingClientRect(); const vh = window.innerHeight;
    return clamp(-r.top / Math.max(1, r.height - vh), 0, 1);
  }
  const stations = $$("#road .station"), steps3 = $$(".step3");
  const roadTruck = $("#road-truck"), road = $("#road");
  function setRoadWidth() { if (road && roadTruck) road.style.setProperty("--road-w", Math.max(0, road.clientWidth - roadTruck.offsetWidth - 20) + "px"); }
  let ticking = false, frozen = false, forcedValue = null;
  function applyProblem(p) {
    if (!problem) return;
    problem.style.setProperty("--p", p.toFixed(3));
    $$(".chip", chipField).forEach((c) => { c.style.setProperty("--p", p.toFixed(3)); c.style.setProperty("--amp", (1 - p).toFixed(2)); });
    problem.classList.toggle("done", p >= 0.98);
  }
  function applyHow(p) {
    if (!how) return;
    how.style.setProperty("--p", p.toFixed(3));
    stations.forEach((s, i) => { const on = p >= parseFloat(s.dataset.at) - 0.03; s.classList.toggle("on", on); if (steps3[i]) steps3[i].classList.toggle("on", on); });
  }
  function applyProgress(p) { applyProblem(p); applyHow(p); }
  function onScroll() {
    if (ticking) return; ticking = true;
    requestAnimationFrame(() => {
      ticking = false;
      if (reduced() || frozen) return;
      if (heroEl && window.fleetMap) { const r = heroEl.getBoundingClientRect(); window.fleetMap.setScroll(clamp(-r.top / Math.max(1, r.height), 0, 1)); }
      if (problem) applyProblem(sectionProgress(problem));
      if (how) applyHow(sectionProgress(how));
    });
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", () => { buildChips(); setRoadWidth(); if (forcedValue !== null) applyProgress(forcedValue); else onScroll(); });
  buildChips(); setRoadWidth(); onScroll();
  if (reduced()) { steps3.forEach((s) => s.classList.add("on")); stations.forEach((s) => s.classList.add("on")); }
  /* review hooks (screenshots): ?review=<section id> shows one section at the top of the page,
     ?p=0.6 freezes a scroll-linked section at that progress, ?scroll=<px> jumps to an offset. */
  const review = (location.search.match(/[?&]review=([a-z]+)/) || [])[1];
  const forcedP = location.search.match(/[?&]p=([0-9.]+)/);
  if (review) {
    root.classList.add("review");
    $$("main > section, .lp-top, .lp-foot").forEach((s) => { if (s.id !== review) s.hidden = true; });
    if (forcedP) { frozen = true; forcedValue = clamp(parseFloat(forcedP[1]), 0, 1); }
    buildChips(); setRoadWidth();
    if (forcedValue !== null) applyProgress(forcedValue);
  }
  const jump = location.search.match(/[?&]scroll=(\d+)/);
  if (jump) { window.scrollTo({ top: +jump[1], behavior: "instant" }); setTimeout(onScroll, 50); }

  /* ---------- reveals, count-ups, lanes pause ---------- */
  $$(".section-head, .tile, .stat, .lane").forEach((el) => el.classList.add("reveal"));
  const io = new IntersectionObserver((entries) => {
    entries.forEach((en) => {
      if (!en.isIntersecting) return;
      en.target.classList.add("in");
      $$(".num[data-count]", en.target).forEach(countUp);
      if (en.target.id === "results") en.target.classList.add("in");
      io.unobserve(en.target);
    });
  }, { threshold: 0.25 });
  $$(".reveal, #results").forEach((el) => io.observe(el));
  function countUp(el) {
    if (el.dataset.done) return; el.dataset.done = "1";
    const target = parseFloat(el.dataset.count), decimals = parseInt(el.dataset.decimals || "0", 10);
    const fmt = (v) => (decimals ? v.toFixed(decimals) : Math.round(v).toLocaleString());
    if (reduced()) { el.textContent = fmt(target); return; }
    const t0 = performance.now(), dur = 1200;
    (function tick(t) { const k = clamp((t - t0) / dur, 0, 1), e = 1 - Math.pow(1 - k, 3); el.textContent = fmt(target * e); if (k < 1) requestAnimationFrame(tick); })(t0);
  }
  const lanes = $("#lanes");
  if (lanes) new IntersectionObserver((en) => lanes.classList.toggle("paused", !en[0].isIntersecting), { threshold: 0.1 }).observe(lanes);

  /* ---------- light that follows the cursor across cards ---------- */
  if (hoverable) {
    document.addEventListener("pointermove", (e) => {
      if (reduced()) return;
      const lit = e.target.closest ? e.target.closest(".lit") : null;
      if (lit) { const r = lit.getBoundingClientRect(); lit.style.setProperty("--mx", (e.clientX - r.left) + "px"); lit.style.setProperty("--my", (e.clientY - r.top) + "px"); }
    }, { passive: true });
  }

  /* ---------- transition into the app ---------- */
  document.addEventListener("click", (e) => {
    const a = e.target.closest("[data-demo]"); if (!a) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    const go = () => { location.href = a.getAttribute("href"); };
    if (reduced()) { go(); return; }
    root.classList.add("leaving"); setTimeout(go, 330);
  });
})();
