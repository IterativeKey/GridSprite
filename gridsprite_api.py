#!/usr/bin/env python3
"""gridsprite_api.py — stateless JSON HTTP API for the Gridsprite pixel-art
vectorizer.

Endpoints (all under /api/v1, JSON in / JSON out, binary image bodies):
  GET  /api/v1/capabilities  machine-readable capability description
  POST /api/v1/analyze       grid + palette + background report, no artifacts
  POST /api/v1/process       full pipeline: image -> SVG + PNG (+render size)
  POST /api/v1/render        palette + mapping (optionally edited) -> SVG + PNG
  GET  /api/v1/download?f=   fetch a file saved via save=1 (out/ only)
  GET  /health               liveness probe

Run:  python3 gridsprite_api.py [--port 8643]
Full reference: API.md.  This is a plain HTTP/JSON API, not an MCP server.
"""

import argparse
import base64
import io
import json
import os
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from PIL import Image

import gridsprite as px

API_VERSION = "1.0"
MAX_BODY = 64 * 1024 * 1024      # 64 MB upload cap
HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
OUT_DIR = "out"


class ApiError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- query params

def qget(q, key, default=None):
    vals = q.get(key)
    return vals[0] if vals else default


def qfloat(q, key, default):
    try:
        return float(qget(q, key, default))
    except (TypeError, ValueError):
        raise ApiError(f"bad value for {key!r}")


def qint(q, key, default):
    return int(qfloat(q, key, default))


def qbool(q, key, default=False):
    v = qget(q, key)
    return default if v is None else v.lower() not in ("", "0", "false", "no")


def qswaps(q):
    """All repeatable swap=OLD=NEW query pairs -> list for px.apply_swaps."""
    return swaps_safe(q.get("swap", []))


def swaps_safe(pairs):
    for s in pairs:
        if "=" not in s or s.count("=") != 1 or \
                not all(HEX_RE.match(x) for x in s.split("=")):
            raise ApiError(f"bad swap {s!r}; expected OLDHEX=NEWHEX "
                           f"(e.g. #ff0000=#00ff00)")
    return [px.parse_swap(s) for s in pairs]


# ---------------------------------------------------------------- images/artifacts

def jf(v, nd=3):
    """JSON-safe float (numpy scalars -> python float)."""
    return round(float(v), nd)


def decode_image(blob):
    try:
        im = Image.open(io.BytesIO(blob))
        im.load()
    except Exception:
        raise ApiError("could not decode image body; send raw PNG/JPEG/WebP bytes")
    fmt = (im.format or "PNG").lower()
    return np.asarray(im.convert("RGBA")), fmt


def sanitize_base(name):
    base = os.path.splitext(os.path.basename(name or "image"))[0]
    return re.sub(r"[^A-Za-z0-9_.-]", "_", base) or "image"


def _png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def b64_png(img):
    return base64.b64encode(_png_bytes(img)).decode()


def palette_document(palette, mapping, transparent_idx=None):
    counts = px.palette_counts(palette, mapping)
    return {"grid": [int(mapping.shape[0]), int(mapping.shape[1])],
            "palette": [{"index": int(k), "hex": px.hexc(v),
                         "cells": int(counts[k]),
                         "transparent": k == transparent_idx}
                        for k, v in enumerate(palette)]}


def _rendered_size(shape, render_width, render_scale):
    rows, cols = shape
    width = max(1, int(round(cols * render_scale))) if render_scale else render_width
    return width, max(1, int(round(rows * width / cols)))


def render_png(palette, mapping, bg_mask, render_width, render_scale):
    """(native_png_b64, rendered_at_size_png_b64 | None)."""
    native = Image.fromarray(px.colored_rgba(palette, mapping, bg_mask))
    if not render_scale and not render_width:
        return b64_png(native), None
    rows, cols = mapping.shape
    if render_scale:
        width = max(1, int(round(cols * render_scale)))
    else:
        width = render_width
    height = max(1, int(round(rows * width / cols)))
    up = native.resize((width, height), Image.NEAREST)
    return b64_png(native), b64_png(up)


def render_width_int(q):
    try:
        return int(qget(q, "render_width", 0))
    except ValueError:
        raise ApiError("bad value for 'render_width'")


def render_scale_float(q):
    try:
        return float(qget(q, "render_scale", 0.0))
    except ValueError:
        raise ApiError("bad value for 'render_scale'")


# ---------------------------------------------------------------- core pipeline

