
"use strict";
/* ------------------------------------------------------------------ state */
const sessions = {};              // sid -> state
let current = null;               // sid being edited
let mode = "global", conn = 4;
let selColor = -1;                // selected palette index (source, shimmer)
let selection = new Set();        // region selection as "y,x" keys
const CLEAR_CELL = -1;                 // mapping value of a transparent cell
let brushColor = null;            // palette index used by brush and fill
let undoStack = [];
let dragging = false;
let lastPaint = null;             // last cell a brush or erase drag painted
let marquee = null;               // {y0, x0, base} while dragging a rectangle selection
let pan = null;                   // pointer and scroll origin while panning
let spaceHeld = false;
let pointerFocus = false;         // focus last moved by pointer, not by Tab

const $ = id => document.getElementById(id);
const c = $("c"), ctx = c.getContext("2d", {willReadFrequently:false});
const o = $("overlay"), octx = o.getContext("2d");
const g = $("gridlines"), gctx = g.getContext("2d");
const canvasScroll = $("canvasScroll");
const ZOOMS = [1, 1.5, 2, 3, 4, 6, 8];   // multiples of the fit-to-view scale
const MAX_CANVAS_AREA = 16e6;            // px; Safari refuses larger canvases
let view = {scale: 8, zoom: 0};          // zoom indexes ZOOMS
let hoverPos = null;              // [y, x] under cursor (brush shimmer)
let shimmerRAF = 0;
let fillPreview = null;           // {key, cells} region under the cursor in fill mode

const modeTips = {
  global: "<b>Global</b> — click a palette color to shimmer-preview every cell it would change, then click another color, a preset or <b>Apply to affected</b> to recolor the group. Drag the canvas to pan.",
  region: "<b>Region</b> — click a cluster of same-colored cells (4- or 8-connected) to shimmer-select it, then click a color or preset to recolor it. Delete clears it.",
  select: "<b>Select</b> — drag a rectangle of cells (Shift adds to the selection), then click a color, a preset or <b>Apply to affected</b> to fill it. Delete clears it, Esc deselects.",
  brush:  "<b>Brush</b> — pick a color (palette chip, preset row or the picker), then drag on the canvas to paint. The cell under the pointer shimmers. Alt-click picks a color from the canvas.",
  fill:   "<b>Fill</b> — pick a color, then click a cluster of same-colored or transparent cells (4- or 8-connected) to fill it. The shimmer shows what would change. Alt-click picks a color.",
  erase:  "<b>Erase</b> — drag on the canvas to make cells transparent.",
  pick:   "<b>Pick</b> — click a cell to take its color for the brush; picking a transparent cell switches to Erase.",
};

/* ----------------------------------------------------------------- theme */
const themeBtn = $("themeBtn");
function applyTheme(t){
  document.documentElement.dataset.theme = t;
  themeBtn.textContent = t === "dark" ? "Light theme" : "Dark theme";
  try { localStorage.setItem("gs-theme", t); } catch (e) {}
}
applyTheme((() => {
  let t = null;
  try { t = localStorage.getItem("gs-theme"); } catch (e) {}
  if (t === "dark" || t === "light") return t;
  return matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
})());
themeBtn.onclick = () => {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  if (current) renderGrid();
};

/* ----------------------------------------------------------------- utils */
function newSid(){ return Math.random().toString(36).slice(2,10); }

function toast(msg, ms=2800){
  const t = document.createElement("div");
  t.className = "toast"; t.textContent = msg;
  $("toasts").appendChild(t);
  setTimeout(() => t.remove(), ms);
}

/* ---------------------------------------------------------------- files */
/* image decode + analysis happens entirely in the browser; colors and alpha
   stay exactly as stored so clean pixel art is read exactly */
async function decodeToRGBA(file){
  const bmp = await createImageBitmap(file, { imageOrientation: "from-image",
    colorSpaceConversion: "none", premultiplyAlpha: "none" });
  const cnv = document.createElement("canvas");
  cnv.width = bmp.width; cnv.height = bmp.height;
  const cctx = cnv.getContext("2d", { willReadFrequently: true });
  cctx.drawImage(bmp, 0, 0);
  bmp.close();
  const rgba = cctx.getImageData(0, 0, cnv.width, cnv.height).data;
  return { width: cnv.width, height: cnv.height, data: rgba };
}

async function analyzeFile(file){
  const img = await decodeToRGBA(file);
  const d = GridSprite.analyzeImage(img, { name: file.name });
  const sid = newSid();
  sessions[sid] = d;
  d.edited = false;
  d.transparentOn = d.transparent !== null && d.transparent !== undefined;
  return sid;
}

function fileRow(sid){ return document.querySelector(`.fitem[data-sid="${sid}"]`); }

function addFileRow(d){
  const row = document.createElement("div");
  row.className = "fitem"; row.dataset.sid = d.sid;
  row.innerHTML = `<span class="fname"></span><span class="fstatus"></span>`;
  row.querySelector(".fname").textContent = d.name;
  row.onclick = () => openSession(d.sid);
  $("files").appendChild(row);
}

