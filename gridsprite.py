#!/usr/bin/env python3
"""gridsprite.py — recover true pixel-art structure from upscaled/smeared images.

Pipeline: grid detection -> rectification (dominant color per cell) ->
color quantization -> greedy rectangle meshing -> SVG organized by palette
color (CSS variables, one `<g>` per palette color, for easy recoloring).

Clean pixel art (at most 256 colors, fully opaque or fully clear pixels,
drawn in even square blocks — at native size or a nearest-neighbor upscale)
skips detection and quantization: it is read exactly, and its clear pixels
become transparent cells (palette index -1 in the cell mapping).

Grid detection works per axis on the smoothed luminance edge profile: it
finds strong edge peaks and picks the LARGEST cell size such that every
strong edge lies on a single phase of that period. This resolves the
8px-subphase vs 16px-unit ambiguity (sub-phases of the true unit also fit
perfectly, so the largest consistent size is the true unit).

Also writes a rectified PNG preview (palette colors, nearest-neighbor
upscale back to source resolution) so quality can be eyeballed per file.

Usage:
    python3 gridsprite.py [inputs...] [-o outdir] [--color-tol T] [--min-scale S] [--max-scale S]

With no inputs, processes .png/.jpg in the current directory.
"""

import argparse
import glob
import json
import os
import sys

import numpy as np
from PIL import Image

SEARCH_STEP = 0.05          # candidate cell sizes, px
ALIGN_TOL = 0.25            # peak may sit at most 0.25*step from a grid line
# Margin subtracted from the fit tolerance: knife-edge fits landing exactly
# on (or an fp ulp above) the boundary behave identically across math
# libraries (numpy vs JS Math), keeping the pure-JS port bit-compatible.
FIT_MARGIN = 1e-6


# ---------------------------------------------------------------- grid setup

def edge_profile(img, axis):
    """Edge energy per boundary (summed |luminance derivative| along an axis),
    lightly smoothed so gradient texture peaks are dampened."""
    lum = img.mean(axis=2)
    g = np.abs(np.diff(lum, axis=axis)).sum(axis=1 - axis)
    k = np.exp(-0.5 * ((np.arange(9) - 4) / 2.0) ** 2)
    k /= k.sum()
    return np.convolve(g, k, mode="same")


def find_strong_peaks(profile, pct=90.0):
    thr = np.percentile(profile, pct)
    n = len(profile)
    peaks = [i for i in range(2, n - 2)
             if profile[i] >= thr and profile[i] > profile[i - 1]
             and profile[i] >= profile[i + 1]]
    return np.array(peaks, dtype=float)


def circular_phase(amps, peaks, step):
    """Intensity-weighted phase of peaks modulo step, via circular mean."""
    ang = 2 * np.pi * peaks / step
    return (np.arctan2((amps * np.sin(ang)).sum(),
                       (amps * np.cos(ang)).sum()) * step / (2 * np.pi)) % step


def peak_deviation(peaks, step, phase):
    """Distance of each peak to the nearest grid line of (step, phase)."""
    lines = (np.round((peaks - phase) / step) * step + phase)
    dev = np.abs(peaks - lines)
    return np.minimum(dev, step - dev)


def detect_grid_axis(profile, min_scale, max_scale):
    """Largest cell size in [min_scale, max_scale] such that ALL strong edge
    peaks align to one phase of that period (within ALIGN_TOL*step)."""
    peaks = find_strong_peaks(profile)
    if len(peaks) < 8:
        raise ValueError("not enough edge structure to detect a grid")
    amps = profile[peaks.astype(int)]
    tried_ok = False
    for step in np.arange(max_scale, min_scale - 1e-9, -SEARCH_STEP):
        phase = circular_phase(amps, peaks, step)
        # Local phase refinement scan, then verify all peaks fit.
        for dp in np.arange(-ALIGN_TOL * step, ALIGN_TOL * step, 0.25):
            p = (phase + dp) % step
            dev = peak_deviation(peaks, step, p)
            if dev.max() <= ALIGN_TOL * step - FIT_MARGIN:
                return step, p, dev
        tried_ok = True
    if tried_ok:
        raise ValueError("no common grid phase fits all strong edges; "
                         "the image may mix resolutions or be floating art")
    raise ValueError("no grid found")