def run_pipeline(blob, color_tol, min_scale, max_scale):
    """Common front half: image bytes -> (grid, cells, rmse, palette, mapping)."""
    img, fmt = decode_image(blob)
    grid, cells, rmse, palette, mapping = px.recover(img, color_tol, min_scale, max_scale)
    return img, fmt, grid, cells, rmse, palette, mapping


def background_info(mapping, palette, keep_bg):
    """Detect + flood-fill the background. Returns (info, transparent_idx, mask)."""
    idx, share = px.detect_background(mapping)
    detected = bool(share >= 0.3)
    info = {"index": int(idx), "hex": px.hexc(palette[idx]),
            "border_share": round(float(share), 4), "detected": detected,
            "transparent": False, "edge_cells": 0, "interior_kept": 0}
    transparent_idx, mask = None, None
    if detected:
        mask = px.background_mask(mapping, idx)
        info["edge_cells"] = int(mask.sum())
        info["interior_kept"] = int((mapping == idx).sum()) - int(mask.sum())
        if not keep_bg:
            transparent_idx = idx
            info["transparent"] = True
    return info, transparent_idx, mask


# ---------------------------------------------------------------- endpoints

def grid_stats(grid, rmse):
    return {"rows": int(grid.nrows), "cols": int(grid.ncols), "exact": grid.exact,
            "cell_w": jf(grid.sx), "cell_h": jf(grid.sy),
            "phase_x": jf(grid.x0), "phase_y": jf(grid.y0),
            "align_x_median": jf(np.median(grid.score_x), 2),
            "align_x_max": jf(grid.score_x.max(), 2),
            "align_y_median": jf(np.median(grid.score_y), 2),
            "align_y_max": jf(grid.score_y.max(), 2),
            "raw_rmse": round(float(rmse), 2)}


def analyze_payload(blob, name, q):
    color_tol = qfloat(q, "color_tol", 18.0)
    min_scale = qfloat(q, "min_scale", 4.0)
    max_scale = qfloat(q, "max_scale", 64.0)
    img, fmt, grid, _, rmse, palette, mapping = run_pipeline(
        blob, color_tol, min_scale, max_scale)
    bg, _, _ = background_info(mapping, palette, keep_bg=True)
    counts = px.palette_counts(palette, mapping)
    return {
        "api": API_VERSION, "ok": True, "name": name, "format": fmt,
        "source": {"width": img.shape[1], "height": img.shape[0]},
        "grid": grid_stats(grid, rmse),
        "palette": [px.hexc(v) for v in palette],
        "counts": {str(i): int(c) for i, c in enumerate(counts) if c},
        "mapping": mapping.tolist(),
        "background": bg,
        "shapes": {"rects": len(px.mesh(mapping)),
                   "regions": px.connected_regions(mapping)},
    }


def run_process(blob, name, q):
    color_tol = qfloat(q, "color_tol", 18.0)
    min_scale = qfloat(q, "min_scale", 4.0)
    max_scale = qfloat(q, "max_scale", 64.0)
    keep_bg = qbool(q, "keep_bg", False)
    despeckle = qint(q, "despeckle", 0)
    if despeckle == 0:                    # accept CLI-style "1 = off" as off
        despeckle = 1
    swaps = qswaps(q)
    render_width = render_width_int(q)
    render_scale = render_scale_float(q)
    include_mapping = qbool(q, "include_mapping", True)
    save = qbool(q, "save", False)

    img, fmt, grid, cells, rmse, palette, mapping = run_pipeline(
        blob, color_tol, min_scale, max_scale)
    bg, transparent_idx, bg_mask = background_info(mapping, palette, keep_bg)
    if transparent_idx is None:
        bg_mask = None      # keep_bg: detection stays report-only
        remove_mask = None
    else:
        remove_mask = bg_mask

    removed_regions = removed_cells = 0
    if despeckle > 1:
        removed_regions, removed_cells = px.despeckle(
            mapping, despeckle,
            skip_idx=(transparent_idx,) if transparent_idx is not None else ())

    applied = []
    if swaps:
        palette, applied = px.apply_swaps(palette, swaps)

    rects = px.mesh(mapping, free_mask=remove_mask)
    regions = px.connected_regions(mapping)
    svg = px.svg_string(palette, rects, grid.ncols, grid.nrows,
                        transparent_idx=transparent_idx)

    final = palette[np.maximum(mapping, 0)]
    far = ((np.abs(final - cells) > max(24, color_tol)).any(axis=2)
           & (mapping != px.CLEAR) & (True if remove_mask is None else ~remove_mask))
    lossy = float(far.mean())

    # background hex may have been swapped; re-derive from the index
    if transparent_idx is not None:
        bg = dict(bg)
        bg["hex"] = px.hexc(palette[transparent_idx])

    native_b64, rendered_b64 = render_png(palette, mapping, remove_mask,
                                          render_width, render_scale)

    src_w, src_h = img.shape[1], img.shape[0]
    out = {
        "api": API_VERSION, "ok": True, "name": name, "format": fmt,
        "source": {"width": src_w, "height": src_h},
        "options": {"color_tol": color_tol, "min_scale": min_scale,
                    "max_scale": max_scale, "keep_bg": keep_bg,
                    "despeckle": max(despeckle, 1),
                    "swaps": [{"from": "#" + o, "to": "#" + n}
                              for (o, n) in applied],
                    "transparent": transparent_idx,
                    "render_width": render_width or None,
                    "render_scale": render_scale or None},
        "grid": grid_stats(grid, rmse),
        "palette": [px.hexc(v) for v in palette],
        "background": bg,
        "stats": {"rects": len(rects), "regions": regions,
                  "despeckled_regions": removed_regions,
                  "despeckled_cells": removed_cells,
                  "fidelity_delta": round(lossy, 4)},
        "artifacts": {},
    }
    if include_mapping:
        out["mapping"] = mapping.tolist()
        out["palette_document"] = palette_document(palette, mapping,
                                                   transparent_idx)

    artifacts = {"svg": svg,
                 "png": native_b64,
                 "png_rendered": rendered_b64,
                 "palette_json": json.dumps(
                     palette_document(palette, mapping, transparent_idx),
                     indent=2)}
    out["artifacts"] = artifacts

    if save:
        out["saved"] = save_process(base=sanitize_base(name), svg=svg,
                                    palette=palette, mapping=mapping,
                                    bg_mask=remove_mask,
                                    transparent_idx=transparent_idx,
                                    rendered_b64=rendered_b64,
                                    render_width=render_width,
                                    render_scale=render_scale)
    return out


