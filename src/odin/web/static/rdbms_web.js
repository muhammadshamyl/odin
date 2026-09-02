/* Odin — RDBMS "table web": a canvas graph of a schema's tables and their
   foreign keys, with a live preview panel.

   Layout is fully DETERMINISTIC (no physics sim — that degenerated to a line on
   sparse / star / two-group schemas). A FK graph is a forest of small
   hierarchies plus a pile of unconnected tables, so:
     - WEB      = split into connected components; each component is a radial
                  tree from its most-referenced table; all FK-less tables go in
                  one grid; the pieces are shelf-packed. Same result every run.
     - RING     = every table on one circle, ordered by prefix group.
     - CLUSTERS = one small grid per prefix group, groups around a ring.
   Rendering: static canvas, redrawn on interaction only; a bounded rAF runs
   just for the layout-switch ease and the selection pulse. Nodes are coloured
   by group — table-name prefix when that yields real groups, otherwise FK
   connected component; a 16-hue curated palette for the biggest groups and a
   hashed HSL for the long tail (never a flat grey). Theme colours are read
   from CSS custom properties. */
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

  // ---- colour groups --------------------------------------------------------
  // Colour is purely visual — it never drives the layout. Every group gets a
  // colour: a curated, well-separated palette for the biggest groups, then a
  // deterministic HSL hashed from the group key for the long tail. No grey
  // "other" bucket. Grouping strategy is auto-picked:
  //   * table-name prefix (before the first "_") when that yields real groups;
  //   * otherwise the connected components of the FK graph (so a schema whose
  //     tables have no shared prefixes still colours by "what relates to what").
  var PALETTE = [
    "#46c7f0", "#3fd08b", "#b98cff", "#f2b445", "#6aa8ff", "#ff6f6b",
    "#8bd450", "#f078c8", "#4de0c8", "#ffa24d", "#9db4ff", "#d98cf0",
    "#8fd98c", "#f2c14d", "#ff8fa8", "#6fd3ff"
  ];
  function hashHue(s) {
    var h = 0;
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
    return ((h % 360) + 360) % 360;
  }
  function hslHex(h, s, l) {
    h /= 360;
    var q = l < 0.5 ? l * (1 + s) : l + s - l * s, p = 2 * l - q;
    function ch(t) {
      if (t < 0) t += 1; if (t > 1) t -= 1;
      var v = t < 1 / 6 ? p + (q - p) * 6 * t
            : t < 1 / 2 ? q
            : t < 2 / 3 ? p + (q - p) * (2 / 3 - t) * 6 : p;
      return Math.round(v * 255);
    }
    return "#" + ((1 << 24) + (ch(h + 1 / 3) << 16) + (ch(h) << 8) + ch(h - 1 / 3)).toString(16).slice(1);
  }
  function groupColor(rank, key) {
    // keep to hex — the rgba() helper only parses hex
    return rank < PALETTE.length ? PALETTE[rank]
      : hslHex((hashHue(key) + rank * 47) % 360, 0.58, 0.66);
  }

  function prefixKey(n) {
    var p = n.name.split("_")[0];
    return (p && p !== n.name ? p : n.name).toLowerCase();
  }

  var GROUPS, GROUP_MODE;
  (function assignGroups() {
    // strategy A — prefix
    var pc = {};
    nodes.forEach(function (n) { var k = prefixKey(n); pc[k] = (pc[k] || 0) + 1; });
    var pkeys = Object.keys(pc);
    var shared = pkeys.filter(function (k) { return pc[k] >= 2; });
    var coverage = shared.reduce(function (s, k) { return s + pc[k]; }, 0) / nodes.length;

    if (shared.length >= 2 && coverage >= 0.35) {
      GROUP_MODE = "prefix";
      var ranked = pkeys.sort(function (a, b) { return pc[b] - pc[a] || a.localeCompare(b); });
      var ix = {}; ranked.forEach(function (k, i) { ix[k] = i; });
      GROUPS = ranked.map(function (k, i) {
        return { key: k, label: k + "_", color: groupColor(i, k), n: pc[k] };
      });
      nodes.forEach(function (n) {
        var k = prefixKey(n);
        n.g = ix[k]; n.gp = k; n.color = GROUPS[ix[k]].color;
      });
      return;
    }

    // strategy B — FK connected components
    GROUP_MODE = "component";
    var uf = nodes.map(function (_, i) { return i; });
    function find(x) { while (uf[x] !== x) { uf[x] = uf[uf[x]]; x = uf[x]; } return x; }
    links.forEach(function (l) { uf[find(l.a)] = find(l.b); });
    var comp = {};
    nodes.forEach(function (n) { var r = find(n.id); (comp[r] = comp[r] || []).push(n); });
    var multi = [], iso = [];
    Object.keys(comp).forEach(function (r) { (comp[r].length > 1 ? multi : iso).push(comp[r]); });
    multi.sort(function (a, b) { return b.length - a.length; });
    GROUPS = multi.map(function (members, i) {
      var hub = members.reduce(function (h, m) { return m.deg > h.deg ? m : h; }, members[0]);
      var g = { key: "c" + i, label: hub.name + (members.length > 1 ? " +" + (members.length - 1) : ""),
                color: groupColor(i, "c" + i + hub.name), n: members.length };
      members.forEach(function (m) { m.g = i; m.gp = g.key; m.color = g.color; });
      return g;
    });
    var isoNodes = iso.reduce(function (a, c) { return a.concat(c); }, []);
    if (isoNodes.length) {
      var gi = GROUPS.length;
      // one legend entry, but tint each by its name so name-families still read
      GROUPS.push({ key: "unlinked", label: "unlinked", color: "#8aa0b3", n: isoNodes.length });
      isoNodes.forEach(function (m) {
        m.g = gi; m.gp = "unlinked";
        m.color = hslHex(hashHue(prefixKey(m)), 0.34, 0.6);   // muted, tinted by name
      });
    }
  })();
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

  // ---- layouts (all deterministic — no physics) --------------------------
  // A FK graph is almost always a forest of small hierarchies plus a lot of
  // unconnected tables. So: split into connected components, lay each one out as
  // a radial tree from its most-referenced table, drop every FK-less table into
  // one tidy grid, then shelf-pack the pieces. Same result every time, on any
  // schema shape — worst case is a boring grid, never a broken line.
  var GAP = 34;

  function components(list) {
    var uf = {};
    list.forEach(function (n) { uf[n.id] = n.id; });
    function find(x) { while (uf[x] !== x) { uf[x] = uf[uf[x]]; x = uf[x]; } return x; }
    links.forEach(function (l) {
      if (uf[l.a] != null && uf[l.b] != null) uf[find(l.a)] = find(l.b);
    });
    var g = {};
    list.forEach(function (n) { var r = find(n.id); (g[r] = g[r] || []).push(n.id); });
    return Object.keys(g).map(function (k) { return g[k]; })
      .sort(function (a, b) { return b.length - a.length; });
  }

  function ordCmp(A, aid, bid) {
    return (A[aid].g - A[bid].g) || A[aid].name.localeCompare(A[bid].name);
  }

  // one connected component -> a radial tree, centred on (0,0)
  function radialBox(comp, A) {
    if (comp.length === 1) return { ids: comp, w: GAP + 8, h: GAP + 8, local: (function () { var o = {}; o[comp[0]] = { x: 0, y: 0 }; return o; })() };
    var root = comp.reduce(function (r, id) { return A[id].deg > A[r].deg ? id : r; }, comp[0]);
    var rank = {}, seen = {}, q = [root];
    rank[root] = 0; seen[root] = 1;
    while (q.length) {
      var u = q.shift();
      (adj[u] || []).forEach(function (e) {
        if (!A[e.o] || seen[e.o]) return;
        seen[e.o] = 1; rank[e.o] = rank[u] + 1; q.push(e.o);
      });
    }
    comp.forEach(function (id) { if (rank[id] == null) rank[id] = 1; });   // safety for odd graphs
    var byRank = {};
    comp.forEach(function (id) { (byRank[rank[id]] = byRank[rank[id]] || []).push(id); });
    var local = {}, maxR = 0;
    Object.keys(byRank).map(Number).sort(function (a, b) { return a - b; }).forEach(function (rk) {
      var ring = byRank[rk].sort(function (a, b) { return ordCmp(A, a, b); });
      if (rk === 0 && ring.length === 1) { local[ring[0]] = { x: 0, y: 0 }; return; }
      // radius grows per rank but tapers, so a deep chain coils instead of
      // shooting off in a 2000px spike
      var base = rk <= 5 ? rk * 120 : 600 + (rk - 5) * 44;
      var rad = Math.max((rk ? base : 70), ring.length * 46 / (2 * Math.PI));
      maxR = Math.max(maxR, rad);
      var off = (rk % 2) ? Math.PI / ring.length : 0;
      ring.forEach(function (id, i) {
        var ang = (i / ring.length) * Math.PI * 2 - Math.PI / 2 + off;
        local[id] = { x: Math.cos(ang) * rad, y: Math.sin(ang) * rad };
      });
    });
    return { ids: comp, w: maxR * 2 + 56, h: maxR * 2 + 56, local: local };
  }

  // every FK-less table -> one grid block
  function gridBox(ids, A) {
    ids = ids.slice().sort(function (a, b) { return ordCmp(A, a, b); });
    var cols = Math.max(1, Math.ceil(Math.sqrt(ids.length * 1.7)));
    var g = 30, local = {};
    ids.forEach(function (id, i) { local[id] = { x: (i % cols) * g, y: Math.floor(i / cols) * g }; });
    var w = (cols - 1) * g, h = (Math.ceil(ids.length / cols) - 1) * g;
    ids.forEach(function (id) { local[id].x -= w / 2; local[id].y -= h / 2; });
    return { ids: ids, w: w + 44, h: h + 44, local: local };
  }

  function packBoxes(boxes) {
    boxes.sort(function (a, b) { return Math.max(b.w, b.h) - Math.max(a.w, a.h); });
    var area = boxes.reduce(function (s, b) { return s + (b.w + GAP) * (b.h + GAP); }, 0);
    var targetW = Math.max(560, Math.sqrt(area) * 1.5);
    var x = 0, y = 0, shelfH = 0, out = {};
    boxes.forEach(function (b) {
      if (x > 0 && x + b.w > targetW) { x = 0; y += shelfH + GAP; shelfH = 0; }
      var ox = x + b.w / 2, oy = y + b.h / 2;
      b.ids.forEach(function (id) { out[id] = { x: b.local[id].x + ox, y: b.local[id].y + oy }; });
      x += b.w + GAP; shelfH = Math.max(shelfH, b.h);
    });
    var xs = [], ys = [];
    Object.keys(out).forEach(function (id) { xs.push(out[id].x); ys.push(out[id].y); });
    var cx = (Math.min.apply(null, xs) + Math.max.apply(null, xs)) / 2;
    var cy = (Math.min.apply(null, ys) + Math.max.apply(null, ys)) / 2;
    Object.keys(out).forEach(function (id) { out[id].x -= cx; out[id].y -= cy; });
    return out;
  }

  function layoutStructural(list) {
    var A = {}; list.forEach(function (n) { A[n.id] = n; });
    var comps = components(list);
    var boxes = [], singles = [];
    comps.forEach(function (c) { if (c.length === 1) singles.push(c[0]); else boxes.push(radialBox(c, A)); });
    if (singles.length) boxes.push(gridBox(singles, A));
    if (!boxes.length) return {};
    return packBoxes(boxes);
  }

  // RING — every table on one circle, grouped so colours arc together
  function layoutRing(list) {
    var order = list.slice().sort(function (a, b) { return (a.g - b.g) || a.name.localeCompare(b.name); });
    var R = Math.max(200, order.length * 6), pos = {};
    order.forEach(function (n, i) {
      var t = (i / order.length) * Math.PI * 2 - Math.PI / 2;
      pos[n.id] = { x: Math.cos(t) * R, y: Math.sin(t) * R, a: t };
    });
    return pos;
  }

  // CLUSTERS — one grid per prefix group, groups placed around a ring
  function layoutGrid(list) {
    var pos = {}, groupsUsed = [];
    for (var gi = 0; gi < NG; gi++) if (list.some(function (n) { return n.g === gi; })) groupsUsed.push(gi);
    var R = Math.max(320, groupsUsed.length * 70);
    groupsUsed.forEach(function (gi, gk) {
      var mem = list.filter(function (n) { return n.g === gi; })
        .sort(function (a, b) { return a.name.localeCompare(b.name); });
      var ang = (gk / groupsUsed.length) * Math.PI * 2 - Math.PI / 2;
      var cx = Math.cos(ang) * R, cy = Math.sin(ang) * R * 0.82;
      var cols = Math.max(1, Math.ceil(Math.sqrt(mem.length * 1.4)));
      var rows = Math.ceil(mem.length / cols), gap = 26;
      mem.forEach(function (n, i) {
        pos[n.id] = {
          x: cx + ((i % cols) - (cols - 1) / 2) * gap,
          y: cy + (Math.floor(i / cols) - (rows - 1) / 2) * gap
        };
      });
    });
    return pos;
  }

  var LAYOUTS = {};
  function buildLayouts() {
    var list = activeNodes();
    LAYOUTS = { web: layoutStructural(list), ring: layoutRing(list), grid: layoutGrid(list) };
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
  // listen on the whole stage (not just the canvas) so a wheel over the legend /
  // zoom / hint chips is captured too and never scrolls the page
  canvas.parentElement.addEventListener("wheel", function (e) {
    e.preventDefault();
    e.stopPropagation();
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
  var LEGEND_MAX = 12;
  function renderLegend() {
    if (cssW < 520 || GROUPS.length < 2) { LEGEND.classList.add("hide"); return; }
    LEGEND.classList.remove("hide");
    var ordered = GROUPS.slice().sort(function (a, b) { return b.n - a.n; });
    var shown = ordered.slice(0, LEGEND_MAX);
    var title = GROUP_MODE === "prefix" ? "prefix groups" : "linked groups";
    var rows = shown.map(function (g) {
      return '<div class="lg-row"><span class="lg-dot" style="background:' + g.color + '"></span>' +
             esc(g.label) + '<span class="lg-n">' + g.n + '</span></div>';
    }).join("");
    if (ordered.length > LEGEND_MAX) {
      rows += '<div class="lg-row lg-more">+' + (ordered.length - LEGEND_MAX) + ' more</div>';
    }
    LEGEND.innerHTML = '<div class="lg-t">' + title + '</div>' + rows;
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