def detect_grid(img, min_scale, max_scale):
    px = edge_profile(img, axis=1)   # vertical boundaries
    py = edge_profile(img, axis=0)   # horizontal boundaries
    (sx, x0, dev_x) = detect_grid_axis(px, min_scale, max_scale)
    (sy, y0, dev_y) = detect_grid_axis(py, min_scale, max_scale)
    ncols = max(2, int(round((img.shape[1] - x0) / sx)))
    nrows = max(2, int(round((img.shape[0] - y0) / sy)))
    return Grid(sx, x0, sy, y0, ncols, nrows, dev_x, dev_y)

class Grid:
    def __init__(self, sx, x0, sy, y0, ncols, nrows, score_x, score_y, exact=False):
        self.sx, self.x0, self.sy, self.y0 = sx, x0, sy, y0
        self.ncols, self.nrows = ncols, nrows
        self.score_x, self.score_y = score_x, score_y
        self.exact = exact


# ------------------------------------------------------------- exact pixel art

CLEAR = -1                  # mapping value of a transparent cell
MAX_EXACT_COLORS = 256


def exact_grid(rgba):
    """Clean pixel art read exactly: (grid, palette, mapping), or None.

    The art must have only fully opaque or fully clear pixels, at most
    MAX_EXACT_COLORS colors, and be made of even square blocks from the
    top-left corner; the largest such block is one cell. Palette is ordered
    by cell count (ties by color value); clear cells map to CLEAR."""
    alpha = rgba[:, :, 3]
    if not np.isin(alpha, (0, 255)).all():
        return None
    rgb = rgba[:, :, :3].astype(np.int64)
    key = np.where(alpha == 255, (rgb[:, :, 0] << 16) | (rgb[:, :, 1] << 8) | rgb[:, :, 2], CLEAR)
    H, W = key.shape
    side = int(np.gcd(H, W))
    for block in range(side, 0, -1):
        if side % block:
            continue
        small = key[::block, ::block]
        if (np.repeat(np.repeat(small, block, axis=0), block, axis=1) == key).all():
            break
    nrows, ncols = small.shape
    if nrows < 2 or ncols < 2:
        return None
    opaque = small != CLEAR
    colors, inverse, counts = np.unique(small[opaque], return_inverse=True, return_counts=True)
    if not 0 < len(colors) <= MAX_EXACT_COLORS:
        return None
    order = np.argsort(-counts, kind="stable")
    rank = np.empty(len(order), dtype=np.int64)
    rank[order] = np.arange(len(order))
    mapping = np.full(small.shape, CLEAR, dtype=np.int64)
    mapping[opaque] = rank[inverse.ravel()]
    colors = colors[order]
    palette = np.stack([(colors >> 16) & 255, (colors >> 8) & 255, colors & 255], axis=1)
    zero = np.zeros(1)
    return Grid(float(block), 0.0, float(block), 0.0, ncols, nrows, zero, zero, exact=True), palette, mapping


def recover(rgba, color_tol, min_scale, max_scale):
    """Image (H, W, 4 uint8) -> (grid, cells, rmse, palette, mapping).

    Clean pixel art is read exactly (exact_grid); anything else goes through
    grid detection, rectification and palette quantization. cells holds each
    cell's source color, for fidelity checks."""
    exact = exact_grid(rgba)
    if exact is not None:
        grid, palette, mapping = exact
        return grid, palette[np.maximum(mapping, 0)], 0.0, palette, mapping
    img = rgba[:, :, :3].astype(np.float64)
    grid = detect_grid(img, min_scale, max_scale)
    cells, rmse = rectify(img, grid)
    palette, mapping = build_palette(cells, color_tol)
    return grid, cells, rmse, palette, mapping


def load_rgba(source):
    """Path or file object -> (H, W, 4) uint8 array."""
    return np.asarray(Image.open(source).convert("RGBA"))


# ------------------------------------------------------------- rectification