def save_process(base, svg, palette, mapping, bg_mask, transparent_idx,
                 rendered_b64, render_width, render_scale):
    ensure_out_dir()
    saved = []
    png_img = Image.fromarray(px.colored_rgba(palette, mapping, bg_mask))
    files = [(base + ".svg", svg.encode()),
             (base + ".png", _png_bytes(png_img)),
             (base + ".palette.json",
              json.dumps(palette_document(palette, mapping, transparent_idx),
                         indent=2).encode())]
    if rendered_b64 is not None:
        rw, _ = _rendered_size(mapping.shape, render_width, render_scale)
        files.append((base + f".{rw}px.png", base64.b64decode(rendered_b64)))
    for fname, data in files:
        with open(os.path.join(OUT_DIR, fname), "wb") as f:
            f.write(data)
        saved.append(f"{OUT_DIR}/{fname}")
    return saved


def run_render(body, q):
    despeckle = qint(q, "despeckle", 0)
    if despeckle == 0:
        despeckle = 1
    render_width = render_width_int(q)
    render_scale = render_scale_float(q)
    save = qbool(q, "save", False)
    try:
        data = json.loads(body.decode())
    except Exception:
        raise ApiError("body must be JSON: {palette: [hex...], "
                       "mapping: [[...]], transparent: index|null, ...}")
    if not isinstance(data, dict) or "palette" not in data or "mapping" not in data:
        raise ApiError("JSON body requires 'palette' (hex list) and "
                       "'mapping' (array of index rows)")
    palette = []
    for hexv in data["palette"]:
        if not HEX_RE.match(hexv or ""):
            raise ApiError(f"bad palette hex {hexv!r}; expected 6-digit hex")
        palette.append([int(hexv.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)])
    palette = np.array(palette, dtype=np.int64)
    try:
        mapping = np.array(data["mapping"], dtype=np.int64)
    except (TypeError, ValueError):
        raise ApiError("'mapping' must be a 2-D array of palette indices")
    if mapping.ndim != 2 or mapping.size == 0:
        raise ApiError("'mapping' must be a non-empty 2-D array")
    if mapping.min() < px.CLEAR or mapping.max() >= len(palette):
        raise ApiError("mapping contains palette indices outside the palette "
                       "(-1 marks a transparent cell)")

    transparent = data.get("transparent", None)
    if isinstance(transparent, bool) or \
            not (transparent is None or isinstance(transparent, int)):
        raise ApiError("'transparent' must be an integer index or null")
    if transparent is not None and not 0 <= transparent < len(palette):
        raise ApiError("'transparent' index out of range")
    bg_mask = None
    if transparent is not None:
        bg_mask = px.background_mask(mapping, int(transparent))

    applied = []
    if data.get("swaps"):
        if not isinstance(data["swaps"], (list, tuple)):
            raise ApiError("'swaps' must be a list of OLD=NEW strings")
        palette, applied = px.apply_swaps(palette, swaps_safe(data["swaps"]))

    removed_regions = removed_cells = 0
    if despeckle > 1:
        removed_regions, removed_cells = px.despeckle(
            mapping, despeckle,
            skip_idx=(transparent,) if transparent is not None else ())

    rects = px.mesh(mapping, free_mask=bg_mask)
    regions = px.connected_regions(mapping)
    svg = px.svg_string(palette, rects, mapping.shape[1], mapping.shape[0],
                        transparent_idx=transparent)

    native_b64, rendered_b64 = render_png(palette, mapping, bg_mask,
                                          render_width, render_scale)

    out = {
        "api": API_VERSION, "ok": True, "name": data.get("name", "render"),
        "grid": {"rows": mapping.shape[0], "cols": mapping.shape[1]},
        "palette": [px.hexc(v) for v in palette],
        "swaps_applied": [{"from": "#" + o, "to": "#" + n} for o, n in applied],
        "transparent": transparent,
        "stats": {"rects": len(rects), "regions": regions,
                  "despeckled_regions": removed_regions,
                  "despeckled_cells": removed_cells},
        "artifacts": {"svg": svg, "png": native_b64, "png_rendered": rendered_b64,
                      "palette_json": json.dumps(
                          palette_document(palette, mapping, transparent),
                          indent=2)},
    }
    if save:
        ensure_out_dir()
        saved = []
        png_img = Image.fromarray(px.colored_rgba(palette, mapping, bg_mask))
        base = sanitize_base(data.get("name", "render"))
        files = [(base + ".svg", svg.encode()),
                 (base + ".png", _png_bytes(png_img)),
                 (base + ".palette.json",
                  json.dumps(palette_document(palette, mapping, transparent),
                             indent=2).encode())]
        if rendered_b64 is not None:
            rw, _ = _rendered_size(mapping.shape, render_width, render_scale)
            files.append((base + f".{rw}px.png", base64.b64decode(rendered_b64)))
        for fname, content in files:
            with open(os.path.join(OUT_DIR, fname), "wb") as f:
                f.write(content)
            saved.append(f"{OUT_DIR}/{fname}")
        out["saved"] = saved
    return out


