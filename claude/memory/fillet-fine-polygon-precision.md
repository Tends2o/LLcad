---
name: fillet-fine-polygon-precision
description: llcad/OCCT fillet on extruded polygon profiles fails with PRECISION_UNSUPPORTED when arcs are segmented finer than ~22.5°
metadata: 
  node_type: memory
  type: project
  originSessionId: c73d2c9c-34ea-4e22-93aa-ebb7df52bf4b
  modified: 2026-09-18T15:43:09.266Z
---

In llcad (OCCT worker) a `fillet` (edge_selector horizontal) on an extruded `profile` fails with `PRECISION_UNSUPPORTED` ("Native boundary tolerances exceed the required model accuracy.") once the profile's arcs use kinks of 11.25° or finer (16 or 24 segments per semicircle). It fails regardless of the radius: R0.75, R1.1 and R1.3 all failed. The same ring with 8 segments per semicircle (22.5°) built and validated. Observed 2026-09-18 on a pull-ring part (`pull-ring`).

**Why:** A proposal that promised "24 segments per semicircle, fall back to R1.1 or 16 segments" could not be implemented as accepted, and the whole edit round had to stop.

**How to apply:** When proposing polygon profiles that get filleted, plan at most 8 segments per semicircle (22.5°). Name that as the resolution, or as the fallback, up front. Test the fillet on its own with a discarded candidate before promising finer facets.
