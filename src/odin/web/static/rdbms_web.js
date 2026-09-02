/* Odin — RDBMS "table web": a canvas force-directed graph of a schema's tables
   and their foreign keys, with a live preview panel.

   Per the design handoff (Table Web.dc.html) + agreed refinements:
     - ONE client-side force pass on load (~300 iters), then a static render.
       No permanent rAF loop, no animated links, no traveling dots. A bounded
       rAF runs only during a layout-switch ease and while a node is selected
       (the selection pulse).
     - Single accent colour; node size by row estimate; glow on hub / selection /
       filter match. Name-prefix groups drive WEB gravity anchors + a neutral
       (uncoloured) legend only.
     - Two layouts: WEB (force) and RING (bundled, prefix groups kept adjacent).
     - >200 nodes: render only FK-connected tables + a "show all (N)" toggle.
       >600 nodes: default to RING. <=10 tables: a plain radial, no sim.
   Colours are read from CSS custom properties so the graph tracks the theme. */
(function () {
  "use strict";
  var host = document.getElementById("web-data");
  var canvas = document.getElementById("tw-canvas");
  if (!host || !canvas) return;

  var DATA = JSON.parse(host.textContent || "{}");
  var CID = DATA.cid, SCHEMA = DATA.schema;
  var RAW_NODES = DATA.nodes || [], RAW_EDGES = DATA.edges || [];

  // ---- theme colours ------------------------------------------------------
  var CSS = getComputedStyle(document.documentElement);
  function tok(n, fb) { return (CSS.getPropertyValue(n) || "").trim() || fb; }
  var C = {
    accent: tok("--accent", "#46c7f0"),
    ground: tok("--panel-2", "#0b121b"),
    ink:    tok("--void", "#090d14"),
    grid:   tok("--border", "#1f2e3e"),
    soft:   tok("--text-soft", "#93a6ba"),
    faint:  tok("--text-faint", "#586d82"),
    text:   tok("--text", "#e8eff7")
  };
  function rgba(hex, a) {
    var h = hex.replace("#", "");
    if (h.length === 3) h = h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
    var n = parseInt(h, 16);
    return "rgba(" + ((n>>16)&255) + "," + ((n>>8)&255) + "," + (n&255) + "," + a + ")";
  }

  // ---- model ------------------------------------------------------------
  var byName = {};
  var nodes = RAW_NODES.map(function (t, i) {
    var o = { id:i, name:t.name, rows:+t.rows||0, cols:+t.cols||0, deg:0, x:0, y:0 };
    byName[t.name] = o; return o;
  });
  var links = [];
  RAW_EDGES.forEach(function (e) {
    var a = byName[e.from], b = byName[e.to];
    if (!a || !b || a === b) return;
    links.push({ a:a.id, b:b.id, fromCols:e.fromCols||[], toCols:e.toCols||[] });
    a.deg++; b.deg++;
  });
  var adj = nodes.map(function () { return []; });
  links.forEach(function (l, i) { adj[l.a].push({ o:l.b, i:i }); adj[l.b].push({ o:l.a, i:i }); });

  // prefix groups (before first "_"), the 8 most common get a vivid colour;
  // the long tail is "other" (neutral grey). Colour is purely visual — it does
  // NOT drive the WEB layout (see layoutWeb).
  var PALETTE = ["#46c7f0", "#3fd08b", "#b98cff", "#f2b445",
                 "#6aa8ff", "#ff6f6b", "#8bd450", "#f078c8"];
  var OTHER_COLOR = "#5b6b7d";
  var counts = {};
  nodes.forEach(function (n) {
    var p = (n.name.split("_")[0] || n.name).toLowerCase();
    counts[p] = (counts[p] || 0) + 1;
  });
  var top = Object.keys(counts)
    .sort(function (a, b) { return counts[b] - counts[a] || a.localeCompare(b); })
    .slice(0, 8);
  var groupIx = {}; top.forEach(function (p, i) { groupIx[p] = i; });
  var GROUPS = top.map(function (p, i) { return { name: p, color: PALETTE[i] }; });
  var hasOther = false;
  nodes.forEach(function (n) {
    var p = (n.name.split("_")[0] || n.name).toLowerCase();
    if (p in groupIx) { n.g = groupIx[p]; n.gp = p; n.color = PALETTE[groupIx[p]]; }
    else { n.g = top.length; n.gp = "other"; n.color = OTHER_COLOR; hasOther = true; }
  });
  if (hasOther) GROUPS.push({ name: "other", color: OTHER_COLOR });
  var NG = GROUPS.length;

  var maxDeg = nodes.reduce(function (m, n) { return Math.max(m, n.deg); }, 0);
  var hubCut = Math.max(6, Math.round(maxDeg * 0.6));
  function isHub(n) { return n.deg >= hubCut && n.deg > 1; }

  // ---- scale rules ----------------------------------------------------------
  var TOTAL = nodes.length;
  var LARGE = TOTAL > 220;            // offer the "FK-linked only" declutter toggle
  var HUGE = TOTAL > 1500;            // safety valve: start decluttered, O(n^2) layout
  var showAll = !HUGE;               // every table renders unless the schema is enormous
  var state = {
    layout: (TOTAL > 600 ? "ring" : "web"),
    filter: "", sel: null, hover: null
  };

  function activeNodes() {
    if (showAll) return nodes;
    return nodes.filter(function (n) { return n.deg >= 1; });
  }
  function activeSet() {
    var s = Object.create(null);
    activeNodes().forEach(function (n) { s[n.id] = 1; });
    return s;
  }

  // ---- layouts ------------------------------------------------------------
  function rnd() { seed = (seed * 1664525 + 1013904223) % 4294967296; return seed / 4294967296; }
  var seed = 20260902;

  function layoutRadial(list) {
    var R = Math.max(140, list.length * 14), pos = {};
    var order = list.slice().sort(function (a, b) { return a.name.localeCompare(b.name); });
    order.forEach(function (n, i) {
      var t = (i / order.length) * Math.PI * 2 - Math.PI / 2;
      pos[n.id] = { x: Math.cos(t) * R, y: Math.sin(t) * R, a: t };
    });
    return pos;
  }

  function layoutRing(list) {
    var order = list.slice().sort(function (a, b) { return (a.g - b.g) || a.name.localeCompare(b.name); });
    var R = 360, pos = {};
    order.forEach(function (n, i) {
      var t = (i / order.length) * Math.PI * 2 - Math.PI / 2;
      pos[n.id] = { x: Math.cos(t) * R, y: Math.sin(t) * R, a: t };
    });
    return pos;
  }

  // CLUSTERS — one compact grid per prefix group, groups placed around a ring
  function layoutGrid(list) {
    var pos = {}, R = 380;
    for (var gi = 0; gi < NG; gi++) {
      var mem = list.filter(function (n) { return n.g === gi; });
      if (!mem.length) continue;
      mem.sort(function (a, b) { return a.name.localeCompare(b.name); });
      var ang = (gi / NG) * Math.PI * 2 - Math.PI / 2;
      var cx = Math.cos(ang) * R, cy = Math.sin(ang) * R * 0.86;
      var cols = Math.max(1, Math.ceil(Math.sqrt(mem.length * 1.4)));
      var rows = Math.ceil(mem.length / cols);
      var gap = 26;
      mem.forEach(function (n, i) {
        var gx = (i % cols) - (cols - 1) / 2;
        var gy = Math.floor(i / cols) - (rows - 1) / 2;
        pos[n.id] = { x: cx + gx * gap, y: cy + gy * gap };
      });
    }
    return pos;
  }

  function layoutWeb(list) {
    seed = 20260902;
    var n = list.length;
    var idx = {}; list.forEach(function (nd, i) { idx[nd.id] = i; });
    var px = new Float64Array(n), py = new Float64Array(n), vx = new Float64Array(n), vy = new Float64Array(n);
    var grp = new Int32Array(n);
    // golden-angle spiral seed — even coverage, no axis/colinear bias
    var GA = Math.PI * (3 - Math.sqrt(5));
    for (var i = 0; i < n; i++) {
      var rr = 13 * Math.sqrt(i + 1), aa = i * GA;
      px[i] = Math.cos(aa) * rr + (rnd() - 0.5) * 8;
      py[i] = Math.sin(aa) * rr + (rnd() - 0.5) * 8;
      grp[i] = list[i].g;
    }
    var LL = [];
    links.forEach(function (l) {
      if (idx[l.a] != null && idx[l.b] != null) LL.push([idx[l.a], idx[l.b]]);
    });
    var ITERS = n > 700 ? 110 : n > 400 ? 170 : 320;
    var REP = 2400, CUT = 250000;           // repel within ~500px
    var SPRING_L = 74, SPRING_K = 0.045;
    var COHERE = NG >= 3 ? 0.010 : 0;       // gentle same-colour cohesion, never a well
    var gcx = new Float64Array(NG), gcy = new Float64Array(NG), gcn = new Int32Array(NG);
    for (var it = 0; it < ITERS; it++) {
      var alpha = 1 - it / ITERS;
      if (COHERE) {
        gcx.fill(0); gcy.fill(0); gcn.fill(0);
        for (var c0 = 0; c0 < n; c0++) { var g0 = grp[c0]; gcx[g0] += px[c0]; gcy[g0] += py[c0]; gcn[g0]++; }
        for (var g1 = 0; g1 < NG; g1++) if (gcn[g1]) { gcx[g1] /= gcn[g1]; gcy[g1] /= gcn[g1]; }
      }
      for (var p = 0; p < n; p++) {
        for (var q = p + 1; q < n; q++) {
          var dx = px[q] - px[p], dy = py[q] - py[p], d2 = dx * dx + dy * dy;
          if (d2 > CUT || d2 === 0) continue;
          var d = Math.sqrt(d2), f = REP / d2;
          var ux = dx / d * f, uy = dy / d * f;
          vx[p] -= ux; vy[p] -= uy; vx[q] += ux; vy[q] += uy;
        }
      }
      for (var k = 0; k < LL.length; k++) {
        var A = LL[k][0], B = LL[k][1];
        var lx = px[B] - px[A], ly = py[B] - py[A];
        var ld = Math.hypot(lx, ly) || 1, lf = (ld - SPRING_L) * SPRING_K;
        var lux = lx / ld * lf, luy = ly / ld * lf;
        vx[A] += lux; vy[A] += luy; vx[B] -= lux; vy[B] -= luy;
      }
      for (var m = 0; m < n; m++) {
        if (COHERE) {
          var gm = grp[m];
          vx[m] += (gcx[gm] - px[m]) * COHERE * alpha;
          vy[m] += (gcy[gm] - py[m]) * COHERE * alpha;
        }
        vx[m] -= px[m] * 0.012 * alpha;       // gentle centering, relaxes over time
        vy[m] -= py[m] * 0.012 * alpha;
        px[m] += vx[m] * alpha * 0.85; py[m] += vy[m] * alpha * 0.85;
        vx[m] *= 0.86; vy[m] *= 0.86;
      }
    }
    var cx = 0, cy = 0;
    for (var c = 0; c < n; c++) { cx += px[c] / n; cy += py[c] / n; }
    var rad = [];
    for (var r = 0; r < n; r++) rad.push(Math.hypot(px[r] - cx, py[r] - cy));
    var r92 = rad.slice().sort(function (a, b) { return a - b; })[Math.floor(n * 0.92)] || 1;
    var s = 360 / r92, cap = r92 * 1.25, pos = {};
    for (var w = 0; w < n; w++) {
      var ddx = px[w] - cx, ddy = py[w] - cy, rr = Math.hypot(ddx, ddy) || 1;
      if (rr > cap) { ddx *= cap / rr; ddy *= cap / rr; }
      pos[list[w].id] = { x: ddx * s, y: ddy * s };
    }
    return pos;
  }

  var LAYOUTS = {};
  function buildLayouts() {
    var list = activeNodes();
    if (list.length <= 10) {
      var rad = layoutRadial(list);
      LAYOUTS = { web: rad, ring: rad, grid: rad };
      return;
    }
    LAYOUTS = { web: layoutWeb(list), ring: layoutRing(list), grid: layoutGrid(list) };
  }
  buildLayouts();

  function applyLayout(name, ease) {
    var L = LAYOUTS[name] || LAYOUTS.web;
    nodes.forEach(function (nd) {
      var t = L[nd.id];
      if (!t) return;
      nd.tx = t.x; nd.ty = t.y; nd.ang = t.a;
      if (!ease || nd.x === 0 && nd.y === 0) { nd.x = t.x; nd.y = t.y; }
    });
  }
  applyLayout(state.layout, false);

  // ---- view (pan / zoom) --------------------------------------------------
  var view = { k: 1, x: 0, y: 0 };
  var cssW = 0, cssH = 0, ctx = canvas.getContext("2d"), dpr = 1;

  function resize() {
    var st = canvas.parentElement.getBoundingClientRect();
    cssW = Math.max(1, st.width); cssH = Math.max(1, st.height);
    dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(cssW * dpr); canvas.height = Math.round(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if ((cssW < 40 || cssH < 40) && !resize._retry) {   // layout not settled yet
      resize._retry = requestAnimationFrame(function () { resize._retry = 0; resize(); fit(); draw(); });
    }
  }

  function bbox() {
    var list = activeNodes(), xs = [], ys = [];
    list.forEach(function (n) { xs.push(n.x); ys.push(n.y); });
    if (!xs.length) return { x0: -1, y0: -1, x1: 1, y1: 1 };
    return { x0: Math.min.apply(null, xs), y0: Math.min.apply(null, ys),
             x1: Math.max.apply(null, xs), y1: Math.max.apply(null, ys) };
  }
  function fit() {
    var b = bbox(), pad = 46;
    var w = (b.x1 - b.x0) || 1, h = (b.y1 - b.y0) || 1;
    var k = Math.min((cssW - pad * 2) / w, (cssH - pad * 2) / h);
    view.k = Math.max(0.15, Math.min(9, k));
    view.x = cssW / 2 - ((b.x0 + b.x1) / 2) * view.k;
    view.y = cssH / 2 - ((b.y0 + b.y1) / 2) * view.k;
  }

  function sx(x) { return x * view.k + view.x; }
  function sy(y) { return y * view.k + view.y; }

  // ---- draw -------------------------------------------------------------
  var LEGEND = document.getElementById("tw-legend");
  var ZX = document.getElementById("tw-zx");

  function nodeR(n) {
    var base = 1.6 + Math.log10(n.rows + 10) * 0.72 + n.deg * 0.14;
    base = Math.max(2.1, Math.min(9, base));
    return base * Math.max(0.85, Math.min(1.8, view.k));
  }

  function focusId() {
    return state.hover != null ? state.hover : state.sel;
  }
  function neighborSet(id) {
    var s = Object.create(null); s[id] = 1;
    (adj[id] || []).forEach(function (e) { s[e.o] = 1; });
    return s;
  }

  var boxes = [];   // overlay rects to avoid label collisions (screen space)
  function collectBoxes() {
    boxes = [];
    [LEGEND, document.querySelector(".tweb-zoom"), document.getElementById("tw-hint")].forEach(function (el) {
      if (!el || el.classList.contains("hide") || el.hidden) return;
      var pr = canvas.parentElement.getBoundingClientRect(), r = el.getBoundingClientRect();
      boxes.push({ x0: r.left - pr.left, y0: r.top - pr.top, x1: r.right - pr.left, y1: r.bottom - pr.top });
    });
  }
  function hitsBox(x0, y0, x1, y1) {
    for (var i = 0; i < boxes.length; i++) {
      var b = boxes[i];
      if (x0 < b.x1 && x1 > b.x0 && y0 < b.y1 && y1 > b.y0) return true;
    }
    return false;
  }

  var pulseT = 0;

  function draw() {
    var act = activeSet();
    var fid = focusId();
    var near = fid != null ? neighborSet(fid) : null;
    var flt = state.filter.toLowerCase();
    var matched = flt ? function (n) { return n.name.toLowerCase().indexOf(flt) >= 0; } : null;

    ctx.clearRect(0, 0, cssW, cssH);
    ctx.fillStyle = C.ground; ctx.fillRect(0, 0, cssW, cssH);

    // faint radial accent glow
    var g = ctx.createRadialGradient(cssW / 2, cssH * 0.42, 0, cssW / 2, cssH * 0.42, Math.max(cssW, cssH) * 0.6);
    g.addColorStop(0, rgba(C.accent, 0.06)); g.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = g; ctx.fillRect(0, 0, cssW, cssH);

    // pan/zoom-locked grid
    var step = 44 * view.k;
    if (step > 6) {
      ctx.strokeStyle = rgba(C.grid, 0.42); ctx.lineWidth = 1;
      var ox = view.x % step, oy = view.y % step;
      ctx.beginPath();
      for (var gx = ox; gx < cssW; gx += step) { ctx.moveTo(gx, 0); ctx.lineTo(gx, cssH); }
      for (var gy = oy; gy < cssH; gy += step) { ctx.moveTo(0, gy); ctx.lineTo(cssW, gy); }
      ctx.stroke();
    }

    var bundf = state.layout === "ring" ? 0.86 : 0.28;

    // links
    for (var i = 0; i < links.length; i++) {
      var l = links[i], a = nodes[l.a], b = nodes[l.b];
      if (!act[a.id] || !act[b.id]) continue;
      var lit = fid != null && (l.a === fid || l.b === fid);
      var dim = (fid != null && !lit) || (matched && !(matched(a) || matched(b)));
      var x1 = sx(a.x), y1 = sy(a.y), x2 = sx(b.x), y2 = sy(b.y);
      var mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
      var qx = mx + (cssW / 2 - mx) * bundf, qy = my + (cssH / 2 - my) * bundf;
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.quadraticCurveTo(qx, qy, x2, y2);
      if (lit) { ctx.strokeStyle = rgba(C.accent, 0.85); ctx.lineWidth = 1.4; ctx.shadowColor = rgba(C.accent, 0.5); ctx.shadowBlur = 8; }
      else if (dim) { ctx.strokeStyle = rgba(C.soft, 0.08); ctx.lineWidth = 1; ctx.shadowBlur = 0; }
      else { ctx.strokeStyle = rgba(C.soft, 0.26); ctx.lineWidth = 1; ctx.shadowBlur = 0; }
      ctx.stroke();
    }
    ctx.shadowBlur = 0;

    // nodes
    for (var j = 0; j < nodes.length; j++) {
      var n = nodes[j];
      if (!act[n.id]) continue;
      var x = sx(n.x), y = sy(n.y), r = nodeR(n);
      var isSel = n.id === state.sel, isNear = near && near[n.id];
      var isMatch = matched && matched(n);
      var dimd = (fid != null && !isNear) || (matched && !isMatch);
      var col = n.color || C.accent;
      if (isSel || isHub(n) || isMatch) {
        ctx.beginPath(); ctx.arc(x, y, r + 6, 0, 7);
        ctx.fillStyle = rgba(col, isSel ? 0.24 : 0.12); ctx.fill();
      }
      if (isSel) {
        var pr = r + 6 + pulseT * 10;
        ctx.beginPath(); ctx.arc(x, y, pr, 0, 7);
        ctx.strokeStyle = rgba(col, 0.55 * (1 - pulseT)); ctx.lineWidth = 1.5; ctx.stroke();
      }
      ctx.beginPath(); ctx.arc(x, y, r, 0, 7);
      ctx.fillStyle = dimd ? rgba(col, 0.20) : col;
      ctx.fill();
      ctx.lineWidth = 1; ctx.strokeStyle = C.ink; ctx.stroke();
    }

    // labels
    collectBoxes();
    ctx.font = "500 11px 'IBM Plex Mono', ui-monospace, monospace";
    ctx.textBaseline = "middle";
    for (var p = 0; p < nodes.length; p++) {
      var nd = nodes[p];
      if (!act[nd.id]) continue;
      var sel2 = nd.id === state.sel;
      var show = sel2
        || (near && near[nd.id] && view.k > 0.5)
        || (matched && matched(nd))
        || (fid == null && (view.k > 1.5 || nd.deg > 9));
      if (!show) continue;
      var lx = sx(nd.x), ly = sy(nd.y), r2 = nodeR(nd);
      var label = nd.name.length > 26 ? nd.name.slice(0, 25) + "…" : nd.name;
      var tw = ctx.measureText(label).width;
      var leftHalf = state.layout === "ring" && nd.x < 0;
      var tx = leftHalf ? lx - r2 - 6 - tw : lx + r2 + 6;
      var bx0 = tx - 3, bx1 = tx + tw + 3, by0 = ly - 8, by1 = ly + 8;
      if (!sel2 && hitsBox(bx0, by0, bx1, by1)) continue;
      ctx.fillStyle = rgba(C.ink, 0.72);
      ctx.fillRect(bx0, by0, bx1 - bx0, 16);
      ctx.fillStyle = sel2 ? C.text : C.soft;
      ctx.fillText(label, tx, ly + 1);
    }

    if (ZX) ZX.textContent = Math.round(view.k * 100) + "%";
  }

  // ---- bounded animation (layout ease + selection pulse) -----------------
  var raf = 0, easing = false;
  function tick() {
    var moving = false;
    if (easing) {
      nodes.forEach(function (n) {
        if (n.tx == null) return;
        n.x += (n.tx - n.x) * 0.12; n.y += (n.ty - n.y) * 0.12;
        if (Math.abs(n.tx - n.x) > 0.4 || Math.abs(n.ty - n.y) > 0.4) moving = true;
      });
      if (!moving) { easing = false; nodes.forEach(function (n) { if (n.tx != null) { n.x = n.tx; n.y = n.ty; } }); fit(); }
    }
    if (state.sel != null) { pulseT += 0.02; if (pulseT > 1) pulseT = 0; moving = true; }
    draw();
    if (moving || easing) raf = requestAnimationFrame(tick);
    else raf = 0;
  }
  function kick() { if (!raf) raf = requestAnimationFrame(tick); }

  // ---- panel fetch -----------------------------------------------------
  var PANEL = document.getElementById("rdbms-panel");
  var panelReq = 0;
  function loadPanel(name) {
    if (!name) {
      PANEL.innerHTML = '<div class="pnl-empty"><span class="ring"></span>Select a table in the web.</div>';
      return;
    }
    var my = ++panelReq;
    PANEL.innerHTML = '<div class="pnl-empty"><span class="ring"></span>Loading ' + name + '…</div>';
    fetch("/onboard/rdbms/" + CID + "/" + encodeURIComponent(SCHEMA) + "/" + encodeURIComponent(name) + "/panel")
      .then(function (r) { return r.text(); })
      .then(function (html) { if (my === panelReq) PANEL.innerHTML = html; })
      .catch(function () { if (my === panelReq) PANEL.innerHTML = '<div class="sql-err">could not load preview</div>'; });
  }

  function selectNode(id) {
    state.sel = id;
    pulseT = 0;
    loadPanel(id == null ? null : nodes[id].name);
    if (id == null) { draw(); } else { kick(); }
  }

  // ---- hit testing ---------------------------------------------------------
  function pick(mx, my) {
    var act = activeSet(), best = null, bestD = Infinity;
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (!act[n.id]) continue;
      var d = Math.hypot(sx(n.x) - mx, sy(n.y) - my);
      var hitR = Math.max(9, nodeR(n) + 6);
      if (d <= hitR && d < bestD) { best = n.id; bestD = d; }
    }
    return best;
  }

  // ---- events ---------------------------------------------------------
  var drag = null;
  canvas.addEventListener("mousedown", function (e) {
    drag = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, moved: 0 };
    canvas.classList.add("grabbing");
  });
  window.addEventListener("mousemove", function (e) {
    var pr = canvas.getBoundingClientRect();
    if (drag) {
      var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      drag.moved = Math.max(drag.moved, Math.abs(dx) + Math.abs(dy));
      view.x = drag.vx + dx; view.y = drag.vy + dy;
      draw();
      return;
    }
    var mx = e.clientX - pr.left, my = e.clientY - pr.top;
    if (mx < 0 || my < 0 || mx > cssW || my > cssH) { if (state.hover != null) { state.hover = null; draw(); } return; }
    var h = pick(mx, my);
    if (h !== state.hover) { state.hover = h; canvas.style.cursor = h != null ? "pointer" : "grab"; draw(); }
  });
  window.addEventListener("mouseup", function (e) {
    if (!drag) return;
    var wasClick = drag.moved <= 3;
    var pr = canvas.getBoundingClientRect();
    canvas.classList.remove("grabbing");
    if (wasClick) {
      var mx = e.clientX - pr.left, my = e.clientY - pr.top;
      if (mx >= 0 && my >= 0 && mx <= cssW && my <= cssH) {
        var h = pick(mx, my);
        selectNode(h);
        updateHint();
      }
    }
    drag = null;
  });
  canvas.addEventListener("wheel", function (e) {
    e.preventDefault();
    var pr = canvas.getBoundingClientRect();
    var mx = e.clientX - pr.left, my = e.clientY - pr.top;
    var wx = (mx - view.x) / view.k, wy = (my - view.y) / view.k;
    var k2 = Math.max(0.15, Math.min(9, view.k * Math.exp(-e.deltaY * 0.0016)));
    view.k = k2; view.x = mx - wx * k2; view.y = my - wy * k2;
    draw();
  }, { passive: false });

  document.getElementById("tw-zin").addEventListener("click", function () { zoomBy(1.25); });
  document.getElementById("tw-zout").addEventListener("click", function () { zoomBy(0.8); });
  document.getElementById("tw-fit").addEventListener("click", function () { fit(); draw(); });
  function zoomBy(f) {
    var wx = (cssW / 2 - view.x) / view.k, wy = (cssH / 2 - view.y) / view.k;
    view.k = Math.max(0.15, Math.min(9, view.k * f));
    view.x = cssW / 2 - wx * view.k; view.y = cssH / 2 - wy * view.k;
    draw();
  }

  var FILTER = document.getElementById("tw-filter");
  FILTER.addEventListener("input", function () { state.filter = FILTER.value.trim(); draw(); });

  Array.prototype.forEach.call(document.querySelectorAll('input[name="tw-layout"]'), function (r) {
    r.addEventListener("change", function () {
      if (!r.checked) return;
      state.layout = r.value;
      applyLayout(state.layout, true);
      easing = true; kick();
    });
  });

  // live "showing X / Y" count
  var COUNT = document.getElementById("tw-count");
  function updateCount() {
    if (!COUNT) return;
    var shown = activeNodes().length;
    COUNT.textContent = (shown === TOTAL)
      ? TOTAL + (TOTAL === 1 ? " table" : " tables")
      : shown + " / " + TOTAL + " tables";
  }

  // declutter toggle — show every table vs. only the FK-linked ones
  var SHOWALL = document.getElementById("tw-showall");
  if (LARGE && SHOWALL) {
    SHOWALL.hidden = false;
    var linked = nodes.filter(function (n) { return n.deg >= 1; }).length;
    function labelToggle() {
      SHOWALL.classList.toggle("on", !showAll);
      SHOWALL.textContent = showAll
        ? "FK-linked only (" + linked + ")"
        : "show all " + TOTAL;
      SHOWALL.title = showAll
        ? "hide the " + (TOTAL - linked) + " tables with no foreign keys"
        : "showing only tables that participate in a foreign key";
    }
    labelToggle();
    SHOWALL.addEventListener("click", function () {
      showAll = !showAll;
      labelToggle();
      buildLayouts();
      applyLayout(state.layout, false);
      updateCount(); fit(); draw();
    });
  }
  updateCount();

  // panel → select linked table
  PANEL.addEventListener("click", function (e) {
    var b = e.target.closest("[data-select]");
    if (!b) return;
    var n = byName[b.getAttribute("data-select")];
    if (n) { selectNode(n.id); }
  });

  // ---- hint text ------------------------------------------------------------
  var HINT = document.getElementById("tw-hint");
  function updateHint() {
    HINT.textContent = state.sel != null ? "click empty space to clear" : "scroll = zoom · drag = pan";
  }

  // ---- legend ---------------------------------------------------------------
  function esc(s) { return String(s).replace(/[&<>"]/g, function (m) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m]; }); }
  function renderLegend() {
    if (cssW < 520 || GROUPS.length < 2) { LEGEND.classList.add("hide"); return; }
    LEGEND.classList.remove("hide");
    LEGEND.innerHTML = '<div class="lg-t">prefix groups</div>' + GROUPS.map(function (g) {
      var label = g.name === "other" ? "other" : esc(g.name) + "_";
      return '<div class="lg-row"><span class="lg-dot" style="background:' + g.color + '"></span>' + label + '</div>';
    }).join("");
  }

  // ---- boot -------------------------------------------------------------
  applyLayout(state.layout, false);

  var roIdle = 0;
  var ro = new ResizeObserver(function () {
    resize(); renderLegend();
    clearTimeout(roIdle);
    roIdle = setTimeout(function () { fit(); draw(); }, 60);   // re-frame after resize settles
    draw();
  });
  ro.observe(canvas.parentElement);

  resize(); renderLegend(); fit(); updateHint();
  draw();

  document.addEventListener("visibilitychange", function () { if (!document.hidden) draw(); });
})();