def rectify(img, grid, inner=0.15):
    """Dominant color per cell (sampling the central (1-2*inner) portion of
    the cell so small alignment drift does not spill across boundaries),
    plus per-cell-mean reconstruction RMSE as a quality diagnostic."""
    H, W, _ = img.shape
    cells = np.zeros((grid.nrows, grid.ncols, 3), dtype=np.uint8)
    rmse_num, rmse_den = 0.0, 0.0
    for i in range(grid.nrows):
        ya = max(0, int(np.floor(grid.y0 + i * grid.sy + inner * grid.sy)))
        yb = min(H, int(np.ceil(grid.y0 + (i + 1) * grid.sy - inner * grid.sy)))
        if yb <= ya:
            ya, yb = max(0, i * 1), min(H, ya + 1)
        for j in range(grid.ncols):
            xa = max(0, int(np.floor(grid.x0 + j * grid.sx + inner * grid.sx)))
            xb = min(W, int(np.ceil(grid.x0 + (j + 1) * grid.sx - inner * grid.sx)))
            if xb <= xa:
                xb = min(W, xa + 1)
            block = img[ya:yb, xa:xb].reshape(-1, 3)
            mean = block.mean(axis=0)
            colors, counts = np.unique(block, axis=0, return_counts=True)
            cells[i, j] = colors[counts.argmax()]
            rmse_num += ((block - mean) ** 2).sum()
            rmse_den += block.shape[0]
    rmse = float(np.sqrt(rmse_num / max(1, rmse_den)))
    return cells.astype(np.int64), rmse


# ------------------------------------------------------------- quantization

def build_palette(cells, tol):
    """Greedy color merging (RGB Euclidean tolerance) with one
    weighted-centroid refinement pass. Returns (palette, cell->palette map)."""
    flat = cells.reshape(-1, cells.shape[-1]).astype(np.float64)
    uniq, inv, counts = np.unique(flat, axis=0, return_inverse=True,
                                  return_counts=True)
    order = np.argsort(-counts, kind="stable")
    pal = []
    for idx in order:
        c = uniq[idx]
        w = counts[idx]
        for p in pal:
            if np.linalg.norm(c - p["centroid"]) <= tol:
                p["count"] += w
                p["sum"] += c * w
                p["centroid"] = p["sum"] / p["count"]
                break
        else:
            pal.append({"centroid": c.copy(), "count": w, "sum": c * w})
    centroids = np.array([p["centroid"] for p in pal])
    d = ((uniq[:, None, :] - centroids[None, :, :]) ** 2).sum(axis=2)
    idx_map = d.argmin(axis=1)
    for k in range(len(centroids)):
        m = idx_map == k
        if m.any():
            centroids[k] = (uniq[m] * counts[m, None]).sum(axis=0) / counts[m].sum()
    d = ((uniq[:, None, :] - centroids[None, :, :]) ** 2).sum(axis=2)
    idx_map = d.argmin(axis=1)
    mapping = idx_map[inv].reshape(cells.shape[:2])
    return np.round(centroids).astype(np.int64), mapping


def despeckle(mapping, max_size, skip_idx=()):
    """Remove connected same-color regions smaller than max_size cells by
    reassigning them to the majority color of their 4-adjacent neighbors.
    Returns (regions removed, cells changed)."""
    H, W = mapping.shape
    labels = np.full(mapping.shape, -1, dtype=np.int64)
    regions = []  # (idx, [(y,x), ...])
    for y in range(H):
        for x in range(W):
            if labels[y, x] >= 0:
                continue
            rid = len(regions)
            c = mapping[y, x]
            cells_ = [(y, x)]
            labels[y, x] = rid
            stack = [(y, x)]
            while stack:
                cy, cx = stack.pop()
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if (0 <= ny < H and 0 <= nx < W and labels[ny, nx] < 0
                            and mapping[ny, nx] == c):
                        labels[ny, nx] = rid
                        cells_.append((ny, nx))
                        stack.append((ny, nx))
            regions.append((mapping[y, x], cells_))
    removed_regions = 0
    removed_cells = 0
    for c, cells_ in regions:
        if c == CLEAR or c in skip_idx or len(cells_) >= max_size:
            continue
        # majority color among neighboring cells of other regions
        votes = {}
        for (cy, cx) in cells_:
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < H and 0 <= nx < W and (ny, nx) not in set(cells_) \
                        and mapping[ny, nx] != c:
                    cc = int(mapping[ny, nx])
                    votes[cc] = votes.get(cc, 0) + 1
        if not votes:
            continue
        new = max(votes, key=votes.get)
        for (cy, cx) in cells_:
            mapping[cy, cx] = new
        removed_regions += 1
        removed_cells += len(cells_)
    return removed_regions, removed_cells


def detect_background(mapping):
    """Dominant palette index along the border ring -> background candidate.
    Transparent cells never count as the background color."""
    ring = np.concatenate([mapping[0], mapping[-1], mapping[:, 0], mapping[:, -1]])
    colored = ring[ring != CLEAR]
    if not len(colored):
        return 0, 0.0
    counts = np.bincount(colored)
    return int(counts.argmax()), counts.max() / len(ring)