function addPendingRow(f){
  const sid = newSid();
  const row = document.createElement("div");
  row.className = "fitem dead"; row.dataset.sid = sid;
  row.innerHTML = `<span class="fname"></span><span class="fstatus">…</span>`;
  row.querySelector(".fname").textContent = f.name;
  $("files").appendChild(row);
  return sid;
}

function addFailed(f, msg){
  const sid = newSid();
  const row = document.createElement("div");
  row.className = "fitem dead"; row.dataset.sid = sid;
  const st = document.createElement("span");
  st.className = "fstatus err";
  st.textContent = msg.length > 26 ? msg.slice(0, 25) + "…" : msg;
  const nm = document.createElement("span");
  nm.className = "fname"; nm.textContent = f.name; nm.title = msg;
  row.append(nm, st);
  $("files").prepend(row);
}

function setFileStatus(sid, cls, text, title){
  const row = fileRow(sid);
  if (!row) return;
  const st = row.querySelector(".fstatus");
  st.className = "fstatus " + (cls || "");
  st.textContent = text;
  if (title) st.title = title;
}

function openSession(sid){
  const s = sessions[sid];
  if (!s) return;
  current = sid;
  selection.clear(); selColor = -1; undoStack = []; view.zoom = 0;
  document.querySelectorAll(".fitem").forEach(f =>
    f.classList.toggle("sel", f.dataset.sid === sid));
  $("empty").style.display = "none";
  $("editor").hidden = false;
  const bg = $("bgchk");
  bg.disabled = !(s.transparent !== null && s.transparent !== undefined);
  bg.checked = !!s.transparentOn;
  $("bghint").textContent = bg.disabled
    ? "no dominant border color detected" : "";
  renderCanvas(); renderPalette(); updateStats(); updateUndoBtn();
  canvasScroll.scrollLeft = 0; canvasScroll.scrollTop = 0;
}

function sessionDims(s){ return `${s.ncols}×${s.nrows}`; }
function refreshFileStatus(sid){
  const s = sessions[sid];
  if (s) setFileStatus(sid, "ok",
    s.edited ? `${sessionDims(s)} · edited` : sessionDims(s));
}

function updateStats(){
  const s = sessions[current];
  if (s.exact){
    $("stats").textContent =
      `${s.sourceW}×${s.sourceH} source · clean pixel art read exactly` +
      ` · ${sessionDims(s)} grid (${s.cellW}px blocks) · ${s.palette.length} colors`;
    return;
  }
  $("stats").textContent =
    `${s.sourceW}×${s.sourceH} source · grid ${sessionDims(s)}` +
    ` (${s.cellW}×${s.cellH}px cells) · ${s.palette.length} colors` +
    ` · align ±${s.alignment[0]}/${s.alignment[1]}px · RMSE ${s.rmse}`;
}

function updateUndoBtn(){ $("undo").disabled = !undoStack.length || !current; }

/* ------------------------------------------------------------- rendering */

function computeBgMask(){
  const s = sessions[current];
  const mask = new Set();
  if (!(s.transparentOn) || s.transparent === null || s.transparent === undefined)
    return mask;
  const H = s.nrows, W = s.ncols, t = s.transparent;
  const stack = [];
  const visit = (y, x) => {
    if (s.mapping[y][x] !== t) return;
    const key = y + "," + x;
    if (mask.has(key)) return;
    mask.add(key); stack.push([y, x]);
  };
  for (let x = 0; x < W; x++){ visit(0, x); visit(H - 1, x); }
  for (let y = 0; y < H; y++){ visit(y, 0); visit(y, W - 1); }
  while (stack.length){
    const [cy, cx] = stack.pop();
    for (const [dy, dx] of [[-1,0],[1,0],[0,-1],[0,1]]){
      const ny = cy + dy, nx = cx + dx;
      if (ny < 0 || nx < 0 || ny >= H || nx >= W) continue;
      visit(ny, nx);
    }
  }
  return mask;
}

function fitScale(s){
  const pad = 28;
  const availW = Math.max(120, canvasScroll.clientWidth - pad);
  // flow layout (≤1120px): canvas-scroll height follows its content, so a
  // fixed viewport-relative cap avoids a grow/render feedback loop
  const availH = matchMedia("(max-width: 1120px)").matches
    ? Math.round(window.innerHeight * 0.55)
    : Math.max(240, canvasScroll.clientHeight - pad);
  return Math.max(2, Math.min(64,
    Math.floor(Math.min(availW / s.ncols, availH / s.nrows))));
}

// cell size in px at the current zoom, capped to the largest safe canvas
function viewScale(s){
  const cap = Math.floor(Math.sqrt(MAX_CANVAS_AREA / (s.ncols * s.nrows)));
  return Math.max(1, Math.min(cap, Math.round(fitScale(s) * ZOOMS[view.zoom])));
}

