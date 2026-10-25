#!/usr/bin/env python3
"""tools/validate_static.py — dump reference outputs of gridsprite.py for the
JS-port equivalence check (run tools/validate_static.mjs afterwards).

Writes to <outdir>/ for every sample_*.png, and for generated clean pixel
art (read exactly, with transparent cells):
    <base>.raw          raw RGBA uint8 bytes (the decoder-independent input)
    <base>.expected.json  reference derived values incl. SVG text and RGBA
"""
import base64
import glob
import json
import os
import sys
import tempfile

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gridsprite as gs


def pixel_art(block):
    """Deterministic clean pixel art: 24x18 cells of 9 colors with clear
    pixels around and inside it, each cell drawn as block x block pixels."""
    rng = np.random.default_rng(7)
    colors = rng.integers(0, 256, size=(9, 3))
    cells = rng.integers(0, len(colors), size=(18, 24))
    rgba = np.zeros((18, 24, 4), dtype=np.uint8)
    rgba[:, :, :3] = colors[cells]
    rgba[:, :, 3] = 255
    rgba[:3] = 0
    rgba[8:11, 10:14] = 0
    return np.repeat(np.repeat(rgba, block, axis=0), block, axis=1)


def dump(base, img_u8, outdir):
    raw = os.path.join(outdir, base + ".raw")
    exp = os.path.join(outdir, base + ".expected.json")
    with open(raw, "wb") as f:
        f.write(img_u8.tobytes())

    grid, _, rmse, palette, mapping = gs.recover(img_u8, 18.0, 4.0, 64.0)
    img = img_u8
    idx, share = gs.detect_background(mapping)
    transparent = int(idx) if share >= 0.3 else None
    bg_mask = None
    if transparent is not None:
        bg_mask = gs.background_mask(mapping, transparent)
    rects = gs.mesh(mapping, free_mask=bg_mask)
    regions = gs.connected_regions(mapping)
    svg = gs.svg_string(palette, rects, grid.ncols, grid.nrows,
                        transparent_idx=transparent)
    rgba = gs.colored_rgba(palette, mapping, bg_mask)
    H, W = img.shape[0], img.shape[1]
    preview = np.asarray(Image.fromarray(rgba).resize((W, H), Image.NEAREST))
    _, pal_json_path, pal_txt_path = ("", "j", "t"), ("", "", ""), ("", "", "")
    pjson = os.path.join(outdir, base + ".palette.json")
    ptxt = os.path.join(outdir, base + ".palette.txt")
    gs.write_palette(pjson, ptxt, palette, mapping, transparent)
    with open(pjson) as f:
        pal_json_text = f.read()
    with open(ptxt) as f:
        pal_txt_text = f.read()
    os.remove(pjson)
    os.remove(ptxt)

    data = {
        "name": base + ".png",
        "W": int(W), "H": int(H),
        "sx": float(grid.sx), "sy": float(grid.sy),
        "x0": float(grid.x0), "y0": float(grid.y0),
        "ncols": int(grid.ncols), "nrows": int(grid.nrows),
        "exact": bool(grid.exact),
        "rmse": float(rmse),
        "medianX": float(np.median(grid.score_x)),
        "medianY": float(np.median(grid.score_y)),
        "bgShare": float(share),
        "palette": [gs.hexc(v) for v in palette],
        "mapping": [int(v) for v in mapping.ravel()],
        "transparent": transparent,
        "rects": [[int(v) for v in r] for r in rects],
        "regions": int(regions),
        "svg": svg,
        "palJson": pal_json_text,
        "palTxt": pal_txt_text,
        "rgba": base64.b64encode(rgba.tobytes()).decode(),
        "preview": base64.b64encode(preview.tobytes()).decode(),
    }
    with open(exp, "w") as f:
        json.dump(data, f)
    print(f"  {base}: {grid.ncols}x{grid.nrows} grid, {len(palette)} colors, "
          f"{len(rects)} rects")


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else \
        os.path.join(tempfile.gettempdir(), "gridsprite-validate")
    os.makedirs(outdir, exist_ok=True)
    # drop dumps of fixtures that no longer exist, so the JS side never
    # compares against stale references
    for f in glob.glob(os.path.join(outdir, "*.expected.json")) + \
            glob.glob(os.path.join(outdir, "*.raw")):
        os.remove(f)
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    files = sorted(glob.glob(os.path.join(root, "sample_*.png")))
    print(f"Reference dump for {len(files)} samples + 2 pixel-art fixtures -> {outdir}")
    for p in files:
        dump(os.path.splitext(os.path.basename(p))[0],
             np.asarray(Image.open(p).convert("RGBA")), outdir)
    dump("pixel_art_native", pixel_art(1), outdir)
    dump("pixel_art_x7", pixel_art(7), outdir)
    print("done")


if __name__ == "__main__":
    main()
