// tallywall touch frontend. Vanilla JS, no build step.

const T = {
  title: "Tallywall",
  added: (amount, drink, person) => `${amount > 0 ? "+" : "−"}1 ${drink} for ${person}`,
  failed: "Couldn't save: ",
  offline: "Turff offline",
  pending: (n) => `${n} waiting to sync`,
  serverDown: "No connection",
  unreachable: "Can't reach Turff — retrying…",
  noDevice: "Name device",
  noPeople: "No housemates",
  top: "Top 5",
  noTop: "No drinks yet",
};

const LOCALE = "nl-NL"; // dates and prices; UI strings stay English
const euro = new Intl.NumberFormat(LOCALE, { style: "currency", currency: "EUR" });
const price = (p) => euro.format(p / 1000); // Turff prices are thousandths of a euro
const timeFmt = new Intl.DateTimeFormat(LOCALE, { hour: "2-digit", minute: "2-digit" });
const dateFmt = new Intl.DateTimeFormat(LOCALE, { weekday: "long", day: "numeric", month: "long" });

const REFRESH_MS = 20_000;
const SAVER_REFRESH_MS = 60_000; // screensaver needs fewer refreshes
const RETRY_MS = 5_000;
const TOAST_MS = 2_500;
const ERROR_TOAST_MS = 4_000;
const DEBOUNCE_MS = 100; // ignore a second tap on the same tile within this window
const MINUS_RESET_MS = 10_000; // − mode flips back to + after this long without a tap
const IDLE_MS = 60_000; // screensaver after this long without a touch
const WAKE_SWALLOW_MS = 400; // clicks this soon after waking belong to the waking touch
const TOP_N = 5;
const NIGHT = { from: 23, to: 7 }; // local hours; the screensaver dims in between
const KEY_DRINK = "tallywall.drink";
const KEY_DEVICE = "tallywall.device";

const $ = (id) => document.getElementById(id);

// ---------- storage (may be unavailable: private mode, blocked site data) ----------

function load(key) {
  try {
    const v = localStorage.getItem(key);
    return v == null ? null : JSON.parse(v);
  } catch {
    return null;
  }
}

function save(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* per-device convenience only */
  }
}