function renderCanvas(){
  const s = sessions[current];
  const scale = viewScale(s);
  view.scale = scale;
  $("zoomLabel").textContent = view.zoom ? ZOOMS[view.zoom] + "×" : "Fit";
  $("zoomOut").disabled = $("zoomFit").disabled = !view.zoom;
  $("zoomIn").disabled = view.zoom === ZOOMS.length - 1 ||
    scale < Math.round(fitScale(s) * ZOOMS[view.zoom]);
  c.width = s.ncols * scale; c.height = s.nrows * scale;
  o.width = c.width; o.height = c.height;
  ctx.clearRect(0, 0, c.width, c.height);
  ctx.imageSmoothingEnabled = false;
  s.bgMask = computeBgMask();
  for (let y = 0; y < s.nrows; y++){
    for (let x = 0; x < s.ncols; x++){
      const idx = s.mapping[y][x];
      if (isTransparent(y, x)) continue;
      ctx.fillStyle = s.palette[idx];
      ctx.fillRect(x*scale, y*scale, scale, scale);
    }
  }
  octx.clearRect(0, 0, o.width, o.height);
  fillPreview = null;
  renderGrid();
  updateShimmer();
}

function renderGrid(){
  const s = sessions[current];
  const k = view.scale;
  g.width = c.width; g.height = c.height;
  if (!$("gridchk").checked || k < 4) return;
  const rgb = getComputedStyle(document.documentElement)
    .getPropertyValue("--shimmer").trim() || "255,255,255";
  gctx.strokeStyle = `rgba(${rgb},0.16)`;
  gctx.lineWidth = 1;
  gctx.beginPath();
  for (let x = 1; x < s.ncols; x++){ gctx.moveTo(x*k + 0.5, 0); gctx.lineTo(x*k + 0.5, g.height); }
  for (let y = 1; y < s.nrows; y++){ gctx.moveTo(0, y*k + 0.5); gctx.lineTo(g.width, y*k + 0.5); }
  gctx.stroke();
}

function isTransparent(y, x){
  const s = sessions[current];
  return s.mapping[y][x] === CLEAR_CELL ||
    (s.bgMask !== undefined && s.bgMask.has(y + "," + x));
}

function colorName(i){
  return i === CLEAR_CELL ? "transparent" : sessions[current].palette[i];
}

/* --------------------------------------------------------------- shimmer */

function affectedCells(){
  if (!current) return null;
  const s = sessions[current];
  const out = new Set();
  if (hasSelection()){
    selection.forEach(k => out.add(k));
  } else if (mode === "global" && selColor >= 0){
    for (let y = 0; y < s.nrows; y++)
      for (let x = 0; x < s.ncols; x++)
        if (s.mapping[y][x] === selColor) out.add(y + "," + x);
  } else if (mode === "fill" && hoverPos && brushColor !== null){
    const key = hoverPos + "/" + conn;
    if (!fillPreview || fillPreview.key !== key)
      fillPreview = {key, cells: regionAt(hoverPos[0], hoverPos[1])};
    if (s.mapping[hoverPos[0]][hoverPos[1]] !== brushColor)
      fillPreview.cells.forEach(k => out.add(k));
  } else if (hoverPos && ((mode === "brush" && brushColor !== null) ||
                          mode === "erase" || mode === "pick")){
    out.add(hoverPos[0] + "," + hoverPos[1]);
  }
  return out.size ? out : null;
}

function drawShimmer(aff, ts){
  const k = view.scale;
  const rgb = getComputedStyle(document.documentElement)
    .getPropertyValue("--shimmer").trim() || "255,255,255";
  const wave = 0.5 + 0.5 * Math.sin(ts / 1000 * Math.PI * 1.6); // ~1.25s cycle
  octx.clearRect(0, 0, o.width, o.height);
  octx.lineWidth = Math.max(1.5, k * 0.12);
  octx.fillStyle = `rgba(${rgb},${(0.10 + 0.28 * wave).toFixed(3)})`;
  octx.strokeStyle = `rgba(${rgb},${(0.20 + 0.60 * wave).toFixed(3)})`;
  aff.forEach(key => {
    const [y, x] = key.split(",").map(Number);
    const x0 = x * k, y0 = y * k;
    octx.fillRect(x0, y0, k, k);
    octx.strokeRect(x0 + 0.5, y0 + 0.5, k - 1, k - 1);
  });
}

function shimmerLoop(ts){
  const aff = affectedCells();
  if (!aff){ octx.clearRect(0, 0, o.width, o.height); shimmerRAF = 0; return; }
  drawShimmer(aff, ts);
  shimmerRAF = requestAnimationFrame(shimmerLoop);
}

function updateShimmer(){
  if (shimmerRAF) return;
  if (!affectedCells()) return;
  shimmerRAF = requestAnimationFrame(shimmerLoop);
}

/* ---------------------------------------------------------------- palette */

