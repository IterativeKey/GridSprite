# Gridsprite

<p align="center">
  <img src="sample_5.png" alt="Gridsprite logo" width="256"/>
</p>

Gridsprite takes upscaled, anti-aliased or JPEG-smeared pixel art and recovers the original sprite: the true pixel grid and the true palette. It exports the sprite as a clean SVG that's easy to recolor, plus a crisp PNG.

**Try it in the browser:** [iterativekey.github.io/GridSprite](https://iterativekey.github.io/GridSprite/). Everything runs locally and nothing is uploaded.

![Smeared input (left) and recovered sprite (right)](assets/hero.png)

## How it works

Clean pixel art skips steps 1–3. This means at most 256 colors, every pixel fully opaque or fully clear, drawn in even square blocks, at its true size or as a crisp nearest-neighbor upscale. Gridsprite reads it exactly: same colors, same transparency, one cell per art pixel. Everything else goes through the full pipeline:

1. **Grid detection:** finds the largest cell size whose edges all line up, so an 8px sub-phase never beats the real 16px grid.
2. **Rectification:** takes each cell's dominant color from its center, ignoring anti-aliasing halos.
3. **Palette quantization:** merges near-identical colors (tolerance 18 by default).
4. **Background removal:** makes the border color transparent only where it touches the edge, so white eyes on a white background stay white.
5. **SVG meshing:** merges cells into as few rectangles as possible, with one `<g fill="#hex">` group per color. To recolor, edit a single `fill`.

## Usage

Requires Python 3.9+ with `numpy` and `pillow`:

```bash
python3 -m venv .venv && .venv/bin/pip install numpy pillow
```

| Tool | Command |
|---|---|
| CLI | `.venv/bin/python gridsprite.py sample_1.png -o out [--keep-bg] [--despeckle N] [--swap '#old=#new']` |
| Editor (global/region recoloring; rectangle select; brush, fill, erase and pick tools; zoom and pan; pixel grid; undo, themes) | open `docs/index.html`, or serve `docs/` (see [Static site](#static-site)) |
| HTTP API for scripts and agents ([API.md](API.md)) | `.venv/bin/python gridsprite_api.py` → http://127.0.0.1:8643 |

The CLI processes every image in the current directory when you don't name any files.

The API listens on localhost only. To use it from other devices on your network, add `--host 0.0.0.0`. Anyone on that network can then use it.

## Static site

`docs/` is the editor with the pipeline ported to dependency-free JavaScript. It's served by GitHub Pages from `main` → `/docs` and also works from `file://`.

To check that the JS port matches the Python pipeline byte-for-byte on every `sample_*.png`:

```bash
.venv/bin/python tools/validate_static.py && node tools/validate_static.mjs
# → 172/172 checks passed — equivalence verified
```

## License

[MIT](LICENSE) © 2026 Iterative Key LLC
