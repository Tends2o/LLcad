#!/usr/bin/python3
"""Codex header helper: credentials go only to its private stdout pipe."""
import json
import os
from pathlib import Path
import re
import stat
import sys

path = Path(os.environ.get("MATHFORGE_DATA", Path(__file__).resolve().parents[1] / "data")) / "local-token"
try:
    with path.open() as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Token file must be private")
        token = stream.read(513).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,512}", token):
        raise ValueError("Invalid token format")
except (OSError, ValueError):
    sys.exit("LLcad credential unavailable; check the local service and private token file.")
print(json.dumps({"Authorization": "Bearer " + token}))