function renderPalette(){
  const s = sessions[current];
  const counts = {};
  s.mapping.forEach(row => row.forEach(v => counts[v] = (counts[v]||0)+1));
  const box = $("palette"); box.innerHTML = "";
  const isBg = s.transparent !== null && s.transparent !== undefined;
  s.palette.forEach((hcol, i) => {
    const chip = document.createElement("div");
    chip.className = "pchip";
    if (i === selColor) chip.classList.add("selected-src");
    if (isBg && i === s.transparent) chip.classList.add("is-bg");
    chip.style.background = hcol;
    chip.title = `${hcol} · ${counts[i]||0} cells` +
      (isBg && i === s.transparent ? " (background)" : "");
    const n = document.createElement("span");
    n.className = "n"; n.textContent = counts[i]||0;
    chip.appendChild(n);
    chip.onclick = () => paletteClick(i);
    box.appendChild(chip);
  });
  $("pcount").textContent = s.palette.length + " colors";
}

/* ------------------------------------------------------------------ edits */

function snapshot(){
  const s = sessions[current];
  undoStack.push({mapping: JSON.parse(JSON.stringify(s.mapping)),
                  palette: [...s.palette]});
  if (undoStack.length > 100) undoStack.shift();
  updateUndoBtn();
}

function undo(){
  if (!undoStack.length || !current) return;
  const s = sessions[current];
  const st = undoStack.pop();
  sessions[current] = {...s, mapping: st.mapping, palette: st.palette, edited: true};
  selection.clear(); selColor = -1;
  refreshFileStatus(current);
  renderCanvas(); renderPalette(); updateUndoBtn();
}

function paletteClick(i){
  if (!current) { toast("Open a file first"); return; }
  const s = sessions[current];
  if (mode === "global"){
    if (selColor < 0){ selColor = i; }
    else if (selColor === i){ selColor = -1; }
    else {
      snapshot();
      const from = selColor;
      s.mapping = s.mapping.map(row => row.map(v => v === from ? i : v));
      s.edited = true;
      refreshFileStatus(current);
      toast(`Recolored all ${s.palette[from]} cells → ${s.palette[i]}`);
      selColor = -1;
    }
  } else if (hasSelection()){
    snapshot();
    const to = i;
    selection.forEach(key => {
      const [y, x] = key.split(",").map(Number);
      s.mapping[y][x] = to;
    });
    s.edited = true;
    refreshFileStatus(current);
    selection.clear(); selColor = -1;
  } else {
    brushColor = i; selColor = -1;
    if (mode === "erase" || mode === "pick") setMode("brush");
    toast(`${mode === "fill" ? "Fill" : "Brush"} → ${s.palette[i]}`);
  }
  renderCanvas(); renderPalette();
}

function ensureColor(hex){
  // index of an exact palette match, appending as a new color when missing
  const s = sessions[current];
  const i = s.palette.findIndex(h => h.toLowerCase() === hex.toLowerCase());
  return i >= 0 ? i : (s.palette.push(hex), s.palette.length - 1);
}

function applyCustomColor(){
  if (!current) { toast("Open a file first"); return; }
  const s = sessions[current];
  const hex = $("custom").value;
  if (hasSelection()){
    snapshot();
    const idx = ensureColor(hex);
    selection.forEach(key => {
      const [y, x] = key.split(",").map(Number);
      s.mapping[y][x] = idx;
    });
    s.edited = true;
    refreshFileStatus(current);
    toast(`Region recolored → ${hex}`);
    selection.clear(); selColor = -1;
  } else if (mode === "brush" || mode === "fill" || mode === "erase" || mode === "pick"){
    brushColor = ensureColor(hex);
    selColor = -1;
    if (mode === "erase" || mode === "pick") setMode("brush");
    toast(`${mode === "fill" ? "Fill" : "Brush"} → ${hex}`);
  } else {
    if (selColor < 0){ toast("Select a palette color first (shimmer shows what would change)"); return; }
    snapshot();
    s.palette[selColor] = hex;
    s.edited = true;
    refreshFileStatus(current);
    toast(`Palette entry → ${hex}`);
    selColor = -1;
  }
  renderCanvas(); renderPalette();
}

function hasSelection(){
  return (mode === "region" || mode === "select") && selection.size > 0;
}

function eraseSelection(){
  const s = sessions[current];
  snapshot();
  selection.forEach(key => {
    const [y, x] = key.split(",").map(Number);
    s.mapping[y][x] = CLEAR_CELL;
  });
  s.edited = true;
  refreshFileStatus(current);
  selection.clear(); selColor = -1;
  $("tipbar").textContent = "";
  renderCanvas(); renderPalette();
}

// selects the cell rectangle between the drag start and (y1, x1)
function updateMarquee(y1, x1){
  const {y0, x0, base} = marquee;
  selection = new Set(base);
  for (let y = Math.min(y0, y1); y <= Math.max(y0, y1); y++)
    for (let x = Math.min(x0, x1); x <= Math.max(x0, x1); x++)
      selection.add(y + "," + x);
  const w = Math.abs(x1 - x0) + 1, h = Math.abs(y1 - y0) + 1;
  $("tipbar").textContent = `Selected ${selection.size} cells (${w}×${h} rectangle) — click a palette color, a preset or Apply to fill them; Delete clears them, Esc deselects.`;
  updateShimmer();
}

