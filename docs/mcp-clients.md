# Connecting MCP clients

LLcad is a standard MCP server. Any client that can start a local process (stdio) or call a
Streamable HTTP endpoint can use it. Replace `/absolute/path/to/LLcad` with your checkout.

## Which transport?

| | stdio | HTTP |
|---|---|---|
| Start | client starts `deployment/start-mcp.sh` | you run `npm start` (or a systemd unit) |
| Authentication | none; the OS process is the identity | `Authorization: Bearer` with `data/local-token`, or OAuth |
| Viewer | started on demand by `cad_viewer_open` | always served on the same origin |
| Several clients | one process and one data directory per client | shared server and data |

## Generic `mcpServers` format

Many desktop and IDE clients read this JSON shape (file name and location depend on the client):

```json
{
  "mcpServers": {
    "llcad": {
      "command": "/absolute/path/to/LLcad/deployment/start-mcp.sh",
      "env": { "MATHFORGE_DATA": "/absolute/path/to/LLcad/data" }
    }
  }
}
```

For a running HTTP server the same clients usually accept a URL plus headers:

```json
{
  "mcpServers": {
    "llcad": {
      "url": "http://127.0.0.1:4310/mcp",
      "headers": { "Authorization": "Bearer <contents of data/local-token>" }
    }
  }
}
```

## OpenCode

`opencode.json` (project root) or the global config; local servers use a command array:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "llcad": {
      "type": "local",
      "command": ["/absolute/path/to/LLcad/deployment/start-mcp.sh"],
      "environment": { "MATHFORGE_DATA": "/absolute/path/to/LLcad/data" },
      "enabled": true,
      "timeout": 30000
    }
  }
}
```

Remote variant against a running server:

```json
{
  "mcp": {
    "llcad": {
      "type": "remote",
      "url": "http://127.0.0.1:4310/mcp",
      "headers": { "Authorization": "Bearer <contents of data/local-token>" }
    }
  }
}
```

The first `tools/list` starts the Python worker pool; raise `timeout` if your machine is slow.

## Hermes Agent

`~/.hermes/config.yaml`:

```yaml
mcp_servers:
  llcad:
    command: "/absolute/path/to/LLcad/deployment/start-mcp.sh"
    args: []
    env:
      MATHFORGE_DATA: "/absolute/path/to/LLcad/data"
    enabled: true
    timeout: 120
```

Run `/reload-mcp` in a chat to pick up the change. Tools appear as `mcp__llcad__cad_…`.

## Other clients

- **Command line / scripts:** the same tools are reachable without an MCP framing as
  `POST /api/tools/<name>` with a JSON body and the bearer key.
- **Protocol versions:** the official SDK adapter negotiates `2025-03-26`, `2025-06-18` and
  `2025-11-25`; a separate adapter serves `2026-07-28` (see [Tools and protocol](api.md)).
- **Instructions:** the server announces short usage instructions in `initialize`; keep them
  visible to the model. They tell it to discover models with `cad_list_models`, to plan,
  validate and commit every change and never to ask for clicks in the viewer.

## Checklist after connecting

1. `cad_capabilities` answers with operators, formats and limits.
2. `cad_list_models` lists your models (empty on a fresh data directory).
3. A first `cad_create_model` followed by `cad_apply_patch`, `cad_validate` and `cad_commit`
   produces a `checks_passed_within_profile` revision.
4. `cad_viewer_open` returns a URL; the browser logs in without a token prompt.
