# Gridsprite

Recovers the **true pixel-art structure** from upscaled, anti-aliased or
smeared images: it detects the original pixel grid, picks a dominant color per
logical cell, quantizes a palette, then meshes maximal color rectangles into a
crisp, recolorable SVG — plus RGBA PNGs at native or any requested size.

Solid backgrounds are removed with an **edge-connected flood fill**: the
dominant border-ring color (≥ 30% share) becomes transparent only where it is
4-connected to the canvas border. Interior islands of the same color — think
white eyes on a sprite over a white background — are always kept opaque.

## Components

| File | What it is |
|---|---|
| `gridsprite.py` | Core library + CLI: `python3 gridsprite.py image.png -o out [--color-tol T] [--swap #old=#new] [--despeckle N] [--keep-bg]` |
| `gridsprite_gui.py` | Local web GUI (paint/recolor regions, shimmer previews, palette presets): `python3 gridsprite_gui.py --port 8799` |
| `gridsprite_api.py` | Stateless JSON HTTP API for scripts and AI agents: `python3 gridsprite_api.py --port 8643` — see [`API.md`](API.md) |
| `.claude/skills/gridsprite/SKILL.md` | Agent skill describing the HTTP API |
| `sample_*.png` | Test fixtures (upscaled pixel-art sprites) |

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install numpy pillow

# CLI
.venv/bin/python gridsprite.py sample_1.png -o out

# API (used by agents — see API.md and the gridsprite skill)
.venv/bin/python gridsprite_api.py --port 8643
curl -sS -X POST --data-binary @sample_1.png \
  "http://127.0.0.1:8643/api/v1/process?render_width=512"
```

## Pipeline

1. **Grid detection** — per-axis smoothed luminance edge profile; finds the
   largest cell size whose edge peaks all sit on one phase (resolves
   sub-grid ambiguity).
2. **Rectification** — dominant color per cell (raw RMSE reported as a
   smearometer).
3. **Palette quantization** — merge colors within `color_tol`.
4. **Background handling** — border-ring detection + edge-connected
   transparency (interior cells of the same color survive), despeckle of tiny
   color regions.
5. **Rect meshing → SVG** — one `<g id="pcN" fill="#...">` per palette color;
   recoloring is just editing `fill` values.

Requires Python ≥ 3.9, `numpy`, `pillow`. No authentication/binding defaults
to `127.0.0.1` — keep it local.
