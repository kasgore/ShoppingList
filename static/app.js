// Register the service worker so the app can be installed on the iPhone /
// Android home screen and survive flaky kitchen Wi-Fi.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { scope: "/" })
      .catch((err) => console.warn("SW registration failed:", err));
  });
}

// ---------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------

// Per-page-load id. Sent on every list-mutation request via X-Client-Id
// so the SSE stream can tell this tab to ignore its own echoed event
// (we already updated the DOM optimistically when we made the change).
const KASA_CLIENT_ID = (() => {
  try { return crypto.randomUUID(); }
  catch (e) { return Date.now().toString(36) + Math.random().toString(36).slice(2); }
})();

function isHomePath() {
  return location.pathname === "/" || location.pathname === "/index" || location.pathname === "";
}

// Cook Mode: full-screen step-by-step recipe view with Wake Lock so the
// screen stays on while hands are messy. Wake Lock is stable on iOS 16.4+
// in installed PWAs and Chrome Android since 2021. Falls back gracefully
// (the page works, the screen just sleeps normally).
(function setupCookMode() {
  const root = document.getElementById("cook-mode");
  if (!root) return;
  const dataNode = document.getElementById("cook-steps-data");
  const steps = dataNode ? JSON.parse(dataNode.textContent || "[]") : [];
  if (!steps.length) return;

  const stepText = document.getElementById("cook-step-text");
  const counter = document.getElementById("cook-current");
  const prevBtn = document.getElementById("cook-prev");
  const nextBtn = document.getElementById("cook-next");
  const progress = document.getElementById("cook-progress");
  const wakeIndicator = document.getElementById("cook-wake-indicator");
  let current = 0;

  function render() {
    stepText.textContent = steps[current];
    // Re-annotate this step's text for tappable timers. textContent was
    // just replaced, so clear the "already done" marker first. The timer
    // module sets window.__kasaAnnotateTimers once it has run; for the
    // very first render (before that) the timer module's own DOM-ready
    // pass handles #cook-step-text.
    delete stepText.dataset.timersDone;
    if (window.__kasaAnnotateTimers) window.__kasaAnnotateTimers(stepText);
    counter.textContent = String(current + 1);
    progress.value = current + 1;
    prevBtn.disabled = current === 0;
    nextBtn.disabled = current === steps.length - 1;
  }
  function go(delta) {
    const next = current + delta;
    if (next < 0 || next >= steps.length) return;
    current = next;
    render();
  }

  prevBtn.addEventListener("click", () => go(-1));
  nextBtn.addEventListener("click", () => go(1));

  // Keyboard navigation for desktop / iPad with keyboard.
  document.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") go(-1);
    else if (e.key === "ArrowRight") go(1);
  });

  // Touch-swipe navigation. Threshold is 60 px to avoid accidental swipes
  // while scrolling the ingredient panel.
  let touchStartX = null;
  let touchStartY = null;
  root.addEventListener("touchstart", (e) => {
    touchStartX = e.touches[0].clientX;
    touchStartY = e.touches[0].clientY;
  }, { passive: true });
  root.addEventListener("touchend", (e) => {
    if (touchStartX == null) return;
    const dx = e.changedTouches[0].clientX - touchStartX;
    const dy = e.changedTouches[0].clientY - touchStartY;
    // Only trigger on mostly-horizontal swipes — vertical scrolling
    // shouldn't change the step.
    if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) {
      if (dx < 0) go(1);
      else go(-1);
    }
    touchStartX = null;
    touchStartY = null;
  }, { passive: true });

  // Wake Lock — keep the screen on while cooking.
  let wakeLock = null;
  async function requestWakeLock() {
    if (!("wakeLock" in navigator)) {
      if (wakeIndicator) wakeIndicator.textContent = "screen may sleep";
      return;
    }
    try {
      wakeLock = await navigator.wakeLock.request("screen");
      if (wakeIndicator) wakeIndicator.textContent = "screen-on ⚡";
      wakeLock.addEventListener("release", () => {
        if (wakeIndicator) wakeIndicator.textContent = "screen released";
      });
    } catch (err) {
      console.warn("Wake lock failed:", err);
      if (wakeIndicator) wakeIndicator.textContent = "screen lock denied";
    }
  }
  // Browsers release the wake lock when the tab is hidden. Re-request
  // when it becomes visible again.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      requestWakeLock();
    }
  });
  requestWakeLock();

  render();
})();

