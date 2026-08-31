/* SQL Console — CodeMirror wiring + the Ops-terminal interactions.
   Page-local (loaded only by sql.html); nothing here touches the rest of the app. */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };

  var frame = $("#sqlc-frame");
  var host = $("#sql-editor");
  if (!frame || !host || !window.CodeMirror) return;

  /* ---------- schema drawer ---------- */
  var handle = $("#schema-handle");
  function schemaOpen() { return frame.classList.contains("schema-open"); }
  function setSchema(open) {
    frame.classList.toggle("schema-open", open);
    if (handle) handle.setAttribute("aria-expanded", open ? "true" : "false");
    try { localStorage.setItem("odin-sql-schema-open", open ? "1" : "0"); } catch (e) {}
    if (window.cm) setTimeout(function () { window.cm.refresh(); }, 220);
  }
  try { if (localStorage.getItem("odin-sql-schema-open") === "1") setSchema(true); } catch (e) {}
  if (handle) handle.addEventListener("click", function () { setSchema(!schemaOpen()); });

  /* ---------- CodeMirror ---------- */
  var ta = host.querySelector("textarea");
  var tables = {};
  try { tables = JSON.parse(document.getElementById("sql-hint-data").textContent || "{}"); } catch (e) {}

  var cm = CodeMirror.fromTextArea(ta, {
    mode: "text/x-pgsql",
    lineNumbers: true,
    matchBrackets: true,
    autofocus: true,
    indentUnit: 2,
    smartIndent: true,
    lineWrapping: true,
    cursorBlinkRate: 530,
    hintOptions: { tables: tables, completeSingle: false },
    extraKeys: {
      "Cmd-Enter": run,
      "Ctrl-Enter": run,
      "Ctrl-Space": "autocomplete",
      "Tab": function (c) { c.replaceSelection("  "); }
    }
  });
  cm.setSize("100%", 264);
  cm.on("change", function () { cm.save(); });
  cm.on("inputRead", function (c, ev) {
    if (ev && ev.text && /[\w.]/.test(ev.text[0])) c.showHint({ completeSingle: false });
  });
  window.cm = cm;

  function run() {
    cm.save();
    var b = $("#sql-run-btn");
    if (b) b.click();
  }

  /* ---------- keyboard: ⌘B toggles schema, Esc closes ---------- */
  document.addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && (e.key === "b" || e.key === "B")) {
      e.preventDefault();
      setSchema(!schemaOpen());
    } else if (e.key === "Escape" && schemaOpen()) {
      setSchema(false);
    }
  });

  /* ---------- insert table / column names + snippets into the editor ---------- */
  document.body.addEventListener("click", function (e) {
    var ins = e.target.closest("[data-sql-insert]");
    if (ins) {
      e.preventDefault();
      cm.replaceSelection(ins.getAttribute("data-sql-insert"));
      cm.focus();
      setSchema(false);
      return;
    }
    var sn = e.target.closest("[data-sql-snippet]");
    if (sn) {
      e.preventDefault();
      cm.setValue(sn.getAttribute("data-sql-snippet"));
      cm.focus();
    }
  });

  /* ---------- Write mode: shift the console to amber ---------- */
  var wm = $('#sql-form input[name="write_mode"]');
  var sw = $("#sqlc-switch");
  var wlabel = $("#sqlc-wlabel");
  var modePill = $("#sqlc-mode");
  function paintMode() {
    var on = !!(wm && wm.checked);
    frame.classList.toggle("sqlc-write", on);
    if (sw) sw.dataset.on = on ? "1" : "0";
    if (wlabel) wlabel.textContent = on ? "Write" : "Read-only";
    if (modePill) modePill.textContent = on ? "Write mode" : "Read-only";
  }
  if (wm) wm.addEventListener("change", paintMode);
  paintMode();

  /* ---------- running sweep on the toolbar while a query is in flight ---------- */
  var toolbar = $("#sqlc-toolbar");
  var runBtn = $("#sql-run-btn");
  document.body.addEventListener("htmx:beforeRequest", function (e) {
    if (e.target && e.target.id === "sql-form" && toolbar) {
      toolbar.classList.add("busy");
      if (runBtn) runBtn.classList.add("running");
    }
  });
  document.body.addEventListener("htmx:afterRequest", function (e) {
    if (e.target && e.target.id === "sql-form" && toolbar) {
      toolbar.classList.remove("busy");
      if (runBtn) runBtn.classList.remove("running");
    }
  });

  /* ---------- Danger zone: split "source::table" into the hidden inputs ---------- */
  var pick = $("#dereg-pick");
  if (pick) {
    pick.addEventListener("change", function () {
      var v = (pick.value || "").split("::");
      $("#dereg-sid").value = v[0] || "";
      $("#dereg-tname").value = v[1] || "";
      var box = $('#dereg-form input[name="confirm"]');
      if (box && v[0]) box.placeholder = "type " + v[0] + "." + v[1] + " to confirm";
    });
  }
})();
