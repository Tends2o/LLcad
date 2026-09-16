#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
command -v node >/dev/null
command -v bwrap >/dev/null
test -x /usr/bin/flock
test "$(dpkg-query -W -f='${Version}' python3-openvdb)" = '10.0.1-2.3+b1'
node -e 'if(Number(process.versions.node.split(".")[0])<24)process.exit(1)'
python3 -m venv .venv
.venv/bin/pip install --only-binary=:all: -r requirements.lock
npm ci
npm run schemas
npm run build
printf "LLcad is built. Start the HTTP server with: npm start\n"
