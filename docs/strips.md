# Strips (`strip`): thick polylines as solids

A `strip` builds one solid of constant width and height from one or more polylines, the way
a copper trace lies on a circuit board, a rib on a housing or a channel in a plate. Before the
operator existed, such a net had to be unioned outside the server and passed in as
`profile` + `extrude`; the operator makes that preprocessing unnecessary.

## Construction

```json
{
  "kind": "strip",
  "parameters": { "width": { "value": "0.3", "unit": "mm" }, "height": { "value": "0.035", "unit": "mm" } },
  "construction": {
    "operator": "strip",
    "paths": [[["14.75", "24", "1.565"], ["14.75", "26.65", "1.565"], ["44", "26.65", "1.565"]]],
    "pads": [{ "center": ["14.75", "24", "1.565"], "width": "0.8", "depth": "0.9" }]
  }
}
```

- `paths`: 1 to 64 polylines with 2 to 256 points each (decimal strings, mm). All points of all
  paths and all pad centres share one z coordinate; it is the bottom of the solid.
- `width`: strip width. Every segment becomes a rectangle and every path vertex a disc of
  radius `width/2`, which gives round corners and ends like a trace.
- `pads` (optional, at most 256): axis-aligned rectangles given by centre, width (x) and
  depth (y) that join the union (solder pads, connection areas, supports).
- `height`: extrusion along +z. The measured `height` is part of the profile checks.

## Semantics and checks

The worker fuses all rectangles and discs in their plane (OCCT `BRepAlgoAPI_Fuse`), merges
the resulting faces (`ShapeUpgrade_UnifySameDomain`) and extrudes the single remaining face.
If the union falls apart into several faces because paths and pads do not touch, the
candidate is rejected with `GEOMETRY_INVALID`: one net is one solid. Closed loops are allowed
and produce a face with a hole. Segments without length, pads without area and points outside
the common plane are already rejected by the compiler.

Face roles for `cad_inspect` and selection handles: `bottom`, `top` and `wall` (planar sides
and the cylindrical corner faces). A strip has no inputs (`refs` 0); overlapping strips of
different nets stay separate solids of a compound, and LLcad does not check electrical
clearances.

## Limits

No self-offsetting of loops: a path that crosses itself is allowed but yields the same union
as two separate paths. No variable width per segment; use several strips instead. Coordinates
are checked with the usual 1e-9 mm tolerance.

## Tests

`tests/geometry/test_native.py::test_strip_union_with_round_joints_and_pads` (volume, height,
loop with hole, disconnected paths), `tests/regression/corpus.ts` (classes
`strip_union_connected` and `strip_union_disconnected`) and `tests/unit/compiler.test.ts`
(coplanarity, zero-length segments, empty pads).