// ---------------------------------------------------------------------
// Real-time list sync + AJAX list mutations.
//
// When the shopping list changes — from another device, or from one of
// our own .js-list-mutate forms — we patch just the affected regions of
// the home page in place (#list-body, #quick-add-region, #on-list-region).
// No full reload, so scroll position and anything half-typed in the cards
// above survive. Events echoed back from *this* tab's own changes are
// ignored (we already updated the DOM optimistically).
// ---------------------------------------------------------------------

// Swap the home-page list regions from a freshly-rendered "/" document.
// Returns false if the HTML doesn't look like the home page (login
// redirect, error page, etc.) so callers can fall back to a real nav.
function applyHomeHtml(htmlText, opts) {
  opts = opts || {};
  let doc;
  try { doc = new DOMParser().parseFromString(htmlText, "text/html"); }
  catch (e) { return false; }
  if (!doc.getElementById("list-body")) return false;
  const swapInner = (id) => {
    const fresh = doc.getElementById(id);
    const cur = document.getElementById(id);
    if (fresh && cur) cur.innerHTML = fresh.innerHTML;
  };
  swapInner("list-body");
  swapInner("quick-add-region");
  swapInner("on-list-region");
  if (opts.flashes) {
    const fresh = doc.getElementById("flashes");
    const cur = document.getElementById("flashes");
    if (fresh && cur) { cur.innerHTML = fresh.innerHTML; scheduleFlashFade(); }
  }
  updateListCounters();
  return true;
}

let _homeRefreshInFlight = false;
async function refreshHomeFromServer() {
  if (_homeRefreshInFlight || !isHomePath()) return;
  _homeRefreshInFlight = true;
  try {
    const r = await fetch("/", { headers: { "X-Requested-With": "fetch" } });
    if (r.ok) applyHomeHtml(await r.text(), { flashes: false });
  } catch (e) { /* offline / transient — retry on the next event */ }
  finally { _homeRefreshInFlight = false; }
}

// .js-list-mutate forms: POST in the background, then patch the home
// regions from the redirected "/" response.
async function submitListForm(form) {
  const btns = form.querySelectorAll("button[type=submit], button:not([type])");
  btns.forEach((b) => { b.disabled = true; });
  try {
    const resp = await fetch(form.action, {
      method: (form.method || "POST").toUpperCase(),
      body: new FormData(form),
      headers: { "X-Requested-With": "fetch", "X-Client-Id": KASA_CLIENT_ID },
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    if (!applyHomeHtml(await resp.text(), { flashes: true })) {
      window.location.href = resp.url || "/";
      return;
    }
    if (form.classList.contains("js-reset-on-submit")) {
      form.reset();
      const nameInput = form.querySelector('input[name="name"]');
      if (nameInput) nameInput.focus();
    }
  } catch (err) {
    form.submit();  // degrade to a normal navigation
  } finally {
    btns.forEach((b) => { b.disabled = false; });
  }
}

document.addEventListener("submit", (e) => {
  const form = e.target;
  if (!form.matches || !form.matches(".js-list-mutate")) return;
  if (e.defaultPrevented) return;   // an inline onsubmit confirm() said no
  e.preventDefault();
  submitListForm(form);
});

(function setupListSync() {
  if (!("EventSource" in window) || !isHomePath()) return;
  let refreshTimer = null;
  const sse = new EventSource("/events");
  sse.addEventListener("list_changed", (e) => {
    if (e.data && e.data === KASA_CLIENT_ID) return;   // our own echo
    if (refreshTimer) clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => {
      if (document.hidden) return;   // catch up via visibilitychange instead
      refreshHomeFromServer();
    }, 500);  // brief debounce so a flurry of edits collapses to one refresh
  });
  sse.onerror = () => {
    // Browser auto-reconnects EventSource; real-time just pauses.
  };
})();

// Catch up when the tab regains focus (events that arrived while it was
// hidden were skipped above).
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && isHomePath()) refreshHomeFromServer();
});

// When the recipe edit page loads with #image_file in the URL (because the
// user tapped the "Add a photo" stock thumbnail), open the file picker.
window.addEventListener("DOMContentLoaded", () => {
  if (location.hash === "#image_file") {
    const el = document.getElementById("image_file");
    if (el) {
      el.scrollIntoView({ block: "center" });
      // Slight delay so iOS/Android settle layout before opening the picker.
      setTimeout(() => el.click(), 150);
    }
  }
});

