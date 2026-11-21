# Reference Pack

`ingest.py` turns the sources of one product (URLs and/or local files: images, mp4, PDF, SVG) into a
measured, hash-frozen Reference Pack before modelling starts. `ingest.py load` binds a pack to a model
with `cad_reference_setup`. Run it with the LLcad venv (numpy, scipy, Pillow); it also needs `ffmpeg`,
`ffprobe`, `pdftotext`, `pdfimages`, `pdftocairo` and `curl`.

```powershell
$PY = ".venv\Scripts\python.exe"   # from the LLcad folder
& $PY -X utf8 tools/reference-pack/ingest.py ingest --out packs/example-device --height-mm 160 `
    --dims length=160,width=40,depth=50 --hull `
    https://example.com/products/example-device/promo.mp4 datasheet.pdf flyer.pdf
& $PY -X utf8 tools/reference-pack/ingest.py load --pack packs/example-device/pack.json --model model-... `
    --cameras --register            # optional: absolute cameras from the turntable frames
```

## What it does

Only the URLs and files you pass are used. A page URL is fetched once; the images (largest `srcset`
entry), videos, PDFs and SVGs it links directly are downloaded, nothing beyond that page. Every source is
recorded with URL, SHA-256, size and a licence note (rights stay with the owner).

| Source | Output |
| --- | --- |
| PDF | `pdftotext` + regex: product dimensions (a triple is read length x width x depth, packing lines skipped) and weight, tolerance class A. `pdfimages`: renders with a soft mask are recombined into RGBA cut-outs (deduplicated) and measured. `pdftocairo -svg`: vector pages; filled paths clustered by colour, each saturated cluster saved as its own SVG (logo candidates; the brand print colour is not the device colour). |
| SVG | Same fill clustering. |
| mp4 | 480p scan of every frame (background class, product = largest dark component), white-background turntable segments; per segment the canonical front (narrowest, most mirror-symmetric) and side (widest) frames, refined at full resolution; subpixel silhouettes scaled to the height; width-per-height CSVs with cross-check residuals against the stated width/depth; absolute orthographic camera hints; base/top landmarks. Every shot is scaled on its own (the zoom differs between shots). Optional `--hull`: visual hull of the segment that turns the most (constant-speed assumption anchored on front = 0 deg, side = 90 deg, 90 % vote), outer bound only. |
| Images | Mask (alpha or border background), region colours (k-means in CIELAB, lit-pixel median, hex, CIEDE2000 p90 spread, area share); cut-outs are scaled only with `--scale-cutouts`. |

Optional `--sam3` calls hosted SAM 3 (fal) with concept prompts when `LLCAD_FAL_KEY`/`FAL_KEY` is set;
it is off by default because images then leave the machine (not for confidential products). The adapter
parses the response defensively and has not been exercised against the live endpoint here.

## pack.json (`llcad-reference-pack-1`)

`sources`, `dimensions` (≤32, class A stated/measured, B derived, C estimated), `weights`, `references`
(source_type `turntable_frame` / `datasheet_alpha_render` / `photo` / `closeup`, mask, camera hint,
px/mm), `contours` (CSV path, px/mm, axis stability, cross-check, optional `--compare-csv` result),
`colours`, `markings`, `landmarks` (base/top from turntable frames; signed asymmetric placeholders to be
filled), `videos` (scan statistics and segments), `hull`, `segmentation`, `scope` and `hash` (SHA-256 of
the canonical pack without timestamps). Freeze the pack before modelling; do not edit it during a run.

## load

Uploads up to 12 references, maps dimension roles to axes (`--axis length=z,width=x,depth=y`, tolerance
max(1 mm, 2 %) by default, as datasheets round) and calls `cad_reference_setup` with acceptance
`gates_judge`. Camera hints assume the pack's frame convention (base centre at the origin, product axis
+Z, front facing -Y); they are attached with `--cameras`, and front cameras are marked registered only
with `--register`. Side views stay unregistered unless `--register-side` is given, because their
handedness (from +X or -X) is not known from the video.

## Measured on this box (smoke)

- 4K turntable promo video (47 s, 1175 frames): scan 8 s, whole pack 50 s with hull. Front widths vs a
  hand-made width-per-height CSV: mean |d| 0.036 mm, p95 0.084 mm over 352 heights.
- Datasheet with a rear alpha render: width measured from the render within 1.1 % of the stated width.
  Flyer: vector logo extracted as 5 paths.
