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

## What’s new in 0.2.1

- **Auth fixed**: `tokan.authToken` is resource-scoped, and every
  `getConfiguration('tokan')` call now passes the workspace folder URI, so
  `/metrics` polls succeed and the status bar stops showing `offline`
  despite a correct token.
- **Installable vsix**: `build.sh` now zips under `extension/` and recreates
  the archive each run, so every produced `.vsix` is installable by VS Code.
- **Safe `.mcp.json` handling**: activation merges into an existing
  `.mcp.json` and never overwrites a pre-existing `tokan` entry — live
  credentials can’t be dropped into a tracked file.

## How it works for a 4-dev team

When four developers share one live gateway:

1. **Shared gateway URL** — every dev sets `tokan.gatewayUrl` to the same
   live gateway (`https://tokan-gateway.mangobush-7cced29d.eastus.azurecontainerapps.io/v1`)
   and `tokan.authToken` to their gateway key.
2. **Live savings meter** — the status bar shows `$(zap) Tokan: x.x% saved`
   polled from `/metrics` every 5 seconds, so everyone sees the same live
   numbers as they edit, run tests, or invoke the coding agent.
3. **Provider prompt cache** — the gateway keeps shared prefixes byte-stable
   (provider caches `60,288 tokens` in our demo, 57.9% ratio), so repeated
   prompts — common imports, recurring code patterns — are served at cache
   price instead of full price.
4. **Team cost savings** — across a 12-request demo session, Tokan reported
   **15,532 tokens saved (12.97%)**, about **$0.04** avoided, with 11 LLM
   calls. Extrapolated to daily active coding, that’s a 10–15% token
   reduction per developer directly lowering cloud spend.
5. **No-flag behavior unchanged** — clean code user content is
   authority-protected (Tokan doesn’t rewrite it), so savings are honest:
   noisy tool output and repeated context get compacted; clean files don’t.

## Example measured results

From a simulated 5-turn repo conversation + noisy tool-output session:

| Metric | Value |
|---|---|
| Requests | 12 |
| Tokens saved | 15,532 |
| Saved % | 12.97% |
| Provider cache served | 60,288 tok |
| Prefix reuse | 80,196 tok |
| Cost saved | $0.0388 |

## Value it brings

- **Immediate visibility**: token savings shown live in the status bar — no
  more guessing how much you’re saving.
- **Cost efficiency**: 10–15% token reduction across the team directly
  lowers cloud costs.
- **Security**: no credentials in argv / ARM state; gateway key injected via
  `tokan.authToken` (workspace setting or environment).
- **Zero runtime deps**: pure TypeScript, works in any VS Code environment.
- **Team alignment**: all devs see the same live metrics, making it easy to
  discuss optimizations and share best practices.

> **Note**: The dashboard (`/dashboard`) requires a valid `X-Optimizer-Api-Key`
> header. Browsers can’t send this on navigation, so the extension renders
> authenticated `/metrics` in an editor tab instead of a browser tab.