// Hamburger menu open/close.
(function () {
  const btn = document.getElementById("menu-toggle");
  const close = document.getElementById("menu-close");
  const menu = document.getElementById("side-menu");
  const overlay = document.getElementById("menu-overlay");
  if (!btn || !menu || !overlay) return;

  function setOpen(open) {
    btn.classList.toggle("open", open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    menu.classList.toggle("open", open);
    menu.setAttribute("aria-hidden", open ? "false" : "true");
    overlay.hidden = !open;
  }
  btn.addEventListener("click", () => setOpen(!menu.classList.contains("open")));
  overlay.addEventListener("click", () => setOpen(false));
  if (close) close.addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && menu.classList.contains("open")) setOpen(false);
  });
})();

// Toggle checkbox state via fetch so the page doesn't reload while shopping.
function updateListCounters() {
  const total = document.querySelectorAll(".item").length;
  const checked = document.querySelectorAll(".item.checked").length;
  const checkedEl = document.getElementById("list-checked");
  if (checkedEl) checkedEl.textContent = String(checked);
  const totalEl = document.getElementById("list-total");
  if (totalEl) totalEl.textContent = String(total);
  const prog = document.getElementById("list-progress");
  if (prog) { prog.max = Math.max(1, total); prog.value = checked; }
}

document.addEventListener("change", async (e) => {
  const cb = e.target;
  if (!cb.matches(".item .toggle")) return;
  const li = cb.closest(".item");
  const key = li && li.dataset.key;
  if (!key) return;
  const checked = cb.checked;
  li.classList.toggle("checked", checked);
  updateListCounters();
  try {
    const r = await fetch("/list/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Client-Id": KASA_CLIENT_ID },
      body: JSON.stringify({ key, checked }),
    });
    if (!r.ok) {
      // Server reachable but unhappy — revert the optimistic state.
      cb.checked = !checked;
      li.classList.toggle("checked", cb.checked);
      updateListCounters();
    }
  } catch (err) {
    // Couldn't reach the server (a dead zone in the store, etc.) — keep
    // the optimistic state and queue the change to replay when we're
    // back online, rather than silently dropping it.
    enqueueToggle(key, checked);
  }
});

// ---------------------------------------------------------------------
// Offline-resilient check-offs. When /list/toggle can't reach the server
// we keep the optimistic state and stash the change here, then replay it
// on the next `online` event or page load. (The Background Sync API
// isn't available on iOS Safari, so this is the manual version.)
// ---------------------------------------------------------------------
const TOGGLE_QUEUE_KEY = "shoppinglist:pendingToggles";
function loadToggleQueue() {
  try { return JSON.parse(localStorage.getItem(TOGGLE_QUEUE_KEY)) || []; }
  catch (e) { return []; }
}
function saveToggleQueue(q) {
  try { localStorage.setItem(TOGGLE_QUEUE_KEY, JSON.stringify(q)); } catch (e) {}
}
function enqueueToggle(key, checked) {
  const q = loadToggleQueue().filter((t) => t.key !== key);  // collapse to latest
  q.push({ key, checked, ts: Date.now() });
  saveToggleQueue(q);
}
let _flushingToggles = false;
async function flushToggleQueue() {
  if (_flushingToggles) return;
  const snapshot = loadToggleQueue();
  if (!snapshot.length) return;
  _flushingToggles = true;
  let synced = false;
  try {
    for (const t of snapshot) {
      try {
        const r = await fetch("/list/toggle", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Client-Id": KASA_CLIENT_ID },
          body: JSON.stringify({ key: t.key, checked: t.checked }),
        });
        if (!r.ok) break;  // server's there but unhappy — stop, retry later
        // Drop just this exact entry; a fresh re-toggle would have a
        // different ts and must survive.
        saveToggleQueue(
          loadToggleQueue().filter((x) => !(x.key === t.key && x.ts === t.ts))
        );
        synced = true;
      } catch (e) {
        break;  // still offline
      }
    }
  } finally { _flushingToggles = false; }
  if (synced && isHomePath()) refreshHomeFromServer();
}
window.addEventListener("online", flushToggleQueue);
document.addEventListener("DOMContentLoaded", flushToggleQueue);

