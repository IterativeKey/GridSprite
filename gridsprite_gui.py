#!/usr/bin/env python3
"""gridsprite_gui.py — small local web GUI for gridsprite.py.

Features:
  * Batch drag & drop of images onto the browser window
  * Automatic grid detection + quantization (reuses gridsprite.py)
  * Recoloring: globally (whole palette entry), by region (4- or
    8-connected group of same-colored cells), or per-cell brush painting
  * Undo, background-transparency toggle, despeckle option
  * Export all processed images to out/ (SVG + native PNG + preview PNG
    + palette JSON/TXT)

Usage:
    python3 gridsprite_gui.py [--port 8642] [--outdir out]

Opens http://127.0.0.1:PORT in your default browser. Ctrl+C to stop.
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from PIL import Image

import gridsprite as px

SESSIONS = {}
LOCK = threading.Lock()


# ------------------------------------------------------------------ analysis

def analyze(name, blob, color_tol=18.0, min_scale=4.0, max_scale=64.0):
    img = np.asarray(Image.open(io.BytesIO(blob)).convert("RGB")).astype(np.float64)
    grid = px.detect_grid(img, min_scale, max_scale)
    cells, rmse = px.rectify(img, grid)
    palette, mapping = px.build_palette(cells, color_tol)
    bg_idx, bg_share = px.detect_background(mapping)
    transparent = bg_idx if bg_share >= 0.3 else None
    sid = uuid.uuid4().hex[:12]
    with LOCK:
        SESSIONS[sid] = {
            "name": name,
            "img": img,
            "shape": img.shape,          # H, W for preview aspect
            "palette": palette,
            "mapping": mapping.copy(),
            "transparent": transparent,
        }
    return {
        "sid": sid, "name": name,
        "sourceW": img.shape[1], "sourceH": img.shape[0],
        "ncols": grid.ncols, "nrows": grid.nrows,
        "cellW": round(grid.sx, 2), "cellH": round(grid.sy, 2),
        "rmse": round(rmse, 2),
        "alignment": [round(float(np.median(grid.score_x)), 2),
                      round(float(np.median(grid.score_y)), 2)],
        "bgShare": round(bg_share, 3),
        "palette": [hexc(v) for v in palette],
        "mapping": mapping.tolist(),
        "transparent": transparent,
    }


def hexc(rgb):
    return "#%02x%02x%02x" % tuple(int(v) for v in rgb)


def unhex(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def export(data):
    sid = data["sid"]
    with LOCK:
        sess = SESSIONS[sid]
    mapping = np.array(data["mapping"], dtype=np.int64)
    palette = np.array([unhex(h) for h in data["palette"]], dtype=np.int64)
    transparent = data.get("transparent", None)
    if isinstance(transparent, str) and transparent == "":
        transparent = None
    despeckle = int(data.get("despeckle", 0) or 0)

    removed = 0
    if despeckle > 1:
        removed, _ = px.despeckle(mapping, despeckle,
                                  skip_idx=(transparent,) if transparent is not None else ())
    outdir = data.get("outdir", "out")
    os.makedirs(outdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(sess["name"]))[0]
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", base)

    bg_mask = None
    if transparent is not None:
        bg_mask = px.background_mask(mapping, transparent)
    rects = px.mesh(mapping, free_mask=bg_mask)
    svg_path = os.path.join(outdir, base + ".svg")
    png_path = os.path.join(outdir, base + ".png")
    big_path = os.path.join(outdir, base + ".rect.png")
    pjson_path = os.path.join(outdir, base + ".palette.json")
    ptxt_path = os.path.join(outdir, base + ".palette.txt")

    px.write_svg(svg_path, palette, mapping, rects,
                 mapping.shape[1], mapping.shape[0], transparent_idx=transparent)
    rgba = px.colored_rgba(palette, mapping, bg_mask)
    Image.fromarray(rgba).save(png_path)
    H, W = sess["shape"][0], sess["shape"][1]
    Image.fromarray(rgba).resize((W, H), Image.NEAREST).save(big_path)
    px.write_palette(pjson_path, ptxt_path, palette, mapping, transparent)

    files = [svg_path, png_path, big_path, pjson_path, ptxt_path]
    return {"files": files, "despeckled": removed,
            "rects": len(rects), "regions": px.connected_regions(mapping)}


# -------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter console
        sys.stderr.write("%s %s\n" % (self.command, self.path))

    # -- helpers ------------------------------------------------------------
    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file_bytes(self, path):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_json({"error": "file not found"}, 404)
            return
        mime = {"svg": "image/svg+xml", "png": "image/png", "json": "application/json",
                "txt": "text/plain", "html": "text/html"}.get(
                    path.rsplit(".", 1)[-1], "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % os.path.basename(path))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n)

    # -- routes -------------------------------------------------------------
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            self.send_json({"ok": True})
        elif self.path.startswith("/download?"):
            q = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&"))
            path = q.get("f", "")
            if os.path.dirname(path) or path.startswith("."):
                return self.send_json({"error": "bad path"}, 400)
            self.send_file_bytes(os.path.join("out", path))
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            if self.path.startswith("/analyze"):
                name = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&")) \
                    if "?" in self.path else {}
                name = name.get("name", "dropped.png")
                data = analyze(name, self.read_body())
                return self.send_json(data)
            if self.path == "/export":
                return self.send_json(export(json.loads(self.read_body())))
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        except Exception as e:  # noqa — report to UI
            return self.send_json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
        self.send_json({"error": "not found"}, 404)


# ---------------------------------------------------------------------- HTML

HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Gridsprite</title>
<style>
  :root { --bg:#14151a; --panel:#1e2029; --panel2:#262836; --fg:#e8e6e3;
          --muted:#9a9aa5; --accent:#7b6cf6; --border:#33364a; }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.45 -apple-system,BlinkMacSystemFont,sans-serif;
         color:var(--fg); background:var(--bg); height:100vh;
         display:flex; flex-direction:column; }
  header { padding:10px 16px; border-bottom:1px solid var(--border);
            display:flex; gap:14px; align-items:center; }
  header h1 { font-size:16px; margin:0; }
  header .hint { color:var(--muted); font-size:12px; }
  main { flex:1; display:flex; min-height:0; }
  #left { width:250px; border-right:1px solid var(--border); display:flex;
          flex-direction:column; min-height:0; }
  #drop { margin:10px; padding:22px 12px; border:2px dashed var(--border);
          border-radius:10px; text-align:center; color:var(--muted);
          font-size:13px; transition:all .15s; }
  #drop.over { border-color:var(--accent); color:var(--accent);
               background:rgba(123,108,246,.08); }
  #files { flex:1; overflow:auto; padding:4px 10px; }
  .fitem { display:flex; align-items:center; gap:8px; padding:6px;
           border-radius:8px; cursor:pointer; font-size:13px; }
  .fitem:hover { background:var(--panel2); }
  .fitem.sel { background:var(--accent); color:white; }
  .fitem .st { font-size:11px; }
  .fitem .st.ok { color:#7ddb8a; } .fitem .st.err { color:#ff8a8a; }
  #editor { flex:1; display:flex; min-width:0; }
  #center { flex:1; display:flex; flex-direction:column; align-items:center;
            justify-content:flex-start; padding:16px; overflow:auto; min-width:0; }
  #stats { color:var(--muted); font-size:12px; margin:4px 0 10px; }
  #canvaswrap { border:1px solid var(--border); position:relative;
    background:repeating-conic-gradient(#2a2c38 0% 25%, #20222c 0% 50%) 0 / 24px 24px; }
  canvas { image-rendering:pixelated; display:block; cursor:crosshair; }
  #overlay { position:absolute; inset:0; pointer-events:none; }
  #right { width:280px; border-left:1px solid var(--border); overflow:auto;
           padding:12px; }
  .modes { display:flex; gap:6px; margin-bottom:10px; flex-wrap:wrap; }
  .mode { padding:6px 10px; border:1px solid var(--border); border-radius:8px;
          cursor:pointer; font-size:12px; }
  .mode.on { background:var(--accent); border-color:var(--accent); color:white; }
  .palette { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0; }
  .pchip { width:34px; height:34px; border-radius:7px; cursor:pointer;
           border:2px solid transparent; position:relative; }
  .pchip.sel { border-color:white; box-shadow:0 0 0 2px var(--accent); }
  .pchip .n { position:absolute; bottom:-2px; right:2px; font-size:9px;
              color:white; text-shadow:0 1px 2px black; }
  .swrow { display:flex; flex-wrap:wrap; gap:5px; align-items:center; margin:6px 0; }
  .sw { width:22px; height:22px; border-radius:6px; cursor:pointer; flex:none;
        border:2px solid rgba(255,255,255,.15); transition:transform .08s; }
  .sw:hover { transform:scale(1.2); border-color:#fff; }
  .swgroup { font-size:10px; color:var(--muted); margin-right:4px; width:100%; }
  .row { display:flex; gap:8px; align-items:center; margin:8px 0; flex-wrap:wrap; }
  button { background:var(--panel2); color:var(--fg); border:1px solid var(--border);
           border-radius:8px; padding:7px 12px; cursor:pointer; font-size:12px; }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); border-color:var(--accent); color:white; }
  button:disabled { opacity:.4; cursor:default; }
  input[type=color] { width:44px; height:34px; border:none; border-radius:7px;
                      background:none; cursor:pointer; padding:0; }
  label.chk { font-size:12px; display:flex; gap:6px; align-items:center;
              color:var(--muted); cursor:pointer; }
  .sec { border-bottom:1px solid var(--border); padding-bottom:10px;
         margin-bottom:10px; }
  .sec h3 { margin:6px 0; font-size:12px; text-transform:uppercase;
            color:var(--muted); letter-spacing:.06em; }
  #toolbar { display:none; }
  #tip { color:var(--muted); font-size:12px; margin-top:8px; min-height:32px; }
  .toast { position:fixed; bottom:16px; left:50%; transform:translateX(-50%);
           background:var(--panel2); border:1px solid var(--border);
           padding:10px 16px; border-radius:10px; font-size:13px; z-index:10; }
  #dropzone-full { display:none; }
  #dropzone-full.over { display:flex; position:fixed; inset:0; z-index:5;
    align-items:center; justify-content:center; pointer-events:none;
    background:rgba(20,21,26,.6); }
  #dropzone-full div { border:3px dashed var(--accent); border-radius:16px;
    padding:40px 60px; font-size:20px; color:var(--accent); background:var(--panel); }
  #exports { font-size:12px; }
  #exports a { color:var(--accent); display:block; text-decoration:none;
               margin:2px 0; word-break:break-all; }
</style>
</head>
<body>
<header>
  <h1>⚙ Gridsprite</h1>
  <span class="hint">drop images anywhere — they get grid-snapped &amp; quantized automatically</span>
</header>
<main>
  <div id="left">
    <div id="drop">Drop images here<br><small>(or click to browse)</small>
      <input type="file" id="fileinput" multiple accept="image/*" hidden>
    </div>
    <div id="files"></div>
  </div>
  <div id="editor">
    <div id="center">
      <div id="toolbar" style="display:none">
        <div id="stats"></div>
        <div id="canvaswrap"><canvas id="c"></canvas><canvas id="overlay"></canvas></div>
        <div id="tip"></div>
      </div>
      <div id="empty" style="color:var(--muted); margin-top:40px">
        Drop images — then select one to edit.
      </div>
    </div>
    <div id="right">
      <div class="sec">
        <h3>Mode</h3>
        <div class="modes">
          <div class="mode on" data-mode="global">Global</div>
          <div class="mode" data-mode="region">Region</div>
          <div class="mode" data-mode="brush">Brush</div>
        </div>
        <div class="row" id="connrow" style="display:none">
          <label class="chk"><input type="radio" name="conn" value="4" checked>
            4-connected</label>
          <label class="chk"><input type="radio" name="conn" value="8">
            8-connected</label>
        </div>
        <label class="chk"><input type="checkbox" id="bgchk" checked>
          transparent background</label>
        <div class="row">
          <label class="chk">despeckle &lt;
            <select id="desp"><option value="0">off</option>
              <option value="2">2</option><option value="3">3</option>
              <option value="5">5</option></select> cells
          </label>
        </div>
      </div>
      <div class="sec">
        <h3>Palette <span id="pcount" class="hint"></span></h3>
        <div class="palette" id="palette"></div>
        <div class="swrow" id="presets"></div>
        <div class="row">
          <input type="color" id="custom" value="#7b6cf6">
          <button id="applyCustom">Apply → affected</button>
        </div>
        <div id="tip"></div>
      </div>
      <div class="sec">
        <h3>History</h3>
        <div class="row"><button id="undo">Undo</button></div>
      </div>
      <div class="sec" style="border-bottom:none">
        <h3>Export</h3>
        <div class="row">
          <button id="exportBtn" class="primary">Export this file</button>
          <button id="exportAll">Export all</button>
        </div>
        <div id="exports"></div>
      </div>
    </div>
  </div>
</main>
<div id="dropzone-full"><div>Drop to process</div></div>
<div id="toasts"></div>
<script>
"use strict";
const sessions = {};              // sid -> state
let current = null;               // sid being edited
let mode = "global", conn = 4;
let selColor = -1;                // selected palette index (source)
let selection = new Set();        // region selection as "y,x" keys
let brushColor = -1;              // palette index used by brush
let undoStack = [];
let dragging = false;

const $ = id => document.getElementById(id);
const c = $("c"), ctx = c.getContext("2d", {willReadFrequently:false});
const o = $("overlay"), octx = o.getContext("2d");
let view = {scale: 8, ox: 0, oy: 0};
let hoverPos = null;              // [y, x] under cursor (brush shimmer)
let shimmerRAF = 0;               // requestAnimationFrame id while animating

function newSid(){ return Math.random().toString(36).slice(2,10); }

function toast(msg, ms=2600){
  const t = document.createElement("div"); t.className="toast"; t.textContent=msg;
  $("toasts").appendChild(t); setTimeout(()=>t.remove(), ms);
}

function hexToRgb(h){ h=h.replace("#",""); return [0,2,4].map(i=>parseInt(h.slice(i,i+2),16)); }
function rgbToHex(r){ return "#"+r.map(v=>v.toString(16).padStart(2,"0")).join(""); }

async function analyzeFile(file){
  const buf = await file.arrayBuffer();
  const r = await fetch("/analyze?name=" + encodeURIComponent(file.name),
                        {method:"POST", body: buf});
  const d = await r.json();
  if (d.error) throw new Error(d.error);
  sessions[d.sid] = d;
  d.edited = false;
  addFileRow(d);
  return d;
}

function addFileRow(d){
  const row = document.createElement("div");
  row.className = "fitem"; row.dataset.sid = d.sid;
  row.innerHTML = `<span>${d.name}</span><span class="st"></span>`;
  row.onclick = () => openSession(d.sid);
  $("files").appendChild(row);
}

function setFileStatus(sid, cls, text){
  const row = document.querySelector(`.fitem[data-sid="${sid}"] .st`);
  if (row){ row.className = "st " + cls; row.textContent = text; }
}

function openSession(sid){
  current = sid;
  selection.clear(); selColor = -1; undoStack = [];
  document.querySelectorAll(".fitem").forEach(f => f.classList.toggle("sel", f.dataset.sid === sid));
  $("empty").style.display = "none";
  $("toolbar").style.display = "block";
  const s = sessions[sid];
  $("bgchk").checked = s.transparent !== null && s.transparent !== undefined;
  renderCanvas();
  renderPalette();
  updateStats();
}

function updateStats(){
  const s = sessions[current];
  $("stats").textContent =
    `${s.sourceW}×${s.sourceH} source → ${s.ncols}×${s.nrows} grid ` +
    `(cells ${s.cellW}×${s.cellH}px) · ${s.palette.length} colors · ` +
    `alignment ±${s.alignment[0]}/${s.alignment[1]}px`;
}

// ---------------------------------------------------------------- rendering

function computeBgMask(){
  const s = sessions[current];
  const mask = new Set();
  if (!($("bgchk").checked) || s.transparent === null || s.transparent === undefined)
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

function renderCanvas(){
  const s = sessions[current];
  const maxW = Math.max(400, window.innerWidth - 640);
  const maxH = window.innerHeight - 190;
  const scale = Math.max(2, Math.floor(Math.min(maxW / s.ncols, maxH / s.nrows)));
  view.scale = scale;
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
  updateShimmer();
}
function isTransparent(y, x){
  const s = sessions[current];
  return s.bgMask !== undefined && s.bgMask.has(y + "," + x);
}

// ------------------------------------------------------------------ shimmer

function affectedCells(){
  // which cells a pending edit would change (drives the shimmer preview)
  if (!current) return null;
  const s = sessions[current];
  const out = new Set();
  if (mode === "region" && selection.size){
    selection.forEach(k => out.add(k));
  } else if (mode === "global" && selColor >= 0){
    for (let y = 0; y < s.nrows; y++)
      for (let x = 0; x < s.ncols; x++)
        if (s.mapping[y][x] === selColor) out.add(y + "," + x);
  } else if (mode === "brush" && hoverPos && brushColor >= 0){
    out.add(hoverPos[0] + "," + hoverPos[1]);
  }
  return out.size ? out : null;
}

function drawShimmer(aff, ts){
  const k = view.scale;
  const wave = 0.5 + 0.5 * Math.sin(ts / 1000 * Math.PI * 1.6); // ~1.25s cycle
  octx.clearRect(0, 0, o.width, o.height);
  octx.lineWidth = Math.max(1.5, k * 0.12);
  octx.fillStyle = `rgba(255,255,255,${(0.10 + 0.28 * wave).toFixed(3)})`;
  octx.strokeStyle = `rgba(255,255,255,${(0.20 + 0.60 * wave).toFixed(3)})`;
  aff.forEach(key => {
    const [y, x] = key.split(",").map(Number);
    const px = x * k, py = y * k;
    octx.fillRect(px, py, k, k);
    octx.strokeRect(px + 0.5, py + 0.5, k - 1, k - 1);
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

function renderPalette(){
  const s = sessions[current];
  const counts = {};
  s.mapping.forEach(row => row.forEach(v => counts[v] = (counts[v]||0)+1));
  const box = $("palette"); box.innerHTML = "";
  s.palette.forEach((hcol, i) => {
    const chip = document.createElement("div");
    chip.className = "pchip" + (i === selColor ? " sel" : "");
    chip.style.background = hcol + (s.transparent !== null && i === s.transparent ? "" : "");
    chip.title = `${hcol} · ${counts[i]||0} cells${i===s.transparent ? " (background)" : ""}`;
    const n = document.createElement("span"); n.className = "n"; n.textContent = counts[i]||0;
    chip.appendChild(n);
    chip.onclick = () => paletteClick(i);
    box.appendChild(chip);
  });
  $("pcount").textContent = "· " + s.palette.length + " colors";
}

// ------------------------------------------------------------------- edits

function snapshot(){
  const s = sessions[current];
  undoStack.push({mapping: JSON.parse(JSON.stringify(s.mapping)),
                  palette: [...s.palette]});
  if (undoStack.length > 100) undoStack.shift();
}

function undo(){
  if (!undoStack.length || !current) return;
  const s = sessions[current];
  const st = undoStack.pop();
  sessions[current] = {...s, mapping: st.mapping, palette: st.palette, edited: true};
  selection.clear(); selColor = -1;
  renderCanvas(); renderPalette();
}

function paletteClick(i){
  if (!current) return;
  const s = sessions[current];
  if (mode === "global"){
    if (selColor < 0){ selColor = i; }
    else if (selColor === i){ selColor = -1; }
    else {
      snapshot();
      const from = selColor, to = i;
      s.mapping = s.mapping.map(row => row.map(v => v === from ? to : v));
      s.edited = true;
      toast(`Recolored all ${s.palette[from]} cells → ${s.palette[to]}`);
      selColor = -1;
    }
  } else if (mode === "region" && selection.size){
    snapshot();
    const to = i;
    selection.forEach(key => {
      const [y, x] = key.split(",").map(Number);
      s.mapping[y][x] = to;
    });
    s.edited = true;
    selection.clear(); selColor = -1;
  } else {
    brushColor = i; selColor = i;
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
  if (mode === "region" && selection.size){
    snapshot();
    const idx = ensureColor(hex);
    selection.forEach(key => {
      const [y, x] = key.split(",").map(Number);
      s.mapping[y][x] = idx;
    });
    s.edited = true;
    toast(`Region recolored → ${hex}`);
  } else if (mode === "brush"){
    brushColor = ensureColor(hex);
    selColor = brushColor;
    toast(`Brush → ${hex}`);
  } else {
    if (selColor < 0){ toast("Select a palette color first"); return; }
    snapshot();
    s.palette[selColor] = hex;
    s.edited = true;
    toast(`Palette entry → ${hex}`);
  }
  renderCanvas(); renderPalette();
}

function selectRegion(y, x){
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
  selection = sel;
  selColor = start;
}

function canvasPos(ev){
  const r = c.getBoundingClientRect();
  const s = sessions[current];
  const x = Math.floor((ev.clientX - r.left) / view.scale);
  const y = Math.floor((ev.clientY - r.top) / view.scale);
  if (x < 0 || y < 0 || x >= s.ncols || y >= s.nrows) return null;
  return [y, x];
}

c.addEventListener("mousedown", ev => {
  if (!current) return;
  const pos = canvasPos(ev); if (!pos) return;
  const [y, x] = pos;
  if (mode === "region"){
    selectRegion(y, x);
    renderCanvas();
    $("tip").textContent = `Selected ${selection.size} cells (${sessions[current].palette[selColor]}). Click a palette color, a preset or Apply to recolor them; click elsewhere for a different region.`;
  } else if (mode === "brush"){
    if (brushColor < 0){ toast("Pick a brush color from the palette first"); return; }
    dragging = true; snapshot(); paintCell(y, x);
  }
});
c.addEventListener("mousemove", ev => {
  if (dragging && mode === "brush"){
    const pos = canvasPos(ev); if (pos) paintCell(pos[0], pos[1]);
    return;
  }
  const pos = canvasPos(ev);
  hoverPos = pos;
  updateShimmer();
});
c.addEventListener("mouseleave", () => { hoverPos = null; });
window.addEventListener("mouseup", () => dragging = false);

function paintCell(y, x){
  const s = sessions[current];
  if (s.mapping[y][x] === brushColor) return;
  const wasMasked = isTransparent(y, x);
  s.mapping[y][x] = brushColor;
  s.edited = true;
  if (wasMasked || ($("bgchk").checked && s.transparent === brushColor))
    s.bgMask = computeBgMask();
  if (isTransparent(y, x)){
    ctx.clearRect(x*view.scale, y*view.scale, view.scale, view.scale);
  } else {
    ctx.fillStyle = s.palette[brushColor];
    ctx.fillRect(x*view.scale, y*view.scale, view.scale, view.scale);
  }
}

// ---------------------------------------------------------------- exports

async function exportSession(sid){
  const s = sessions[sid];
  const r = await fetch("/export", {method:"POST", headers:
    {"Content-Type":"application/json"}, body: JSON.stringify({
      sid,
      palette: s.palette,
      mapping: s.mapping,
      transparent: $("bgchk").checked && s.transparent !== null && s.transparent !== undefined
                   ? s.transparent : null,
      despeckle: Number($("desp").value),
    })});
  const d = await r.json();
  if (d.error) throw new Error(d.error);
  return d;
}

$("exportBtn").onclick = async () => {
  if (!current) return;
  try {
    const d = await exportSession(current);
    addExportLinks(d);
    setFileStatus(current, "ok", "exported ✓");
    toast(`Exported (${d.rects} rects, ${d.regions} regions` +
          (d.despeckled ? `, ${d.despeckled} specks cleaned` : "") + ")");
  } catch(e){ toast("Export failed: " + e.message); }
};

$("exportAll").onclick = async () => {
  const ids = Object.keys(sessions);
  if (!ids.length) return;
  for (const sid of ids){
    try {
      const d = await exportSession(sid);
      addExportLinks(d);
      setFileStatus(sid, "ok", "exported ✓");
    } catch(e){ setFileStatus(sid, "err", "export failed"); }
  }
  toast("Exported " + ids.length + " files to out/");
};

function addExportLinks(d){
  d.files.forEach(f => {
    const a = document.createElement("a");
    a.href = "/download?f=" + encodeURIComponent(f.split("/").pop());
    a.textContent = f;
    $("exports").prepend(a);
  });
}

// ----------------------------------------------------------------- wiring

document.querySelectorAll(".mode").forEach(m => {
  m.onclick = () => {
    document.querySelectorAll(".mode").forEach(x => x.classList.remove("on"));
    m.classList.add("on");
    mode = m.dataset.mode;
    $("connrow").style.display = mode === "region" ? "flex" : "none";
    selection.clear(); selColor = -1;
    if (current) { renderCanvas(); renderPalette(); }
    $("tip").textContent = modeTips[mode];
  };
});
const modeTips = {
  global: "Global: click a color to shimmer-preview every cell it would change, then click another color / a preset / Apply to recolor the group.",
  region: "Region: click a cluster of same-colored cells (4- or 8-connected) to shimmer-select it, then click a color or preset to recolor it.",
  brush:  "Brush: pick a color (palette chip, preset row or the picker), then drag on the canvas to paint. The cell under the cursor shimmers.",
};
$("tip").textContent = modeTips.global;
document.querySelectorAll('input[name="conn"]').forEach(r =>
  r.onchange = () => conn = Number(r.value));
$("bgchk").onchange = () => { if (current) renderCanvas(); };
const PRESETS = {
  pastel:  ["#ffd6e0", "#ffe3b3", "#fff5b8", "#c9f2d7", "#c3e7fa",
            "#dcc7f7", "#f7c7e9", "#c2ece6", "#fbdfc2", "#e7e9ee"],
  vivid:   ["#ff5d8f", "#ff9f1c", "#ffd60a", "#7ae582", "#00b4d8",
            "#5e60ce", "#9d4edd", "#ff6b35", "#00bbf9", "#e94f37"],
  classic: ["#101418", "#2b2d42", "#7f8c8d", "#d3d6db", "#ffffff",
            "#e0f8d0", "#306230", "#0f380f", "#d8a48f", "#3f266b"],
};
(function renderPresets(){
  const box = $("presets");
  Object.entries(PRESETS).forEach(([label, cols]) => {
    const cap = document.createElement("div");
    cap.className = "swgroup"; cap.textContent = label;
    box.appendChild(cap);
    cols.forEach(hex => {
      const sw = document.createElement("div");
      sw.className = "sw"; sw.style.background = hex; sw.title = `${label} ${hex}`;
      sw.onclick = () => { $("custom").value = hex; applyCustomColor(); };
      box.appendChild(sw);
    });
  });
})();
$("applyCustom").onclick = applyCustomColor;
$("undo").onclick = undo;
window.addEventListener("keydown", e => {
  if ((e.metaKey || e.ctrlKey) && e.key === "z"){ e.preventDefault(); undo(); }
});

// drag & drop — anywhere on the page
const full = $("dropzone-full");
["dragenter","dragover"].forEach(evt =>
  window.addEventListener(evt, e => { e.preventDefault(); full.classList.add("over"); }));
["dragleave","drop"].forEach(evt =>
  window.addEventListener(evt, e => {
    e.preventDefault();
    if (evt === "drop" || e.target === document.documentElement) full.classList.remove("over");
  }));
window.addEventListener("drop", async e => {
  e.preventDefault(); full.classList.remove("over");
  const files = [...(e.dataTransfer ? e.dataTransfer.files : [])]
    .filter(f => f.type.startsWith("image/"));
  for (const f of files){
    const sid0 = newSid();
    try { const d = await analyzeFile(f); setFileStatus(d.sid, "ok", d.ncols + "×" + d.nrows); }
    catch(err){ toast(f.name + " — " + err.message); addFailed(f, err.message); }
  }
});
function addFailed(f, msg){
  const sid = newSid();
  const row = document.createElement("div");
  row.className = "fitem"; row.dataset.sid = sid;
  row.innerHTML = `<span>${f.name}</span><span class="st err">${msg}</span>`;
  $("files").appendChild(row);
}
window.addEventListener("resize", () => { if (current) renderCanvas(); });
</script>
</body>
</html>"""


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8642)
    ap.add_argument("--outdir", default="out")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    url = f"http://127.0.0.1:{args.port}"

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Gridsprite GUI running at {url}  (Ctrl+C to stop)")
    print("Outputs are written to ./out/ as in the CLI.")
    if not args.no_browser:
        try:
            subprocess.Popen(["open", url])
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