function selectRegion(y, x){
  selection = regionAt(y, x);
  selColor = sessions[current].mapping[y][x];
}

// cells of the 4- or 8-connected same-valued cluster at (y, x), as "y,x" keys
function regionAt(y, x){
  const s = sessions[current];
  const start = s.mapping[y][x];
  const sel = new Set([y + "," + x]);
  const stack = [[y, x]];
  while (stack.length){
    const [cy, cx] = stack.pop();
    for (let dy = -1; dy <= 1; dy++)
      for (let dx = -1; dx <= 1; dx++){
        if (!dy && !dx) continue;
        if (conn === 4 && dy && dx) continue;
        const ny = cy + dy, nx = cx + dx;
        if (ny < 0 || nx < 0 || ny >= s.nrows || nx >= s.ncols) continue;
        if (s.mapping[ny][nx] !== start) continue;
        const key = ny + "," + nx;
        if (!sel.has(key)){ sel.add(key); stack.push([ny, nx]); }
      }
  }
  return sel;
}

function pickAt(y, x){
  const s = sessions[current];
  const v = s.mapping[y][x];
  if (v === CLEAR_CELL){
    setMode("erase");
    toast("Picked transparent → Erase");
    return;
  }
  brushColor = v;
  if (mode === "pick") setMode("brush");
  toast(`Picked ${s.palette[v]}`);
}

function fillAt(y, x){
  const s = sessions[current];
  if (s.mapping[y][x] === brushColor) return;
  snapshot();
  const cells = regionAt(y, x);
  cells.forEach(key => {
    const [cy, cx] = key.split(",").map(Number);
    s.mapping[cy][cx] = brushColor;
  });
  s.edited = true;
  refreshFileStatus(current);
  renderCanvas(); renderPalette();
}

/* ------------------------------------------------------- canvas pointers */

// cell under the pointer; off-canvas gives null, or the nearest edge cell
// when clamp is set
function canvasPos(ev, clamp){
  const r = c.getBoundingClientRect();
  const s = sessions[current];
  let x = Math.floor((ev.clientX - r.left) / view.scale);
  let y = Math.floor((ev.clientY - r.top) / view.scale);
  if (clamp){
    x = Math.max(0, Math.min(s.ncols - 1, x));
    y = Math.max(0, Math.min(s.nrows - 1, y));
  }
  if (x < 0 || y < 0 || x >= s.ncols || y >= s.nrows) return null;
  return [y, x];
}

/* ----------------------------------------------------------- zoom & pan */

// steps through ZOOMS, keeping the canvas point under anchor (client
// coordinates, the view center by default) in place
function setZoom(z, anchor){
  if (!current) return;
  z = Math.max(0, Math.min(ZOOMS.length - 1, z));
  if (z === view.zoom) return;
  const box = canvasScroll.getBoundingClientRect();
  const [ax, ay] = anchor || [box.left + box.width / 2, box.top + box.height / 2];
  const r0 = c.getBoundingClientRect();
  const u = (ax - r0.left) / view.scale, v = (ay - r0.top) / view.scale;
  view.zoom = z;
  renderCanvas();
  const r1 = c.getBoundingClientRect();
  canvasScroll.scrollLeft += r1.left + u * view.scale - ax;
  canvasScroll.scrollTop += r1.top + v * view.scale - ay;
}

$("zoomIn").onclick = () => setZoom(view.zoom + 1);
$("zoomOut").onclick = () => setZoom(view.zoom - 1);
$("zoomFit").onclick = () => setZoom(0);

// ctrl/cmd + wheel (and trackpad pinch) zooms at the pointer
let wheelAcc = 0;
canvasScroll.addEventListener("wheel", ev => {
  if (!current || !(ev.ctrlKey || ev.metaKey)) return;
  ev.preventDefault();
  wheelAcc += ev.deltaY;
  if (Math.abs(wheelAcc) < 50) return;
  setZoom(view.zoom + (wheelAcc < 0 ? 1 : -1), [ev.clientX, ev.clientY]);
  wheelAcc = 0;
}, {passive: false});

// middle-drag, space-drag or (in Global mode, where clicks do nothing) a
// plain drag pans; the capture listener runs before the canvas tools
canvasScroll.addEventListener("pointerdown", ev => {
  if (!current) return;
  if (!(ev.button === 1 || (ev.button === 0 && (spaceHeld || mode === "global")))) return;
  pan = {x: ev.clientX, y: ev.clientY,
         left: canvasScroll.scrollLeft, top: canvasScroll.scrollTop};
  canvasScroll.setPointerCapture(ev.pointerId);
  canvasScroll.classList.add("panning");
  ev.preventDefault(); ev.stopPropagation();
}, true);
canvasScroll.addEventListener("pointermove", ev => {
  if (!pan) return;
  canvasScroll.scrollLeft = pan.left - (ev.clientX - pan.x);
  canvasScroll.scrollTop = pan.top - (ev.clientY - pan.y);
});
const endPan = () => { pan = null; canvasScroll.classList.remove("panning"); };
canvasScroll.addEventListener("pointerup", endPan);
canvasScroll.addEventListener("pointercancel", endPan);

