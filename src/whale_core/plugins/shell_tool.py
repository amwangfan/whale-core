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


def _receipt(result) -> str:
    """回执保留 status/exit_code，模型可据此判断 partial/nonzero。"""
    body = result.stdout + '\n' + result.stderr + '\n' + result.verdict_reason
    return f"[status={result.status} exit={result.exit_code}]\n{body}"[:4000]


def _classify(result):
    """把 ExecutionResult 分类为 (ok, receipt)。

    success 恒为 ok；failed 且带 exit_code、stdout 非空也视为拿到结果
    （如 curl 计时探针退出码非零但已输出数据）。failed 空 stdout、
    timeout / output_limit / denied / error 即使带残留 stdout 也不伪称完成。
    """
    if result.status == 'success':
        return True, _receipt(result)
    if (result.status == 'failed' and result.exit_code is not None
            and result.stdout.strip()):
        return True, _receipt(result)
    # failed 无 stdout、timeout / output_limit / denied / error：不伪称完成
    return False, _receipt(result)


def register_shell(registry, tools, workspace: Path, *, env=None):
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    import os as _os
    _drop = 'nobody' if _os.geteuid() == 0 else None
    executor = ShellExecutor(ShellPolicy.from_env(env), allowed_cwds=(str(workspace),),
                             sandbox='bubblewrap', drop_to_user=_drop)
    class Adapter:
        def execute(self, action, target, args):
            command = args.get('command')
            if not isinstance(command, str) or not command.strip():
                return False, 'command required'
            # 身份只能来自网关注入的宿主标志，绝不可信模型/用户槽位自抬权。
            is_owner = bool(args.get('_whale_is_owner'))
            has_grant = bool(args.get('_whale_shell_grant'))
            result = executor.run(
                command, cwd=str(workspace), is_owner=is_owner, has_shell_grant=has_grant)
            return _classify(result)
    registry.register(PluginManifest(plugin_id=SHELL_PLUGIN_ID, name='受控终端', version='1.0.0', kind='output',
                      actions=(SHELL_RUN_ACTION,), action_risk={SHELL_RUN_ACTION: 'communicate'},
                      description='在Linux bubblewrap隔离工作目录执行；沙箱不可用时拒绝。personal档可由宿主配置免逐次审批'), executor=Adapter())
    # capability=shell.safe → 执行端 required scope = tool.shell.safe（与 SCOPE_TOOL_SHELL_SAFE 对齐）
    tools.register(ToolSpec(name='run_shell', capability='shell.safe', plugin_id=SHELL_PLUGIN_ID,
                           action=SHELL_RUN_ACTION, risk_class='communicate',
                           description='在专用工作目录内执行command；系统沙箱隔离，personal档可免逐次审批。沙箱无 raw socket（ping 不可用）；测网络延迟改用 curl 计时（curl -o /dev/null -s -w 格式化计时 https://目标）'))
