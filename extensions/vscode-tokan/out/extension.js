"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = __importStar(require("vscode"));
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
/** Strip a trailing /v1 so the gateway base can take any sub-path. */
function gatewayBase(raw) {
    return (raw || '').replace(/\/+$/, '').replace(/\/v1$/, '');
}
/**
 * Inbound-auth headers for gateway endpoints (M6): send the configured key
 * both ways — X-Optimizer-Api-Key is Tokan's purpose-built header, while
 * Bearer is the shape agents already speak (and the pattern where a Copilot
 * OAuth token doubles as the gateway key). The gateway accepts either.
 */
function authHeaders(token) {
    if (!token) {
        return {};
    }
    return {
        'X-Optimizer-Api-Key': token,
        'Authorization': `Bearer ${token}`,
    };
}
function activate(context) {
    // 1. Read workspace config and keep it in sync (Tokan keys only — do NOT
    // touch Copilot's deprecated debug.overrideProxyUrl; modern VS Code routes
    // through BYOK Custom Endpoint models in chatLanguageModels.json instead).
    const workspaceFolders = vscode.workspace.workspaceFolders;
    // tokan.authToken is resource-scoped: read it against the workspace folder
    // URI, or VS Code returns the default ('') and every poll 401s.
    const cfgScope = workspaceFolders?.[0]?.uri;
    if (workspaceFolders && workspaceFolders.length > 0) {
        const rootPath = workspaceFolders[0].uri.fsPath;
        const settingsFile = path.join(rootPath, '.vscode', 'settings.json');
        const mcpFile = path.join(rootPath, '.mcp.json');
        const config = vscode.workspace.getConfiguration('tokan', cfgScope);
        const gatewayUrl = config.get('gatewayUrl', '');
        const authToken = config.get('authToken', '');
        try {
            const settings = fs.existsSync(settingsFile) ? JSON.parse(fs.readFileSync(settingsFile, 'utf8')) : {};
            delete settings['github.copilot.advanced.debug.overrideProxyUrl'];
            settings['tokan.gatewayUrl'] = gatewayUrl;
            if (authToken && authToken.length > 0) {
                settings['tokan.authToken'] = authToken;
            }
            fs.mkdirSync(path.dirname(settingsFile), { recursive: true });
            fs.writeFileSync(settingsFile, JSON.stringify(settings, null, 2));
        }
        catch (e) {
            console.error('Failed to write workspace settings:', e);
        }
        try {
            // Merge into the existing file instead of clobbering it: .mcp.json may
            // be a committed, sanitized example (mcpServers schema) or a user's own
            // config — rewriting it wholesale would destroy their entries and drop
            // live credentials into a tracked file. Support both the VS Code
            // `servers` and the Claude Code `mcpServers` top-level key, and if a
            // `tokan` entry already exists, leave it alone: once the file exists,
            // its config belongs to the user/repo, not to this extension.
            let mcpConfig = null;
            if (fs.existsSync(mcpFile)) {
                try {
                    mcpConfig = JSON.parse(fs.readFileSync(mcpFile, 'utf8'));
                }
                catch {
                    mcpConfig = null; // unreadable JSON: replace with a fresh config
                }
            }
            if (mcpConfig && typeof mcpConfig === 'object') {
                const rootKey = mcpConfig['mcpServers'] && !mcpConfig['servers']
                    ? 'mcpServers' : 'servers';
                const existing = mcpConfig[rootKey];
                const hasTokan = !!(existing && typeof existing === 'object'
                    && existing['tokan']);
                if (!hasTokan) {
                    // No tokan entry yet: add ours alongside whatever else lives here.
                    if (!existing || typeof existing !== 'object') {
                        mcpConfig[rootKey] = {};
                    }
                    mcpConfig[rootKey]['tokan'] = {
                        command: 'python',
                        args: ['-m', 'tokan.mcp'],
                        url: gatewayBase(gatewayUrl) + '/v1/mcp',
                        env: authToken ? { TOKAN_AUTH_TOKEN: authToken } : {}
                    };
                    fs.writeFileSync(mcpFile, JSON.stringify(mcpConfig, null, 2) + '\n');
                }
                // Pre-existing tokan entry: leave it alone — once the file exists,
                // its config belongs to the user/repo, not to this extension.
            }
            else {
                fs.writeFileSync(mcpFile, JSON.stringify({
                    servers: {
                        tokan: {
                            command: 'python',
                            args: ['-m', 'tokan.mcp'],
                            url: gatewayBase(gatewayUrl) + '/v1/mcp',
                            env: authToken ? { TOKAN_AUTH_TOKEN: authToken } : {}
                        }
                    }
                }, null, 2) + '\n');
            }
        }
        catch (e) {
            console.error('Failed to write .mcp.json:', e);
        }
    }
    // 2. Status bar: live ROI metrics from GET /metrics (no hardcoded values).
    const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    statusBar.command = 'tokan.showMetrics';
    statusBar.text = '$(zap) Tokan: connecting';
    statusBar.tooltip = 'Fetching gateway metrics…';
    statusBar.show();
    let inflight = false;
    const pollMetrics = async () => {
        if (inflight) {
            return;
        }
        inflight = true;
        const cfg = vscode.workspace.getConfiguration('tokan', cfgScope);
        const gatewayUrl = cfg.get('gatewayUrl', '');
        const authToken = cfg.get('authToken', '');
        const metricsUrl = gatewayBase(gatewayUrl) + '/metrics';
        if (!metricsUrl.startsWith('http')) {
            statusBar.text = '$(zap) Tokan: no gateway';
            statusBar.tooltip = 'Set tokan.gatewayUrl in settings';
            inflight = false;
            return;
        }
        try {
            // M6: /metrics requires a key when the gateway runs with --api-keys.
            const res = await fetch(metricsUrl, {
                signal: AbortSignal.timeout(4000),
                headers: authHeaders(authToken),
            });
            if (!res.ok) {
                throw new Error(`HTTP ${res.status}${res.status === 401 ? ' (set tokan.authToken)' : ''}`);
            }
            const data = await res.json();
            const s = data.session || {};
            const saved = Number(s.saved_pct ?? 0);
            const requests = Number(s.requests ?? 0);
            const cacheHits = Number(data.cache?.hits ?? 0);
            statusBar.text = `$(zap) Tokan: ${saved.toFixed(1)}% saved`;
            statusBar.tooltip = [
                `Token savings: ${saved.toFixed(1)}% (${s.tokens_saved ?? 0} tok)`,
                `Requests: ${requests}  (cache ${cacheHits}, local ${s.local_solves ?? 0}, llm ${s.llm_calls ?? 0})`,
                `Cost saved: $${Number(s.cost_saved_usd ?? 0).toFixed(4)}`,
                ...(Number(s.secrets_redacted ?? 0) > 0 ? [`Secrets redacted: ${s.secrets_redacted} (of ${s.secrets_detected} found)`] : []),
                ...(Number(s.auth_failures ?? 0) > 0 ? [`Auth failures: ${s.auth_failures}`] : []),
                '',
                'Click to open the full dashboard',
            ].join('\n');
        }
        catch (e) {
            statusBar.text = '$(zap) Tokan: offline';
            statusBar.tooltip = `Cannot reach ${metricsUrl}\n${e instanceof Error ? e.message : e}`;
        }
        finally {
            inflight = false;
        }
    };
    const interval = setInterval(() => {
        void pollMetrics();
    }, 5000);
    void pollMetrics();
    // 3. Command: Export Terminal Environment
    const terminalEnvCmd = vscode.commands.registerCommand('tokan.exportTerminalEnv', () => {
        const cfg = vscode.workspace.getConfiguration('tokan', cfgScope);
        const gatewayUrl = cfg.get('gatewayUrl', '');
        const authToken = cfg.get('authToken', '');
        const base = gatewayBase(gatewayUrl);
        vscode.window.showInformationMessage(`Tokan environment set: OPENAI_BASE_URL=${base}/v1`);
        const terminal = vscode.window.createTerminal({ name: 'Tokan Env' });
        terminal.sendText(`export OPENAI_BASE_URL=${base}/v1`);
        terminal.sendText(`export ANTHROPIC_BASE_URL=${base}/v1/messages`);
        if (authToken) {
            // M6 gateway auth: terminal agents authenticate to the gateway with the
            // same Bearer they'd use for a provider. When the gateway holds its own
            // upstream key it swaps it in; in BYOK passthrough this is the provider
            // token doubling as the gateway key (the Copilot OAuth pattern).
            terminal.sendText(`export OPENAI_API_KEY='${authToken.replace(/'/g, "'\\''")}'`);
            terminal.sendText(`export ANTHROPIC_API_KEY='${authToken.replace(/'/g, "'\\''")}'`);
            terminal.sendText('export TOKAN_API_KEY="$OPENAI_API_KEY"');
        }
        terminal.show();
    });
    // 4. Command: open the live dashboard directly in the browser.
    //    The dashboard requires a key when the gateway runs with --api-keys and
    //    browsers cannot set headers on a top-level navigation — so with a token
    //    configured we render authenticated /metrics in an editor tab instead
    //    (same numbers as the dashboard, no key in the URL).
    const showMetricsCmd = vscode.commands.registerCommand('tokan.showMetrics', async () => {
        const cfg = vscode.workspace.getConfiguration('tokan', cfgScope);
        const gatewayUrl = cfg.get('gatewayUrl', '');
        const authToken = cfg.get('authToken', '');
        const base = gatewayBase(gatewayUrl);
        if (!base.startsWith('http')) {
            vscode.window.showWarningMessage('Tokan: set tokan.gatewayUrl in settings to open the dashboard.');
            return;
        }
        if (authToken) {
            try {
                const res = await fetch(base + '/metrics', {
                    signal: AbortSignal.timeout(4000),
                    headers: authHeaders(authToken),
                });
                if (!res.ok) {
                    throw new Error(`HTTP ${res.status}`);
                }
                const data = await res.json();
                const s = data.session || {};
                const lines = [
                    'Tokan gateway metrics', '='.repeat(40),
                    `requests        : ${s.requests ?? 0}`,
                    `saved           : ${s.saved_pct ?? 0}% (${s.tokens_saved ?? 0} tok)`,
                    `cache hits      : ${data.cache?.hits ?? 0} / ${data.cache?.misses ?? 0} (miss/hit)`,
                    `semantic hits   : ${s.semantic_cache_hits ?? 0}`,
                    `cost saved      : $${Number(s.cost_saved_usd ?? 0).toFixed(4)}`,
                    `auth failures   : ${s.auth_failures ?? 0}`,
                    `secrets found   : ${s.secrets_detected ?? 0} (redacted ${s.secrets_redacted ?? 0})`,
                    `doc fallbacks   : ${s.doc_full_fallbacks ?? 0}  low coverage: ${s.doc_low_coverage ?? 0}`,
                    `router tiers    : ${JSON.stringify(data.tiers ?? {})}`,
                    '',
                    `Full dashboard (key required): ${base}/dashboard`,
                ];
                const doc = await vscode.workspace.openTextDocument({
                    language: 'markdown',
                    content: '```\n' + lines.join('\n') + '\n```',
                });
                await vscode.window.showTextDocument(doc, { preview: false });
            }
            catch (e) {
                vscode.window.showErrorMessage(`Tokan: metrics fetch failed (${e instanceof Error ? e.message : e}) — check tokan.gatewayUrl / tokan.authToken`);
            }
            return;
        }
        void vscode.env.openExternal(vscode.Uri.parse(base + '/dashboard'));
    });
    context.subscriptions.push(statusBar, terminalEnvCmd, showMetricsCmd, { dispose: () => clearInterval(interval) });
}
function deactivate() { }