// ---------------------------------------------------------------------
// "Hide checked" toggle — collapses checked-off items so the list shrinks
// to what's left as you shop. Preference persists in localStorage.
// ---------------------------------------------------------------------
(function setupHideChecked() {
  const KEY = "shoppinglist:hideChecked";
  const list = document.getElementById("list");
  const btn = document.getElementById("hide-checked-toggle");
  if (!list || !btn) return;
  function apply(on) {
    list.classList.toggle("hide-checked", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  }
  let on = false;
  try { on = localStorage.getItem(KEY) === "1"; } catch (e) {}
  apply(on);
  btn.addEventListener("click", () => {
    on = !list.classList.contains("hide-checked");
    apply(on);
    try { localStorage.setItem(KEY, on ? "1" : "0"); } catch (e) {}
  });
})();

// ---------------------------------------------------------------------
// Flash messages auto-fade after a few seconds (errors linger longer).
// ---------------------------------------------------------------------
function scheduleFlashFade() {
  document.querySelectorAll(".flashes .flash").forEach((el) => {
    if (el.dataset.fadeScheduled) return;
    el.dataset.fadeScheduled = "1";
    const delay = el.classList.contains("flash-error") ? 9000 : 4500;
    setTimeout(() => el.classList.add("fade"), delay);
    setTimeout(() => el.remove(), delay + 600);
  });
}
document.addEventListener("DOMContentLoaded", scheduleFlashFade);

// ---------------------------------------------------------------------
// Recipe view: client-side ingredient scaling.
// ---------------------------------------------------------------------
function kasaFormatQty(qty) {
  // Mirrors ingredient.format_quantity(): friendly fraction-aware text.
  if (!isFinite(qty) || qty <= 0) return "";
  const whole = Math.trunc(qty);
  const frac = qty - whole;
  if (Math.abs(frac) < 0.01) return String(whole);
  for (const denom of [2, 3, 4, 6, 8]) {
    const num = Math.round(frac * denom);
    if (num >= 1 && num < denom && Math.abs(frac - num / denom) < 0.02) {
      let n = num, d = denom;
      const gcd = (a, b) => (b ? gcd(b, a % b) : a);
      const g = gcd(n, d);
      n /= g; d /= g;
      return whole ? `${whole} ${n}/${d}` : `${n}/${d}`;
    }
  }
  return qty.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

(function setupRecipeScale() {
  const controls = document.querySelector(".scale-controls");
  if (!controls) return;
  const qtyNums = Array.from(document.querySelectorAll(".cook-ings .qty-num[data-base]"));
  const servesNum = document.querySelector(".serves-num[data-base]");
  const servesScaled = document.querySelector(".serves-scaled");
  const custom = controls.querySelector(".scale-custom");
  // The "Add to list" form on this page sends this as the multiplier.
  const addMultInput = document.getElementById("scale-add-multiplier");

  function applyFactor(factor) {
    factor = (isFinite(factor) && factor > 0) ? factor : 1;
    qtyNums.forEach((el) => {
      const base = parseFloat(el.dataset.base);
      if (!isFinite(base)) return;
      el.textContent = kasaFormatQty(base * factor);
      const li = el.closest("li");
      if (li) li.classList.toggle("scaled", factor !== 1);
    });
    if (servesNum) {
      const baseS = parseFloat(servesNum.dataset.base);
      if (factor === 1 || !isFinite(baseS) || baseS <= 0) {
        servesNum.textContent = servesNum.dataset.base;
        if (servesScaled) servesScaled.hidden = true;
      } else {
        const scaled = baseS * factor;
        const shown = Number.isInteger(scaled) ? String(scaled) : scaled.toFixed(1);
        if (servesScaled) { servesScaled.textContent = " → " + shown; servesScaled.hidden = false; }
      }
    }
    let matchedBtn = false;
    controls.querySelectorAll(".scale-btn").forEach((b) => {
      const on = parseFloat(b.dataset.factor) === factor;
      b.classList.toggle("active", on);
      if (on) matchedBtn = true;
    });
    if (custom && matchedBtn && document.activeElement !== custom) custom.value = "";
    if (addMultInput) addMultInput.value = String(factor);
  }

  controls.querySelectorAll(".scale-btn").forEach((b) => {
    b.addEventListener("click", () => applyFactor(parseFloat(b.dataset.factor)));
  });
  if (custom) {
    custom.addEventListener("input", () => {
      const v = parseFloat(custom.value);
      applyFactor(isFinite(v) && v > 0 ? v : 1);
    });
  }
})();

// ---------------------------------------------------------------------
// "Hide pantry" toggle — collapses recipe staples you keep stocked.
// Default ON (the pantry feature only does anything once you've added
// staples, and once you have, collapsing them is the point).
// ---------------------------------------------------------------------
(function setupHidePantry() {
  const KEY = "shoppinglist:hidePantry";
  const list = document.getElementById("list");
  const btn = document.getElementById("hide-pantry-toggle");
  if (!list || !btn) return;
  function apply(on) {
    list.classList.toggle("hide-pantry", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  }
  let on = true;
  try { on = localStorage.getItem(KEY) !== "0"; } catch (e) {}
  apply(on);
  btn.addEventListener("click", () => {
    on = !list.classList.contains("hide-pantry");
    apply(on);
    try { localStorage.setItem(KEY, on ? "1" : "0"); } catch (e) {}
  });
})();

// ---------------------------------------------------------------------
// "Copy list" — plain-text shopping list to the clipboard (or share /
// prompt fallback for plain-HTTP contexts where the Clipboard API is
// unavailable).
// ---------------------------------------------------------------------
function buildShoppingListText() {
  const list = document.getElementById("list");
  const hidePantry = !!(list && list.classList.contains("hide-pantry"));
  const out = [];
  document.querySelectorAll("#list-body .cat-section").forEach((sec) => {
    const cat = (sec.querySelector(".cat") || {}).textContent || "";
    const rows = [];
    sec.querySelectorAll(".item").forEach((li) => {
      if (li.classList.contains("checked")) return;
      if (hidePantry && li.classList.contains("pantry")) return;
      const qty = ((li.querySelector(".qty") || {}).textContent || "").replace(/\s+/g, " ").trim();
      const name = ((li.querySelector(".name") || {}).textContent || "").trim();
      const note = ((li.querySelector(".note") || {}).textContent || "").trim();
      let line = "- " + (qty ? qty + " " : "") + name;
      if (note) line += " " + note;   // note already starts with "— "
      rows.push(line);
    });
    if (rows.length) {
      out.push(cat.trim() + ":");
      out.push.apply(out, rows);
      out.push("");
    }
  });
  return out.join("\n").trim();
}

async function copyTextToClipboard(text) {
  if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* fall through */ }
  }
  // Legacy path — works over plain HTTP, unlike the Clipboard API.
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand && document.execCommand("copy");
    document.body.removeChild(ta);
    if (ok) return true;
  } catch (e) { /* fall through */ }
  return false;
}