def capabilities_payload():
    return {
        "api": API_VERSION,
        "name": "Gridsprite",
        "description": "Recovers true pixel-art structure from upscaled or "
                       "smeared images: detects the pixel grid, rectifies "
                       "cells, quantizes a palette, meshes maximal "
                       "rectangles, and emits crisp SVG + PNG. Solid "
                       "backgrounds are made transparent with an "
                       "edge-connected flood fill (interior islands of the "
                       "same color are kept). Clean pixel art (<=256 colors, "
                       "binary alpha, even square blocks) is read exactly "
                       "instead; its clear pixels map to -1.",
        "endpoints": {
            "GET /api/v1/capabilities": "this document",
            "POST /api/v1/analyze": {
                "body": "raw image bytes (PNG/JPEG/WebP); or ?name=<file> in query",
                "returns": "grid, palette, mapping, background report, shape stats",
                "params": {
                    "color_tol": "float 0-100, palette merge tolerance (default 18)",
                    "min_scale": "float, smallest expected cell size in px (default 4)",
                    "max_scale": "float, largest expected cell size in px (default 64)",
                },
            },
            "POST /api/v1/process": {
                "body": "raw image bytes (PNG/JPEG/WebP); or ?name=<file> in query",
                "returns": {"artifacts.svg": "SVG text (inline, readable)",
                            "artifacts.png": "base64 PNG at native grid size (alpha when bg removed)",
                            "artifacts.png_rendered": "base64 PNG resized with render_width/render_scale, or null",
                            "artifacts.palette_json": "stringified palette document"},
                "params": {
                    "color_tol": "palette merge tolerance (default 18)",
                    "min_scale": "smallest expected cell size px (default 4)",
                    "max_scale": "largest expected cell size px (default 64)",
                    "keep_bg": "1 to keep the background color opaque (default 0 = removed)",
                    "despeckle": "int, drop connected color regions smaller than N cells (1 or 0 = off, try 2-5)",
                    "swap": "repeatable OLDHEX=NEWHEX, recolor palette entries, e.g. swap=#ff0000=#00ff00",
                    "render_width": "int, additionally render the PNG at this exact pixel width (nearest-neighbor)",
                    "render_scale": "float, alternatively upscale by this multiplier (render_width wins if missing)",
                    "include_mapping": "0 to omit mapping/palette_document (default 1)",
                    "save": "1 to also write artifacts into the server out/ folder; fetch via /api/v1/download",
                },
            },
            "POST /api/v1/render": {
                "body": "JSON {palette:[\"#hex\"], mapping:[[idx]], transparent: idx|null, swaps:[\"old=new\"]}",
                "returns": "svg + png for a (possibly edited) palette/mapping, round-tripping /analyze",
                "params": {
                    "despeckle": "int (default off)",
                    "render_width": "int px width for an additional upscale",
                    "render_scale": "float multiplier alternative",
                    "save": "1 to write into out/",
                },
            },
            "GET /api/v1/download?f=name": "download a file written via save=1 (out/ only)",
            "GET /health": "liveness: {ok: true}",
        },
        "transparent_cells": "mapping value -1 is a transparent cell, in any "
                             "response and in /render input",
        "background_rule": "Background = dominant color on the border ring "
                           "(>=30% share). It is made transparent only where "
                           "4-connected to the border; interior islands of "
                           "the same color stay opaque.",
        "limits": {"max_body_bytes": MAX_BODY, "bind": "127.0.0.1 by default"},
    }


