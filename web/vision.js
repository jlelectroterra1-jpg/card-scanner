// Card finding (OpenCV.js), name reading (PaddleOCR model via onnxruntime-web)
// and printing matching. Mirrors recognizer.py / printmatch.py in the desktop app.

export const CARD_W = 630, CARD_H = 880;
const NAME_BOX = [0.04, 0.025, 0.80, 0.105];
const NAME_SHIFTS = [0, -0.012, 0.012];
const SIG_W = 40, SIG_H = 56;

let cv, ort, session, chars;

export async function initVision(cvModule, ortModule, modelBytes, dict) {
  cv = cvModule;
  ort = ortModule;
  session = await ort.InferenceSession.create(modelBytes, { executionProviders: ["wasm"] });
  // CTC class 0 is "blank"; the dictionary's last entry "" is the space.
  chars = ["", ...dict.map(c => c || " ")];
}

// ---- finding the card ----------------------------------------------------

// canvas -> straightened card as a CARD_W x CARD_H canvas, or null
export function findCard(srcCanvas) {
  const src = cv.imread(srcCanvas);
  const gray = new cv.Mat(), edges = new cv.Mat(), kernel = cv.Mat.ones(5, 5, cv.CV_8U);
  const contours = new cv.MatVector(), hierarchy = new cv.Mat();
  try {
    cv.cvtColor(src, gray, cv.COLOR_RGBA2GRAY);
    cv.GaussianBlur(gray, gray, new cv.Size(5, 5), 0);
    cv.Canny(gray, edges, 40, 120);
    cv.dilate(edges, edges, kernel);
    cv.findContours(edges, contours, hierarchy, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE);
    // Take the biggest outline that is actually card-shaped, so a card held far
    // away (small in the picture) is still found but a random blob isn't.
    const minArea = 0.025 * src.cols * src.rows;
    const outlines = [];
    for (let i = 0; i < contours.size(); i++) {
      const c = contours.get(i);
      const a = cv.contourArea(c);
      if (a >= minArea) outlines.push({ a, rect: cv.minAreaRect(c) });
      c.delete();
    }
    outlines.sort((x, y) => y.a - x.a);
    const found = outlines.find(({ a, rect: r }) => {
      const rw = r.size.width, rh = r.size.height;
      if (rw * rh <= 0) return false;
      const aspect = Math.min(rw, rh) / Math.max(rw, rh);
      return aspect >= 0.6 && aspect <= 0.85 && a / (rw * rh) > 0.75;
    });
    if (!found) return null;
    const rect = found.rect;
    let [tl, tr, br, bl] = orderCorners(boxPoints(rect));
    if (dist(tl, tr) > dist(tl, bl)) [tl, tr, br, bl] = [tr, br, bl, tl]; // sideways -> portrait
    const from = cv.matFromArray(4, 1, cv.CV_32FC2, [tl.x, tl.y, tr.x, tr.y, br.x, br.y, bl.x, bl.y]);
    const to = cv.matFromArray(4, 1, cv.CV_32FC2, [0, 0, CARD_W - 1, 0, CARD_W - 1, CARD_H - 1, 0, CARD_H - 1]);
    const M = cv.getPerspectiveTransform(from, to);
    const out = new cv.Mat();
    cv.warpPerspective(src, out, M, new cv.Size(CARD_W, CARD_H));
    const canvas = document.createElement("canvas");
    cv.imshow(canvas, out);
    [from, to, M, out].forEach(m => m.delete());
    return canvas;
  } finally {
    [src, gray, edges, kernel, contours, hierarchy].forEach(m => m.delete());
  }
}

function boxPoints(r) {
  const a = (r.angle * Math.PI) / 180, c = Math.cos(a) / 2, s = Math.sin(a) / 2;
  const { width: w, height: h } = r.size, { x, y } = r.center;
  return [
    { x: x - c * w + s * h, y: y - s * w - c * h },
    { x: x + c * w + s * h, y: y + s * w - c * h },
    { x: x + c * w - s * h, y: y + s * w + c * h },
    { x: x - c * w - s * h, y: y - s * w + c * h },
  ];
}

function orderCorners(pts) {
  const by = f => pts.reduce((a, b) => (f(b) < f(a) ? b : a));
  const byMax = f => pts.reduce((a, b) => (f(b) > f(a) ? b : a));
  return [by(p => p.x + p.y), by(p => p.y - p.x), byMax(p => p.x + p.y), byMax(p => p.y - p.x)];
}

const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