def background_mask(mapping, bg_idx):
    """4-connected flood fill of `bg_idx` cells reachable from the border ring.

    Returns a boolean mask marking only the edge-connected background region.
    Interior islands of the same palette color are NOT part of the mask and
    therefore stay opaque."""
    H, W = mapping.shape
    mask = np.zeros(mapping.shape, dtype=bool)
    stack = []
    for x in range(W):
        for y in (0, H - 1):
            if mapping[y, x] == bg_idx and not mask[y, x]:
                mask[y, x] = True
                stack.append((y, x))
    for y in range(H):
        for x in (0, W - 1):
            if mapping[y, x] == bg_idx and not mask[y, x]:
                mask[y, x] = True
                stack.append((y, x))
    while stack:
        cy, cx = stack.pop()
        for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
            if (0 <= ny < H and 0 <= nx < W and not mask[ny, nx]
                    and mapping[ny, nx] == bg_idx):
                mask[ny, nx] = True
                stack.append((ny, nx))
    return mask


# --------------------------------------------------------------- meshing/SVG

def connected_regions(mapping):
    """Count 4-connected same-palette-color components."""
    seen = np.zeros(mapping.shape, dtype=bool)
    regions = 0
    stack = []
    for y in range(mapping.shape[0]):
        for x in range(mapping.shape[1]):
            if seen[y, x]:
                continue
            regions += 1
            c = mapping[y, x]
            stack.append((y, x))
            seen[y, x] = True
            while stack:
                cy, cx = stack.pop()
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if (0 <= ny < mapping.shape[0] and 0 <= nx < mapping.shape[1]
                            and not seen[ny, nx] and mapping[ny, nx] == c):
                        seen[ny, nx] = True
                        stack.append((ny, nx))
    return regions


def mesh(mapping, free_mask=None):
    """Greedy maximal-rectangle decomposition (merged per color region).

    Cells where free_mask is True are skipped entirely (e.g. the transparent
    background region), as are transparent (CLEAR) cells; interior cells of
    the same palette color that are not part of that mask still produce
    rects."""
    free = mapping != CLEAR
    if free_mask is not None:
        free &= ~np.asarray(free_mask, dtype=bool)
    rects = []
    H, W = mapping.shape
    for y in range(H):
        for x in range(W):
            if not free[y, x]:
                continue
            c = mapping[y, x]
            w = 1
            while x + w < W and free[y, x + w] and mapping[y, x + w] == c:
                w += 1
            h = 1
            while (y + h < H and free[y + h, x:x + w].all()
                   and (mapping[y + h, x:x + w] == c).all()):
                h += 1
            free[y:y + h, x:x + w] = False
            rects.append((x, y, w, h, c))
    return rects


def hexc(rgb):
    return "#%02x%02x%02x" % tuple(int(v) for v in rgb)


def fmt(v):
    return ("%.3f" % v).rstrip("0").rstrip(".")


def svg_string(palette, rects, ncols, nrows, transparent_idx=None):
    """Build the SVG document as a string (one <g> per palette color)."""
    cells_used = np.zeros(len(palette), dtype=np.int64)
    for (_, _, w, h, c) in rects:
        cells_used[c] += w * h
    rects.sort(key=lambda r: r[4])
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {ncols} {nrows}" '
        f'width="{ncols * 16}" height="{nrows * 16}" shape-rendering="crispEdges">',
        '  <!-- Palette: one <g> per color. To recolor, change the fill of the',
        '       matching g element (hex values are unique to the whole file). -->',
    ]
    for k, c in enumerate(palette):
        if k == transparent_idx:
            lines.append(f'  <!-- pc{k} {hexc(c)} ({int(cells_used[k])} cells) '
                         f'-> background color; edge-connected region is transparent '
                         f'-->')
        elif cells_used[k] > 0:
            lines.append(f'  <!-- pc{k} {hexc(c)} ({int(cells_used[k])} cells) -->')
    for k in range(len(palette)):
        group = [r for r in rects if r[4] == k]
        if not group:
            continue
        lines.append(
            f'  <g id="pc{k}" fill="{hexc(palette[k])}">')
        lines.extend(f'    <rect x="{fmt(x)}" y="{fmt(y)}" width="{fmt(w)}" height="{fmt(h)}"/>'
                     for (x, y, w, h, _) in group)
        lines.append('  </g>')
    lines.append('</svg>')
    return "\n".join(lines) + "\n"


