// Fuzzy card-name matching, same scoring as rapidfuzz's fuzz.ratio in the desktop app.

let lookups = [], names = [];

export function initNames(pairs) {
  lookups = pairs.map(p => p[0]);
  names = pairs.map(p => p[1]);
}

// Lower-case, strip accents and odd characters so 'ALTAÏR' matches 'Altaïr'.
export function normalise(text) {
  return text
    .normalize("NFKD")
    .replace(/[^\x00-\x7f]/g, "")
    .replace(/`/g, "'")
    .toLowerCase()
    .replace(/[^a-z0-9',\-/ ]/g, "")
    .trim();
}

const row = new Uint16Array(256);

// 100 * 2 * LCS / (len(a) + len(b))  — the "indel" similarity rapidfuzz uses.
function ratio(a, b) {
  const n = a.length, m = b.length;
  if (!n || !m) return 0;
  row.fill(0, 0, m + 1);
  for (let i = 1; i <= n; i++) {
    let prev = 0;
    const ca = a.charCodeAt(i - 1);
    for (let j = 1; j <= m; j++) {
      const tmp = row[j];
      row[j] = ca === b.charCodeAt(j - 1) ? prev + 1 : Math.max(row[j], row[j - 1]);
      prev = tmp;
    }
  }
  return (200 * row[m]) / (n + m);
}

// Returns [[cardName, score], ...] best first.
export function matchName(text, limit = 5) {
  const q = normalise(text).slice(0, 250);
  if (q.length < 3) return [];
  const best = []; // kept sorted, at most `limit` long
  let floor = 0;
  for (let i = 0; i < lookups.length; i++) {
    const l = lookups[i];
    // Upper bound on the score from lengths alone; skip if it can't make the list.
    if ((200 * Math.min(q.length, l.length)) / (q.length + l.length) <= floor) continue;
    const s = ratio(q, l.slice(0, 250));
    if (s <= floor) continue;
    const name = names[i];
    const existing = best.findIndex(b => b[0] === name);
    if (existing >= 0) {
      if (best[existing][1] >= s) continue;
      best.splice(existing, 1);
    }
    best.push([name, s]);
    best.sort((a, b) => b[1] - a[1]);
    if (best.length > limit) best.pop();
    if (best.length === limit) floor = best[limit - 1][1];
  }
  return best;
}
