import { initNames, matchName } from "./match.js";
import { initVision, findCard, readName, rankPrintings, CARD_W, CARD_H } from "./vision.js";

const OPENCV_URL = "https://cdn.jsdelivr.net/npm/@techstark/opencv-js@4.12.0-release.1/dist/opencv.js";
const ORT_WASM = "https://cdn.jsdelivr.net/npm/onnxruntime-web@1.30.0/dist/";
const CARD_RATIO = 63 / 88;

// Auto-scan tuning (on a 32x44 grey thumbnail of the guide box).
const SAMPLE_MS = 150;
const STILL_LEVEL = 11;   // mean pixel change between samples below this = steady enough (hand-held is fine)
const STILL_SAMPLES = 2;  // ~0.3 s
const REARM_FRAC = 0.30;  // after a card is added, this much of the box must change before the next one

const $ = id => document.getElementById(id);
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode / full */ } },
};

const state = {
  entries: store.get("entries", []),     // one per physical card
  lastAlts: store.get("lastAlts", null), // printings of the most recent card, best first
  settings: Object.assign({ lockSet: "", defaultFoil: false, sound: true, auto: true }, store.get("settings", {})),
  busy: false,
  review: null,
  unsure: 0,
  armed: true,
  prevThumb: null,
  lastScanThumb: null,
  still: 0,
  ready: false,
};

// ---------------------------------------------------------------- loading

const loadSteps = { opencv: 0, ort: 0, model: 0, names: 0 };
const loadWeights = { opencv: 0.35, ort: 0.3, model: 0.3, names: 0.05 };
function progress(step, frac) {
  loadSteps[step] = frac;
  const total = Object.keys(loadSteps).reduce((s, k) => s + loadSteps[k] * loadWeights[k], 0);
  $("load-progress").firstElementChild.style.width = `${Math.round(total * 100)}%`;
}

