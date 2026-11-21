# LLcad pitfalls: error -> cause -> fix

From the error log of earlier product reconstructions (handheld devices, a charging dock A/B test) and
the worker code. The lint hook and `llcad.py lint` catch the starred (*) ones before a call is sent.

## Schema and patch errors (INVALID_SCHEMA, the most frequent code)

| message / symptom | cause | fix |
|---|---|---|
| "Input does not match the schema." + issues path | wrong field name/type, number instead of decimal string, missing required field | `llcad.py schema <operator>` / `schema set_structure`; generate with the DSL; details.issues[].path names the field |
| "Model outputs must be registered as part outputs." (8x) | a model output is not listed in any part's outputs | add it to the part (`b.part(..., outputs=[...])`); the DSL filters outputs per patch |
| "Part outputs must be unique own features." | part output is owned by another part (owner_part) | set `part=` on the feature to the part that lists it |
| "A feature needs its own part ..." | owner_part is not a declared part | declare the part in the same patch (DSL does this) |
| "Structure units need unique ids; world is reserved." * | a part/frame/assembly id equals a feature id (or 'world') | parts `pt-…`, frames `fr-…`, features `<prefix>-…` |
| "Parameter height is missing." | required parameter missing | see the `*` parameters in operators.md |
| "Wrong number of operator inputs." | depends_on count outside `refs` | union/difference 2..16, loft 2..16, extrude/revolve exactly 1, sweep 2 |
| features too_big / > 64 operations * | patch over limits | ≤64 ops per patch (DSL splits); model ≤4096 features |
| set_structure expected_hash format * | placeholder or typo hash | read `structure.hash` (cad_get_model) or let the DSL chain hashes |
| STALE_REVISION "The project structure has already been changed." | structure hash is not the current one | re-read hash; one set_structure per patch at most |
| STALE_REVISION on apply/commit | base_revision is not the head (someone committed) | re-read head (`llcad.py head`), rebuild the patch against it |
| IDEMPOTENCY_CONFLICT "The idempotency key was used with different arguments." * | same key, different arguments | new key per attempt (lint hook rewrites; DSL derives keys from content) |
| same failed result returned again | same key + same arguments replays the earlier (failed) response | new key after a failure (hooks and DSL do this automatically) |

## Geometry and kernel errors

| message / symptom | cause | fix |
|---|---|---|
| negative volume / inside-out solid * | clockwise profile extruded | points CCW seen from +Z (DSL `profile()` reverses and notes it) |
| "The profile is degenerate." / not coplanar * | <3 distinct points, collinear, or points off one plane | one z for extrude profiles; XZ half-plane for revolve |
| "Loft sections have different segment counts" * | sections with different point counts | resample every station to the same count (`rrect_pts(..., segs)` constant, `grip_loft_csv`) |
| "The cutting tool cuts no material." | hole/pocket/groove z not on the top face (they cut DOWN) | z = top face; depth downward |
| "The strip falls apart into N separate faces" | strip paths/pads not connected | one strip per connected glyph stroke, or `text` operator |
| "The shell opening is not unique." | shell needs exactly one planar top/bottom face | shell the simple body first, then add details |
| KERNEL_FAILURE "The worker ended prematurely." exit 152 | 40 s CPU / memory limit: huge glyph patches, 0.06 mm tessellation, 500+ feature evaluate | split patches (≤1 ring text per patch), `text` operator, deflection 0.12–0.18 mm, sub-model/fragment |
| PRECISION_UNSUPPORTED / "Native boundary tolerances exceed …" | kernel boundary tolerance above 50 % of the model tolerance (lofts, NURBS, fillets on compounds, near-tangent booleans) | break edges before booleans; offset cuts ≥0.5 mm or exactly on; for photo reconstructions the explicit preset `visual_reconstruction` (0.01 mm) via set_model_settings |
| GEOMETRY_INVALID on fillet/chamfer | edge set not blendable on the compound | fillet single bodies before union; narrower selector; or `between_features` |
| AMBIGUOUS_SELECTION "No match for edge_selector=…" | no edge matches all/vertical/horizontal | check the orientation of the body; use another selector |
| AMBIGUOUS_SELECTION "The face is not uniquely available in this revision" / "A selection anchor needs an explicit face id." | cad_inspect/measure without a face id | `cad_find` first, then inspect with face_id |
| BUILD_MISMATCH "The candidate was created with a different build." | service rebuilt between apply and validate | re-apply (new key); old revisions: `cad_rebuild` to the current registry hash |
| OUT_OF_SCOPE on import / artifact sharing | artifacts of another model cannot be bound silently | `instantiate_fragment` (new builds) or regenerate from the generator |
| boolean with many inputs fails | union of >16 inputs or heavy compound | stage as a tree (`b.union` does it) |

## Behaviour pitfalls (cost the most time in past runs)

- Reading LLcad source to learn operators (28 min in one run): use operators.md / `llcad.py schema`.
- Arithmetic in thinking (two 64k-token truncations, 30 min): compute in Python.
- Printing patch JSON back (`cat p1.json`, 32 KB) and re-typing it as a tool argument: use patch_ref / --http.
- Polling in a loop (438 poll turns, 16 % of the run): wait_ms / hooks, at most one explicit poll.
- 0.01 mm prints: z-fighting in the viewer. Use ≥0.05 mm relief.
- Variants as displaced copies next to the device ("3 parts side by side"): configurations only, on request.
- A model named "Complete model" that contained 19 accessory features: name = content; look at the 4 views.
- Keys 8 mm too high and covered, logo 8.8 mm off, wrong logo tint: ledger items with measured acceptance.
- A /goal free-text judge that extended a finished run by 1.9 h for invisible details: deterministic
  stop gate on the listed scope only; out-of-scope items go to the report.
- Export before the last change (stale STEP/STL next to new ones): export once, at the end.
