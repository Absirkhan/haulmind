/* Haulmind client behaviour: theme, keyboard, command palette, toasts,
   decisions with an undo window, local "not now" snoozing. No framework, no build step. */
(function () {
  "use strict";
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
  };
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------- theme ---------- */
  function applyTheme(t) {
    const root = document.documentElement;
    if (t === "light" || t === "dark") root.setAttribute("data-theme", t); else root.removeAttribute("data-theme");
    const dark = root.getAttribute("data-theme") === "dark" ||
      (!root.getAttribute("data-theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
    $$("[data-theme-toggle]").forEach((b) => {
      b.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
      b.title = b.getAttribute("aria-label");
      const use = $("use", b); if (use) use.setAttribute("href", dark ? "/static/icons.svg#sun" : "/static/icons.svg#moon");
    });
  }
  const urlTheme = (location.search.match(/[?&]theme=(dark|light)\b/) || [])[1] || null;
  applyTheme(store.get("theme", null) || urlTheme);
  function toggleTheme() {
    const root = document.documentElement;
    const dark = root.getAttribute("data-theme") === "dark" ||
      (!root.getAttribute("data-theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
    const next = dark ? "light" : "dark";
    store.set("theme", next); applyTheme(next);
  }
  document.addEventListener("click", (e) => { if (e.target.closest("[data-theme-toggle]")) toggleTheme(); });

  /* ---------- toasts ---------- */
  function toast(message, opts) {
    opts = opts || {};
    const region = $("#toasts"); if (!region) return null;
    const el = document.createElement("div");
    el.className = "toast"; el.setAttribute("role", "status");
    el.innerHTML = '<span class="toast-text"></span>';
    $(".toast-text", el).textContent = message;
    if (opts.action) {
      const b = document.createElement("button"); b.className = "btn btn-sm"; b.type = "button"; b.textContent = opts.action.label;
      b.addEventListener("click", () => { opts.action.run(); remove(); });
      el.appendChild(b);
    }
    let timer = null;
    if (opts.ms) {
      const bar = document.createElement("span"); bar.className = "bar"; bar.innerHTML = "<i></i>"; el.appendChild(bar);
      const i = $("i", bar);
      if (!reduced) { i.style.transition = `transform ${opts.ms}ms linear`; requestAnimationFrame(() => { i.style.transform = "scaleX(0)"; }); }
      timer = setTimeout(remove, opts.ms);
    }
    function remove() { if (timer) clearTimeout(timer); el.remove(); if (opts.onClose) opts.onClose(); }
    region.appendChild(el);
    while (region.children.length > 3) region.firstElementChild.remove();
    return { remove, el };
  }
  window.haulmindToast = toast;

  /* ---------- queue: enter animation, snoozes, selection ---------- */
  let knownIds = null;
  document.body.addEventListener("htmx:beforeSwap", (e) => {
    if (e.detail.target && e.detail.target.id === "queue") {
      knownIds = new Set($$("#queue .item").map((el) => el.dataset.id));
    }
  });
  function applySnoozes(root) {
    const snoozed = store.get("snoozed", {});
    const now = Date.now(); let hidden = 0; let changed = false;
    Object.keys(snoozed).forEach((id) => { if (snoozed[id] < now) { delete snoozed[id]; changed = true; } });
    if (changed) store.set("snoozed", snoozed);
    $$(".item", root).forEach((el) => { const on = !!snoozed[el.dataset.id]; el.classList.toggle("snoozed", on); if (on) hidden += 1; });
    const foot = $("#snooze-note");
    if (foot) {
      foot.hidden = hidden === 0;
      const n = $("#snooze-count"); if (n) n.textContent = hidden === 1 ? "1 item hidden until later." : `${hidden} items hidden until later.`;
    }
  }
  document.body.addEventListener("htmx:afterSwap", (e) => {
    const target = e.detail.target;
    if (target && target.id === "queue") {
      if (knownIds && knownIds.size) {
        $$("#queue .item").forEach((el) => { if (!knownIds.has(el.dataset.id)) el.classList.add("is-new"); });
      }
      applySnoozes(target);
    }
    if (target && target.id === "detail") {
      target.classList.add("open");
      const back = $(".detail-back", target);
      if (back) back.addEventListener("click", (ev) => { ev.preventDefault(); target.classList.remove("open"); });
      $$("#queue .item").forEach((el) => el.setAttribute("aria-selected", String(el.dataset.id === target.dataset.selected)));
      window.dispatchEvent(new Event("haulmind:detail"));
    }
  });
  document.addEventListener("DOMContentLoaded", () => {
    applySnoozes(document);
    const d = $("#detail");
    if (d && d.dataset.selected && /[?&]selected=/.test(location.search)) {
      d.classList.add("open");
      const back = $(".detail-back", d);
      if (back) back.addEventListener("click", (ev) => { ev.preventDefault(); d.classList.remove("open"); });
    }
  });
  document.addEventListener("click", (e) => {
    const show = e.target.closest("[data-show-snoozed]");
    if (show) { store.set("snoozed", {}); applySnoozes(document); toast("Showing everything again.", { ms: 2500 }); }
  });

  /* ---------- decisions with an undo window ---------- */
  const UNDO_MS = 6000;
  const pending = new Map(); // actionId -> {url, values, timer, commit}
  function commit(id) {
    const p = pending.get(id); if (!p) return;
    pending.delete(id);
    htmx.ajax("POST", p.url, { target: "#detail", swap: "innerHTML", values: p.values, headers: { "HX-Request": "true" } });
  }
  function commitAllNow() {
    pending.forEach((p, id) => {
      pending.delete(id); if (p.timer) clearTimeout(p.timer);
      const body = new URLSearchParams(p.values);
      try { fetch(p.url, { method: "POST", body, keepalive: true, headers: { "HX-Request": "true" } }); } catch (err) { /* best effort */ }
    });
  }
  window.addEventListener("pagehide", commitAllNow);
  window.addEventListener("beforeunload", commitAllNow);

  function startDecision(btn) {
    const id = btn.dataset.decide; const kind = btn.dataset.kind; const url = btn.dataset.url;
    const detail = $("#detail"); const card = detail ? $(".detail-card", detail) : null;
    const values = {};
    if (kind === "dismiss") {
      const menu = $("#dismiss-menu");
      const chosen = menu ? $("input[name=reason_code]:checked", menu) : null;
      values.reason_code = chosen ? chosen.value : "not_an_issue";
      const note = menu ? $("input[name=reason_text]", menu) : null; values.reason_text = note ? note.value : "";
    } else {
      const note = $("#decision-note"); values.reason_text = note ? note.value : "";
    }
    const row = $(`#queue .item[data-id="${id}"]`);
    const buttons = $$("[data-decide], [data-dismiss-open]", card || document);
    buttons.forEach((b) => { b.disabled = true; });
    if (row) row.classList.add("is-leaving");
    const state = $("#decision-state"); const original = state ? state.innerHTML : "";
    const doing = btn.dataset.doing || "Working on it";
    if (state) { state.hidden = false; state.className = "state-line"; state.innerHTML = `<span>${doing}</span>`; }
    const label = btn.dataset.toast || "Done";
    const entry = { url, values, timer: null };
    pending.set(id, entry);
    const t = toast(label, {
      ms: UNDO_MS,
      action: { label: "Undo", run: () => {
        pending.delete(id); if (entry.timer) clearTimeout(entry.timer);
        buttons.forEach((b) => { b.disabled = false; });
        if (row) row.classList.remove("is-leaving");
        if (state) { state.innerHTML = original; state.hidden = original === ""; }
        toast("Undone. Nothing was changed.", { ms: 2500 });
      } },
      onClose: () => { if (pending.has(id)) commit(id); },
    });
    entry.timer = setTimeout(() => { if (t) t.remove(); else commit(id); }, UNDO_MS + 50);
  }
  document.addEventListener("click", (e) => {
    const open = e.target.closest("[data-dismiss-open]");
    if (open) { const m = $("#dismiss-menu"); if (m) { m.hidden = !m.hidden; if (!m.hidden) { const f = $("input", m); if (f) f.focus(); } } return; }
    const btn = e.target.closest("[data-decide]");
    if (btn && !btn.disabled) { e.preventDefault(); startDecision(btn); return; }
    const later = e.target.closest("[data-not-now]");
    if (later) {
      e.preventDefault();
      const id = later.dataset.notNow; const snoozed = store.get("snoozed", {});
      snoozed[id] = Date.now() + 60 * 60 * 1000; store.set("snoozed", snoozed);
      applySnoozes(document);
      const next = $("#queue .item:not(.snoozed)");
      toast("Hidden for an hour on this screen. Nothing was changed.", { ms: 5000, action: { label: "Undo", run: () => { delete snoozed[id]; store.set("snoozed", snoozed); applySnoozes(document); } } });
      if (next) next.click(); else { const d = $("#detail"); if (d) d.classList.remove("open"); }
    }
  });

  /* ---------- keyboard ---------- */
  function typing(e) {
    const t = e.target; return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
  }
  function visibleItems() { return $$("#queue .item:not(.snoozed)"); }
  function move(delta) {
    const items = visibleItems(); if (!items.length) return;
    let i = items.findIndex((el) => el.getAttribute("aria-selected") === "true");
    i = i < 0 ? 0 : Math.min(items.length - 1, Math.max(0, i + delta));
    items[i].click(); items[i].focus({ preventScroll: false });
    items[i].scrollIntoView({ block: "nearest", behavior: reduced ? "auto" : "smooth" });
  }
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }
    if (typing(e) || e.altKey || e.ctrlKey || e.metaKey) return;
    const dlg = document.querySelector("dialog[open]");
    if (dlg) return;
    switch (e.key) {
      case "j": e.preventDefault(); move(1); break;
      case "k": e.preventDefault(); move(-1); break;
      case "a": { const b = $('#detail [data-decide][data-kind="approve"], #detail [data-decide][data-kind="handled"]'); if (b && !b.disabled) { e.preventDefault(); b.click(); } break; }
      case "d": { const b = $("#detail [data-dismiss-open]"); if (b && !b.disabled) { e.preventDefault(); b.click(); } break; }
      case "n": { const b = $("#detail [data-not-now]"); if (b) { e.preventDefault(); b.click(); } break; }
      case "?": { e.preventDefault(); const s = $("#shortcuts"); if (s) s.showModal(); break; }
      case "Escape": { const d = $("#detail.open"); if (d) d.classList.remove("open"); break; }
      default: break;
    }
  });

  /* ---------- command palette ---------- */
  let commands = [];
  function loadCommands() {
    const data = $("#palette-data");
    try { commands = data ? JSON.parse(data.textContent) : []; } catch (e) { commands = []; }
    commands.push({ label: "Switch light or dark theme", hint: "", run: toggleTheme });
    commands.push({ label: "Show keyboard shortcuts", hint: "?", run: () => { const s = $("#shortcuts"); if (s) s.showModal(); } });
  }
  function openPalette() {
    const dlg = $("#palette"); if (!dlg) return;
    if (!commands.length) loadCommands();
    const input = $("input", dlg); input.value = ""; renderPalette("");
    dlg.showModal(); input.focus();
  }
  function renderPalette(q) {
    const list = $("#palette-list"); if (!list) return;
    const words = q.toLowerCase().split(/\s+/).filter(Boolean);
    const hits = commands.filter((c) => words.every((w) => c.label.toLowerCase().includes(w) || (c.keywords || "").toLowerCase().includes(w))).slice(0, 12);
    list.innerHTML = "";
    if (!hits.length) { const li = document.createElement("li"); li.className = "none"; li.textContent = "Nothing matches. Try a truck number, a driver's name, or a page."; list.appendChild(li); return; }
    hits.forEach((c, i) => {
      const li = document.createElement("li"); li.setAttribute("role", "option"); li.setAttribute("aria-selected", String(i === 0));
      li.innerHTML = '<span class="label"></span><span class="hint"></span>';
      $(".label", li).textContent = c.label; $(".hint", li).textContent = c.hint || "";
      li.addEventListener("click", () => runCommand(c)); list.appendChild(li);
    });
  }
  function runCommand(c) {
    const dlg = $("#palette"); if (dlg) dlg.close();
    if (c.run) c.run(); else if (c.href) window.location.href = c.href;
  }
  document.addEventListener("input", (e) => { if (e.target.matches("#palette input")) renderPalette(e.target.value); });
  document.addEventListener("keydown", (e) => {
    const dlg = $("#palette"); if (!dlg || !dlg.open) return;
    const items = $$("#palette-list li[role=option]"); if (!items.length) return;
    let i = items.findIndex((el) => el.getAttribute("aria-selected") === "true");
    if (e.key === "ArrowDown") { e.preventDefault(); i = Math.min(items.length - 1, i + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); i = Math.max(0, i - 1); }
    else if (e.key === "Enter") { e.preventDefault(); items[Math.max(0, i)].click(); return; }
    else return;
    items.forEach((el, k) => el.setAttribute("aria-selected", String(k === i)));
    items[i].scrollIntoView({ block: "nearest" });
  });
  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-open-palette]")) openPalette();
    if (e.target.closest("[data-close-dialog]")) { const d = e.target.closest("dialog"); if (d) d.close(); }
    const d = e.target.closest("dialog");
    if (d && e.target === d) d.close(); // click on backdrop
  });

  /* ---------- htmx error states ---------- */
  document.body.addEventListener("htmx:responseError", (e) => {
    const target = e.detail.target; if (!target) return;
    if (target.id === "stream") return; // the clock keeps its last reading and retries on its own
    target.innerHTML = '<div class="error-state" role="alert"><span>We couldn\'t load this. Check that Haulmind is still running.</span><button class="btn btn-sm" type="button" onclick="location.reload()">Try again</button></div>';
  });
})();
