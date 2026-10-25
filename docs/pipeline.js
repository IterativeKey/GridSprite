/* pipeline.js — static, in-browser port of gridsprite.py (GitHub Pages).
 *
 * Faithful 1:1 translation of the numpy pipeline: grid detection,
 * rectification, palette quantization, background flood fill, despeckle,
 * maximal-rectangle meshing, SVG generation. Validated bit-for-bit
 * against the Python implementation on the repo's sample images
 * (see tools/validate_static.py + tools/validate_static.mjs).
 *
 * Image input: { width, height, data } with data = Uint8ClampedArray RGBA
 * (4 bytes per pixel, row-major), matching PIL's Image.convert("RGBA").
 * Clean pixel art is read exactly (exactGrid); everything else goes through
 * detection on the RGB channels. Mapping value CLEAR (-1) is a transparent
 * cell.
 *
 * Runs both in the browser (window.GridSprite) and under node
 * (module.exports) for validation.
 */
"use strict";

const SEARCH_STEP = 0.05;   // candidate cell sizes, px
const ALIGN_TOL = 0.25;     // peak may sit at most 0.25*step from a grid line
const CLEAR = -1;           // mapping value of a transparent cell
const MAX_EXACT_COLORS = 256;

/* ------------------------------------------------------------- math utils */

/* numpy-style rounding: half-to-even (banker's rounding). */
function rint(x){
  const f = Math.floor(x);
  const d = x - f;
  if (d < 0.5) return f;
  if (d > 0.5) return f + 1;
  return (f % 2 === 0) ? f : f + 1;
}

function roundHalfEven(x){
  const f = Math.floor(x);
  const d = x - f;
  if (d < 0.5) return f;
  if (d > 0.5) return f + 1;
  return (f % 2 === 0) ? f : f + 1;
}

function clamp(v, lo, hi){ return v < lo ? lo : v > hi ? hi : v; }

/* np.percentile(profile, pct) with the default 'linear' interpolation. */
function percentile(sorted, pct){
  const n = sorted.length;
  if (n === 1) return sorted[0];
  const h = (n - 1) * (pct / 100);
  const lo = Math.floor(h);
  const frac = h - lo;
  const hi = Math.min(lo + 1, n - 1);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * frac;
}

function median(sorted){
  const n = sorted.length;
  if (n % 2 === 1) return sorted[(n - 1) / 2];
  return (sorted[n / 2 - 1] + sorted[n / 2]) / 2;
}

/* np.convolve(g, k, mode="same"): output length = g.length, window centered. */
function convolveSame(g, k){
  const n = g.length, m = k.length;
  const off = (m - 1) >> 1;                 // 4 for a length-9 kernel
  const out = new Float64Array(n);
  for (let i = 0; i < n; i++){
    let s = 0;
    for (let j = 0; j < m; j++){
      const gi = i + off - j;
      if (gi >= 0 && gi < n) s += g[gi] * k[j];
    }
    out[i] = s;
  }
  return out;
}

/* ------------------------------------------------------------------ colors */

function hexc(rgb){ // rgb: array/tuple of 3 ints
  return "#" + ((1 << 24) | (rgb[0] << 16) | (rgb[1] << 8) | rgb[2])
    .toString(16).slice(1);
}