function randomId() {
  // crypto.randomUUID needs a secure context; the tablet uses plain http on the LAN.
  const b = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

// ---------- app state ----------

let state = null; // last /api/state payload, with confirmed mutations applied locally
let drinkId = load(KEY_DRINK); // preferred drink; kept even while Turff doesn't list it
let device = load(KEY_DEVICE);
if (!device || typeof device.id !== "string") device = { id: randomId(), name: "" };

const inflight = new Map(); // "person|drink" -> unconfirmed delta
let epoch = 0; // bumped on every confirmed mutation; older refresh responses are dropped
const lastTap = new Map(); // person -> timestamp, for double-tap protection
const tiles = new Map(); // person -> {el, badge}
let mode = 1; // +1 adds, −1 removes; never persisted, so a reload starts on +
let modeTimer = 0;
let tileKey = "";

const k = (person, drink) => `${person}|${drink}`;

function count(person, drink) {
  return (person.counts[drink] ?? 0) + (inflight.get(k(person.id, drink)) ?? 0);
}

function addInflight(key, d) {
  const v = (inflight.get(key) ?? 0) + d;
  if (v) inflight.set(key, v);
  else inflight.delete(key);
}

// ---------- API ----------

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function api(method, path, body) {
  let r;
  try {
    r = await fetch(path, {
      method,
      headers: body ? { "content-type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, T.serverDown);
  }
  let data = null;
  try {
    data = await r.json();
  } catch {
    /* empty or non-JSON body */
  }
  if (!r.ok) {
    const detail = data && typeof data.detail === "string" ? data.detail : `HTTP ${r.status}`;
    throw new ApiError(r.status, detail);
  }
  return data;
}

let refreshTimer = 0;
let netError = false;

async function refresh() {
  clearTimeout(refreshTimer);
  const started = epoch;
  try {
    const s = await api("GET", "/api/state");
    netError = false;
    if (started === epoch) {
      state = s;
      render();
    }
    $("blocker").hidden = true;
    schedule(saverOn ? SAVER_REFRESH_MS : REFRESH_MS);
  } catch (e) {
    netError = e.status === 0;
    if (!state) {
      $("blocker-msg").textContent = e.status === 0 ? T.serverDown : T.unreachable;
      $("blocker").hidden = false;
    }
    renderStatus();
    schedule(RETRY_MS);
  }
}

function schedule(ms) {
  clearTimeout(refreshTimer);
  if (document.visibilityState === "visible") refreshTimer = setTimeout(refresh, ms);
}

// ---------- rendering ----------

function onColor(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
  if (!m) return "#fff";
  const n = parseInt(m[1], 16);
  const lum = (0.299 * (n >> 16) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255;
  return lum > 0.6 ? "#111418" : "#ffffff";
}

function initials(name) {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  const s = parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : (parts[0] || "?").slice(0, 2);
  return s.toUpperCase();
}

function currentDrink() {
  return state.drinks.find((d) => d.id === drinkId) || state.drinks[0] || null;
}

function render() {
  if (!state) return;
  document.title = state.settings.houseName || T.title;
  $("house").textContent = state.settings.houseName || T.title;
  const drink = currentDrink();
  renderDrinks(drink);
  renderTiles(drink);
  renderStatus();
  if (saverOn) renderSaver();
}

function renderDrinks(drink) {
  const nav = $("drinks");
  const key = JSON.stringify(state.drinks.map((d) => [d.id, d.name, d.color, d.price]));
  if (nav.dataset.key !== key) {
    nav.dataset.key = key;
    nav.replaceChildren(
      ...state.drinks.map((d) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "drink";
        b.dataset.id = d.id;
        const name = document.createElement("span");
        name.textContent = d.name;
        b.append(name);
        if (d.price) {
          const p = document.createElement("span");
          p.className = "price";
          p.textContent = price(d.price);
          b.append(p);
        }
        b.style.setProperty("--c", d.color || "#888");
        b.style.setProperty("--on-c", onColor(d.color));
        b.addEventListener("click", () => selectDrink(d.id));
        return b;
      }),
    );
  }
  for (const b of nav.children) b.setAttribute("aria-pressed", String(b.dataset.id === drink?.id));
}

function renderTiles(drink) {
  const grid = $("grid");
  const key = JSON.stringify(state.people.map((p) => [p.id, p.name, p.color, p.isGroup]));
  if (key !== tileKey) {
    tileKey = key;
    tiles.clear();
    if (state.people.length) grid.replaceChildren(...state.people.map(makeTile));
    else {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = T.noPeople;
      grid.replaceChildren(empty);
    }
    fit();
  }
  for (const p of state.people) {
    const t = tiles.get(p.id);
    const n = drink ? count(p, drink.id) : 0;
    if (t.badge.textContent !== String(n)) {
      if (t.badge.textContent !== "") bump(t.badge);
      t.badge.textContent = String(n);
    }
    t.badge.classList.toggle("zero", n === 0);
    t.badge.classList.toggle("neg", n < 0);
    t.badge.style.setProperty("--d", drink?.color || "#444");
    t.badge.style.setProperty("--on-d", onColor(drink?.color));
    t.el.setAttribute("aria-label", `${p.name}: ${n} ${drink?.name ?? ""}`);
  }
}

function makeTile(p) {
  const el = document.createElement("button");
  el.type = "button";
  el.className = "tile" + (p.isGroup ? " group" : "");
  el.style.setProperty("--c", p.color || "#64748b");
  el.style.setProperty("--on-c", onColor(p.color));

  const avatar = makeAvatar(p);

  const name = document.createElement("span");
  name.className = "name";
  name.textContent = p.name;

  const badge = document.createElement("span");
  badge.className = "badge";

  el.append(badge, avatar, name);
  el.addEventListener("click", () => tap(p.id));
  tiles.set(p.id, { el, badge });
  return el;
}

// Local photo if present, else initials on the person's colour (set --c/--on-c on a parent).
function makeAvatar(p) {
  const avatar = document.createElement("span");
  avatar.className = "avatar";
  avatar.textContent = initials(p.name);
  const img = new Image();
  img.alt = "";
  img.decoding = "async";
  img.onload = () => avatar.replaceChildren(img);
  img.src = `/avatars/${encodeURIComponent(p.id)}.jpg`;
  return avatar;
}

function restart(el, cls) {
  el.classList.remove(cls);
  void el.offsetWidth; // restart the animation
  el.classList.add(cls);
}
const bump = (el) => restart(el, "bump");

function renderStatus() {
  const el = $("status");
  let text = "";
  if (netError) text = T.serverDown;
  else if (state?.stale) text = T.offline;
  if (state?.pending) text = text ? `${text} · ${T.pending(state.pending)}` : T.pending(state.pending);
  el.textContent = text;
  el.hidden = !text;
  el.classList.toggle("error", netError || !!state?.stale);
  $("saver-status").textContent = text;
  $("saver-status").hidden = !text;
}

// Size tiles so they all fit on a landscape screen; phones scroll a 2–3 column grid.
function fit() {
  const grid = $("grid");
  const n = Math.max(1, grid.children.length);
  const cs = getComputedStyle(grid);
  const gap = parseFloat(cs.columnGap) || 12;
  const W = grid.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  const H = grid.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);

  let cols;
  let row;
  if (window.innerWidth <= 640 || H < 220) {
    cols = W < 360 ? 2 : 3;
    row = (W - gap * (cols - 1)) / cols;
  } else {
    let best = -1;
    for (let c = 1; c <= n; c++) {
      const r = Math.ceil(n / c);
      const tw = (W - gap * (c - 1)) / c;
      const th = (H - gap * (r - 1)) / r;
      const score = Math.min(tw, th * 1.15);
      if (score > best) {
        best = score;
        cols = c;
        row = th;
      }
    }
  }
  const colW = (W - gap * (cols - 1)) / cols;
  grid.style.setProperty("--cols", cols);
  grid.style.setProperty("--row", `${Math.floor(row)}px`);
  grid.style.setProperty("--t", `${Math.floor(Math.min(colW, row))}px`);
}

// ---------- actions ----------

function selectDrink(id) {
  drinkId = id;
  save(KEY_DRINK, id);
  render();
}

// Optimistically apply `d`, run `call`; on success fold the delta into local state.
async function mutate(personId, drink, d, call) {
  const key = k(personId, drink);
  addInflight(key, d);
  render();
  try {
    const res = await call();
    epoch++;
    const p = state.people.find((x) => x.id === personId);
    if (p) p.counts[drink] = (p.counts[drink] ?? 0) + d;
    return res;
  } finally {
    addInflight(key, -d);
    render();
    refresh();
  }
}

function setMode(m) {
  mode = m;
  document.body.classList.toggle("minus", m < 0);
  $("mode").setAttribute("aria-checked", String(m < 0));
  clearTimeout(modeTimer);
  if (m < 0) modeTimer = setTimeout(() => setMode(1), MINUS_RESET_MS);
}

function tap(personId) {
  if (!state || !device.name || saverOn) return;
  const now = performance.now();
  if (now - (lastTap.get(personId) ?? -Infinity) < DEBOUNCE_MS) return;
  lastTap.set(personId, now);

  const person = state.people.find((p) => p.id === personId);
  const drink = currentDrink();
  const tile = tiles.get(personId);
  if (!person || !drink || !tile) return; // removed by a refresh since it was drawn
  const amount = mode;
  if (amount < 0) setMode(-1); // restart the reset timer
  restart(tile.el, "flash");
  navigator.vibrate?.(amount > 0 ? 20 : [20, 60, 20]);

  mutate(person.id, drink.id, amount, () =>
    api("POST", "/api/tally", {
      person: person.id,
      drink: drink.id,
      amount,
      device_id: device.id,
      device_name: device.name,
    }),
  ).catch((e) => toast(T.failed + e.message, "error", ERROR_TOAST_MS));
  toast(T.added(amount, drink.name, person.name), amount < 0 ? "minus" : "", TOAST_MS);
}

let toastTimer = 0;

function toast(msg, kind, ms) {
  const t = $("toast");
  $("toast-msg").textContent = msg;
  t.className = "toast" + (kind ? ` ${kind}` : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), ms);
}

// ---------- screensaver ----------

let saverOn = false;
let idleTimer = 0;
let clockTimer = 0;
let wokeAt = -Infinity;
let boardKey = "";

function poke() {
  clearTimeout(idleTimer);
  if (!saverOn && document.visibilityState === "visible") idleTimer = setTimeout(sleep, IDLE_MS);
}

function sleep() {
  // Never cover a dialog, the connection blocker, or a page nobody can see.
  if (saverOn || !state || !$("name-dialog").hidden || !$("blocker").hidden) return poke();
  saverOn = true;
  setMode(1);
  $("toast").hidden = true;
  renderSaver();
  tick();
  $("saver").classList.add("on");
  $("saver").setAttribute("aria-hidden", "false");
  schedule(SAVER_REFRESH_MS);
}

function wake() {
  if (!saverOn) return;
  saverOn = false;
  wokeAt = performance.now();
  clearTimeout(clockTimer);
  $("saver").classList.remove("on");
  $("saver").setAttribute("aria-hidden", "true");
  refresh();
  poke();
}

function tick() {
  clearTimeout(clockTimer);
  if (!saverOn) return;
  const now = new Date();
  $("clock").textContent = timeFmt.format(now);
  $("date").textContent = dateFmt.format(now);
  const h = now.getHours();
  const night = NIGHT.from > NIGHT.to ? h >= NIGHT.from || h < NIGHT.to : h >= NIGHT.from && h < NIGHT.to;
  $("saver").classList.toggle("night", night);
  // Drift a little every minute so nothing sits on the same pixels all night.
  const r = () => Math.round((Math.random() * 2 - 1) * 24);
  $("saver-body").style.transform = `translate(${r()}px, ${r()}px)`;
  clockTimer = setTimeout(tick, 60_000 - (now.getSeconds() * 1000 + now.getMilliseconds()) + 50);
}

// Persons (not groups) by total drinks over all types; ties share a rank.
function leaders() {
  const rows = state.people
    .filter((p) => !p.isGroup)
    .map((p) => ({ p, n: state.drinks.reduce((sum, d) => sum + count(p, d.id), 0) }))
    .filter((x) => x.n > 0)
    .sort((a, b) => b.n - a.n || a.p.name.localeCompare(b.p.name, LOCALE))
    .slice(0, TOP_N);
  rows.forEach((x, i) => (x.rank = i && x.n === rows[i - 1].n ? rows[i - 1].rank : i + 1));
  return rows;
}

function renderSaver() {
  if (!state) return;
  $("saver-house").textContent = state.settings.houseName || T.title;
  $("board-title").textContent = T.top;
  const rows = leaders();
  const key = JSON.stringify(rows.map((x) => [x.p.id, x.p.name, x.p.color, x.n, x.rank]));
  if (key === boardKey) return;
  boardKey = key;
  const max = rows[0]?.n || 1;
  const board = $("board");
  if (!rows.length) {
    const li = document.createElement("li");
    li.className = "none";
    li.textContent = T.noTop;
    board.replaceChildren(li);
    return;
  }
  board.replaceChildren(
    ...rows.map((x) => {
      const li = document.createElement("li");
      li.style.setProperty("--c", x.p.color || "#64748b");
      li.style.setProperty("--on-c", onColor(x.p.color));
      li.style.setProperty("--w", `${Math.round((x.n / max) * 100)}%`);
      const rank = document.createElement("span");
      rank.className = "rank";
      rank.textContent = String(x.rank);
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = x.p.name;
      const n = document.createElement("span");
      n.className = "n";
      n.textContent = String(x.n);
      li.append(rank, makeAvatar(x.p), name, n);
      return li;
    }),
  );
}

$("saver").addEventListener("pointerdown", (e) => {
  e.preventDefault();
  wake();
});
// The click that follows a waking touch may land on a tile once the overlay is gone.
document.addEventListener(
  "click",
  (e) => {
    if (performance.now() - wokeAt < WAKE_SWALLOW_MS) {
      e.stopPropagation();
      e.preventDefault();
    }
  },
  true,
);
for (const type of ["pointerdown", "keydown", "wheel"]) {
  document.addEventListener(type, () => poke(), { capture: true, passive: true });
}

// ---------- device name ----------

function renderDevice() {
  $("device").textContent = device.name || T.noDevice;
}

function askName() {
  const required = !device.name;
  $("name-input").value = device.name;
  $("name-cancel").hidden = required;
  $("name-dialog").hidden = false;
  if (!required) $("name-input").select();
  $("name-input").focus();
}

$("name-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const name = $("name-input").value.trim().slice(0, 64);
  if (!name) return;
  device = { ...device, name };
  save(KEY_DEVICE, device);
  renderDevice();
  $("name-dialog").hidden = true;
});
$("name-cancel").addEventListener("click", () => {
  if (device.name) $("name-dialog").hidden = true;
});
$("device").addEventListener("click", askName);

// ---------- wiring ----------

$("mode").addEventListener("click", () => setMode(-mode));

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    refresh();
    poke();
  } else {
    clearTimeout(refreshTimer);
    clearTimeout(idleTimer);
    setMode(1);
  }
});
window.addEventListener("focus", () => refresh());

let fitFrame = 0;
window.addEventListener("resize", () => {
  cancelAnimationFrame(fitFrame);
  fitFrame = requestAnimationFrame(fit);
});

renderDevice();
if (!device.name) askName();
refresh();
poke();
