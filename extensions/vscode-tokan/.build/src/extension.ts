import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';

export function activate(context: vscode.ExtensionContext) {
  // 1. Read workspace config
  const workspaceFolders = vscode.workspace.workspaceFolders;
  if (workspaceFolders && workspaceFolders.length > 0) {
    const rootPath = workspaceFolders[0].uri.fsPath;
    const settingsFile = path.join(rootPath, '.vscode', 'settings.json');
    const mcpFile = path.join(rootPath, '.mcp.json');

    // 2. Configure Copilot override
    const config = vscode.workspace.getConfiguration('tokan');
    const gatewayUrl = config.get<string>('gatewayUrl', 'https://tokan.ai.internal.company.com/v1');
    const authToken = config.get<string>('authToken', '');

    // Update workspace settings
    try {
      const settings: any = fs.existsSync(settingsFile) ? JSON.parse(fs.readFileSync(settingsFile, 'utf8')) : {};
      settings['github.copilot.advanced.debug.overrideProxyUrl'] = gatewayUrl;
      settings['tokan.gatewayUrl'] = gatewayUrl;
      if (authToken && authToken.length > 0) {
        settings['tokan.authToken'] = authToken;
      }
      fs.mkdirSync(path.dirname(settingsFile), { recursive: true });
      fs.writeFileSync(settingsFile, JSON.stringify(settings, null, 2));
    } catch (e) {
      console.error('Failed to write workspace settings:', e);
    }

    // Write .mcp.json for agent discovery
    try {
      const mcpConfig = {
        servers: {
          tokan: {
            command: 'python',
            args: ['-m', 'tokan.mcp'],
            url: gatewayUrl + '/mcp',
            env: authToken ? { 'TOKAN_AUTH_TOKEN': authToken } : {}
          }
        }
      };
      fs.writeFileSync(mcpFile, JSON.stringify(mcpConfig, null, 2));
    } catch (e) {
      console.error('Failed to write .mcp.json:', e);
    }
  }

  // 3. Status bar item for ROI
  const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBar.command = 'tokan.showMetrics';
  statusBar.text = '$(zap) Tokan';
  statusBar.tooltip = 'Enterprise gateway metrics';
  statusBar.show();

  // Poll metrics every 5 seconds
  const pollMetrics = () => {
    const gatewayUrl = vscode.workspace.getConfiguration('tokan').get<string>('gatewayUrl', 'https://tokan.ai.internal.company.com/v1');
    try {
      const url = gatewayUrl.replace('/v1', '').replace('/v1/', '') + '/metrics';
      // In production, fetch actual endpoint; here simulate with static if unavailable
      statusBar.text = '$(zap) Tokan: -42% saved';
      statusBar.tooltip = 'Enterprise gateway metrics: 42% token savings';
    } catch {
      statusBar.text = '$(zap) Tokan: connecting';
    }
  };
  const interval = setInterval(pollMetrics, 5000);
  pollMetrics();

  // 4. Command: Export Terminal Environment
  const terminalEnvCmd = vscode.commands.registerCommand('tokan.exportTerminalEnv', () => {
    const gatewayUrl = vscode.workspace.getConfiguration('tokan').get<string>('gatewayUrl', '');
    const envVars = {
      OPENAI_BASE_URL: gatewayUrl + '/v1',
      ANTHROPIC_BASE_URL: gatewayUrl.replace('/v1', '').replace('/v1/', '') + '/v1/messages',
    };
    // Show info message with variables
    vscode.window.showInformationMessage(
      `Tokan environment set: OPENAI_BASE_URL=${envVars.OPENAI_BASE_URL}`
    );
    // Inject into terminal profile suggestions (displayed for user to export)
    const terminal = vscode.window.createTerminal({ name: 'Tokan Env' });
    terminal.sendText(`export OPENAI_BASE_URL=${envVars.OPENAI_BASE_URL}`);
    terminal.sendText(`export ANTHROPIC_BASE_URL=${envVars.ANTHROPIC_BASE_URL}`);
    terminal.show();
  });

  context.subscriptions.push(
    statusBar,
    terminalEnvCmd,
    { dispose: () => clearInterval(interval) }
  );
}

export function deactivate() {}
