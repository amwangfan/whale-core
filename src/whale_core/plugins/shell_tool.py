"""Official Shell tool. The manifest approval gate and OS sandbox both apply."""
from __future__ import annotations
from pathlib import Path
from whale_core.core.runtime import ToolSpec
from whale_core.plugins.manifest import PluginManifest
from whale_core.cloud.shell_executor import ShellExecutor
from whale_core.cloud.shell_policy import ShellPolicy


def register_shell(registry, tools, workspace: Path, *, env=None):
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    executor = ShellExecutor(ShellPolicy.from_env(env), allowed_cwds=(str(workspace),), sandbox='bubblewrap')
    class Adapter:
        def execute(self, action, target, args):
            command = args.get('command')
            if not isinstance(command, str) or not command.strip():
                return False, 'command required'
            result = executor.run(command, cwd=str(workspace), is_owner=True)
            return result.status == 'success', (result.stdout + '\n' + result.stderr + '\n' + result.verdict_reason)[:4000]
    registry.register(PluginManifest(plugin_id='shell_sandbox', name='受控终端', version='1.0.0', kind='output',
                      actions=('run_command',), action_risk={'run_command': 'communicate'},
                      description='审批后在Linux bubblewrap隔离工作目录执行；沙箱不可用时拒绝'), executor=Adapter())
    tools.register(ToolSpec(name='run_shell', capability='shell.execute', plugin_id='shell_sandbox',
                           action='run_command', risk_class='communicate',
                           description='在专用工作目录内执行command；必须人工批准且要求系统沙箱'))
