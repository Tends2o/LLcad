#!/usr/bin/env bash
set -euo pipefail
llcad_project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$llcad_project_root"
exec node dist/packages/mcp-gateway/stdio.js