async function fetchWithProgress(url, step, as = "arrayBuffer") {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url}: ${res.status}`);
  const total = +res.headers.get("content-length") || 0;
  if (!res.body || !total) { const v = await res[as](); progress(step, 1); return v; }
  const reader = res.body.getReader(), chunks = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value); got += value.length;
    progress(step, Math.min(0.99, got / total));
  }
  const blob = new Blob(chunks);
  progress(step, 1);
  return as === "json" ? JSON.parse(await blob.text()) : blob.arrayBuffer();
}

function loadOpenCV() {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = OPENCV_URL; s.async = true;
    let fake = 0;
    const tick = setInterval(() => progress("opencv", Math.min(0.9, (fake += 0.03))), 200);
    s.onerror = () => { clearInterval(tick); reject(new Error("Couldn't load OpenCV")); };
    s.onload = async () => {
      let cv = window.cv;
      if (cv instanceof Promise) cv = await cv;
      else if (!cv.Mat) await new Promise(r => (cv.onRuntimeInitialized = r));
      clearInterval(tick); progress("opencv", 1);
      // The OpenCV module has a .then() of its own; resolving a promise with it
      // directly makes the browser loop forever, so hand it over wrapped.
      resolve({ cv });
    };
    document.head.appendChild(s);
  });
}

async function boot() {
  try {
    ort.env.wasm.wasmPaths = ORT_WASM;
    ort.env.wasm.numThreads = 1; // GitHub Pages can't enable cross-origin isolation for threads
    $("load-text").textContent = "Downloading card reader (first time ~15 MB)…";
    const namesP = fetchWithProgress("data/names.json", "names", "json");
    const dictP = fetch("models/en_dict.json").then(r => r.json());
    const modelP = fetchWithProgress("models/en_rec.onnx", "model");
    const cvP = loadOpenCV();
    initNames(await namesP);
    const ortTick = setInterval(() => progress("ort", Math.min(0.95, loadSteps.ort + 0.05)), 250);
    await initVision((await cvP).cv, ort, new Uint8Array(await modelP), await dictP);
    clearInterval(ortTick); progress("ort", 1);
    state.ready = true;
    $("load-text").textContent = "Ready.";
    $("start-btn").disabled = false;
  } catch (e) {
    console.error(e);
    $("load-text").textContent = `Loading failed: ${e.message}. Check your connection and reload.`;
  }
}

// ---------------------------------------------------------------- camera

const video = $("video");
const grab = document.createElement("canvas");
const grabCtx = grab.getContext("2d", { willReadFrequently: true });

async function startCamera() {
  unlockAudio();
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } },
    });
    video.srcObject = stream;
    await video.play();
    const track = stream.getVideoTracks()[0];
    try { await track.applyConstraints({ advanced: [{ focusMode: "continuous" }] }); } catch { /* not supported */ }
    showScanner();
    requestAnimationFrame(layoutGuide);
    setInterval(sample, SAMPLE_MS);
  } catch (e) {
    console.error(e);
    alert("Couldn't open the camera. Allow camera access for this site (Settings › Safari › Camera), or use “scan a photo”.");
  }
}

function showScanner() {
  $("start").classList.add("hidden");
  $("scan").classList.remove("hidden");
  renderAll();
}

// The guide box in *video pixel* coordinates.
function guideRect() {
  const vw = video.videoWidth, vh = video.videoHeight;
  // Only the part of the video visible on screen (object-fit: cover crops it).
  const vp = $("viewport").getBoundingClientRect();
  const scale = Math.max(vp.width / vw, vp.height / vh);
  const visW = vp.width / scale, visH = vp.height / scale;
  let h = Math.min(visH * 0.8, (visW * 0.86) / CARD_RATIO);
  const w = h * CARD_RATIO;
  return { x: (vw - w) / 2, y: (vh - h) / 2 - visH * 0.02, w, h, scale, vw, vh, vp };
}

function layoutGuide() {
  if (!video.videoWidth) return requestAnimationFrame(layoutGuide);
  const g = guideRect();
  const offX = (g.vp.width - g.vw * g.scale) / 2, offY = (g.vp.height - g.vh * g.scale) / 2;
  Object.assign($("guide").style, {
    left: `${offX + g.x * g.scale}px`, top: `${offY + g.y * g.scale}px`,
    width: `${g.w * g.scale}px`, height: `${g.h * g.scale}px`,
  });
}
window.addEventListener("resize", () => setTimeout(layoutGuide, 100));

// Copy the guide box (plus a margin, so card edges are inside) to a canvas.
function grabGuide(margin = 0.1) {
  const g = guideRect();
  const x = Math.max(0, g.x - g.w * margin), y = Math.max(0, g.y - g.h * margin);
  const w = Math.min(g.vw - x, g.w * (1 + 2 * margin)), h = Math.min(g.vh - y, g.h * (1 + 2 * margin));
  const c = document.createElement("canvas");
  const k = Math.min(1, 1100 / h); // plenty for reading; keeps OpenCV fast
  c.width = Math.round(w * k); c.height = Math.round(h * k);
  c.getContext("2d").drawImage(video, x, y, w, h, 0, 0, c.width, c.height);
  return c;
}

function thumb() {
  const g = guideRect();
  grab.width = 32; grab.height = 44;
  grabCtx.drawImage(video, g.x, g.y, g.w, g.h, 0, 0, 32, 44);
  const px = grabCtx.getImageData(0, 0, 32, 44).data, out = new Float32Array(32 * 44);
  for (let i = 0; i < out.length; i++) out[i] = 0.3 * px[i * 4] + 0.59 * px[i * 4 + 1] + 0.11 * px[i * 4 + 2];
  return out;
}
const meanDiff = (a, b) => { let s = 0; for (let i = 0; i < a.length; i++) s += Math.abs(a[i] - b[i]); return s / a.length; };
const fracDiff = (a, b) => { let n = 0; for (let i = 0; i < a.length; i++) if (Math.abs(a[i] - b[i]) > 25) n++; return n / a.length; };

// Auto-scan: when the picture in the box holds still and is different from the
// last card scanned, read it.
function sample() {
  if (!state.ready || !video.videoWidth || document.hidden) return;
  const t = thumb();
  if (state.prevThumb) state.still = meanDiff(t, state.prevThumb) < STILL_LEVEL ? state.still + 1 : 0;
  state.prevThumb = t;
  if (!state.armed && state.lastScanThumb && fracDiff(t, state.lastScanThumb) > REARM_FRAC) state.armed = true;
  // Keep trying while something is in view; only a successful scan (or a
  // "which card?" question) pauses until the picture changes.
  if (state.settings.auto && state.armed && state.still >= STILL_SAMPLES && !state.busy && !state.review && modalsClosed()) {
    scanNow(true);
  }
  $("guide").classList.toggle("looking", state.settings.auto && state.armed && !state.busy);
}

const modalsClosed = () => ["review", "list", "settings"].every(id => $(id).classList.contains("hidden"));

// ---------------------------------------------------------------- scanning

async function scanNow(auto = false) {
  if (state.busy || !state.ready) return;
  state.busy = true;
  const seen = state.prevThumb || thumb();
  setGuide("busy");
  try {
    const card = findCard(grabGuide());
    if (!card) {
      // Nothing card-shaped: stay quiet in auto mode (probably an empty desk).
      if (!auto) status("No card found — fill the box with the card", "bad");
      setGuide("");
      return;
    }
    const outcome = await identify(card, auto);
    if (outcome !== "retry") {
      // Added, or asked the user: don't scan this same card again until the view changes.
      state.armed = false;
      state.lastScanThumb = seen;
    }
  } catch (e) {
    console.error(e);
    status(`Error: ${e.message}`, "bad");
  } finally {
    state.busy = false;
  }
}

async function scanPhoto(file) {
  if (!file || !state.ready) return;
  const img = await createImageBitmap(file);
  const k = Math.min(1, 1400 / Math.max(img.width, img.height));
  const c = document.createElement("canvas");
  c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
  c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
  if ($("scan").classList.contains("hidden")) showScanner();
  state.busy = true;
  try {
    const card = findCard(c) || c;
    await identify(card, false);
  } finally {
    state.busy = false;
  }
}

async function identify(card, auto) {
  status("Reading…");
  const res = await readName(card, matchName);
  if (!res.candidates.length) {
    if (!auto) status(`Couldn't read the name${res.text ? ` (“${res.text}”)` : ""} — try again`, "bad");
    else status("");
    setGuide("");
    if (!auto) beep(false);
    return "retry";
  }
  if (res.confident) {
    state.unsure = 0;
    await addByName(res.candidates[0][0], res.card);
    return "added";
  }
  // In auto mode a shaky or half-in-view frame often reads badly; try a few more
  // frames before bothering the user with a question.
  if (auto && ++state.unsure < 3) { status(""); setGuide(""); return "retry"; }
  state.unsure = 0;
  openReview(res);
  return "asked";
}

