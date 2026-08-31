/* Odin UI glue: toasts, modal, drag-drop upload, theme, relative time,
   and htmx integration (styled confirm, busy buttons, server-driven toasts). */
(function () {
  "use strict";
  const Odin = (window.Odin = {});

  /* ---- theme ---------------------------------------------------------- */
  try {
    const saved = localStorage.getItem("odin-theme");
    if (saved) document.documentElement.setAttribute("data-theme", saved);
  } catch (e) {}
  Odin.toggleTheme = function () {
    const cur = document.documentElement.getAttribute("data-theme");
    const dark = cur
      ? cur === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("odin-theme", next); } catch (e) {}
  };

  /* ---- toasts ------------------------------------------------------- */
  function toastHost() {
    let h = document.getElementById("toasts");
    if (!h) {
      h = document.createElement("div");
      h.id = "toasts";
      document.body.appendChild(h);
    }
    return h;
  }
  Odin.toast = function (message, kind) {
    if (!message) return;
    kind = kind || "info";
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.innerHTML =
      '<div>' + escapeHtml(message) + "</div>" +
      '<span class="x" aria-label="dismiss">&times;</span>';
    const kill = () => {
      el.classList.add("leaving");
      setTimeout(() => el.remove(), 250);
    };
    el.querySelector(".x").addEventListener("click", kill);
    toastHost().appendChild(el);
    setTimeout(kill, 4800);
  };

  /* ---- modal ------------------------------------------------------ */
  function modalRoot() {
    let r = document.getElementById("modal-root");
    if (!r) {
      r = document.createElement("div");
      r.id = "modal-root";
      document.body.appendChild(r);
    }
    return r;
  }
  Odin.closeModal = function () { modalRoot().innerHTML = ""; };
  Odin.openModal = function (html, opts) {
    opts = opts || {};
    const root = modalRoot();
    root.innerHTML =
      '<div class="modal-backdrop" data-close>' +
        '<div class="modal ' + (opts.size || "") + '" role="dialog" aria-modal="true">' +
          html +
        "</div>" +
      "</div>";
    root.querySelector("[data-close]").addEventListener("click", (e) => {
      if (e.target.hasAttribute("data-close")) Odin.closeModal();
    });
    if (window.htmx) window.htmx.process(root);
  };
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") Odin.closeModal();
  });
  // any element with [data-modal-close] inside the modal closes it
  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-modal-close]")) Odin.closeModal();
  });

  Odin.confirm = function (opts) {
    return new Promise((resolve) => {
      Odin.openModal(
        '<div class="modal-head"><h3>' + escapeHtml(opts.title || "Are you sure?") +
          '</h3><div class="grow"></div>' +
          '<button class="icon-btn" data-modal-close aria-label="close">&times;</button></div>' +
        '<div class="modal-body">' + (opts.body || "") + "</div>" +
        '<div class="modal-foot">' +
          '<button class="btn ghost" data-modal-close>Cancel</button>' +
          '<button class="btn ' + (opts.danger ? "danger" : "") + '" data-ok>' +
            escapeHtml(opts.confirmLabel || "Confirm") + "</button>" +
        "</div>",
        { size: "sm" }
      );
      modalRoot().querySelector("[data-ok]").addEventListener("click", () => {
        Odin.closeModal();
        resolve(true);
      });
      const bd = modalRoot().querySelector("[data-close]");
      bd.addEventListener("click", (e) => {
        if (e.target.hasAttribute("data-close")) resolve(false);
      });
    });
  };

  /* ---- copy-to-clipboard (generated SQL blocks) ---------------- */
  Odin.copy = function (btn) {
    var wrap = btn.closest(".sqlwrap");
    var pre = wrap && wrap.querySelector(".sqlblock");
    if (!pre) return;
    var done = function () {
      var old = btn.textContent;
      btn.textContent = "copied";
      btn.classList.add("copied");
      setTimeout(function () {
        btn.textContent = old;
        btn.classList.remove("copied");
      }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(pre.textContent).then(done, function () {
        Odin.toast("Copy failed — select the text manually", "err");
      });
    } else {
      var r = document.createRange();
      r.selectNodeContents(pre);
      var sel = getSelection();
      sel.removeAllRanges();
      sel.addRange(r);
      try { document.execCommand("copy"); done(); } catch (e) {}
      sel.removeAllRanges();
    }
  };

  /* ---- drag & drop upload --------------------------------------- */
  Odin.wireDropzone = function (zone) {
    const input = zone.querySelector('input[type=file]');
    const label = zone.querySelector("[data-dropzone-label]");
    if (!input) return;
    const form = zone.matches("form") ? zone : zone.closest("form");
    const autosubmit = form && form.hasAttribute("data-autosubmit");
    const show = () => {
      if (input.files && input.files.length) {
        const f = input.files[0];
        label.innerHTML =
          '<span class="file-chip">📄 ' + escapeHtml(f.name) +
          ' <span class="muted">' + fmtBytes(f.size) + "</span></span>";
        if (autosubmit) {
          if (form.requestSubmit) form.requestSubmit();
          else form.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
        }
      }
    };
    zone.addEventListener("click", (e) => {
      if (e.target.tagName !== "INPUT") input.click();
    });
    input.addEventListener("change", show);
    ["dragenter", "dragover"].forEach((ev) =>
      zone.addEventListener(ev, (e) => {
        e.preventDefault();
        zone.classList.add("drag");
      })
    );
    ["dragleave", "drop"].forEach((ev) =>
      zone.addEventListener(ev, (e) => {
        e.preventDefault();
        zone.classList.remove("drag");
      })
    );
    zone.addEventListener("drop", (e) => {
      if (e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files;
        show();
      }
    });
  };

  /* ---- command-bar UTC clock --------------------------------- */
  function tickClock() {
    var el = document.getElementById("odin-clock");
    if (!el) return;
    var d = new Date();
    var p = function (n) { return String(n).padStart(2, "0"); };
    el.textContent =
      p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + ":" + p(d.getUTCSeconds()) + " UTC";
  }

  /* ---- sparklines --------------------------------------------- */
  var SPARK_HUE = {
    ok: "var(--ok)", warn: "var(--warn)", info: "var(--info)", signal: "var(--accent)",
  };
  function drawSparks(scope) {
    (scope || document).querySelectorAll(".spark").forEach(function (el) {
      if (el.dataset.drawn) return;
      var pts = (el.dataset.pts || "").split(",").map(Number)
        .filter(function (n) { return !isNaN(n); });
      el.dataset.drawn = "1";
      if (pts.length < 2) return;
      var w = 160, h = 34, pad = 3;
      var min = Math.min.apply(null, pts), max = Math.max.apply(null, pts);
      var span = max - min || 1;
      var step = (w - pad * 2) / (pts.length - 1);
      var xy = pts.map(function (v, i) {
        return [pad + i * step, pad + (h - pad * 2) * (1 - (v - min) / span)];
      });
      var line = xy.map(function (p, i) {
        return (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1);
      }).join(" ");
      var area = line + " L " + xy[xy.length - 1][0].toFixed(1) + " " + h +
        " L " + xy[0][0].toFixed(1) + " " + h + " Z";
      var col = SPARK_HUE[el.dataset.hue] || "var(--accent)";
      var uid = "sp" + Math.random().toString(36).slice(2, 8);
      var last = xy[xy.length - 1];
      el.innerHTML =
        '<svg viewBox="0 0 ' + w + " " + h + '" preserveAspectRatio="none" width="100%" height="' + h + '">' +
          '<defs><linearGradient id="' + uid + '" x1="0" x2="0" y1="0" y2="1">' +
            '<stop offset="0" stop-color="' + col + '" stop-opacity="0.28"/>' +
            '<stop offset="1" stop-color="' + col + '" stop-opacity="0"/>' +
          "</linearGradient></defs>" +
          '<path d="' + area + '" fill="url(#' + uid + ')"/>' +
          '<path d="' + line + '" fill="none" stroke="' + col + '" stroke-width="1.4" vector-effect="non-scaling-stroke"/>' +
          '<circle cx="' + last[0].toFixed(1) + '" cy="' + last[1].toFixed(1) + '" r="2" fill="' + col + '"/>' +
        "</svg>";
    });
  }

  /* ---- relative time ------------------------------------------- */
  function tickReltime() {
    document.querySelectorAll("time.reltime[datetime]").forEach((el) => {
      const t = Date.parse(el.getAttribute("datetime"));
      if (!isNaN(t)) el.textContent = relTime(t);
    });
  }
  Odin.relTime = relTime;
  function relTime(t) {
    const s = Math.round((Date.now() - t) / 1000);
    if (s < 5) return "just now";
    if (s < 60) return s + "s ago";
    const m = Math.round(s / 60);
    if (m < 60) return m + "m ago";
    const h = Math.round(m / 60);
    if (h < 24) return h + "h ago";
    const d = Math.round(h / 24);
    return d + "d ago";
  }

  /* ---- helpers ---------------------------------------------------- */
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }
  function fmtBytes(n) {
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }

  /* ---- htmx integration ----------------------------------------- */
  // styled confirm: elements with [hx-confirm] pop our modal instead of window.confirm
  document.body.addEventListener("htmx:confirm", function (e) {
    if (!e.detail.question) return;
    e.preventDefault();
    const danger = e.detail.elt.classList.contains("danger");
    Odin.confirm({
      title: e.detail.question,
      confirmLabel: e.detail.elt.getAttribute("data-confirm-label") || "Confirm",
      danger: danger,
    }).then((ok) => { if (ok) e.detail.issueRequest(true); });
  });
  // busy state on the triggering button
  document.body.addEventListener("htmx:beforeRequest", function (e) {
    const t = e.detail.elt;
    if (t && t.classList && t.classList.contains("btn")) t.classList.add("is-busy");
  });
  document.body.addEventListener("htmx:afterRequest", function (e) {
    const t = e.detail.elt;
    if (t && t.classList) t.classList.remove("is-busy");
  });
  // server-driven toast / modal-close via HX-Trigger response header
  document.body.addEventListener("odin:toast", function (e) {
    const d = e.detail || {};
    Odin.toast(d.message, d.kind);
  });
  document.body.addEventListener("odin:close-modal", Odin.closeModal);
  // transport / 5xx errors (routes normally return 200 + a toast, so this is rare)
  document.body.addEventListener("htmx:responseError", function (e) {
    Odin.toast("Request failed (" + e.detail.xhr.status + ")", "err");
  });
  document.body.addEventListener("htmx:sendError", function () {
    Odin.toast("Network error — is the server running?", "err");
  });
  // re-wire dropzones and reltimes after swaps
  document.body.addEventListener("htmx:load", init);

  function init(scope) {
    var root = scope && scope.target ? scope.target : document;
    root.querySelectorAll(".dropzone").forEach(Odin.wireDropzone);
    drawSparks(root);
    tickReltime();
  }
  document.addEventListener("DOMContentLoaded", () => {
    init();
    tickClock();
    setInterval(tickReltime, 30000);
    setInterval(tickClock, 1000);
  });
})();
