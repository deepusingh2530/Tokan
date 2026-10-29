import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';

/** Strip a trailing /v1 so the gateway base can take any sub-path. */
function gatewayBase(raw: string): string {
  return (raw || '').replace(/\/+$/, '').replace(/\/v1$/, '');
}

export function activate(context: vscode.ExtensionContext) {
  // 1. Read workspace config and keep it in sync (Tokan keys only — do NOT
  // touch Copilot's deprecated debug.overrideProxyUrl; modern VS Code routes
  // through BYOK Custom Endpoint models in chatLanguageModels.json instead).
  const workspaceFolders = vscode.workspace.workspaceFolders;
  if (workspaceFolders && workspaceFolders.length > 0) {
    const rootPath = workspaceFolders[0].uri.fsPath;
    const settingsFile = path.join(rootPath, '.vscode', 'settings.json');
    const mcpFile = path.join(rootPath, '.mcp.json');

    const config = vscode.workspace.getConfiguration('tokan');
    const gatewayUrl = config.get<string>('gatewayUrl', '');
    const authToken = config.get<string>('authToken', '');

    try {
      const settings: any = fs.existsSync(settingsFile) ? JSON.parse(fs.readFileSync(settingsFile, 'utf8')) : {};
      delete settings['github.copilot.advanced.debug.overrideProxyUrl'];
      settings['tokan.gatewayUrl'] = gatewayUrl;
      if (authToken && authToken.length > 0) {
        settings['tokan.authToken'] = authToken;
      }
      fs.mkdirSync(path.dirname(settingsFile), { recursive: true });
      fs.writeFileSync(settingsFile, JSON.stringify(settings, null, 2));
    } catch (e) {
      console.error('Failed to write workspace settings:', e);
    }

    try {
      const mcpConfig = {
        servers: {
          tokan: {
            command: 'python',
            args: ['-m', 'tokan.mcp'],
            url: gatewayBase(gatewayUrl) + '/v1/mcp',
            env: authToken ? { TOKAN_AUTH_TOKEN: authToken } : {}
          }
        }
      };
      fs.writeFileSync(mcpFile, JSON.stringify(mcpConfig, null, 2));
    } catch (e) {
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
    const gatewayUrl = vscode.workspace.getConfiguration('tokan').get<string>('gatewayUrl', '');
    const metricsUrl = gatewayBase(gatewayUrl) + '/metrics';
    if (!metricsUrl.startsWith('http')) {
      statusBar.text = '$(zap) Tokan: no gateway';
      statusBar.tooltip = 'Set tokan.gatewayUrl in settings';
      inflight = false;
      return;
    }
    try {
      const res = await fetch(metricsUrl, { signal: AbortSignal.timeout(4000) });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const data: any = await res.json();
      const s = data.session || {};
      const saved = Number(s.saved_pct ?? 0);
      const requests = Number(s.requests ?? 0);
      const cacheHits = Number(data.cache?.hits ?? 0);
      statusBar.text = `$(zap) Tokan: ${saved.toFixed(1)}% saved`;
      statusBar.tooltip = [
        `Token savings: ${saved.toFixed(1)}% (${s.tokens_saved ?? 0} tok)`,
        `Requests: ${requests}  (cache ${cacheHits}, local ${s.local_solves ?? 0}, llm ${s.llm_calls ?? 0})`,
        `Cost saved: $${Number(s.cost_saved_usd ?? 0).toFixed(4)}`,
        '',
        'Click to open the full dashboard',
      ].join('\n');
    } catch (e) {
      statusBar.text = '$(zap) Tokan: offline';
      statusBar.tooltip = `Cannot reach ${metricsUrl}\n${e instanceof Error ? e.message : e}`;
    } finally {
      inflight = false;
    }
  };
  const interval = setInterval(() => {
    void pollMetrics();
  }, 5000);
  void pollMetrics();

  // 3. Command: Export Terminal Environment
  const terminalEnvCmd = vscode.commands.registerCommand('tokan.exportTerminalEnv', () => {
    const gatewayUrl = vscode.workspace.getConfiguration('tokan').get<string>('gatewayUrl', '');
    const base = gatewayBase(gatewayUrl);
    vscode.window.showInformationMessage(`Tokan environment set: OPENAI_BASE_URL=${base}/v1`);
    const terminal = vscode.window.createTerminal({ name: 'Tokan Env' });
    terminal.sendText(`export OPENAI_BASE_URL=${base}/v1`);
    terminal.sendText(`export ANTHROPIC_BASE_URL=${base}/v1/messages`);
    terminal.show();
  });

  // 4. Command: open the live dashboard directly in the browser.
  const showMetricsCmd = vscode.commands.registerCommand('tokan.showMetrics', () => {
    const gatewayUrl = vscode.workspace.getConfiguration('tokan').get<string>('gatewayUrl', '');
    const dashUrl = gatewayBase(gatewayUrl) + '/dashboard';
    if (dashUrl.startsWith('http')) {
      void vscode.env.openExternal(vscode.Uri.parse(dashUrl));
    } else {
      vscode.window.showWarningMessage(
        'Tokan: set tokan.gatewayUrl in settings to open the dashboard.'
      );
    }
  });

  context.subscriptions.push(
    statusBar,
    terminalEnvCmd,
    showMetricsCmd,
    { dispose: () => clearInterval(interval) }
  );
}

export function deactivate() {}
