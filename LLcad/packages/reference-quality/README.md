# Controlled reference check and CAD correction

This executable pilot checks rendered views of a small synthetic CAD model and
corrects named errors through real LLcad candidates. It uses Open CASCADE,
`cad_plan_edit`, `cad_apply_patch`, `cad_validate`, `cad_render` and
`cad_commit` with the server-side check digest. The tools are called in-process
through `ModelService`. No HTTP MCP client is involved yet.

## Start

In the LLcad project directory:

```bash
node --import tsx scripts/reference-quality-pilot.ts
```

Optionally pass a new, not yet existing output folder as an argument.
The run creates its own database, reference and check contract, before/after
images, difference images, parametric IR files, check digests, `report.json` and
a browser-readable `report.html` with a before/after comparison.
Existing databases and user models are not opened. An existing output folder is
not overwritten. No service restart is required.

The prerequisites are the existing LLcad Node dependencies and the existing
OCCT Python environment. The raster checker uses the system Python with NumPy and Pillow.
The normal CAD worker must be startable (on Windows a separate process in a job object). In nested sandboxes
this may require an execution permission; the pilot does not switch off
protection rules. No LLM APIs are called and no data is sent to the outside.

## What is checked

The public target construction is a housing with a knob wheel and a raised
X label. The reference is created once with real CAD geometry. Camera,
resolution, colours and measurement regions are fixed before the tests. The image check
receives only PNG files and this contract; the error manipulations are kept separately
in the experiment program. The corrector knows the manually defined target construction.

| Criterion | Measurement and threshold |
| --- | --- |
| Housing width | Relative deviation of the pixel width in a fixed measuring strip, at most 2 % |
| Knob wheel | Difference of the foreground masks in the wheel region, at most 2 % of the reference mask |
| Label | The dark pixel mask of the X template must match exactly; no OCR |
| Overall view | Changed pixels relative to the reference foreground area, at most 2 % |
| Unexpected geometry | No foreground pixels outside the reference silhouette dilated by one raster pixel |

All criteria must pass. The 2 % values are individual image metrics, not
2 % surface accuracy or 3D accuracy. The additional test is necessary: a small foreign
component could otherwise disappear below the overall tolerance. Geometry that is not visible
and hidden details are not covered by a front view.

The four core cases are unchanged, wrong width, missing wheel and wrong
label. Combined errors and an unexpected extra part are tested in addition. An independent evaluator compares the actual native
geometry hashes of all output parts with the reference and checks unchanged
starting revisions. It does not simply adopt the verdict of the image checker.

## Flow and limits

`loop.ts` runs check → targeted correction → renewed check → adoption.
At most three correction attempts are allowed. Repeated identical findings,
missing images, failed geometry checks or unsupported
deviations end with `not_verified`. Before every adoption the checked
candidate revision is compared with the adopted revision. On abort,
open CAD candidates are discarded; the starting revision is kept.

The whole experiment gets 15 minutes from the start of the experiment program, including
reference creation, data import, waiting times, checking and correction. The time budget
works cooperatively: CAD jobs and image processes are aborted, and between steps
the budget is checked again. A synchronous commit operation that is already running cannot be
rolled back atomically at the time limit. Custom adapters must respect AbortSignals;
the interface offers no hard real-time guarantee for arbitrary third-party code.
Even after a timeout no successful check status is reported.

This is a controlled development experiment with fixed repair rules.
It is **not yet** a system that recognises all details from arbitrary photos on its own
and constructs CAD. Unknown labels, camera registration,
hidden internal components, general repair planning and PowerPoint animation output
are further integration steps. A real product reconstruction and the complete
15-minute goal were not demonstrated with this test.

## Repeatable checks

```bash
node --import tsx tests/reference-quality/loop.test.ts
node --import tsx tests/reference-quality/shutdown.test.ts
node --import tsx tests/reference-quality/frozen-inputs.test.ts
python3 -m unittest discover -s tests/reference-quality -p 'test_*.py' -v
npm run typecheck
node --import tsx scripts/reference-quality-pilot.ts
```

The test files are started directly: in this environment the combination
`node --import tsx --test` reports only one file-level test instead of the individual test cases.
Running them directly gives their actual names and results.

Measurements per tool contain run times including job waiting time as well as the
available native metrics. `tokens` and `cost` stay `null`; no
LLM calls take place. A successful run reports `experiment_passed`, a correct
single check run only `verified_within_contract`. The expected rejection of the
extra part is counted as a passed negative test.