function flashOnButton(btn, msg) {
  if (!btn._origLabel) btn._origLabel = btn.textContent;
  btn.textContent = msg;
  btn.disabled = true;
  setTimeout(() => { btn.textContent = btn._origLabel; btn.disabled = false; }, 1500);
}

(function setupCopyList() {
  const btn = document.getElementById("copy-list-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const text = buildShoppingListText();
    if (!text) { flashOnButton(btn, "Nothing to copy"); return; }
    if (await copyTextToClipboard(text)) { flashOnButton(btn, "Copied!"); return; }
    // Last resort: a prompt the user can copy out of manually.
    try { window.prompt("Shopping list — copy this:", text); } catch (e) {}
  });
})();

// Recipe form: pressing Enter in an ingredient name field adds a new row
// rather than submitting the whole form (a common data-entry hiccup).
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || !e.target.matches || !e.target.matches('input[name="ing_name[]"]')) return;
  e.preventDefault();
  const tpl = document.getElementById("ing-template");
  const tbody = document.getElementById("ing-rows");
  if (!tpl || !tbody) return;
  tbody.appendChild(tpl.content.cloneNode(true));
  const rows = tbody.querySelectorAll('input[name="ing_name[]"]');
  if (rows.length) rows[rows.length - 1].focus();
});

