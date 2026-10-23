# Gridsprite

<p align="center">
  <img src="sample_5.png" alt="Gridsprite logo" width="256"/>
</p>

<p align="center">
  <strong>Recovers the true pixel grid from upscaled, anti-aliased or smeared sprites — and hands back a crisp, recolorable SVG</strong>
</p>

<p align="center">
  <em>"Every pixel ends up exactly where it belongs."</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.9%2B-blue.svg?logo=python&logoColor=white" alt="Python 3.9+"/>
  <img src="https://img.shields.io/badge/Static%20Web-No%20Backend%2C%20No%20Uploads-brightgreen.svg" alt="Static"/>
  <img src="https://img.shields.io/badge/JS%20Port%20Fidelity-152%2F152%20Checks-orange.svg" alt="Fidelity"/>
  <img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License MIT"/>
</p>

---

## Table of Contents

- [Overview](#overview)
- [The Pipeline](#the-pipeline)
  - [1. Grid Detection](#1-grid-detection)
  - [2. Rectification](#2-rectification)
  - [3. Palette Quantization](#3-palette-quantization)
  - [4. Edge-Connected Background Removal](#4-edge-connected-background-removal)
  - [5. Rectangle Meshing → SVG](#5-rectangle-meshing--svg)
- [The Editor (GUI)](#the-editor-gui)
  - [Global / Region / Brush](#global--region--brush)
  - [Shimmer Previews](#shimmer-previews)
  - [Palettes & Presets](#palettes--presets)
  - [Themes & Responsiveness](#themes--responsiveness)
- [Components](#components)
- [The Static Web Version (GitHub Pages)](#the-static-web-version-github-pages)
- [Architecture & Tech Stack](#architecture--tech-stack)
- [Getting Started & Development](#getting-started--development)
  - [Running the CLI](#running-the-cli)
  - [Running the Local GUI](#running-the-local-gui)
  - [Running the HTTP API](#running-the-http-api)
  - [Verifying the JS Port](#verifying-the-js-port)
- [License](#license)

---

## Overview

**Gridsprite** is published by Iterative Key LLC. It takes images that *should* be pixel art — upscaled, JPEG-smeared, resized-broken screenshots — and recovers the original sprite hiding inside them: the true pixel grid, the true palette, one dominant color per logical cell. The result is exported as a hand-editable SVG where every color is one `<g>` group, so recoloring a sprite is changing a `fill` value.

Born from scraping sprites out of game captures and retro website assets and being tired of tracing them by hand:

- **Sub-Grid Ambiguity, Resolved:** the detector scans per axis for the *largest* cell size whose edge peaks all sit on one phase — so an 8px sub-phase of a true 16px unit never fools it.
- **Built for Bad Sources:** dominant-per-cell sampling ignores anti-aliasing halos; a reported RMSE ("smearometer") tells you how noisy the input was.
- **Smart Transparency:** solid backgrounds are removed with an *edge-connected* flood fill — white eyeballs on a white background survive, canvas around them does not.
- **Editable Output:** maximal-rectangle meshing keeps SVG node counts low and grouping keeps edits one attribute away.

Three ways to use it: a **Python CLI**, a **local web editor** (paint, recolor, export — light/dark themes, works on phones), a **stateless HTTP API** for scripts and agents — plus a **static browser-only version** of the editor you can host on GitHub Pages.

---

## The Pipeline

### 1. Grid Detection

Per axis, the smoothed luminance edge profile is searched for strong edge peaks; the largest candidate cell size in `[min_scale, max_scale]` (default 4–64 px) whose *every* strong edge lands on a single phase (within 0.25 × step) wins. Rectification reports per-axis alignment offsets and a median deviation so you can see exactly how much the source disagrees.

### 2. Rectification

Each logical cell contributes its dominant color, sampled from the central (1 − 2×0.15) portion of the cell so alignment drift does not spill across boundaries. The reconstruction RMSE per cell is kept as a quality diagnostic.

### 3. Palette Quantization

Greedy merging in RGB space within a configurable tolerance (default 18), one weighted-centroid refinement pass, then dominant-color reassignment. Output: a compact palette and a cell → palette index map.

### 4. Edge-Connected Background Removal

The dominant color along the border ring (≥ 30% share) becomes transparent — but only where it is 4-connected to the canvas border. Interior islands of the same color (think white eyes on a sprite over a white background) always stay opaque. Optional despeckle removes connected regions smaller than N cells by flooding them with the majority color of their neighbors.

### 5. Rectangle Meshing → SVG

A greedy maximal-rectangle decomposition (background cells excluded) meshes the grid into as few `<rect>` elements as possible. The SVG is organized one `<g id="pcN" fill="#hex">` per palette color — recoloring is editing the `fill`, and each hex is unique file-wide so find-and-replace works.

Alongside the SVG: a native-resolution RGBA PNG, a nearest-neighbor preview PNG at source size, and palette JSON/TXT.

---

## The Editor (GUI)

A local web GUI (`gridsprite_gui.py`) for hands-on sprite surgery. Drag images anywhere onto the window; they are grid-snapped and quantized automatically, then sit in a file list awaiting edits.

### Global / Region / Brush

| Mode | Pick | What it does |
|---|---|---|
| **Global** | a palette color | shimmer-previews every cell that could change; click a second color to recolor the whole class |
| **Region** | a cluster of same-colored cells | 4- or 8-connected flood selection; recolor the whole region with one click |
| **Brush** | palette chip, preset or picker | paints individual cells, drag to paint continuously |

Additional tools: `⌘Z` undo (100 levels), background-transparency toggle, despeckle strength, and an **Apply to affected** button that routes the custom picker to whichever mode is active.

### Shimmer Previews

Nothing changes until you confirm: cells affected by a pending edit pulse with an animated shimmer overlay — a global recolor shows every cell it would touch, a region shows its extent, the brush shows the cell under the pointer.

### Palettes & Presets

Every palette chip carries cell counts; four preset swatch sets (pastel, vivid, classic, nature) inject colors into the active mode's flow.

### Themes & Responsiveness

- **Light & dark themes** with a header toggle, persisted per browser, defaulting to the OS preference — the canvas checkerboard and shimmer overlay adapt to both.
- **Responsive:** three-pane on desktop (files / canvas / tools), full-width tool grid on tablets, single column with a horizontal file strip on phones. Pointer events give touch phones real brush painting.
- **Typography:** embedded Inter variable font (SIL OFL 1.1), tabular numerals for stats — renders identically offline.

---

## Components

| File | What it is |
|---|---|
| `gridsprite.py` | Core library + CLI: `python3 gridsprite.py image.png -o out [--color-tol T] [--keep-bg] [--despeckle N] [--swap #old=#new]` |
| `gridsprite_gui.py` | Local web editor: `python3 gridsprite_gui.py --port 8799` |
| `gridsprite_api.py` | Stateless JSON HTTP API for scripts and AI agents: `python3 gridsprite_api.py --port 8643` — see [`API.md`](API.md) |
| `docs/` | The same editor as a **fully static site** — pipeline ported 1:1 to JavaScript, deployable to GitHub Pages |
| `.claude/skills/gridsprite/SKILL.md` | Agent skill describing the HTTP API |
| `tools/validate_static.py` + `.mjs` | Cross-implementation equivalence proof for the JS port (see [below](#verifying-the-js-port)) |
| `sample_*.png` | Test fixtures (upscaled pixel-art sprites) |

---

## The Static Web Version (GitHub Pages)

`docs/` is a dependency-free static web app — the same GUI as the local server, with the entire pipeline (grid detection, quantization, background flood fill, despeckle, rectangle meshing, SVG/PNG generation) executed **in the browser**:

- **Nothing leaves your device:** no backend, no uploads, works offline via `file://`.
- **Identical output:** the JS port is validated byte-for-byte against the numpy pipeline (SVG text, palettes, cell maps, PNG data) — see the fidelity numbers below.
- **Exports download directly:** SVG / native PNG / upscaled preview / palette JSON+TXT, individually or zipped.

The published site lives at **[https://iterativekey.github.io/GridSprite/](https://iterativekey.github.io/GridSprite/)**.

To publish (one-time setup):

> **Settings → Pages → Build and deployment → Deploy from a branch →** branch `main`, folder `/docs`

---

## Architecture & Tech Stack

```
gridsprite/
├── gridsprite.py            # Core pipeline + CLI (numpy: grid detect, rectify, quantize, mesh)
├── gridsprite_gui.py        # Local web editor — HTTP server + embedded UI (themes, presets, brush)
├── gridsprite_api.py        # Stateless JSON HTTP API for scripts and AI agents
├── docs/
│   ├── index.html           # Static web editor (UI shell, embedded Inter font)
│   ├── pipeline.js          # 1:1 JS port of the numpy pipeline (validated)
│   ├── app.js               # Static editor logic — in-browser analyze/export, Blob downloads
│   └── zip.js               # Minimal store-only ZIP writer (no dependencies)
├── tools/
│   ├── validate_static.py   # Dumps numpy-pipeline reference outputs for all fixtures
│   └── validate_static.mjs  # Compares the JS port — byte-equal SVG/PNG/palette/mapping
├── assets/hero.png          # Input fixture next to its recovered output
├── sample_*.png             # Test fixtures (upscaled pixel-art sprites)
└── API.md                   # HTTP API reference
```

---

## Getting Started & Development

### Running the CLI

Requires **Python 3.9+** with `numpy` and `pillow`:

```bash
python3 -m venv .venv && .venv/bin/pip install numpy pillow

# Process one image
.venv/bin/python gridsprite.py sample_1.png -o out

# Process everything in the current directory, keeping the background opaque,
# cleaning specks smaller than 3 cells and swapping a palette color
.venv/bin/python gridsprite.py -o out --keep-bg --despeckle 3 --swap '#7e8c54=#aab88a'
```

### Running the Local GUI

```bash
.venv/bin/python gridsprite_gui.py --port 8799
```

Opens `http://127.0.0.1:8799` — drop images anywhere in the window. Bindings default to `127.0.0.1`; keep it that way.

### Running the HTTP API

```bash
.venv/bin/python gridsprite_api.py --port 8643
curl -sS -X POST --data-binary @sample_1.png \
  "http://127.0.0.1:8643/api/v1/process?render_width=512"
```

The API is stateless, speaks JSON, and serves rendered artifacts for download — see [`API.md`](API.md) for the full surface (analyze/process, palette swaps, size-exact rendering).

### Verifying the JS Port

The static version's pipeline is proven equivalent to the numpy implementation on every fixture — 152 checks covering grid parameters, palette order, cell mapping, rect counts, region counts, SVG text, palette files and PNG bytes:

```bash
.venv/bin/python tools/validate_static.py && node tools/validate_static.mjs
# → 152/152 checks passed — equivalence verified
```

One deliberate mechanism makes cross-library equivalence possible: the grid-fit tolerance subtracts a 1e-6 px margin (`FIT_MARGIN`), so knife-edge fits that land exactly on the boundary are rejected deterministically instead of flipping on floating-point ulp differences between math libraries. It has never changed the chosen grid on real inputs — the validation run is the receipt.

---

<p align="center">
  <sub>Built for pixel artists, game modders and archival scrapers of retro web assets.</sub>
</p>

<p align="center">
  <sub>© 2026 Iterative Key LLC. All rights reserved.</sub>
</p>
