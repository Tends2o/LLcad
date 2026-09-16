#!/usr/bin/python3
"""Optional host PostToolUse hook (Bauplan 13). Summarises public tool results only.

Reads the completed tool call from stdin and prints a one-line summary of the
revision, job or validation status for the conversation. It never contacts the
network, never prints tokens and cannot undo an already executed action.
"""
import json
import sys

FIELDS = ("status", "revision", "candidate_revision", "job_id", "transaction_id", "committed")


def main() -> int:
    raw = sys.stdin.read(262144)
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    name = str(payload.get("tool_name", ""))
    if not name.startswith("mcp__") or "cad_" not in name:
        return 0
    response = payload.get("tool_response")
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except json.JSONDecodeError:
            return 0
    if not isinstance(response, dict):
        return 0
    structured = response.get("structuredContent", response)
    if not isinstance(structured, dict):
        return 0
    summary = {k: structured[k] for k in FIELDS if k in structured and isinstance(structured[k], (str, bool, int))}
    errors = structured.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0] if isinstance(errors[0], dict) else {}
        summary["error_code"] = str(first.get("code", ""))[:64]
    if summary:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "LLcad " + name.split("__")[-1] + ": " + json.dumps(summary, sort_keys=True)}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