function unhex(h){
  const n = parseInt(h.replace(/^#/, ""), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/* "%.3f" then rstrip("0").rstrip(".") */
function fmt(v){
  let s = v.toFixed(3);
  s = s.replace(/0+$/, "");
  if (s.endsWith(".")) s = s.slice(0, -1);
  return s;
}

/* ------------------------------------------------------------ grid metrics */

function edgeProfile(img, axis){
  // img: {height H, width W, data Uint8Array RGB}
  const H = img.height, W = img.width, d = img.data;
  let xlen, ylen;
  if (axis === 1){
    // vertical boundaries: |diff along x| per row, summed over rows -> len W-1
    xlen = W - 1; ylen = H;
  } else {
    // horizontal boundaries: |diff along y| per column, summed over cols
    xlen = W; ylen = H - 1;
  }
  const g = new Float64Array(axis === 1 ? xlen : ylen);
  if (axis === 1){
    for (let y = 0; y < ylen; y++){
      const row = y * W * 3;
      for (let x = 0; x < xlen; x++){
        const a = row + x * 3, b = a + 3;
        const la = (d[a] + d[a+1] + d[a+2]) / 3;
        const lb = (d[b] + d[b+1] + d[b+2]) / 3;
        g[x] += Math.abs(la - lb);
      }
    }
  } else {
    // horizontal boundaries: per row-boundary |diff| summed over columns
    // (np.diff(lum, axis=0).sum(axis=1)) -> length H-1, entry per row boundary
    for (let y = 0; y < ylen; y++){
      const a = y * W * 3, b = a + W * 3;
      let s = 0;
      for (let x = 0; x < xlen; x++){
        const p = a + x * 3, q = b + x * 3;
        const la = (d[p] + d[p+1] + d[p+2]) / 3;
        const lb = (d[q] + d[q+1] + d[q+2]) / 3;
        s += Math.abs(la - lb);
      }
      g[y] = s;
    }
  }
  // matches np.diff(img.mean(axis=2), axis).sum(order): per-pixel mean of
  // channels first (rounded), then |diff|, then ascending accumulation.
  // gaussian smoothing kernel k = exp(-0.5*((i-4)/2)^2), normalized
  const k = new Float64Array(9);
  let ks = 0;
  for (let i = 0; i < 9; i++){
    const v = Math.exp(-0.5 * ((i - 4) / 2.0) ** 2);
    k[i] = v; ks += v;
  }
  for (let i = 0; i < 9; i++) k[i] /= ks;
  return convolveSame(g, k);
}

function findStrongPeaks(profile, pct){
  if (pct === undefined) pct = 90.0;
  const sorted = Float64Array.from(profile).sort();
  const thr = percentile(sorted, pct);
  const n = profile.length;
  const peaks = [];
  for (let i = 2; i < n - 2; i++){
    if (profile[i] >= thr && profile[i] > profile[i-1] && profile[i] >= profile[i+1])
      peaks.push(i);
  }
  return peaks;
}

function circularPhase(amps, peaks, step){
  let S = 0, C = 0;
  for (let i = 0; i < peaks.length; i++){
    const ang = 2 * Math.PI * peaks[i] / step;
    S += amps[i] * Math.sin(ang);
    C += amps[i] * Math.cos(ang);
  }
  let ph = Math.atan2(S, C) * step / (2 * Math.PI);
  ph = ph % step;
  if (ph < 0) ph += step;          // python % semantics (result sign = divisor)
  return ph;
}

function peakDeviations(peaks, step, phase){
  const dev = new Float64Array(peaks.length);
  for (let i = 0; i < peaks.length; i++){
    const lines = roundHalfEven((peaks[i] - phase) / step) * step + phase;
    let v = Math.abs(peaks[i] - lines);
    const alt = step - v;
    dev[i] = Math.min(v, alt);
  }
  return dev;
}

function detectGridAxis(profile, minScale, maxScale){
  const peaks = findStrongPeaks(profile);
  if (peaks.length < 8)
    throw new Error("not enough edge structure to detect a grid");
  const amps = peaks.map(p => profile[p]);
  let triedOk = false;
  for (let i = 0;; i++){
    const step = maxScale - i * SEARCH_STEP;
    if (!(step > minScale - 1e-9)) break;    // np.arange(max, min-1e-9, -step)
    const phase = circularPhase(amps, peaks, step);
    const lo = -ALIGN_TOL * step, hi = ALIGN_TOL * step;
    for (let j = 0;; j++){
      const dp = lo + j * 0.25;
      if (dp >= hi) break;                   // np.arange(lo, hi, 0.25)
      const p = (phase + dp) % step;
      let ok = true, k = 0;
      // FIT_MARGIN mirrors gridsprite.py: knife-edge fits exactly at the
      // tolerance boundary are rejected so JS matches numpy on razor ties
      const limit = ALIGN_TOL * step - 1e-6;
      for (; k < peaks.length; k++){
        const lines = roundHalfEven((peaks[k] - p) / step) * step + p;
        let v = Math.abs(peaks[k] - lines);
        if (v > step - v) v = step - v;
        if (v > limit){ ok = false; break; }
      }
      if (ok) return { step, phase: p, dev: peakDeviations(peaks, step, p) };
    }
    triedOk = true;
  }
  if (triedOk)
    throw new Error("no common grid phase fits all strong edges; " +
                    "the image may mix resolutions or be floating art");
  throw new Error("no grid found");
}

function detectGrid(img, minScale, maxScale){
  const px = edgeProfile(img, 1);   // vertical boundaries
  const py = edgeProfile(img, 0);   // horizontal boundaries
  const gx = detectGridAxis(px, minScale, maxScale);
  const gy = detectGridAxis(py, minScale, maxScale);
  const W = img.width, H = img.height;
  const ncols = Math.max(2, rint((W - gx.phase) / gx.step));
  const nrows = Math.max(2, rint((H - gy.phase) / gy.step));
  return {
    sx: gx.step, x0: gx.phase, sy: gy.step, y0: gy.phase,
    ncols, nrows, devX: gx.dev, devY: gy.dev,
  };
}

/* -------------------------------------------------------- exact pixel art */

/* Clean pixel art read exactly: {grid, palette, mapping}, or null. Port of
 * exact_grid: only fully opaque or fully clear pixels, at most
 * MAX_EXACT_COLORS colors, even square blocks from the top-left corner (the
 * largest such block is one cell). Palette ordered by cell count, ties by
 * color value; clear cells map to CLEAR. */
function exactGrid(image){
  const W = image.width, H = image.height, d = image.data;
  const key = new Int32Array(W * H);
  for (let i = 0; i < W * H; i++){
    const a = d[i*4+3];
    if (a !== 0 && a !== 255) return null;
    key[i] = a === 255 ? (d[i*4] << 16) | (d[i*4+1] << 8) | d[i*4+2] : CLEAR;
  }
  let side = W, b = H;
  while (b){ [side, b] = [b, side % b]; }
  let block = side;
  for (; block >= 1; block--){
    if (side % block) continue;
    let even = true;
    for (let y = 0; y < H && even; y++){
      const row = Math.floor(y / block) * block * W;
      for (let x = 0; x < W; x++){
        if (key[y * W + x] !== key[row + Math.floor(x / block) * block]){ even = false; break; }
      }
    }
    if (even) break;
  }
  const nrows = H / block, ncols = W / block;
  if (nrows < 2 || ncols < 2) return null;
  const counts = new Map();
  const small = new Int32Array(nrows * ncols);
  for (let y = 0; y < nrows; y++){
    for (let x = 0; x < ncols; x++){
      const k = key[y * block * W + x * block];
      small[y * ncols + x] = k;
      if (k !== CLEAR) counts.set(k, (counts.get(k) || 0) + 1);
    }
  }
  if (counts.size === 0 || counts.size > MAX_EXACT_COLORS) return null;
  const colors = [...counts.keys()].sort((p, q) => p - q)
    .sort((p, q) => counts.get(q) - counts.get(p));   // stable: ties by value
  const rank = new Map(colors.map((c, k) => [c, k]));
  const mapping = new Int32Array(nrows * ncols);
  for (let i = 0; i < mapping.length; i++){
    mapping[i] = small[i] === CLEAR ? CLEAR : rank.get(small[i]);
  }
  const palette = new Uint8Array(colors.length * 3);
  colors.forEach((c, k) => {
    palette[k*3] = (c >> 16) & 255; palette[k*3+1] = (c >> 8) & 255; palette[k*3+2] = c & 255;
  });
  const zero = new Float64Array(1);
  return {
    grid: { sx: block, x0: 0, sy: block, y0: 0, ncols, nrows, devX: zero, devY: zero, exact: true },
    palette, mapping,
  };
}

/* RGBA image -> the RGB image grid detection reads. */
function rgbOf(image){
  const n = image.width * image.height, d = image.data;
  const rgb = new Uint8Array(n * 3);
  for (let i = 0; i < n; i++){
    rgb[i*3] = d[i*4]; rgb[i*3+1] = d[i*4+1]; rgb[i*3+2] = d[i*4+2];
  }
  return { width: image.width, height: image.height, data: rgb };
}

/* ----------------------------------------------------------- rectification */

function rectify(img, grid, inner){
  if (inner === undefined) inner = 0.15;
  const H = img.height, W = img.width, d = img.data;
  const {nrows, ncols, sx, sy, x0, y0} = grid;
  const cells = new Uint8Array(nrows * ncols * 3);
  let rmseNum = 0, rmseDen = 0;
  for (let i = 0; i < nrows; i++){
    let ya = Math.max(0, Math.floor(y0 + i * sy + inner * sy));
    let yb = Math.min(H, Math.ceil(y0 + (i + 1) * sy - inner * sy));
    if (yb <= ya){
      const oldYa = ya;
      ya = Math.max(0, i * 1);
      yb = Math.min(H, oldYa + 1);
    }
    for (let j = 0; j < ncols; j++){
      let xa = Math.max(0, Math.floor(x0 + j * sx + inner * sx));
      let xb = Math.min(W, Math.ceil(x0 + (j + 1) * sx - inner * sx));
      if (xb <= xa) xb = Math.min(W, xa + 1);
      // dominant color (np.unique lexicographic order, argmax = first max)
      const counts = new Map();               // key -> count
      const sums = [0, 0, 0];
      let n = 0;
      for (let y = ya; y < yb; y++){
        const base = y * W * 3;
        for (let x = xa; x < xb; x++){
          const p = base + x * 3;
          const key = (d[p] << 16) | (d[p+1] << 8) | d[p+2];
          counts.set(key, (counts.get(key) || 0) + 1);
          sums[0] += d[p]; sums[1] += d[p+1]; sums[2] += d[p+2];
          n++;
        }
      }
      const keys = [...counts.keys()].sort((a, b) => a - b);
      let bestKey = -1, bestCount = -1;
      for (const key of keys){
        const cnt = counts.get(key);
        if (cnt > bestCount){ bestCount = cnt; bestKey = key; }
      }
      const ci = (i * ncols + j) * 3;
      cells[ci]   = (bestKey >> 16) & 255;
      cells[ci+1] = (bestKey >> 8) & 255;
      cells[ci+2] = bestKey & 255;
      // per-cell-mean reconstruction RMSE
      const mr = sums[0] / n, mg = sums[1] / n, mb = sums[2] / n;
      for (let y = ya; y < yb; y++){
        const base = y * W * 3;
        for (let x = xa; x < xb; x++){
          const p = base + x * 3;
          rmseNum += (d[p] - mr) ** 2 + (d[p+1] - mg) ** 2 + (d[p+2] - mb) ** 2;
        }
      }
      rmseDen += n;
    }
  }
  const rmse = Math.sqrt(rmseNum / Math.max(1, rmseDen));
  return { cells, rmse };
}

/* ------------------------------------------------------------ quantization */

function buildPalette(cells, nrows, ncols, tol){
  const flat = new Float64Array(nrows * ncols * 3);
  for (let i = 0; i < flat.length; i++) flat[i] = cells[i];
  // np.unique(axis=0): lexically sorted unique colors + counts + inverse
  const uniqMap = new Map(); // key -> {idx, count}
  const uniqKeys = [];
  const inv = new Int32Array(nrows * ncols);
  for (let i = 0; i < nrows * ncols; i++){
    const key = (cells[i*3] << 16) | (cells[i*3+1] << 8) | cells[i*3+2];
    let e = uniqMap.get(key);
    if (e === undefined){
      e = { idx: uniqKeys.length, key, count: 0 };
      uniqMap.set(key, e);
      uniqKeys.push(key);
    }
    e.count++;
    inv[i] = e.idx;
  }
  uniqKeys.sort((a, b) => a - b);
  // remap uniq indices to sorted order
  const sortedIdx = uniqKeys.map(key => uniqMap.get(key).idx); // old idx per sorted
  const sortedCounts = uniqKeys.map(key => uniqMap.get(key).count);
  // inv must map to sorted positions:
  const oldToSorted = new Int32Array(uniqKeys.length);
  for (let s = 0; s < uniqKeys.length; s++) oldToSorted[sortedIdx[s]] = s;
  for (let i = 0; i < inv.length; i++) inv[i] = oldToSorted[inv[i]];
  const uniq = new Uint8Array(uniqKeys.length * 3);
  for (let s = 0; s < uniqKeys.length; s++){
    uniq[s*3]   = (uniqKeys[s] >> 16) & 255;
    uniq[s*3+1] = (uniqKeys[s] >> 8) & 255;
    uniq[s*3+2] = uniqKeys[s] & 255;
  }

  // greedy merging in descending count order (stable for ties)
  const order = uniqKeys.map((_, s) => s)
    .sort((a, b) => (sortedCounts[b] - sortedCounts[a]) || (a - b));
  const pal = [];
  for (const idx of order){
    const cr = uniq[idx*3], cg = uniq[idx*3+1], cb = uniq[idx*3+2];
    const w = sortedCounts[idx];
    let merged = false;
    for (const p of pal){
      const dr = cr - p.cent[0], dg = cg - p.cent[1], db2 = cb - p.cent[2];
      const dist = Math.sqrt(dr*dr + dg*dg + db2*db2);
      if (dist <= tol){
        p.count += w;
        p.sum[0] += cr * w; p.sum[1] += cg * w; p.sum[2] += cb * w;
        p.cent[0] = p.sum[0] / p.count;
        p.cent[1] = p.sum[1] / p.count;
        p.cent[2] = p.sum[2] / p.count;
        merged = true;
        break;
      }
    }
    if (!merged){
      pal.push({ cent: [cr, cg, cb], count: w, sum: [cr * w, cg * w, cb * w] });
    }
  }

  const K = pal.length;
  let centroids = new Float64Array(K * 3);
  for (let k = 0; k < K; k++){
    centroids[k*3] = pal[k].cent[0];
    centroids[k*3+1] = pal[k].cent[1];
    centroids[k*3+2] = pal[k].cent[2];
  }

  let idxMap;
  for (let pass = 0; pass < 2; pass++){
    // idx_map = argmin over squared distances (first minimum wins)
    idxMap = new Int32Array(uniqKeys.length);
    for (let s = 0; s < uniqKeys.length; s++){
      let best = 0, bestD = Infinity;
      for (let k = 0; k < K; k++){
        const dr = uniq[s*3]   - centroids[k*3];
        const dg = uniq[s*3+1] - centroids[k*3+1];
        const db = uniq[s*3+2] - centroids[k*3+2];
        const dd = dr*dr + dg*dg + db*db;
        if (dd < bestD){ bestD = dd; best = k; }
      }
      idxMap[s] = best;
    }
    if (pass === 0){
      // refine centroids: weighted mean of member colors
      const sums = new Float64Array(K * 3);
      const cnts = new Float64Array(K);
      for (let s = 0; s < uniqKeys.length; s++){
        const k = idxMap[s], w = sortedCounts[s];
        sums[k*3]   += uniq[s*3]   * w;
        sums[k*3+1] += uniq[s*3+1] * w;
        sums[k*3+2] += uniq[s*3+2] * w;
        cnts[k] += w;
      }
      for (let k = 0; k < K; k++){
        if (cnts[k] > 0){
          centroids[k*3]   = sums[k*3]   / cnts[k];
          centroids[k*3+1] = sums[k*3+1] / cnts[k];
          centroids[k*3+2] = sums[k*3+2] / cnts[k];
        }
      }
    } else {
      // keep idxMap (final assignment with refined centroids)
    }
  }
  // mapping = idx_map[inv], palette = rounded centroids
  const palette = new Uint8Array(K * 3);
  for (let k = 0; k < K; k++){
    palette[k*3]   = roundHalfEven(centroids[k*3]);
    palette[k*3+1] = roundHalfEven(centroids[k*3+1]);
    palette[k*3+2] = roundHalfEven(centroids[k*3+2]);
  }
  const mapping = new Uint16Array(nrows * ncols);
  for (let i = 0; i < nrows * ncols; i++) mapping[i] = idxMap[inv[i]];
  return { palette, mapping };
}

/* -------------------------------------------------- background / sections */

function despeckle(mapping, nrows, ncols, maxSize, skipIdx){
  const map = mapping.slice();
  const skip = skipIdx instanceof Set ? skipIdx : new Set(skipIdx || []);
  const labelOf = new Int32Array(nrows * ncols).fill(-1);
  const regions = []; // {c, cells: [{y,x},...], set: Set("y,x")}
  for (let y = 0; y < nrows; y++){
    for (let x = 0; x < ncols; x++){
      if (labelOf[y * ncols + x] >= 0) continue;
      const rid = regions.length;
      const c = map[y * ncols + x];
      const startKey = y + "," + x;
      const entry = { c, cells: [startKey], set: new Set([startKey]) };
      labelOf[y * ncols + x] = rid;
      const stack = [[y, x]];
      while (stack.length){
        const [cy, cx] = stack.pop();
        const neigh = [[cy-1, cx], [cy+1, cx], [cy, cx-1], [cy, cx+1]];
        for (const [ny, nx] of neigh){
          if (ny < 0 || nx < 0 || ny >= nrows || nx >= ncols) continue;
          if (labelOf[ny * ncols + nx] >= 0) continue;
          if (map[ny * ncols + nx] !== c) continue;
          labelOf[ny * ncols + nx] = rid;
          const key = ny + "," + nx;
          entry.cells.push(key);
          entry.set.add(key);
          stack.push([ny, nx]);
        }
      }
      regions.push(entry);
    }
  }
  let removedRegions = 0, removedCells = 0;
  for (const reg of regions){
    if (reg.c === CLEAR || skip.has(reg.c) || reg.cells.length >= maxSize) continue;
    const c = reg.c;
    const votes = new Map();
    for (const key of reg.cells){
      const [cy, cx] = key.split(",").map(Number);
      const neigh = [[cy-1, cx], [cy+1, cx], [cy, cx-1], [cy, cx+1]];
      for (const [ny, nx] of neigh){
        if (ny < 0 || nx < 0 || ny >= nrows || nx >= ncols) continue;
        if (reg.set.has(ny + "," + nx)) continue;
        const cc = map[ny * ncols + nx];
        if (cc === c) continue;
        votes.set(cc, (votes.get(cc) || 0) + 1);
      }
    }
    if (votes.size === 0) continue;
    let best = -1, bestCount = -1;
    for (const [cc, cnt] of votes){         // Map preserves insertion order
      if (cnt > bestCount){ bestCount = cnt; best = cc; }
    }
    for (const key of reg.cells){
      const [cy, cx] = key.split(",").map(Number);
      map[cy * ncols + cx] = best;
    }
    removedRegions++;
    removedCells += reg.cells.length;
  }
  return { mapping: map, removedRegions, removedCells };
}

function detectBackground(mapping, nrows, ncols){
  // transparent cells never count as the background color
  const counts = new Map();
  let ringLen = 0;
  const visit = (y, x) => {
    const v = mapping[y * ncols + x];
    if (v !== CLEAR) counts.set(v, (counts.get(v) || 0) + 1);
    ringLen++;
  };
  for (let x = 0; x < ncols; x++){ visit(0, x); visit(nrows - 1, x); }
  for (let y = 0; y < nrows; y++){ visit(y, 0); visit(y, ncols - 1); }
  // bincount + argmax = first index holding the max count
  let maxIdx = -1, maxCount = -1;
  for (const idx of [...counts.keys()].sort((a, b) => a - b)){
    const cnt = counts.get(idx);
    if (cnt > maxCount){ maxCount = cnt; maxIdx = idx; }
  }
  if (maxIdx < 0) return { idx: 0, share: 0 };
  return { idx: maxIdx, share: maxCount / ringLen };
}

function backgroundMask(mapping, nrows, ncols, bgIdx){
  const mask = new Uint8Array(nrows * ncols);
  const stack = [];
  const visit = (y, x) => {
    const i = y * ncols + x;
    if (mapping[i] === bgIdx && !mask[i]){
      mask[i] = 1;
      stack.push([y, x]);
    }
  };
  for (let x = 0; x < ncols; x++){ visit(0, x); visit(nrows - 1, x); }
  for (let y = 0; y < nrows; y++){ visit(y, 0); visit(y, ncols - 1); }
  while (stack.length){
    const [cy, cx] = stack.pop();
    const neigh = [[cy-1, cx], [cy+1, cx], [cy, cx-1], [cy, cx+1]];
    for (const [ny, nx] of neigh){
      if (ny < 0 || nx < 0 || ny >= nrows || nx >= ncols) continue;
      visit(ny, nx);
    }
  }
  return mask;
}

function connectedRegions(mapping, nrows, ncols){
  const seen = new Uint8Array(nrows * ncols);
  let regions = 0;
  for (let y = 0; y < nrows; y++){
    for (let x = 0; x < ncols; x++){
      if (seen[y * ncols + x]) continue;
      regions++;
      const c = mapping[y * ncols + x];
      const stack = [[y, x]];
      seen[y * ncols + x] = 1;
      while (stack.length){
        const [cy, cx] = stack.pop();
        const neigh = [[cy-1, cx], [cy+1, cx], [cy, cx-1], [cy, cx+1]];
        for (const [ny, nx] of neigh){
          if (ny < 0 || nx < 0 || ny >= nrows || nx >= ncols) continue;
          if (seen[ny * ncols + nx] || mapping[ny * ncols + nx] !== c) continue;
          seen[ny * ncols + nx] = 1;
          stack.push([ny, nx]);
        }
      }
    }
  }
  return regions;
}

/* --------------------------------------------------------------- meshing */

function mesh(mapping, nrows, ncols, freeMask){
  // free = all cells, minus transparent ones and free_mask cells (background)
  const free = new Uint8Array(nrows * ncols);
  for (let i = 0; i < free.length; i++){
    free[i] = mapping[i] === CLEAR || (freeMask && freeMask[i]) ? 0 : 1;
  }
  const rects = [];
  for (let y = 0; y < nrows; y++){
    for (let x = 0; x < ncols; x++){
      const i = y * ncols + x;
      if (!free[i]) continue;
      const c = mapping[i];
      let w = 1;
      while (x + w < ncols && free[y * ncols + x + w] && mapping[y * ncols + x + w] === c) w++;
      let h = 1;
      grow: while (y + h < nrows){
        for (let k = 0; k < w; k++){
          const j = (y + h) * ncols + x + k;
          if (!free[j] || mapping[j] !== c) break grow;
        }
        h++;
      }
      for (let dy = 0; dy < h; dy++){
        free.fill(0, (y + dy) * ncols + x, (y + dy) * ncols + x + w);
      }
      rects.push([x, y, w, h, c]);
    }
  }
  return rects;
}

/* -------------------------------------------------------------- SVG / PNG */

function svgString(palette, rects, ncols, nrows, transparentIdx){
  const K = palette.length / 3;
  const cellsUsed = new Array(K).fill(0);
  for (const [, , w, h, c] of rects) cellsUsed[c] += w * h;
  const sorted = rects.slice().sort((a, b) => a[4] - b[4]); // stable
  const lines = [];
  lines.push(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${ncols} ${nrows}" ` +
    `width="${ncols * 16}" height="${nrows * 16}" shape-rendering="crispEdges">`);
  lines.push('  <!-- Palette: one <g> per color. To recolor, change the fill of the');
  lines.push('       matching g element (hex values are unique to the whole file). -->');
  const col = k => hexc([palette[k*3], palette[k*3+1], palette[k*3+2]]);
  for (let k = 0; k < K; k++){
    if (k === transparentIdx){
      lines.push(`  <!-- pc${k} ${col(k)} (${cellsUsed[k]} cells) ` +
                 `-> background color; edge-connected region is transparent ` +
                 `-->`);
    } else if (cellsUsed[k] > 0){
      lines.push(`  <!-- pc${k} ${col(k)} (${cellsUsed[k]} cells) -->`);
    }
  }
  for (let k = 0; k < K; k++){
    const group = sorted.filter(r => r[4] === k);
    if (!group.length) continue;
    lines.push(`  <g id="pc${k}" fill="${col(k)}">`);
    for (const [x, y, w, h] of group){
      lines.push(`    <rect x="${fmt(x)}" y="${fmt(y)}" width="${fmt(w)}" height="${fmt(h)}"/>`);
    }
    lines.push('  </g>');
  }
  lines.push('</svg>');
  return lines.join("\n") + "\n";
}

function coloredRGBA(palette, mapping, nrows, ncols, bgMask){
  const rgba = new Uint8Array(nrows * ncols * 4);
  for (let i = 0; i < nrows * ncols; i++){
    if (mapping[i] === CLEAR) continue;   // clear black
    const p = mapping[i] * 3, q = i * 4;
    rgba[q] = palette[p];
    rgba[q+1] = palette[p+1];
    rgba[q+2] = palette[p+2];
    rgba[q+3] = (bgMask && bgMask[i]) ? 0 : 255;
  }
  return rgba;
}

/* nearest-neighbor upscale to the source resolution (preview PNG) */
function scaleNearest(rgba, nrows, ncols, W, H){
  const out = new Uint8Array(W * H * 4);
  for (let y = 0; y < H; y++){
    const sy = Math.min(nrows - 1, Math.floor((y + 0.5) * nrows / H));
    for (let x = 0; x < W; x++){
      const sx = Math.min(ncols - 1, Math.floor((x + 0.5) * ncols / W));
      const s = (sy * ncols + sx) * 4, t = (y * W + x) * 4;
      out[t] = rgba[s]; out[t+1] = rgba[s+1];
      out[t+2] = rgba[s+2]; out[t+3] = rgba[s+3];
    }
  }
  return out;
}

function paletteFiles(palette, mapping, nrows, ncols, transparentIdx){
  const K = palette.length / 3;
  const counts = new Array(K).fill(0);
  for (let i = 0; i < nrows * ncols; i++) if (mapping[i] !== CLEAR) counts[mapping[i]]++;
  const entries = [];
  for (let k = 0; k < K; k++){
    entries.push({
      index: k,
      hex: hexc([palette[k*3], palette[k*3+1], palette[k*3+2]]),
      cells: counts[k],
      transparent: k === transparentIdx,
    });
  }
  const palJson = JSON.stringify({ grid: [nrows, ncols], palette: entries }, null, 2);
  let palTxt = "";
  for (const e of entries){
    palTxt += e.hex + (e.transparent ? " (transparent)" : "") + "\n";
  }
  return { palJson, palTxt };
}

/* --------------------------------------------------------- pipeline entry */

function to2D(mapping, nrows, ncols){
  const rows = [];
  for (let y = 0; y < nrows; y++){
    rows.push(Array.from(mapping.slice(y * ncols, (y + 1) * ncols)));
  }
  return rows;
}

/* Equivalent of the server's /analyze.
 * image: {width, height, data (RGBA bytes)}; opts: {name, colorTol, minScale, maxScale} */
function analyzeImage(image, opts){
  opts = opts || {};
  const colorTol = opts.colorTol !== undefined ? opts.colorTol : 18.0;
  const minScale = opts.minScale !== undefined ? opts.minScale : 4.0;
  const maxScale = opts.maxScale !== undefined ? opts.maxScale : 64.0;
  const W = image.width, H = image.height;
  let grid, rmse, palette, mapping;
  const exact = exactGrid(image);
  if (exact){
    ({ grid, palette, mapping } = exact);
    rmse = 0;
  } else {
    const rgb = rgbOf(image);
    grid = detectGrid(rgb, minScale, maxScale);
    grid.exact = false;
    let cells;
    ({ cells, rmse } = rectify(rgb, grid));
    ({ palette, mapping } = buildPalette(cells, grid.nrows, grid.ncols, colorTol));
  }
  const { idx: bgIdx, share } = detectBackground(mapping, grid.nrows, grid.ncols);
  const transparent = share >= 0.3 ? bgIdx : null;
  const sortedX = Float64Array.from(grid.devX).sort();
  const sortedY = Float64Array.from(grid.devY).sort();
  const K = palette.length / 3;
  const palHex = [];
  for (let k = 0; k < K; k++){
    palHex.push(hexc([palette[k*3], palette[k*3+1], palette[k*3+2]]));
  }
  return {
    name: opts.name || "dropped.png",
    sourceW: W, sourceH: H,
    ncols: grid.ncols, nrows: grid.nrows, exact: grid.exact,
    cellW: Math.round(grid.sx * 100) / 100,
    cellH: Math.round(grid.sy * 100) / 100,
    rmse: Math.round(rmse * 100) / 100,
    alignment: [Math.round(median(sortedX) * 100) / 100,
                Math.round(median(sortedY) * 100) / 100],
    bgShare: Math.round(share * 1000) / 1000,
    palette: palHex,
    mapping: to2D(mapping, grid.nrows, grid.ncols),
    transparent,
  };
}

/* Equivalent of the server's /export.
 * state: {name, palette (hex list), mapping (2D rows), transparentOn,
 *         transparent, sourceW, sourceH}
 * Returns strings/binary arrays; the caller turns them into files. */
function exportArtifacts(state, despeckleSize){
  despeckleSize = despeckleSize || 0;
  const nrows = state.mapping.length, ncols = state.mapping[0].length;
  let mapping = new Int32Array(nrows * ncols);
  for (let y = 0; y < nrows; y++)
    for (let x = 0; x < ncols; x++) mapping[y * ncols + x] = state.mapping[y][x];
  const paletteFlat = new Uint8Array(state.palette.length * 3);
  state.palette.forEach((h, i) => {
    const [r, g, b] = unhex(h);
    paletteFlat[i*3] = r; paletteFlat[i*3+1] = g; paletteFlat[i*3+2] = b;
  });
  const transparent = (state.transparentOn && state.transparent !== null &&
                       state.transparent !== undefined) ? state.transparent : null;

  let removed = 0;
  if (despeckleSize > 1){
    const res = despeckle(mapping, nrows, ncols, despeckleSize,
                          transparent !== null ? new Set([transparent]) : []);
    mapping = res.mapping;
    removed = res.removedRegions;
  }

  const bgMask = transparent !== null
    ? backgroundMask(mapping, nrows, ncols, transparent) : null;
  const rects = mesh(mapping, nrows, ncols, bgMask);
  const regions = connectedRegions(mapping, nrows, ncols);
  const svg = svgString(paletteFlat, rects, ncols, nrows, transparent);
  const pngNative = coloredRGBA(paletteFlat, mapping, nrows, ncols, bgMask);
  const preview = scaleNearest(pngNative, nrows, ncols, state.sourceW, state.sourceH);
  const { palJson, palTxt } = paletteFiles(paletteFlat, mapping, nrows, ncols, transparent);

  const base = (state.name || "sprite").replace(/\.[^.]*$/, "")
    .replace(/[^A-Za-z0-9_.-]/g, "_");
  return {
    base,
    files: [
      { name: base + ".svg", type: "image/svg+xml", text: svg },
      { name: base + ".png", type: "image/png", rgba: pngNative,
        width: ncols, height: nrows },
      { name: base + ".rect.png", type: "image/png", rgba: preview,
        width: state.sourceW, height: state.sourceH },
      { name: base + ".palette.json", type: "application/json", text: palJson },
      { name: base + ".palette.txt", type: "text/plain", text: palTxt },
    ],
    stats: { despeckled: removed, rects: rects.length, regions },
  };
}

/* --------------------------------------------------------------- exports */

const GridSprite = {
  SEARCH_STEP, ALIGN_TOL, CLEAR, rint, roundHalfEven, percentile, median,
  edgeProfile, findStrongPeaks, circularPhase, peakDeviations,
  detectGrid, detectGridAxis, exactGrid, rgbOf, rectify, buildPalette, despeckle,
  detectBackground, backgroundMask, connectedRegions, mesh,
  hexc, unhex, svgString, coloredRGBA, scaleNearest, paletteFiles,
  analyzeImage, exportArtifacts,
};

if (typeof module === "object" && module.exports) module.exports = GridSprite;
if (typeof window !== "undefined") window.GridSprite = GridSprite;
