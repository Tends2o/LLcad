#!/usr/bin/python3
"""Optional host PreToolUse hook (Bauplan 13). Read-only, no network, no secrets.

The host passes the pending tool call as JSON on stdin. This script only annotates:
it prints a short reminder of the revision binding rules and never changes the
arguments, blocks legitimate calls or reads any credential. If the host does not
support hooks, nothing here is required; the server enforces every rule itself.
"""
import json
import sys

ALLOWED_KEYS = {"tool_name", "tool_input", "session_id", "hook_event_name", "cwd", "transcript_path"}


def main() -> int:
    raw = sys.stdin.read(65536)
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict) or not set(payload).issubset(ALLOWED_KEYS):
        return 0
    name = str(payload.get("tool_name", ""))
    if not name.startswith("mcp__") or "cad_" not in name:
        return 0
    arguments = payload.get("tool_input")
    if not isinstance(arguments, dict):
        return 0
    notes = []
    if "base_revision" in arguments:
        notes.append("base_revision " + str(arguments.get("base_revision"))[:24] + " is the exact revision this write binds to.")
    if name.endswith("cad_commit"):
        notes.append("Commit only with the server-generated validation_digest of this candidate.")
    if name.endswith("cad_apply_patch") and "selection_handle" not in arguments:
        notes.append("Patch without selection handle: the server resolves feature ids by exact revision.")
    if notes:
        # Annotation only; hooks cannot approve, deny or rewrite server-side gates.
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": " ".join(notes)}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