// Auto-timers: scan instruction steps for time mentions ("bake 25 min",
// "simmer 1 hour") and turn them into tappable countdown buttons backed by
// a small floating timer panel.
//
// Timers persist in localStorage so they survive navigation between pages
// and PWA cold starts. Each row shows what recipe + step it came from so
// the family can tell at a glance which dish a beep belongs to.
(function () {
  const STORAGE_KEY = "shoppinglist:timers";
  // Capture: leading number, optional range (e.g., "20 to 25"), unit.
  const TIME_RE =
    /\b(\d+)(?:\s*(?:to|-|–)\s*(\d+))?\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b/gi;

  function unitSeconds(u) {
    u = u.toLowerCase();
    if (u.startsWith("s")) return 1;
    if (u.startsWith("h")) return 3600;
    return 60;
  }

  function loadStored() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || []; }
    catch { return []; }
  }
  function saveStored(list) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(list)); }
    catch {}
  }
  function patchStored(id, patch) {
    const list = loadStored();
    const i = list.findIndex((t) => t.id === id);
    if (i >= 0) {
      list[i] = patch === null ? null : Object.assign(list[i], patch);
      saveStored(list.filter(Boolean));
    }
  }
  function removeStored(id) { patchStored(id, null); }

  // -- Step annotation ----------------------------------------------------

  function annotate(span) {
    if (!span || span.dataset.timersDone) return;
    const text = span.textContent;
    TIME_RE.lastIndex = 0;
    let m, last = 0, found = false;
    const frag = document.createDocumentFragment();
    while ((m = TIME_RE.exec(text))) {
      found = true;
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      const high = parseInt(m[2] || m[1], 10);
      const seconds = high * unitSeconds(m[3]);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "step-timer";
      btn.dataset.seconds = seconds;
      btn.dataset.label = m[0].trim();
      btn.textContent = "⏱ " + m[0].trim();
      frag.appendChild(btn);
      last = m.index + m[0].length;
    }
    if (!found) return;
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    span.textContent = "";
    span.appendChild(frag);
    span.dataset.timersDone = "1";
  }
  function initStepTimers() {
    document.querySelectorAll(".cook-step-list .cook-check span").forEach(annotate);
    // Cook mode shows one step at a time in #cook-step-text — annotate
    // that too (subsequent steps are annotated by cook mode's render()).
    const cookStep = document.getElementById("cook-step-text");
    if (cookStep) annotate(cookStep);
  }
  // Exposed so cook mode's render() can re-annotate each step it shows.
  window.__kasaAnnotateTimers = annotate;

  // -- Panel + rendering --------------------------------------------------

  let panel = null;
  function ensurePanel() {
    if (panel) return panel;
    panel = document.createElement("div");
    panel.id = "timer-panel";
    panel.hidden = true;
    document.body.appendChild(panel);
    return panel;
  }
  function fmtSeconds(s) {
    s = Math.max(0, Math.round(s));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
  }
  // Audio: we use a real bundled WAV played through a regular <audio>
  // element as the primary alarm. <audio> with playsinline is the most
  // reliable way to ring on iOS PWAs. Web Audio is kept as a fallback in
  // case the <audio> path is muted but Web Audio isn't.
  //
  // iOS rule: the first .play() must happen during a user gesture. We warm
  // it on the first click/touch by calling play() then immediately pause()
  // so subsequent timer-fired plays succeed.
  let audioWarmed = false;
  function getAlarmEl() {
    return document.getElementById("timer-alarm");
  }
  function warmAlarm() {
    if (audioWarmed) return;
    const el = getAlarmEl();
    if (!el) return;
    audioWarmed = true;
    try {
      el.muted = true;
      const p = el.play();
      if (p && p.then) {
        p.then(() => { el.pause(); el.currentTime = 0; el.muted = false; })
         .catch(() => { el.muted = false; });
      } else {
        el.pause(); el.currentTime = 0; el.muted = false;
      }
    } catch (e) { el.muted = false; }
  }

  // Web Audio fallback context, also unlocked on first gesture.
  let audioCtx = null;
  function getAudioCtx() {
    if (!audioCtx) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return null;
      audioCtx = new Ctx();
    }
    if (audioCtx.state === "suspended") {
      audioCtx.resume().catch(() => {});
    }
    return audioCtx;
  }
  function warmWebAudio() {
    const ctx = getAudioCtx();
    if (!ctx) return;
    try {
      const buf = ctx.createBuffer(1, 1, 22050);
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(ctx.destination);
      src.start(0);
    } catch (e) { /* ignore */ }
  }

  // Track scheduled oscillators so we can silence them on cancel.
  let activeOscillators = [];

  function playFallbackBeeps() {
    const ctx = getAudioCtx();
    if (!ctx) return;
    try {
      const t0 = ctx.currentTime;
      [0, 0.4, 0.8].forEach((offset) => {
        const o = ctx.createOscillator();
        const g = ctx.createGain();
        o.connect(g); g.connect(ctx.destination);
        o.type = "square";
        o.frequency.setValueAtTime(880, t0 + offset);
        g.gain.setValueAtTime(0.0001, t0 + offset);
        g.gain.exponentialRampToValueAtTime(0.5, t0 + offset + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + offset + 0.32);
        o.start(t0 + offset);
        o.stop(t0 + offset + 0.35);
        activeOscillators.push(o);
        o.onended = () => {
          activeOscillators = activeOscillators.filter((x) => x !== o);
        };
      });
    } catch (e) { /* ignore */ }
  }

  function stopAlarm() {
    const el = getAlarmEl();
    if (el) {
      try { el.pause(); el.currentTime = 0; } catch (e) {}
    }
    activeOscillators.forEach((o) => {
      try { o.stop(0); o.disconnect(); } catch (e) {}
    });
    activeOscillators = [];
  }

  function ringAlarm() {
    const el = getAlarmEl();
    let played = false;
    if (el) {
      try {
        el.currentTime = 0;
        el.volume = 1.0;
        const p = el.play();
        if (p && p.then) {
          p.then(() => { played = true; })
           .catch(() => { /* fall back to Web Audio below */ });
        } else {
          played = true;
        }
      } catch (e) { /* ignore */ }
    }
    // Fire Web Audio in parallel — on iOS the silent switch can mute one
    // path but not the other, and the user gets *something* either way.
    playFallbackBeeps();
  }

  // Warm both audio paths. Called inside a deliberate user gesture
  // (the step-timer click handler) — NOT on every first tap, because
  // iOS Safari occasionally lets a sliver of audio leak through the
  // muted-warm trick, so any random tap could produce a phantom "ding".
  function unlockAllAudio() {
    warmAlarm();
    warmWebAudio();
  }
  function notifyDone(label) {
    if ("Notification" in window && Notification.permission === "granted") {
      try { new Notification("Timer done", { body: label }); } catch {}
    }
    if (navigator.vibrate) navigator.vibrate([150, 80, 150, 80, 300]);
  }

  function renderTimer(t) {
    const p = ensurePanel();
    p.hidden = false;
    const row = document.createElement("div");
    row.className = "timer-row";
    row.dataset.timerId = t.id;
    row.innerHTML =
      '<div class="timer-meta">' +
        '<strong class="timer-recipe"></strong>' +
        '<span class="timer-step"></span>' +
        '<span class="timer-remaining"></span>' +
      '</div>' +
      '<button type="button" class="link timer-cancel" aria-label="Cancel">×</button>';
    row.querySelector(".timer-recipe").textContent = t.recipeName || "Timer";
    const stepEl = row.querySelector(".timer-step");
    if (t.stepText) {
      stepEl.textContent = t.stepText;
      stepEl.title = t.stepText;
    } else {
      stepEl.remove();
    }
    p.appendChild(row);
    const remEl = row.querySelector(".timer-remaining");

    let alreadyFired = !!t.fired;
    function tick() {
      if (!row.isConnected) return;
      const left = (t.endAt - Date.now()) / 1000;
      if (left <= 0) {
        remEl.textContent = "Done!";
        row.classList.add("finished");
        if (!alreadyFired) {
          alreadyFired = true;
          ringAlarm();
          notifyDone(`${t.recipeName || "Timer"} — ${t.matchedText || "done"}`);
          patchStored(t.id, { fired: true });
        }
        return;
      }
      remEl.textContent = fmtSeconds(left);
      setTimeout(tick, 250);
    }
    row.querySelector(".timer-cancel").addEventListener("click", () => {
      stopAlarm();
      removeStored(t.id);
      row.remove();
      if (!p.querySelector(".timer-row")) p.hidden = true;
    });
    tick();
  }

  function newId() {
    return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  }

  function startTimer(seconds, ctx) {
    const t = {
      id: newId(),
      recipeName: ctx.recipeName,
      stepText: ctx.stepText,
      matchedText: ctx.matchedText,
      endAt: Date.now() + seconds * 1000,
      fired: false,
    };
    const list = loadStored();
    list.push(t);
    saveStored(list);
    renderTimer(t);
  }

  function restoreTimers() {
    const list = loadStored();
    if (!list.length) return;
    const now = Date.now();
    const STALE_MS = 10 * 60 * 1000; // 10 minutes past expiry
    let mutated = false;
    list.forEach((t) => {
      // If the timer expired well before the app opened, don't re-ring it
      // (probably from an old session the user already moved on from).
      // Mark it fired so renderTimer skips the alarm but still shows
      // "Done!" until the user dismisses the row.
      if (!t.fired && now - t.endAt > STALE_MS) {
        t.fired = true;
        mutated = true;
      }
      renderTimer(t);
    });
    if (mutated) saveStored(list);
  }

  // -- Click handler ------------------------------------------------------

  function getRecipeName() {
    const h = document.querySelector(".recipe-view h1, .cook-title");
    return h ? h.textContent.trim() : (document.title || "").replace(/ [·—].*/, "").trim();
  }
  function getStepText(btn) {
    const ctx = btn.closest("li, .cook-step");
    if (!ctx) return "";
    return ctx.textContent.replace(/\s+/g, " ").trim().slice(0, 90);
  }

  document.addEventListener("click", (e) => {
    if (!e.target.matches(".step-timer")) return;
    e.preventDefault();
    e.stopPropagation();
    const sec = parseInt(e.target.dataset.seconds, 10) || 0;
    if (sec <= 0) return;
    // Unlock audio inside this deliberate user gesture so the alarm can
    // ring later when the timer fires from setTimeout.
    unlockAllAudio();
    if ("Notification" in window && Notification.permission === "default") {
      Notification.requestPermission().catch(() => {});
    }
    startTimer(sec, {
      recipeName: getRecipeName(),
      stepText: getStepText(e.target),
      matchedText: e.target.dataset.label,
    });
  });

  document.addEventListener("DOMContentLoaded", () => {
    initStepTimers();
    restoreTimers();
  });
})();