# ---------------------------------------------------------------- HTTP server

def ensure_out_dir():
    os.makedirs(OUT_DIR, exist_ok=True)


class Handler(BaseHTTPRequestHandler):
    server_version = f"GridspriteAPI/{API_VERSION}"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, self.path))

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_file_bytes(self, path):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return self.send_json({"ok": False, "error": "file not found"}, 404)
        ext = os.path.splitext(path)[1].lower()
        ctype = {".svg": "image/svg+xml", ".png": "image/png",
                 ".json": "application/json",
                 ".txt": "text/plain"}.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ApiError(f"body too large ({n} bytes; max {MAX_BODY})", 413)
        if n == 0:
            raise ApiError("empty request body; POST raw image bytes")
        return self.rfile.read(n)

    def route_query(self):
        return urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query, keep_blank_values=True)

    def _body_name(self, q):
        """Filename hint: ?name= query, else Content-Disposition, else Content-Type."""
        if qget(q, "name"):
            return qget(q, "name")
        cd = self.headers.get("Content-Disposition") or ""
        m = re.findall(r'filename="?([^";]+)"?', cd)
        if m:
            return os.path.basename(m[0])
        ct = (self.headers.get("Content-Type") or "").lower().split(";")[0].strip()
        return {"image/png": "uploaded.png", "image/jpeg": "uploaded.jpg",
                "image/webp": "uploaded.webp"}.get(ct, "uploaded.png")

    # -- routes -------------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            return self.send_json({"ok": True, "api": API_VERSION})
        if path in ("/", "/index.html"):
            return self.send_json({"ok": True, "api": API_VERSION,
                                   "see": ["/api/v1/capabilities",
                                           "/api/v1/analyze",
                                           "/api/v1/process",
                                           "/api/v1/render"],
                                   "reference": "API.md"})
        if path == "/api/v1/capabilities":
            return self.send_json(capabilities_payload())
        if path.startswith("/api/v1/download"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            fname = (q.get("f") or [""])[0]
            if not fname or os.path.dirname(fname) or fname.startswith("."):
                return self.send_json({"ok": False, "error": "bad path"}, 400)
            return self.send_file_bytes(os.path.join(OUT_DIR, fname))
        return self.send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/v1/analyze":
                q = self.route_query()
                return self.send_json(analyze_payload(self.read_body(),
                                                      self._body_name(q), q))
            if path == "/api/v1/process":
                q = self.route_query()
                return self.send_json(run_process(self.read_body(),
                                                  self._body_name(q), q))
            if path == "/api/v1/render":
                q = self.route_query()
                return self.send_json(run_render(self.read_body(), q))
        except ApiError as e:
            return self.send_json({"ok": False, "error": str(e)}, e.code)
        except ValueError as e:
            return self.send_json({"ok": False, "error": str(e)}, 400)
        except Exception as e:  # noqa — surface to the client
            return self.send_json(
                {"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        return self.send_json({"ok": False, "error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8643)
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; 0.0.0.0 exposes it to your network")
    args = ap.parse_args()
    ensure_out_dir()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Gridsprite API v{API_VERSION} on http://{args.host}:{args.port}"
          f" — POST /api/v1/process with raw image bytes", flush=True)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: listening on {args.host} — anyone on your network "
              f"can use it", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("bye")


if __name__ == "__main__":
    main()