export function rotate180(canvas) {
  const out = document.createElement("canvas");
  out.width = canvas.width; out.height = canvas.height;
  const g = out.getContext("2d");
  g.translate(canvas.width, canvas.height);
  g.rotate(Math.PI);
  g.drawImage(canvas, 0, 0);
  return out;
}

// ---- reading text --------------------------------------------------------

// OCR one line of text from a region of `canvas` given as fractions [x0, y0, x1, y1].
export async function readLine(canvas, box) {
  const [x0, y0, x1, y1] = box;
  const sx = x0 * canvas.width, sy = y0 * canvas.height;
  const sw = (x1 - x0) * canvas.width, sh = (y1 - y0) * canvas.height;
  const H = 48, W = Math.min(640, Math.max(16, Math.ceil((H * sw) / sh)));
  const c = document.createElement("canvas");
  c.width = W; c.height = H;
  const g = c.getContext("2d", { willReadFrequently: true });
  g.imageSmoothingQuality = "high";
  g.drawImage(canvas, sx, sy, sw, sh, 0, 0, W, H);
  const px = g.getImageData(0, 0, W, H).data;
  // Model input: 1x3x48xW, BGR channel order (as trained with OpenCV), scaled to [-1, 1].
  const data = new Float32Array(3 * H * W), plane = H * W;
  for (let i = 0; i < plane; i++) {
    data[i] = px[i * 4 + 2] / 127.5 - 1;
    data[plane + i] = px[i * 4 + 1] / 127.5 - 1;
    data[2 * plane + i] = px[i * 4] / 127.5 - 1;
  }
  const input = new ort.Tensor("float32", data, [1, 3, H, W]);
  const out = (await session.run({ [session.inputNames[0]]: input }))[session.outputNames[0]];
  const [, T, K] = out.dims, p = out.data;
  let text = "", last = -1;
  for (let t = 0; t < T; t++) {
    let bi = 0, bv = -Infinity;
    for (let k = 0; k < K; k++) if (p[t * K + k] > bv) { bv = p[t * K + k]; bi = k; }
    if (bi !== last && bi !== 0) text += chars[bi] ?? "";
    last = bi;
  }
  return text.trim();
}

// Read the name bar (both ways up) and fuzzy-match it. Returns
// { card, text, candidates: [[name, score]], confident }.
export async function readName(card, matchName) {
  let best = { text: "", candidates: [], top: 0, card };
  for (const img of [card, rotate180(card)]) {
    for (const dy of NAME_SHIFTS) {
      const text = await readLine(img, [NAME_BOX[0], NAME_BOX[1] + dy, NAME_BOX[2], NAME_BOX[3] + dy]);
      const candidates = matchName(text);
      const top = candidates[0]?.[1] ?? 0;
      if (top > best.top) best = { text, candidates, top, card: img };
      if (best.top >= 95) break;
    }
    if (best.top >= 95) break;
  }
  const second = best.candidates[1]?.[1] ?? 0;
  // An exact read is trusted even when a similar name exists (Lightning Bolt / Lightning Colt).
  best.confident = best.top >= 97 || (best.top >= 85 && best.top - second >= 8);
  return best;
}

// ---- picking the printing ------------------------------------------------

const sigCache = new Map();

function signatureOf(source) {
  const c = document.createElement("canvas");
  c.width = SIG_W; c.height = SIG_H;
  const g = c.getContext("2d", { willReadFrequently: true });
  g.imageSmoothingQuality = "high";
  g.filter = "blur(0.6px)";
  g.drawImage(source, 0, 0, SIG_W, SIG_H);
  const px = g.getImageData(0, 0, SIG_W, SIG_H).data, n = SIG_W * SIG_H;
  const out = new Float32Array(n * 3);
  for (let ch = 0; ch < 3; ch++) {
    let mean = 0, sq = 0;
    for (let i = 0; i < n; i++) mean += px[i * 4 + ch];
    mean /= n;
    for (let i = 0; i < n; i++) sq += (px[i * 4 + ch] - mean) ** 2;
    const sd = Math.sqrt(sq / n) + 1e-3;
    for (let i = 0; i < n; i++) out[i * 3 + ch] = (px[i * 4 + ch] - mean) / sd;
  }
  return out;
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = url;
  });
}

async function referenceSignature(p) {
  if (!sigCache.has(p.id)) {
    sigCache.set(p.id, loadImage(p.img_small).then(signatureOf).catch(() => null));
  }
  return sigCache.get(p.id);
}