async function addByName(name, cardCanvas) {
  status(`Finding ${name}…`);
  let prints = await fetchPrintings(name);
  if (!prints.length) { status(`Couldn't load ${name} from Scryfall`, "bad"); beep(false); return; }
  const lock = state.settings.lockSet.trim().toLowerCase();
  if (lock) {
    const inSet = prints.filter(p => p.set === lock);
    if (inSet.length) prints = inSet;
  }
  if (cardCanvas) prints = await rankPrintings(cardCanvas, prints);
  const p = prints[0];
  const finish = pickFinish(p, state.settings.defaultFoil ? "foil" : "nonfoil");
  state.entries.push({ ...p, finish, t: Date.now() });
  state.lastAlts = { idx: 0, list: prints.slice(0, 60) };
  save();
  renderAll();
  setGuide("ok");
  status(`+ ${p.name}`, "ok");
  beep(true);
  setTimeout(() => setGuide(""), 700);
}

function pickFinish(p, wanted) {
  return p.finishes.includes(wanted) ? wanted : p.finishes[0] || "nonfoil";
}

// ---------------------------------------------------------------- Scryfall

const printCache = new Map();
let lastScryfall = 0;

async function scryfall(url) {
  // Scryfall asks for ≤10 requests a second.
  const wait = lastScryfall + 110 - Date.now();
  if (wait > 0) await new Promise(r => setTimeout(r, wait));
  lastScryfall = Date.now();
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`Scryfall ${res.status}`);
  return res.json();
}