c.addEventListener("pointerdown", ev => {
  if (!current || ev.button > 0) return;
  const pos = canvasPos(ev); if (!pos) return;
  const [y, x] = pos;
  if (mode === "select"){
    marquee = {y0: y, x0: x, base: ev.shiftKey ? new Set(selection) : new Set()};
    c.setPointerCapture(ev.pointerId);
    updateMarquee(y, x);
  } else if (mode === "region"){
    selectRegion(y, x);
    renderCanvas();
    const s = sessions[current];
    $("tipbar").textContent = `Selected ${selection.size} cells (${colorName(selColor)}) — click a palette color, a preset or Apply to recolor them; click elsewhere for a different region.`;
  } else if (mode === "pick" || (ev.altKey && (mode === "brush" || mode === "fill"))){
    pickAt(y, x);
  } else if (mode === "fill"){
    if (brushColor === null){ toast("Pick a fill color from the palette first"); return; }
    fillAt(y, x);
  } else if (mode === "brush" || mode === "erase"){
    if (mode === "brush" && brushColor === null){ toast("Pick a brush color from the palette first"); return; }
    dragging = true;
    c.setPointerCapture(ev.pointerId);
    snapshot(); paintCell(y, x); lastPaint = pos;
  }
  ev.preventDefault();
});

c.addEventListener("pointermove", ev => {
  if (marquee){
    const [y, x] = canvasPos(ev, true);
    updateMarquee(y, x);
    return;
  }
  if (dragging && (mode === "brush" || mode === "erase")){
    const pos = canvasPos(ev); if (pos) paintLine(lastPaint || pos, pos);
    lastPaint = pos;
    return;
  }
  hoverPos = canvasPos(ev);
  updateShimmer();
});

c.addEventListener("pointerleave", () => { hoverPos = null; });
c.addEventListener("pointercancel", () => { dragging = false; marquee = null; });
window.addEventListener("pointerup", () => {
  if (dragging && current){ refreshFileStatus(current); renderPalette(); }
  dragging = false; marquee = null;
});

// paints every cell on the line between two cells, so fast drags leave no gaps
function paintLine([y0, x0], [y1, x1]){
  const n = Math.max(Math.abs(y1 - y0), Math.abs(x1 - x0));
  for (let i = 1; i <= n; i++)
    paintCell(Math.round(y0 + (y1 - y0) * i / n), Math.round(x0 + (x1 - x0) * i / n));
  if (!n) paintCell(y1, x1);
}

function paintCell(y, x){
  const s = sessions[current];
  const color = mode === "erase" ? CLEAR_CELL : brushColor;
  if (s.mapping[y][x] === color) return;
  const wasMasked = isTransparent(y, x);
  s.mapping[y][x] = color;
  s.edited = true;
  if (wasMasked || (s.transparentOn && s.transparent === color))
    s.bgMask = computeBgMask();
  if (isTransparent(y, x)){
    ctx.clearRect(x*view.scale, y*view.scale, view.scale, view.scale);
  } else {
    ctx.fillStyle = s.palette[color];
    ctx.fillRect(x*view.scale, y*view.scale, view.scale, view.scale);
  }
}

/* ---------------------------------------------------------------- exports */

function bytesToBlob(f){
  return f.bytes !== undefined
    ? new Blob([f.bytes], { type: f.type })
    : new Blob([f.text], { type: f.type + ";charset=utf-8" });
}

function clearExports(){
  $("exports").querySelectorAll("a").forEach(a => URL.revokeObjectURL(a.href));
  $("exports").innerHTML = "";
}
$("clearExports").onclick = clearExports;

async function exportSession(sid){
  const s = sessions[sid];
  const art = GridSprite.exportArtifacts(s, Number($("desp").value));
  const files = art.files;
  const links = files.map(f => {
    const blob = bytesToBlob(f);
    return { name: f.name, type: f.type, blob, url: URL.createObjectURL(blob) };
  });
  return { stats: art.stats, files, links };
}

$("exportBtn").onclick = async () => {
  if (!current) { toast("Open a file first"); return; }
  $("exportBtn").disabled = true;
  const s = sessions[current];
  try {
    const d = await exportSession(current);
    clearExports();
    addExportLinks(d, s.name);
    setFileStatus(current, "ok", sessionDims(s) + " · exported");
    toast(`Exported (${d.stats.rects} rects, ${d.stats.regions} regions` +
          (d.stats.despeckled ? `, ${d.stats.despeckled} specks cleaned` : "") + ")");
  } catch(e){ toast("Export failed: " + e.message); }
  $("exportBtn").disabled = false;
};