def write_svg(path, palette, mapping, rects, ncols, nrows, transparent_idx=None):
    with open(path, "w") as f:
        f.write(svg_string(palette, rects, ncols, nrows,
                           transparent_idx=transparent_idx))


def colored_rgba(palette, mapping, bg_mask=None):
    """RGBA uint8 image at native logical resolution.

    bg_mask: boolean mask of cells to make fully transparent (the
    edge-connected background region). Interior cells of the same palette
    color stay opaque. Transparent (CLEAR) cells are clear black."""
    clear = mapping == CLEAR
    q = palette[np.maximum(mapping, 0)].astype(np.uint8)
    h, w = mapping.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[:, :, :3] = q
    rgba[:, :, 3] = 255
    rgba[clear] = 0
    if bg_mask is not None:
        rgba[np.asarray(bg_mask, dtype=bool), 3] = 0
    return rgba


def palette_counts(palette, mapping):
    """Cells per palette entry; transparent cells count for none."""
    return np.bincount(mapping[mapping != CLEAR], minlength=len(palette))


def write_palette(path_json, path_txt, palette, mapping, transparent_idx=None):
    counts = palette_counts(palette, mapping)
    entries = [{"index": int(k), "hex": hexc(c), "cells": int(counts[k]),
                "transparent": k == transparent_idx}
               for k, c in enumerate(palette)]
    with open(path_json, "w") as f:
        json.dump({"grid": [int(mapping.shape[0]), int(mapping.shape[1])],
                   "palette": entries}, f, indent=2)
    with open(path_txt, "w") as f:
        for e in entries:
            name = " (transparent)" if e["transparent"] else ""
            f.write(f'{e["hex"]}{name}\n')


def parse_swap(s):
    try:
        old, new = s.split("=")
    except ValueError:
        raise SystemExit(f"bad --swap '{s}', expected '#oldhex=#newhex'")
    old = old.lstrip("#").upper()
    new = new.lstrip("#").upper()
    if len(old) != 6 or len(new) != 6:
        raise SystemExit(f"bad --swap '{s}': both colors must be 6 hex digits")
    return tuple(int(old[i:i + 2], 16) for i in (0, 2, 4)), \
           tuple(int(new[i:i + 2], 16) for i in (0, 2, 4))


def apply_swaps(palette, swaps):
    palette = palette.copy()
    applied = []
    for old, new in swaps:
        d = np.abs(palette - np.array(old)).sum(axis=1)
        hits = np.where(d == d.min())[0].tolist()
        if d.min() <= 6:
            for k in hits:
                palette[k] = new
            applied.append((hexc(old), hexc(new)))
        else:
            print(f"  ! swap target {hexc(old)} not in palette (closest "
                  f"{hexc(palette[np.argmin(d)])}, dist {int(d.min())}) — skipped")
    return palette, applied


# ---------------------------------------------------------------------- main