async function fetchPrintings(name) {
  if (printCache.has(name)) return printCache.get(name);
  try {
    const card = await scryfall(`https://api.scryfall.com/cards/named?exact=${encodeURIComponent(name)}`);
    let url = `${card.prints_search_uri}&order=released&dir=desc`;
    const out = [];
    for (let page = 0; url && page < 3; page++) {
      const r = await scryfall(url);
      out.push(...r.data);
      url = r.has_more ? r.next_page : null;
    }
    const prints = out.filter(c => !c.digital).map(compact).filter(p => p.img_small);
    printCache.set(name, prints);
    return prints;
  } catch (e) {
    console.error(e);
    return [];
  }
}

function compact(c) {
  const imgs = c.image_uris || c.card_faces?.[0]?.image_uris || {};
  return {
    id: c.id, name: c.name, set: c.set, set_name: c.set_name, cn: c.collector_number,
    rarity: c.rarity, lang: c.lang, finishes: c.finishes || ["nonfoil"], promo: !!c.promo,
    usd: c.prices?.usd, usd_foil: c.prices?.usd_foil, usd_etched: c.prices?.usd_etched,
    img_small: imgs.small, img_normal: imgs.normal,
  };
}

const priceOf = e => parseFloat((e.finish === "foil" ? e.usd_foil : e.finish === "etched" ? e.usd_etched : e.usd) || e.usd || 0) || 0;
const money = v => `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

// ---------------------------------------------------------------- review

function openReview(res) {
  state.review = res;
  beep(false);
  status("Not sure — pick the card", "busy");
  const c = $("review-img").getContext("2d");
  c.drawImage(res.card, 0, 0, 126, 176);
  renderChoices(res.candidates);
  $("review-search").value = "";
  $("review").classList.remove("hidden");
}

function renderChoices(cands) {
  const list = $("review-list");
  list.innerHTML = "";
  for (const [name, score] of cands.slice(0, 5)) {
    const b = document.createElement("button");
    b.innerHTML = `<span></span><span class="muted small">${Math.round(score)}%</span>`;
    b.firstChild.textContent = name;
    b.onclick = () => chooseReview(name);
    list.appendChild(b);
  }
  if (!cands.length) list.innerHTML = `<p class="muted small">Type the name below.</p>`;
}

async function chooseReview(name) {
  const res = state.review;
  closeReview();
  await addByName(name, res?.card);
}

function closeReview() {
  state.review = null;
  $("review").classList.add("hidden");
  $("review-search").blur();
}

// ---------------------------------------------------------------- list & editing

function save() {
  store.set("entries", state.entries);
  store.set("lastAlts", state.lastAlts);
}

function renderAll() {
  const total = state.entries.reduce((s, e) => s + priceOf(e), 0);
  $("count").textContent = $("list-count").textContent = state.entries.length;
  $("total").textContent = $("list-total").textContent = money(total);
  const e = state.entries[state.entries.length - 1];
  $("last").classList.toggle("hidden", !e);
  if (!e) return;
  $("last-img").src = e.img_small;
  $("last-name").textContent = e.name;
  $("last-set").textContent = `${e.set_name} · ${e.set.toUpperCase()} #${e.cn} · ${e.rarity}`;
  const p = priceOf(e);
  $("last-price").textContent = money(p);
  $("last-price").classList.toggle("high", p >= 5);
  $("last-finish").textContent = e.finish === "nonfoil" ? "non-foil" : e.finish;
  $("last-finish").classList.toggle("foil", e.finish !== "nonfoil");
  const alts = state.lastAlts;
  const hasAlts = alts && alts.list.length > 1 && alts.list.some(a => a.id === e.id);
  $("prev-print").disabled = $("next-print").disabled = !hasAlts;
  $("print-idx").textContent = hasAlts ? `${alts.idx + 1}/${alts.list.length}` : "";
  $("foil-btn").disabled = e.finishes.length < 2;
}

