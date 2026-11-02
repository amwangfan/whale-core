"""Official Shell tool. The manifest approval gate and OS sandbox both apply.

分层：内置 provider 实现，非插件框架契约（builtin-provider，issue #31）。

审批语义（issue #59）：命令在 Bubblewrap 沙箱内执行（cwd 白名单 / rlimit /
超时 / 环境擦洗），真正的隔离在执行器。宿主装配层在 personal 档下可把
``shell_sandbox.run_command`` 配进 ``PluginActionPolicy.auto_approve_actions``
——主人已明确授权个人沙箱时不再逐条人审；transaction 永不可免审。
"""
from __future__ import annotations
from pathlib import Path
from whale_core.core.runtime import ToolSpec
from whale_core.plugins.manifest import PluginManifest
from whale_core.cloud.shell_executor import ShellExecutor
from whale_core.cloud.shell_policy import ShellPolicy

#: 宿主按动作免审配置用的精确对（issue #59）。
SHELL_PLUGIN_ID = "shell_sandbox"
SHELL_RUN_ACTION = "run_command"
SHELL_AUTO_PAIR = f"{SHELL_PLUGIN_ID}.{SHELL_RUN_ACTION}"


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
    registry.register(PluginManifest(plugin_id=SHELL_PLUGIN_ID, name='受控终端', version='1.0.0', kind='output',
                      actions=(SHELL_RUN_ACTION,), action_risk={SHELL_RUN_ACTION: 'communicate'},
                      description='在Linux bubblewrap隔离工作目录执行；沙箱不可用时拒绝。personal档可由宿主配置免逐次审批'), executor=Adapter())
    tools.register(ToolSpec(name='run_shell', capability='shell.execute', plugin_id=SHELL_PLUGIN_ID,
                           action=SHELL_RUN_ACTION, risk_class='communicate',
                           description='在专用工作目录内执行command；系统沙箱隔离，personal档可免逐次审批'))