// (Wake-lock cook mode was removed because iOS PWAs in standalone mode
// don't reliably expose navigator.wakeLock.)

// Clickable star rating — used on both the recipe edit form (writes to a
// hidden <input name="rating">) and on the cook view (POSTs to
// /recipes/<id>/rate so the change persists immediately).
function paintStars(widget, value) {
  widget.dataset.rating = String(value);
  widget.querySelectorAll(".star-btn").forEach((btn) => {
    btn.textContent = (parseInt(btn.dataset.value, 10) <= value) ? "★" : "☆";
  });
  const hidden = widget.querySelector('input[type="hidden"][name="rating"]');
  if (hidden) hidden.value = String(value);
}
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".star-btn, .star-clear");
  if (!btn) return;
  const widget = btn.closest(".star-rate");
  if (!widget) return;
  e.preventDefault();
  let value = parseInt(btn.dataset.value, 10) || 0;
  // Clicking the currently-selected star clears it (toggles back to 0).
  if (parseInt(widget.dataset.rating || "0", 10) === value && btn.classList.contains("star-btn")) {
    value = 0;
  }
  paintStars(widget, value);
  const recipeId = widget.dataset.recipeId;
  if (recipeId) {
    const fd = new FormData();
    fd.append("rating", value);
    fetch(`/recipes/${recipeId}/rate`, {
      method: "POST",
      body: fd,
      headers: { "X-Requested-With": "fetch" },
    }).catch(() => {});
  }
});