function cyclePrinting(step) {
  const alts = state.lastAlts, e = state.entries[state.entries.length - 1];
  if (!alts || !e) return;
  alts.idx = (alts.idx + step + alts.list.length) % alts.list.length;
  const p = alts.list[alts.idx];
  state.entries[state.entries.length - 1] = { ...p, finish: pickFinish(p, e.finish), t: e.t };
  save(); renderAll();
}

function cycleFinish() {
  const e = state.entries[state.entries.length - 1];
  if (!e) return;
  const i = e.finishes.indexOf(e.finish);
  e.finish = e.finishes[(i + 1) % e.finishes.length];
  save(); renderAll();
}

function removeLast() {
  const e = state.entries.pop();
  state.lastAlts = null;
  save(); renderAll();
  if (e) status(`Removed ${e.name}`);
}

function renderList() {
  const ul = $("list-items");
  ul.innerHTML = "";
  if (!state.entries.length) { ul.innerHTML = `<li class="empty">No cards yet</li>`; return; }
  state.entries.map((e, i) => [e, i]).reverse().forEach(([e, i]) => {
    const li = document.createElement("li");
    li.innerHTML = `<img loading="lazy" alt=""><div class="meta"><div></div><div class="muted small"></div></div><span></span><button class="x" aria-label="Remove">✕</button>`;
    li.querySelector("img").src = e.img_small;
    li.querySelector(".meta div").textContent = e.name + (e.finish !== "nonfoil" ? ` (${e.finish})` : "");
    li.querySelector(".meta .small").textContent = `${e.set.toUpperCase()} #${e.cn}`;
    li.querySelector("span").textContent = money(priceOf(e));
    li.querySelector(".x").onclick = () => {
      state.entries.splice(i, 1);
      if (i === state.entries.length) state.lastAlts = null;
      save(); renderAll(); renderList();
    };
    ul.appendChild(li);
  });
}

function grouped() {
  const m = new Map();
  for (const e of state.entries) {
    const k = `${e.id}|${e.finish}`;
    if (m.has(k)) m.get(k).qty++; else m.set(k, { e, qty: 1 });
  }
  return [...m.values()];
}

const csvCell = v => (/[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));

