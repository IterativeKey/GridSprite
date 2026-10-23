// tools/validate_static.mjs — run the JS port on the same inputs the Python
// reference (tools/validate_static.py) dumped, compare every derived value:
// grid params, palette, cell mapping, rect mesh, region count, SVG text,
// palette files, native RGBA and preview RGBA.
//
// usage: node tools/validate_static.mjs [outdir]
import { createRequire } from "module";
import { readFileSync, readdirSync } from "fs";
import { basename, join, dirname } from "path";
import { fileURLToPath } from "url";
import { tmpdir } from "os";

const require_ = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
const GS = require_(join(here, "..", "docs", "pipeline.js"));
const outdir = process.argv[2] || join(tmpdir(), "gridsprite-validate");

let failures = 0, checks = 0;

function check(name, cond, detail){
  checks++;
  if (!cond){
    failures++;
    console.log(`    FAIL ${name}${detail ? " — " + detail : ""}`);
  }
}
const eqJson = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const near = (a, b, eps) => Math.abs(a - b) <= eps;
const bytesFromB64 = s => Buffer.from(s, "base64");

const expFiles = readdirSync(outdir)
  .filter(f => f.endsWith(".expected.json")).sort();

for (const f of expFiles){
  const base = basename(f, ".expected.json");
  const exp = JSON.parse(readFileSync(join(outdir, f), "utf8"));
  const raw = new Uint8Array(readFileSync(join(outdir, base + ".raw")));
  const img = { width: exp.W, height: exp.H, data: raw };

  console.log(`${base}:`);
  let res;
  try {
    res = GS.analyzeImage(img, { name: exp.name, colorTol: 18.0,
                                 minScale: 4.0, maxScale: 64.0 });
  } catch (e){
    checks++;
    failures++;
    console.log(`    FAIL analyze threw: ${e.message}`);
    continue;
  }

  check("sx", near(res.cellW, exp.sx, 5e-3), `${res.cellW} vs ${exp.sx}`);
  check("sy", near(res.cellH, exp.sy, 5e-3), `${res.cellH} vs ${exp.sy}`);
  check("ncols", res.ncols === exp.ncols, `${res.ncols} vs ${exp.ncols}`);
  check("nrows", res.nrows === exp.nrows, `${res.nrows} vs ${exp.nrows}`);
  check("rmse", near(res.rmse, exp.rmse, 1e-2), `${res.rmse} vs ${exp.rmse}`);
  check("alignX", near(res.alignment[0], exp.medianX, 1e-2),
    `${res.alignment[0]} vs ${exp.medianX}`);
  check("alignY", near(res.alignment[1], exp.medianY, 1e-2),
    `${res.alignment[1]} vs ${exp.medianY}`);
  check("bgShare", near(res.bgShare, exp.bgShare, 5e-3),
    `${res.bgShare} vs ${exp.bgShare}`);
  check("transparent", res.transparent === exp.transparent,
    `${res.transparent} vs ${exp.transparent}`);
  {
    const g = GS.detectGrid(img, 4.0, 64.0);
    check("x0", near(g.x0, exp.x0, 5e-3), `${g.x0} vs ${exp.x0}`);
    check("y0", near(g.y0, exp.y0, 5e-3), `${g.y0} vs ${exp.y0}`);
  }
  check("palette", eqJson(res.palette, exp.palette),
    `${res.palette.length} vs ${exp.palette.length} colors` +
    (res.palette.length === exp.palette.length
      ? " — first diff at " + exp.palette.findIndex((h, i) => res.palette[i] !== h)
      : ""));
  {
    const got = res.mapping.flat();
    check("mapping", eqJson(got, exp.mapping), (() => {
      const i = got.findIndex((v, j) => v !== exp.mapping[j]);
      return i < 0 ? "" : `first diff at cell ${i}: ${got[i]} vs ${exp.mapping[i]}`;
    })());
  }

  // ---- export equivalence
  const state = {
    name: exp.name,
    palette: res.palette,
    mapping: res.mapping,
    transparentOn: exp.transparent !== null,
    transparent: exp.transparent,
    sourceW: exp.W, sourceH: exp.H,
  };
  const art = GS.exportArtifacts(state, 0);
  check("regions", art.stats.regions === exp.regions,
    `${art.stats.regions} vs ${exp.regions}`);
  check("svg", art.files[0].text === exp.svg, "svg text differs");
  check("palJson", art.files[3].text === exp.palJson, "palette.json text differs");
  check("palTxt", art.files[4].text === exp.palTxt, "palette.txt text differs");
  check("png", Buffer.compare(Buffer.from(art.files[1].bytes),
                              bytesFromB64(exp.rgba)) === 0, "native rgba differs");
  check("preview", Buffer.compare(Buffer.from(art.files[2].bytes),
                                  bytesFromB64(exp.preview)) === 0,
        "preview rgba differs");
}

console.log(`\n${checks - failures}/${checks} checks passed` +
            (failures ? `, ${failures} FAILED` : " — equivalence verified"));
process.exit(failures ? 1 : 0);