def process(path, outdir, color_tol, min_scale, max_scale, swaps=(),
            keep_bg=False, despeckle_size=1):
    rgba = load_rgba(path)
    H, W, _ = rgba.shape
    grid, cells, raw_rmse, palette, mapping = recover(rgba, color_tol, min_scale, max_scale)

    # Background detection: dominant color on the border ring, then flood
    # fill from the border so only the edge-connected region is dropped.
    transparent_idx = None
    bg_share = 0.0
    bg_mask = None
    if not keep_bg:
        idx, share = detect_background(mapping)
        if share >= 0.3:            # must wrap the artwork to be the canvas
            transparent_idx = idx
            bg_share = share
            bg_mask = background_mask(mapping, idx)

    removed_regions = removed_cells = 0
    if despeckle_size > 1:
        removed_regions, removed_cells = despeckle(
            mapping, despeckle_size, skip_idx=(transparent_idx,) if transparent_idx is not None else ())

    if swaps:
        palette, applied = apply_swaps(palette, swaps)

    rects = mesh(mapping, free_mask=bg_mask)
    regions = connected_regions(mapping)

    # Fidelity: how many cells ended up far from the raw dominant color.
    final = palette[np.maximum(mapping, 0)]
    far = ((np.abs(final - cells) > max(24, color_tol)).any(axis=2)
           & (mapping != CLEAR) & (True if bg_mask is None else ~bg_mask))
    lossy = float(far.mean())

    base = os.path.splitext(os.path.basename(path))[0]
    svg_path = os.path.join(outdir, base + ".svg")
    big_path = os.path.join(outdir, base + ".rect.png")
    png_path = os.path.join(outdir, base + ".png")
    pal_json = os.path.join(outdir, base + ".palette.json")
    pal_txt = os.path.join(outdir, base + ".palette.txt")
    write_svg(svg_path, palette, mapping, rects, grid.ncols, grid.nrows,
              transparent_idx=transparent_idx)
    Image.fromarray(colored_rgba(palette, mapping, bg_mask)).save(png_path)
    rgba_big = colored_rgba(palette, mapping, bg_mask)
    Image.fromarray(rgba_big).resize((W, H), Image.NEAREST).save(big_path)
    write_palette(pal_json, pal_txt, palette, mapping, transparent_idx)

    print(f"{os.path.basename(path)}:")
    print(f"  source       : {W}x{H}")
    if swaps:
        print("  swaps        : " + (", ".join(f"{o}->{n}" for o, n in applied) or "(no targets found in palette)"))
    if grid.exact:
        print(f"  logical grid : {grid.ncols}x{grid.nrows} "
              f"(clean pixel art, {int(grid.sx)}px blocks, read exactly)")
    else:
        print(f"  logical grid : {grid.ncols}x{grid.nrows} "
              f"(cell {grid.sx:.2f}x{grid.sy:.2f} px, phase {grid.x0:.2f},{grid.y0:.2f})")
        print(f"  alignment    : x median {np.median(grid.score_x):.2f}px / max "
              f"{grid.score_x.max():.2f}px | y median {np.median(grid.score_y):.2f}px / max "
              f"{grid.score_y.max():.2f}px (0 = edges exactly on grid)")
        print(f"  raw RMSE     : {raw_rmse:.2f} (anti-aliasing/noise removed per cell)")
    print(f"  palette      : {len(palette)} colors")
    if transparent_idx is not None:
        interior = int((mapping == transparent_idx).sum()) - int(bg_mask.sum())
        print(f"  background   : {hexc(palette[transparent_idx])} dropped "
              f"({bg_share:.0%} of border ring)")
        if interior:
            print(f"  bg interior  : {interior} interior cells of the same color "
                  f"kept opaque")
    if despeckle_size > 1:
        print(f"  despeckle    : {removed_regions} regions / {removed_cells} cells "
              f"(< {despeckle_size} cells) cleaned")
    print(f"  fidelity     : {lossy:.1%} of cells deviate noticeably from source "
          f"dominant color")
    if lossy > 0.15:
        print("                 ! high loss — consider lowering --color-tol or "
              "checking grid detection for this image")
    print(f"  shapes       : {len(rects)} rects / {regions} connected regions")
    print(f"  wrote        : {png_path} (native {grid.ncols}x{grid.nrows}), "
          f"{big_path} (preview {W}x{H}), {svg_path}, {pal_json}, {pal_txt}")
    return grid, palette, rects


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="*", help="images (default: *.png/*.jpg here)")
    ap.add_argument("-o", "--outdir", default="out")
    ap.add_argument("--color-tol", type=float, default=18.0,
                    help="RGB merge tolerance for palette (default 18)")
    ap.add_argument("--min-scale", type=float, default=4.0)
    ap.add_argument("--max-scale", type=float, default=64.0)
    ap.add_argument("--swap", action="append", default=[],
                    metavar="OLD=NEW", help="recolor palette hex OLD to NEW "
                    "(6-digit hex, repeatable)")
    ap.add_argument("--keep-bg", action="store_true",
                    help="keep detected background as opaque canvas "
                    "(default: drop it, transparent)")
    ap.add_argument("--despeckle", type=int, default=1,
                    help="remove connected regions smaller than N cells "
                    "(default 1 = off, try 2 or 3)")
    args = ap.parse_args()

    files = args.inputs
    if not files:
        files = sorted(glob.glob("*.png") + glob.glob("*.jpg") + glob.glob("*.jpeg"))
    files = [f for f in files
             if not os.path.dirname(os.path.abspath(f)).endswith("out")]
    if not files:
        sys.exit("no input images found")
    os.makedirs(args.outdir, exist_ok=True)
    swaps = [parse_swap(s) for s in args.swap]
    for f in files:
        try:
            process(f, args.outdir, args.color_tol, args.min_scale, args.max_scale,
                    swaps=swaps, keep_bg=args.keep_bg,
                    despeckle_size=args.despeckle)
        except ValueError as e:
            print(f"{os.path.basename(f)}: FAILED — {e}")


if __name__ == "__main__":
    main()