async function exportCSV() {
  if (!state.entries.length) return;
  const rows = [["Name", "Set code", "Set name", "Collector number", "Foil", "Rarity", "Quantity", "Scryfall ID",
    "Purchase price", "Condition", "Language", "Purchase price currency"]];
  for (const { e, qty } of grouped()) {
    rows.push([e.name, e.set.toUpperCase(), e.set_name, e.cn, e.finish === "nonfoil" ? "normal" : e.finish, e.rarity,
      qty, e.id, priceOf(e).toFixed(2), "near_mint", e.lang || "en", "USD"]);
  }
  const csv = rows.map(r => r.map(csvCell).join(",")).join("\n");
  const name = `cards_${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-")}_manabox.csv`;
  const file = new File([csv], name, { type: "text/csv" });
  if (navigator.canShare?.({ files: [file] })) {
    try { await navigator.share({ files: [file], title: name }); return; } catch (e) { if (e.name === "AbortError") return; }
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(file); a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

async function exportText() {
  const text = grouped().map(({ e, qty }) =>
    `${qty} ${e.name} (${e.set.toUpperCase()}) ${e.cn}${e.finish === "foil" ? " *F*" : e.finish === "etched" ? " *E*" : ""}`).join("\n");
  try {
    await navigator.clipboard.writeText(text);
    $("export-text").textContent = "Copied ✓";
    setTimeout(() => ($("export-text").textContent = "Copy list"), 1500);
  } catch {
    prompt("Copy this list:", text);
  }
}

// ---------------------------------------------------------------- feedback

let audio;
function unlockAudio() {
  try { audio = audio || new (window.AudioContext || window.webkitAudioContext)(); audio.resume(); } catch { /* no audio */ }
}
function beep(ok) {
  if (!state.settings.sound || !audio) return;
  const tones = ok ? [[1400, 0, 0.08]] : [[600, 0, 0.1], [600, 0.16, 0.1]];
  for (const [f, at, len] of tones) {
    const o = audio.createOscillator(), g = audio.createGain();
    o.frequency.value = f; o.connect(g); g.connect(audio.destination);
    const t = audio.currentTime + at;
    g.gain.setValueAtTime(0.15, t); g.gain.exponentialRampToValueAtTime(0.001, t + len);
    o.start(t); o.stop(t + len);
  }
  navigator.vibrate?.(ok ? 30 : [40, 60, 40]);
}

let statusTimer;
function status(text, kind = "") {
  const el = $("status");
  el.textContent = text;
  el.style.color = kind === "bad" ? "var(--danger)" : kind === "ok" ? "var(--accent)" : kind === "busy" ? "var(--warn)" : "";
  clearTimeout(statusTimer);
  if (text && kind !== "busy") statusTimer = setTimeout(() => (el.textContent = ""), 3500);
}
function setGuide(cls) { $("guide").className = cls; }

// ---------------------------------------------------------------- wiring

function saveSettings() { store.set("settings", state.settings); }

$("start-btn").onclick = startCamera;
$("photo-start").onchange = e => { unlockAudio(); scanPhoto(e.target.files[0]); e.target.value = ""; };
$("photo-input").onchange = e => { scanPhoto(e.target.files[0]); e.target.value = ""; };
$("scan-btn").onclick = () => { state.armed = true; state.unsure = 3; scanNow(false); };
$("search-btn").onclick = () => openReview({ candidates: [], card: document.createElement("canvas"), text: "" });
$("prev-print").onclick = () => cyclePrinting(-1);
$("next-print").onclick = () => cyclePrinting(1);
$("foil-btn").onclick = cycleFinish;
$("undo-btn").onclick = removeLast;
$("review-skip").onclick = () => { closeReview(); status("Skipped"); };
let searchTimer;
$("review-search").oninput = e => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => renderChoices(matchName(e.target.value, 5)), 120);
};
$("list-btn").onclick = () => { renderList(); $("list").classList.remove("hidden"); };
$("list-close").onclick = () => $("list").classList.add("hidden");
$("export-csv").onclick = exportCSV;
$("export-text").onclick = exportText;
$("clear-list").onclick = () => {
  if (state.entries.length && confirm(`Remove all ${state.entries.length} cards from the list?`)) {
    state.entries = []; state.lastAlts = null; save(); renderAll(); renderList();
  }
};
$("settings-btn").onclick = () => $("settings").classList.remove("hidden");
$("settings-close").onclick = () => $("settings").classList.add("hidden");
$("auto-toggle").checked = state.settings.auto;
$("auto-toggle").onchange = e => { state.settings.auto = e.target.checked; state.armed = true; saveSettings(); };
$("lock-set").value = state.settings.lockSet;
$("lock-set").onchange = e => { state.settings.lockSet = e.target.value.trim().toLowerCase(); saveSettings(); };
$("default-foil").checked = state.settings.defaultFoil;
$("default-foil").onchange = e => { state.settings.defaultFoil = e.target.checked; saveSettings(); };
$("sound-on").checked = state.settings.sound;
$("sound-on").onchange = e => { state.settings.sound = e.target.checked; saveSettings(); };
for (const id of ["review", "list", "settings"]) {
  $(id).addEventListener("click", ev => { if (ev.target.id === id) { $(id).classList.add("hidden"); if (id === "review") closeReview(); } });
}

if ("serviceWorker" in navigator && location.protocol === "https:") {
  navigator.serviceWorker.register("sw.js").catch(() => {});
}

// Test hook: scan an image by URL without a camera (used by the automated tests).
window.cardScanner = {
  async scanUrl(url) {
    const blob = await (await fetch(url)).blob();
    await scanPhoto(blob);
    return state.entries[state.entries.length - 1];
  },
  state,
  matchName,
  CARD_W, CARD_H,
};

boot();
