---
name: gridsprite
description: Vectorize and edit pixel art via the local Gridsprite HTTP API (port 8643). Detects true pixel-grid structure, quantizes palettes, removes solid backgrounds (edge-connected only), swaps recolors, despeckles, and renders SVG or crisp PNGs at any size. Use when the user asks to vectorize pixel art, convert a sprite/pixel-art PNG or JPG to clean SVG or upscaled PNG, remove a solid color background, recolor by palette, upscale pixel art to a specific size, or round-trip edit a sprite's palette/cell map.
---

# Gridsprite API skill

Local HTTP/JSON service that converts pixel-art images (PNG/JPEG/WebP) into
clean SVG + crisp PNG, with palette quantization, background removal,
recoloring, despeckling, and size-exact rendering. Full reference lives in
`API.md` next to `gridsprite_api.py`; a machine-readable capability document
is served at `GET /api/v1/capabilities`.

## 0. Ensure the server is running

1. Check: `curl -s http://127.0.0.1:8643/health` → expect
   `{"ok": true, "api": "1.0"}`.
2. If down, start it as a **long-lived background process** (do not block):

```bash
cd <gridsprite repo root>
PY=.venv/bin/python                       # venv has numpy + pillow
"$PY" -m py_compile gridsprite_api.py     # quick sanity
nohup "$PY" gridsprite_api.py --port 8643 > /tmp/gridsprite_api.log 2>&1 &
# wait ~1-2s, then re-run the health check
```

(Any Python ≥3.9 with `numpy` and `pillow` works: `pip install numpy pillow`
then `python gridsprite_api.py --port 8643`.)

If the port is busy (Errno 48), an old instance may hold it:
`pkill -f "gridsprite_api.py --port 8643"` and restart, or use another port
and tell the client.

## 1. Core endpoint — process an image

`POST /api/v1/process` with **raw image bytes** as the body (no multipart).
Query parameters do the work:

```bash
curl -sS -X POST --data-binary @sprite.png \
  "http://127.0.0.1:8643/api/v1/process?despeckle=3&render_width=512&swap=%23fbf0d8%3D%23ff00ff"
```

| Task | Parameters |
|---|---|
| Vectorize + remove bg (default behavior) | *(no params needed)* |
| Keep the background opaque | `keep_bg=1` |
| Aggressively merge colors / fewer colors | raise `color_tol` (default 18, scale ~0–100) |
| Keep more source detail | lower `color_tol` |
| Grid cells are expected smaller/larger | `min_scale` / `max_scale` (default 4 / 64 px) |
| Clean stray cells | `despeckle=2..5` |
| Recolor: all `#fbf0d8` → `#ff00ff` | `swap=#fbf0d8=#ff00ff` (repeatable) |
| Render at a pixel size | `render_width=<int>` (e.g. 512) or `render_scale=8` |
| Also save artifacts server-side | `save=1` (then `GET /api/v1/download?f=<name>`) |
| Skip big arrays in the response | `include_mapping=0` |

**URL-encode `#` as `%23`** (`=` may stay literal; `%3D` also works). Raw `#`
in a query string silently truncates the query — the #1 calling mistake.

## 2. Response anatomy (process/analyze/render)

```jsonc
{
  "ok": true,
  "grid": {"rows": 63, "cols": 63, "raw_rmse": 21.4},
  "palette": ["#fbf0d8", "#c25f2e"],       // index i ⇔ mapping value i ⇔ SVG <g id="pc{i}">
  "mapping": [[0,0,...],[...]],            // rows×cols palette indices (unless include_mapping=0)
  "background": {"detected": true, "index": 0, "hex": "#fbf0d8",
                 "border_share": 1.0, "transparent": true,
                 "edge_cells": 2146, "interior_kept": 0},
  "stats": {"rects": 312, "regions": 96, "despeckled_regions": 4,
            "fidelity_delta": 0.02},       // >0.15 ⇒ lower color_tol
  "artifacts": {
    "svg":  "<svg xmlns=... >...</svg>",   // plain text, directly writable
    "png": "<base64 PNG, native rows×cols, RGBA>",
    "png_rendered": "<base64 PNG at requested size | null>",
    "palette_json": "{\"grid\":...}"       // stringified palette doc
  },
  "saved": ["out/sprite.svg", ...]         // only when save=1
}
```

Background rule (consistent everywhere): the dominant border-ring color
(≥30% share) is removed **only where 4-connected to the border**; interior
islands of the same color stay opaque. `keep_bg=1` disables removal entirely.

## 3. Advance an edit with analyze → render

For recolors not expressible as palette-wide swaps (e.g. repaint one region),
round-trip:

1. `POST /api/v1/analyze` (image bytes) → get `palette`, `mapping`,
   `background`.
2. Mutate: append a color to `palette` / rewrite `mapping` cells / flip
   `background.index` handling as needed. Keep indices valid:
   `0 ≤ mapping[y][x] < len(palette)`.
3. `POST /api/v1/render` with JSON body:

```json
{"palette": ["#fff", "#3366ff"], "mapping": [[1, 1, 0]],
 "transparent": 0, "swaps": ["#c25f2e=#101418"], "name": "my-edit"}
```

Query params on `/render`: `render_width`, `render_scale`, `despeckle`,
`save`. Response shape is the same `artifacts` block.

## 4. Writing results to disk

- `artifacts.svg` — write the string out verbatim (UTF-8). It uses one
  `<g id="pcN" fill="#...">` per color; recolor by editing `fill`.
- `artifacts.png` / `artifacts.png_rendered` — base64; decode to bytes before
  saving. `png` is exactly `rows×cols` px; `png_rendered` honors
  `render_width` exactly (aspect preserved, nearest-neighbor).
- Or pass `save=1` and use `GET /api/v1/download?f=<basename>`.

Python one-liner style:

```python
import json, urllib.request, base64
blob = open("sprite.png","rb").read()
r = json.loads(urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:8643/api/v1/process?render_width=512",
    data=blob, method="POST")).read())
open("sprite.svg","w").write(r["artifacts"]["svg"])            # note: text
open("sprite_512.png","wb").write(base64.b64decode(r["artifacts"]["png_rendered"]))
```

## 5. Error handling

Every error is JSON `{"ok": false, "error": "..."}` with an HTTP 400/404/413:
decoding failures ("could not decode image body"), grid-not-found
(not-a-pixel-art image), invalid swaps (`OLDHEX=NEWHEX`, 6-digit hex),
mapping/palette mismatches. Trust the `error` strings — they name the bad
parameter. A raw `#` mistake surfaces as "bad swap ''" — that means the URL
query was truncated, not that the colors are wrong.

## 6. Pitfalls checklist

- [ ] Sent **raw bytes**, not multipart/JSON, to image endpoints
- [ ] URL-encoded `%23` for every `#` in query params
- [ ] `png_rendered` may be `null` when no size was requested
- [ ] `despeckle` needs ≥2 to do anything (0/1 = off)
- [ ] Large `color_tol` can eat fine details; check `fidelity_delta`
- [ ] `mapping` indices must stay inside the palette when editing
- [ ] The API is localhost-only (default) and stateless — nothing is cached
      between calls; save artifacts you need.