// Sort printings by how much they look like the scanned card. Reprints with the
// same art and frame look identical, so near-ties keep the regular printing first.
export async function rankPrintings(card, printings, maxCompare = 40, preferId = null) {
  if (printings.length < 2) return printings;
  const mine = signatureOf(card);
  const head = printings.slice(0, maxCompare);
  const sigs = await Promise.all(head.map(referenceSignature));
  const scored = head.map((p, i) => {
    let d = 9;
    if (sigs[i]) {
      d = 0;
      for (let k = 0; k < mine.length; k++) d += Math.abs(mine[k] - sigs[i][k]);
      d /= mine.length;
    }
    return { p, d, i };
  });
  scored.sort((a, b) => a.d - b.d);
  const cutoff = scored[0].d * 1.1 + 0.02;
  // Within look-alikes: the artwork the picture model recognised, then regular printings.
  const regular = p => (p.id === preferId ? 2 : 0) + (!p.promo && p.set !== "plst" ? 1 : 0);
  const close = scored.filter(s => s.d <= cutoff).sort((a, b) => regular(b.p) - regular(a.p) || a.i - b.i);
  const rest = scored.filter(s => s.d > cutoff);
  return [...close, ...rest].map(s => s.p).concat(printings.slice(maxCompare));
}

// ---- picture recognition (model trained on webcam-like shots; see train_model.py) ----

const PIC_W = 128, PIC_H = 176;
const PIC_MEAN = [0.485, 0.456, 0.406], PIC_STD = [0.229, 0.224, 0.225];
export const PICTURE_SURE_GAP = 0.08;
let picSession = null, artIndex = null, artProj = null, artMeta = null;

export async function initPicture(modelBytes, indexBuf, projBuf, meta) {
  picSession = await ort.InferenceSession.create(modelBytes, { executionProviders: ["wasm"] });
  artIndex = new Int8Array(indexBuf);
  artProj = new Float32Array(projBuf);
  artMeta = meta;
}

export const pictureReady = () => !!picSession;

async function embed(card) {
  const c = document.createElement("canvas");
  c.width = PIC_W; c.height = PIC_H;
  const g = c.getContext("2d", { willReadFrequently: true });
  g.imageSmoothingQuality = "high";
  g.drawImage(card, 0, 0, PIC_W, PIC_H);
  const px = g.getImageData(0, 0, PIC_W, PIC_H).data, plane = PIC_W * PIC_H;
  const x = new Float32Array(3 * plane);
  for (let i = 0; i < plane; i++) {
    for (let ch = 0; ch < 3; ch++) x[ch * plane + i] = (px[i * 4 + ch] / 255 - PIC_MEAN[ch]) / PIC_STD[ch];
  }
  const out = await picSession.run({ image: new ort.Tensor("float32", x, [1, 3, PIC_H, PIC_W]) });
  const e = out.embedding.data;                       // 512 numbers
  const D = artMeta.dims, q = new Float32Array(D);   // compress to D like the index
  for (let j = 0; j < D; j++) {
    let s = 0;
    for (let i = 0; i < e.length; i++) s += e[i] * artProj[i * D + j];
    q[j] = s;
  }
  let n = 0;
  for (let j = 0; j < D; j++) n += q[j] * q[j];
  n = Math.sqrt(n) || 1;
  for (let j = 0; j < D; j++) q[j] /= n;
  return q;
}

// Returns { hits: [{name, id, score}] (5 different names, best first), gap }.
async function searchPicture(card) {
  const q = await embed(card), D = artMeta.dims, N = artMeta.count;
  const top = []; // [score, row], best 60
  for (let r = 0; r < N; r++) {
    let s = 0;
    const o = r * D;
    for (let j = 0; j < D; j++) s += artIndex[o + j] * q[j];
    s /= 127;
    if (top.length < 60 || s > top[top.length - 1][0]) {
      top.push([s, r]);
      top.sort((a, b) => b[0] - a[0]);
      if (top.length > 60) top.pop();
    }
  }
  const hits = [], seen = new Set();
  for (const [score, r] of top) {
    const name = artMeta.names[artMeta.name_of[r]];
    if (seen.has(name)) continue;
    seen.add(name);
    hits.push({ name, id: artMeta.ids[r], score });
    if (hits.length === 5) break;
  }
  return { hits, gap: hits.length > 1 ? hits[0].score - hits[1].score : 0 };
}

// Try the card both ways up; returns the more confident result plus the canvas used.
export async function recognisePicture(card) {
  let best = { ...(await searchPicture(card)), card };
  if (best.gap < PICTURE_SURE_GAP) {
    const flipped = rotate180(card);
    const r = await searchPicture(flipped);
    if (r.hits[0] && r.hits[0].score > best.hits[0].score) best = { ...r, card: flipped };
  }
  best.sure = best.gap >= PICTURE_SURE_GAP;
  return best;
}