// Recipe form: dynamic ingredient rows.
document.addEventListener("click", (e) => {
  if (e.target.matches("#add-ing")) {
    const tpl = document.getElementById("ing-template");
    const tbody = document.getElementById("ing-rows");
    if (tpl && tbody) {
      tbody.appendChild(tpl.content.cloneNode(true));
    }
  }
  if (e.target.matches(".remove-row")) {
    const row = e.target.closest("tr.ing-row");
    if (row) row.remove();
  }
});

// Quick "+ List" submit on the Recipes page — POSTs in the background so
// the family can keep scrolling and add several at once.
document.addEventListener("submit", async (e) => {
  if (!e.target.matches(".add-list-form")) return;
  e.preventDefault();
  const form = e.target;
  const btn = form.querySelector(".add-list-btn");
  const originalText = btn.textContent.trim();
  btn.disabled = true;
  btn.textContent = "Adding…";
  try {
    const data = new FormData(form);
    const r = await fetch(form.action, {
      method: "POST",
      body: data,
      headers: { "X-Requested-With": "fetch", "X-Client-Id": KASA_CLIENT_ID },
    });
    if (!r.ok) throw new Error("network");
    btn.textContent = "✓ Added";
    setTimeout(() => {
      btn.textContent = originalText;
      btn.disabled = false;
    }, 1500);
  } catch (err) {
    btn.textContent = "Failed";
    setTimeout(() => {
      btn.textContent = originalText;
      btn.disabled = false;
    }, 1500);
  }
});

// Live favorite toggle without full reload on listing page.
document.addEventListener("submit", async (e) => {
  if (!e.target.matches(".fav-form")) return;
  e.preventDefault();
  const form = e.target;
  try {
    const r = await fetch(form.action, {
      method: "POST",
      headers: { "X-Requested-With": "fetch" },
    });
    const data = await r.json();
    const btn = form.querySelector("button.star");
    if (btn) btn.textContent = data.favorite ? "★" : "☆";
  } catch (err) {
    form.submit();
  }
});
