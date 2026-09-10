/* os_client page 04 — Buku Stok: the data wiring (installment 4a the ledger
 * floors + THE PUSH-OUTCOME SEAM, owner order 2026-08-24).
 *
 * STRUCTURE IS STATIC, DATA IS LIVE (the forge's serving doctrine). The DOM
 * arrived pre-built from the forged artifacts (components-buku-stok/); this
 * file fills it from /api/stok/overview (+ /days for scroll, +
 * /movement-detail lazily) and renders the day-grouped ledger as runtime DOM
 * (BLUEPRINT + build_buku_stok.py's own module docstring: the day-grouping
 * axis does not fit LX.table's column-band grouping, so this page's ledger
 * is NOT rendered through LX.table — a forged day-list host, runtime rows).
 *
 * INHERITED MACHINERY (BLUEPRINT "consume, never rewrite"): every row-
 * template clone goes through `LX.table.uniquifyClone` (the duplicate-id
 * law Produk's heal #3 promoted to the shared lib). THE PUSH-OUTCOME SEAM
 * adds this page's first mutation — `POST /api/stok/movement/{ref}/retry-
 * push` — gated through `LX.confirm` (`ledger_retry_confirm`, ONE dl02
 * dialog every row's retry button shares), same discipline produk.js's
 * judul-approve confirm established.
 */
(function () {
  "use strict";

  var UNMEASURED = "belum terukur";
  var state = { overview: null, dayGroups: [], oldestLoadedDate: null,
    hasMoreDays: false, direction: "semua", searchTerm: "" };

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }

  function text(value) {
    return (value === null || value === undefined || value === "")
      ? UNMEASURED : String(value);
  }

  function money(value) {
    if (value === null || value === undefined) { return UNMEASURED; }
    return window.LX && window.LX.fmt ? window.LX.fmt.money(value) : String(value);
  }

  function setField(root, name, value) {
    $$('[data-field="' + name + '"]', root).forEach(function (el) {
      el.textContent = text(value);
    });
  }

  function unhide(el) {
    if (!el) { return; }
    var node = el.querySelector("[data-component]") || el;
    node.hidden = false;
    el.hidden = false;
  }

  function hide(el) {
    if (!el) { return; }
    var node = el.querySelector("[data-component]") || el;
    node.hidden = true;
    el.hidden = true;
  }

  function toastReceipt(title, receipt, severity) {
    if (window.LX && window.LX.toast) {
      window.LX.toast.receipt(title, receipt, severity || "ok");
    }
  }

  function toastNotify(title, message, severity) {
    if (window.LX && window.LX.toast) {
      window.LX.toast.notify(title, message, severity || "ok");
    }
  }

  /* ── the duplicate-id law (INHERITED MACHINERY) ──────────────────────── */
  function uniquifyClone(node, suffix) {
    return window.LX.table.uniquifyClone(node, suffix);
  }

  /* ── the row-control templates: cloned PER MOVEMENT ──────────────────── */
  function rowControlTemplate(key, suffix) {
    var host = $("#lx-buku-stok-row-controls");
    if (!host) { return null; }
    var node = host.content.querySelector('[data-control="' + key + '"]');
    if (!node) { return null; }
    var clone = node.cloneNode(true);
    return suffix ? uniquifyClone(clone, suffix) : clone;
  }

  /* ── the sync-ribbon (BLUEPRINT: dot + ONE sentence + <=1 action, HIDDEN
     when clean — a probe witness pins hidden-on-clean) ─────────────────── */
  function applySyncRibbon(sync) {
    var dot = $('[data-control="sync_ribbon_dot"]');
    if (dot) {
      var comp = dot.querySelector("[data-component]") || dot;
      comp.setAttribute("data-variant",