$("exportAll").onclick = async () => {
  const ids = Object.keys(sessions);
  if (!ids.length) { toast("Nothing to export yet — drop an image first"); return; }
  $("exportAll").disabled = true;
  clearExports();
  for (const sid of ids){
    try {
      const d = await exportSession(sid);
      addExportLinks(d, sessions[sid].name);
      setFileStatus(sid, "ok", sessionDims(sessions[sid]) + " · exported");
    } catch(e){ setFileStatus(sid, "err", "export failed"); }
  }
  $("exportAll").disabled = false;
  toast("Exported " + ids.length + " file" + (ids.length > 1 ? "s" : "") + " — downloads started");
};

function addExportLinks(d, spriteName){
  const base = d.links[0].name.replace(/\.svg$/, "");
  let box = [...$("exports").children].find(el => el.dataset.base === base);
  if (!box){
    box = document.createElement("div");
    box.className = "exp-group"; box.dataset.base = base;
    const nm = document.createElement("div");
    nm.className = "exp-name"; nm.textContent = base;
    box.appendChild(nm);
    $("exports").appendChild(box);
  }
  [...box.querySelectorAll("a")].forEach(a => { a.remove(); URL.revokeObjectURL(a.href); });
  for (const l of d.links){
    const a = document.createElement("a");
    a.href = l.url; a.download = l.name;
    a.textContent = l.name;
    box.appendChild(a);
  }
  // whole set as a single ZIP download
  const zipBytes = makeZip(d.files.map(f =>
    ({ name: f.name, bytes: f.bytes, text: f.text })));
  const url = URL.createObjectURL(new Blob([zipBytes], { type: "application/zip" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = (spriteName || base).replace(/\.[^.]*$/, "") + ".zip";
  a.textContent = "all files (.zip)";
  box.appendChild(a);
}

/* ----------------------------------------------------------------- wiring */

function setMode(next){
  document.querySelectorAll("#modes .seg").forEach(x => {
    const on = x.dataset.mode === next;
    x.classList.toggle("on", on); x.setAttribute("aria-pressed", String(on));
  });
  mode = next;
  $("connrow").hidden = mode !== "region" && mode !== "fill";
  $("tip").innerHTML = modeTips[mode];
  selection.clear(); selColor = -1;
  $("tipbar").textContent = "";
  c.style.cursor = mode === "global" ? "grab" :
                   mode === "region" || mode === "fill" ? "pointer" : "crosshair";
  if (current) { renderCanvas(); renderPalette(); }
}
document.querySelectorAll("#modes .seg").forEach(m => {
  m.onclick = () => setMode(m.dataset.mode);
});
$("tip").innerHTML = modeTips.global;

document.querySelectorAll('input[name="conn"]').forEach(r =>
  r.onchange = () => conn = Number(r.value));

try { $("gridchk").checked = localStorage.getItem("gs-grid") !== "off"; } catch (e) {}
$("gridchk").onchange = () => {
  try { localStorage.setItem("gs-grid", $("gridchk").checked ? "on" : "off"); } catch (e) {}
  if (current) renderGrid();
};

$("bgchk").onchange = () => {
  if (!current) return;
  sessions[current].transparentOn = $("bgchk").checked;
  renderCanvas();
};

const PRESETS = {
  pastel:  [["red", "#ff9aa2"], ["orange", "#ffbd8a"], ["yellow", "#ffe08a"],
            ["lime", "#d3efb4"], ["green", "#b6e2a1"], ["teal", "#a8dbd2"],
            ["sky", "#a2d2ff"], ["indigo", "#c3b8ff"], ["pink", "#f8b7dd"],
            ["mauve", "#d9b8c9"], ["khaki", "#c8bd99"], ["cloud", "#e7e9ee"]],
  vivid:   [["red", "#e81224"], ["tangerine", "#ff6b35"], ["marigold", "#ffb703"],
            ["yellow", "#ffd60a"], ["lime", "#65d413"], ["green", "#24c95c"],
            ["teal", "#00b4d8"], ["azure", "#0096ff"], ["indigo", "#5e60ce"],
            ["violet", "#9d4edd"], ["magenta", "#ff2fb9"], ["white", "#ffffff"]],
  classic: [["red", "#ff0000"], ["orange", "#ff7f00"], ["yellow", "#ffff00"],
            ["purple", "#800080"], ["blue", "#0000ff"], ["cyan", "#00ffff"],
            ["green", "#008000"], ["magenta", "#ff00ff"], ["brown", "#8b4513"],
            ["moss green", "#7e8c54"], ["gray", "#808080"], ["black", "#000000"],
            ["white", "#ffffff"]],
  nature:  [["moss green", "#7e8c54"], ["sage", "#aab88a"], ["fern", "#567d46"],
            ["forest", "#2d4b2d"], ["pine", "#173a2b"], ["seagreen", "#2e8b57"],
            ["olive", "#6b7023"], ["sand", "#d9c9a3"], ["terracotta", "#b86b4a"],
            ["rust", "#93412f"], ["bark", "#5b4636"], ["stone", "#8a8f80"]],
};
(function renderPresets(){
  const box = $("presets");
  Object.entries(PRESETS).forEach(([label, cols]) => {
    const cap = document.createElement("div");
    cap.className = "swgroup"; cap.textContent = label;
    box.appendChild(cap);
    cols.forEach(([name, hex]) => {
      const sw = document.createElement("div");
      sw.className = "sw"; sw.style.background = hex;
      sw.title = `${name} ${hex}`;
      sw.onclick = () => { $("custom").value = hex; applyCustomColor(); };
      box.appendChild(sw);
    });
  });
})();

$("applyCustom").onclick = applyCustomColor;
$("undo").onclick = undo;
const MODE_KEYS = {b: "brush", f: "fill", e: "erase", i: "pick", m: "select"};
const ZOOM_KEYS = {"+": 1, "=": 1, "-": -1, "_": -1};
window.addEventListener("keydown", e => {
  if ((e.metaKey || e.ctrlKey) && e.key === "z"){
    e.preventDefault(); undo();
    return;
  }
  if (e.metaKey || e.ctrlKey || e.altKey ||
      e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement) return;
  if (e.key === "Tab") pointerFocus = false;
  // Space pans unless a button focused with Tab should take the press
  if (e.key === " " && current &&
      !(e.target instanceof HTMLButtonElement && !pointerFocus)){
    e.preventDefault();
    spaceHeld = true; canvasScroll.classList.add("pan-ready");
  } else if (ZOOM_KEYS[e.key]){
    setZoom(view.zoom + ZOOM_KEYS[e.key]);
  } else if (e.key === "0"){
    setZoom(0);
  } else if ((e.key === "Delete" || e.key === "Backspace") && current && hasSelection()){
    e.preventDefault(); eraseSelection();
  } else if (e.key === "Escape" && selection.size){
    selection.clear(); selColor = -1; $("tipbar").textContent = "";
    if (current) renderPalette();
  } else if (MODE_KEYS[e.key.toLowerCase()]){
    setMode(MODE_KEYS[e.key.toLowerCase()]);
  }
});
window.addEventListener("pointerdown", () => { pointerFocus = true; }, true);
window.addEventListener("keyup", e => {
  if (e.key === " " && spaceHeld){
    e.preventDefault();
    spaceHeld = false; canvasScroll.classList.remove("pan-ready");
  }
});
window.addEventListener("blur", () => {
  spaceHeld = false; canvasScroll.classList.remove("pan-ready");
});

$("clearFiles").onclick = () => {
  for (const sid in sessions) delete sessions[sid];
  current = null; selection.clear(); selColor = -1; undoStack = [];
  hoverPos = null; marquee = null; dragging = false;
  $("files").innerHTML = "";
  $("palette").innerHTML = ""; $("pcount").textContent = "";
  $("tipbar").textContent = "";
  $("editor").hidden = true; $("empty").style.display = "";
  updateUndoBtn();
};

/* ---------------------------------------------------------------- drop */
$("drop").onclick = () => $("fileinput").click();
$("fileinput").onchange = async e => {
  await handleFiles([...e.target.files]);
  e.target.value = "";
};

const full = $("dropzone-full");
["dragenter","dragover"].forEach(evt =>
  window.addEventListener(evt, e => { e.preventDefault(); full.classList.add("over"); }));
["dragleave","drop"].forEach(evt =>
  window.addEventListener(evt, e => {
    e.preventDefault();
    if (evt === "drop" || e.target === document.documentElement)
      full.classList.remove("over");
  }));
window.addEventListener("drop", async e => {
  e.preventDefault(); full.classList.remove("over");
  await handleFiles([...(e.dataTransfer ? e.dataTransfer.files : [])]
    .filter(f => f.type.startsWith("image/")));
});

async function handleFiles(files){
  if (!files.length) return;
  const pendings = files.map(addPendingRow);
  for (let i = 0; i < files.length; i++){
    const f = files[i];
    try {
      const sid = await analyzeFile(f);   // returns sessionId
      const s = sessions[sid];
      const row = fileRow(pendings[i]);   // keep original drop order
      if (row){
        row.classList.remove("dead");
        row.dataset.sid = sid;
        row.onclick = () => openSession(sid);
      } else {
        addFileRow({...s, sid});
      }
      openSession(sid);
      setFileStatus(sid, "ok", sessionDims(s));
    } catch(err){
      const row = fileRow(pendings[i]);
      if (row) row.remove();
      addFailed(f, err.message);
      toast(f.name + " — " + err.message);
    }
  }
}

/* ----------------------------------------------------------------- resize */
let resizeRAF = 0;
const queueRerender = () => {
  if (!current) return;
  cancelAnimationFrame(resizeRAF);
  resizeRAF = requestAnimationFrame(() => {
    const scale = viewScale(sessions[current]);
    if (scale !== view.scale) renderCanvas(); // avoids resize feedback loops
  });
};
window.addEventListener("resize", queueRerender);
if (window.ResizeObserver) new ResizeObserver(queueRerender).observe(canvasScroll);
