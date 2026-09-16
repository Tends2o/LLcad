# Licensing

LLcad is free software: you can redistribute it and/or modify it under the terms of the
**GNU General Public License as published by the Free Software Foundation, either version 3
of the License, or (at your option) any later version.** The full text is in [`LICENSE`](../LICENSE);
`package.json` declares `GPL-3.0-or-later`.

Running LLcad locally or as a service for your own use carries no obligation. Distributing
it, or a product that contains it, requires passing on the source code under the same terms.

## Why GPL

The mesh validation gate of the `watertight_solid` profile (`workers/mesh-cgal/check.cpp`)
uses CGAL's Polygon Mesh Processing package with exact predicates. Those CGAL packages are
available under the GPL-3.0-or-later (or a commercial CGAL licence). A project that ships
that checker as a mandatory component is therefore GPL-compatible only under the GPL itself.
All other dependencies are permissive or weak copyleft and impose no further restriction.

## Third-party components

| Component | Role | Licence |
|---|---|---|
| Open CASCADE Technology (through the `cadquery-ocp` Python bindings) | B-Rep geometry kernel | LGPL-2.1 with the Open CASCADE exception; bindings Apache-2.0 |
| CGAL (Polygon Mesh Processing, exact kernel) | Mesh validity checks | GPL-3.0-or-later or commercial |
| GMP, MPFR | Exact arithmetic for CGAL | LGPL-3.0-or-later (GMP also GPL-2.0-or-later) |
| Boost | CGAL dependency | Boost Software License 1.0 |
| OpenVDB (`python3-openvdb`) | Volumetric import and export | MPL-2.0 |
| NumPy, SciPy, VTK, Matplotlib and helpers | Numerics, solver, previews | BSD-3-Clause, MIT and similar |
| `@modelcontextprotocol/sdk`, Express, Zod, jose, decimal.js, Three.js | MCP transport, HTTP, schemas, tokens, decimals, viewer | MIT |
| TypeScript, esbuild, tsx, Prettier, fast-check | Build and tests only | Apache-2.0 and MIT |
| Playwright | Browser tests and screenshots only | Apache-2.0 |
| Chromium (installed by Playwright) | Browser tests only | BSD-3-Clause and others |

`npm run licenses` regenerates the machine-readable inventory (`reports/license-inventory.json`)
and the distribution review (`reports/license-check.json`). The review flags the CGAL
components as strong copyleft on purpose; an operator acknowledges it after a human legal
review by setting `MATHFORGE_DISTRIBUTION_REVIEW=acknowledged`. Local operation never needs
that acknowledgement.

## Contributions

By contributing you agree that your contribution is licensed under the same terms
(GPL-3.0-or-later). Keep third-party code in its own files with its original licence header.
