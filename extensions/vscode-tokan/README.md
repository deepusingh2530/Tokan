# Tokan for VS Code

Points your IDE workspace at a local [Tokan](../../README.md)
token-optimization gateway and shows **live** savings in the status bar —
every number is fetched from the gateway's `/metrics`, none are hardcoded.

## What it does

1. **Wires the workspace** — writes `tokan.gatewayUrl` into
   `.vscode/settings.json` and, when the workspace has no `.mcp.json`
   `tokan` entry yet, creates one pointing at the gateway's MCP endpoint.
   An existing `tokan` entry is never overwritten — a committed example or
   a user's own config stays byte-for-byte, so live credentials can't be
   dropped into a tracked file (Tokan keys only; it does not touch Copilot's
   deprecated `debug.overrideProxyUrl`).
2. **Status bar meter** — polls `GET /metrics` every 5 s and renders
   `$(zap) Tokan: x.x% saved`; the tooltip breaks it down by route, cost,
   secrets redacted and auth failures. Shows `offline` when the gateway is
   not reachable (or `401 — set tokan.authToken` when it requires a key).
3. **Tokan: Export Terminal Environment** — opens a terminal with
   `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` pointing at the gateway, plus the
   gateway API key as the agent credential when `tokan.authToken` is set.
4. **Tokan: Show Metrics** — opens the full gateway dashboard in the browser
   (no auth configured), or renders authenticated `/metrics` in an editor tab
   when `tokan.authToken` is set (browsers cannot send headers on a
   top-level navigation).

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `tokan.gatewayUrl` | `http://127.0.0.1:4040/v1` | Gateway endpoint. Start it with `token-optimizer serve`. |
| `tokan.authToken` | *(empty)* | One of the gateway's `--api-keys` / `TOKAN_API_KEYS` values. Leave empty when the gateway runs on loopback without inbound auth. |

## Install

```bash
cd extensions/vscode-tokan
./build.sh            # compiles src/extension.ts → out/, packages the .vsix
code --install-extension vscode-tokan-0.2.0.vsix
```

## With gateway auth (recommended off-loopback)

```bash
token-optimizer serve --api-keys "$TOKAN_API_KEYS"   # env or flag
```

Then set the same key in `tokan.authToken`. The extension sends it as both
`X-Optimizer-Api-Key` and `Authorization: Bearer` — the gateway accepts
either. A Copilot OAuth token can double as the gateway key (it is both the
passthrough credential and the inbound key).
