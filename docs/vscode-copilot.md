# VS Code + Tokan + Copilot, step by step

Run a coding agent inside VS Code with your **GitHub Copilot subscription as the
backend** and Tokan in front of it (caching, deterministic local answers,
compaction, stats). No API keys anywhere — the gateway authenticates upstream
with your existing GitHub credential.

## Step 0 — Which path applies to you?

GitHub only lets **VS Code's built-in Copilot** talk to a custom endpoint on
**Business or Enterprise** plans. On individual plans (Free/Pro) the
`overrideProxyUrl` setting is ignored — this is GitHub policy, not Tokan.

Check your plan at <https://github.com/settings/copilot>, or just do the
behavioral test in Step 5: if `token-optimizer stats` counts your chat turns,
you are on Path A; if it stays at zero, use Path B.

| Your plan | Path | What you get |
|---|---|---|
| Copilot Business / Enterprise | **A** — native Copilot Chat + agent mode redirected through Tokan | The stock Copilot panel, optimized |
| Copilot Free / Pro (individual) | **B** — Cline / Roo Code / Continue extension, or Copilot CLI, pointed at Tokan | A full agent UI in VS Code, billed to your Copilot subscription via the gateway |

Both paths use the same gateway; only the client wiring differs.

## Step 1 — Install Tokan

```bash
pipx install tokan        # or: uv tool install tokan  |  pip install --user tokan
token-optimizer --help    # sanity check
```

## Step 2 — Start the gateway on your Copilot subscription

```bash
token-optimizer serve --copilot --api-key "$(gh auth token)"
```

- `--copilot` targets the Copilot API origin for an **individual** plan.
  Business/Enterprise: `--copilot business` / `--copilot enterprise`.
- The token is your existing `gh auth login` credential — not an API key.
  (A fine-grained PAT with the **Copilot Requests** permission, or
  `COPILOT_GITHUB_TOKEN`, also works.)

Leave it running in a terminal.

## Step 3 — Verify the gateway standalone

```bash
curl http://127.0.0.1:4040/healthz
# {"ok": true, "service": "token-optimizer"}

curl -s http://127.0.0.1:4040/v1/models | python3 -m json.tool | head
# your subscription's model catalog, relayed

curl http://127.0.0.1:4040/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Say OK"}]}'
```

If the last call returns HTTP 400 `model_not_supported`, your plan tier gates
that model — pick another one from `/v1/models` (the catalog lists everything;
your tier serves a subset).

## Step 4 — Wire VS Code

### Path A — Business/Enterprise: native Copilot agent mode

1. `Cmd+Shift+P` → **Preferences: Open User Settings (JSON)**
2. Add this key (merge into your existing settings — do not replace them):

   ```jsonc
   {
     "github.copilot.advanced": {
       "debug.overrideProxyUrl": "http://127.0.0.1:4040/v1"
     }
   }
   ```

3. `Cmd+Shift+P` → **Developer: Reload Window**
4. Open the Copilot Chat panel, switch the mode selector to **Agent**, and
   send a test prompt ("explain this file in one sentence").

VS Code's own Copilot auth is passed through the gateway untouched; the
gateway's `--api-key` takes precedence upstream so nothing depends on what
VS Code sends.

### Path B — Individual plan: agent extension in VS Code

**Option 1: Cline / Roo Code / Continue** (full agent UI in the sidebar):

1. Install the extension from the marketplace (e.g. **Cline**).
2. In its settings choose provider **OpenAI-API-Compatible**:
   - **Base URL**: `http://127.0.0.1:4040/v1`
   - **API Key**: any placeholder (e.g. `tokan`) — the gateway replaces
     credentials upstream with your GitHub token
   - **Model**: `gpt-4.1` (or any model that answered in Step 3)
3. Use its agent mode as usual. Every turn flows through Tokan.

**Option 2: Copilot CLI in the VS Code integrated terminal** (official BYOK,
works on all plans):

```bash
export COPILOT_PROVIDER_TYPE=openai
export COPILOT_PROVIDER_BASE_URL=http://127.0.0.1:4040/v1
export COPILOT_MODEL=gpt-4.1
copilot        # no GitHub auth needed in the CLI — the gateway passes it
```

## Step 5 — Verify traffic flows through Tokan

With the gateway running, note the counters, then use your agent:

```bash
token-optimizer stats --port 4040
```

- **requests / llm calls** increase with every agent turn → traffic flows.
- Ask the identical question twice → `cache hits` +1, reply in ~1ms.
- Ask "what is on line number N? context: …" → `local solves` +1, zero
  upstream tokens.

On an individual plan with Path A's setting applied, a permanently-zero
counter means GitHub ignored the override — switch to Path B.

## Step 6 — Keep the gateway running (optional, macOS)

The wrapper re-reads your `gh` token on every (re)start, so it survives
credential refreshes:

```bash
# ~/bin/tokan-gateway.sh
#!/bin/zsh
exec "$(command -v token-optimizer)" serve --copilot --api-key "$(gh auth token)" "$@"
```

```bash
chmod +x ~/bin/tokan-gateway.sh
```

`~/Library/LaunchAgents/com.tokan.gateway.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.tokan.gateway</string>
  <key>ProgramArguments</key><array>
    <string>/Users/YOU/bin/tokan-gateway.sh</string>
  </array>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.tokan.gateway.plist
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `curl /healthz` fails | Gateway not running / wrong port. Check the terminal running `token-optimizer serve`. |
| Port already in use | `token-optimizer serve --copilot --port 4141 --api-key "$(gh auth token)"` and point clients at `4141`. |
| VS Code chat errors after the override | Gateway down, or (individual plan) the override confuses the panel — remove the setting, use Path B. Path A needs the gateway up **before** chatting. |
| HTTP 400 `model_not_supported` upstream | Your plan tier gates that model; choose one that answered in Step 3 from `/v1/models`. |
| Stats stay at zero on Path A | GitHub ignored the override (individual plan policy). Use Path B. |
| Stale gateway process | `pkill -f "token-optimizer serve"` then restart. |
