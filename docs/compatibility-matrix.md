# Protocol and host compatibility

Authoritative for a fresh run is `reports/verification.json` together with its build hash.

| System | Status | Evidence / limit |
|---|---|---|
| MCP `2025-03-26`, `2025-06-18`, `2025-11-25` | Tested locally | Real client and Streamable HTTP transport from the official TypeScript SDK; initialisation, tools, structured results |
| Local MCP stdio transport | Tested locally | Automatic process start, same tool contracts, private local user, persistent models after reconnect, on-demand viewer; no browser login |
| Complete workflow through MCP only | Tested locally | Construction, semantic selection, face selection, change, validation, commit, rebinding and STEP export; `reports/mcp-workflow.json`; not an LLM reasoning test |
| MCP `2026-07-28` | Own adapter, tested locally | Discovery, tools, required metadata, header mismatches, unknown version and methods |
| Other MCP versions | Not released | Not advertised as supported |
| Tasks extension, sampling, elicitation, subscriptions | Not offered | Own durable CAD jobs serve as the portable workflow |
| Chromium | Tested | Login, model construction, detail change, validation, commit, export; `reports/browser.json` |
| OAuth resource server | Tested locally | Signature, issuer, audience, expiry, scopes and signed tenant binding |
| Real OAuth provider with PKCE and target host | Open | No customer-specific issuer, callback or approved client is prescribed |
| Agent hosts over stdio (OpenCode, Hermes and other `mcpServers` clients) | Configuration documented | The stdio entry point is the tested SDK transport; see [Connecting MCP clients](mcp-clients.md) |
| Broad independent LLM evaluation | Open | Local language tasks in one host do not replace an independent broad test set |
| Embedded host UI | Not implemented | Standalone authenticated browser viewer instead |
| Public deployment / package publication | Not done | Deployment templates exist; nothing has been published |

## Sources of the adapter decision

The implementation follows the official MCP SDK structure with typed tools and Streamable
HTTP for the protocol versions the pinned SDK supports. The `2026-07-28` version is a separate
adapter whose HTTP headers mirror the JSON fields; the specification also describes discovery
and the mandatory per-request fields
([Streamable HTTP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http),
[Schema 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/schema)).

For real remote access the operator connects a suitable OAuth provider; its PKCE, resource
and client configuration is part of the host test. The service implements no authorisation
server of its own.

## Host test still to run

With a real HTTPS endpoint and identity provider, verify: auth metadata, client registration,
allowed callbacks, PKCE, token audience, scopes, tool discovery, construction of a private
model, change, validation and commit, retry after a dropped connection and access to private
export files. Result, date, host version, protocol version and current registry hash go into
`reports/target-host.json`. That file is never created automatically with an invented success.
