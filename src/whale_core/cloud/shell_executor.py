"""受控 shell 执行器：ShellPolicy 裁决之后的真实执行层（roadmap §10.3 / P1）。

防线分层（诚实声明）：
- 本模块提供：策略裁决前置（ShellPolicy 三档 + 高危硬拒绝）、超时强杀
  （独立进程组 SIGKILL）、输出截断、环境变量擦洗（默认最小 allowlist +
  密钥名黑名单）、cwd 白名单、可选降权（``drop_to_user``，宿主以 root 运行
  时把子进程降到指定用户）。
- 本模块**不提供**的部分（宿主部署形态）：systemd 沙箱/资源限额
  （MemoryMax/CPUQuota/ProtectSystem 等）——那是 unit 文件的职责，见
  docs/gap-analysis-2026-10-30.md §4。

审计：调用方应把 ``policy.audit(command, verdict)`` 落台账；本模块不记日志、
不回显密钥；命令输出可能含敏感信息，由调用方决定展示范围（回执截断）。
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from whale_core.cloud.shell_policy import ShellPolicy, ShellVerdict

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_OUTPUT_CHARS = 4000

# 环境变量：先取最小 allowlist，再剔除密钥样式的键
_ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ", "TMPDIR")
_ENV_SECRET_RE = re.compile(r"(?:_KEY|_TOKEN|_SECRET|_PASSWORD|_CREDENTIAL|API_?KEY)", re.IGNORECASE)


@dataclass(frozen=True)
class ExecutionResult:
    status: str  # "success" | "failed" | "timeout" | "denied" | "error"
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    verdict_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status, "exit_code": self.exit_code,
            "stdout": self.stdout, "stderr": self.stderr,
            "duration_s": round(self.duration_s, 3),
            "verdict_reason": self.verdict_reason,
        }


def _scrub_env(env: dict[str, str] | None) -> dict[str, str]:
    source = dict(os.environ if env is None else env)
    out = {k: v for k, v in source.items() if k in _ENV_ALLOW}
    return {k: v for k, v in out.items() if not _ENV_SECRET_RE.search(k)}


def _demote_fn(user: str):
    """preexec_fn：降权到指定用户（清附加组 + setgid + setuid）。

    仅宿主为 root 时有效；preexec_fn 在多线程进程里有固有限制
    （执行器应在工作线程启动前完成构造——本执行器每次 run 新建子进程，
    受影响窗口可控）。
    """
    import pwd

    pw = pwd.getpwnam(user)  # KeyError 由调用方转译

    def _demote() -> None:
        os.setgroups([])  # 清 root 附加组（docker/disk 等）——安全审查修正
        os.setgid(pw.pw_gid)
        os.setuid(pw.pw_uid)

    return _demote


class ShellExecutor:
    """策略前置的真实命令执行器。构造不执行任何命令。"""

    def __init__(
        self,
        policy: ShellPolicy | None = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        output_chars: int = DEFAULT_OUTPUT_CHARS,
        allowed_cwds: tuple[str, ...] = (),
        drop_to_user: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.policy = policy or ShellPolicy.from_env()
        self.timeout_s = timeout_s
        self.output_chars = output_chars
        self.allowed_cwds = tuple(str(Path(p).resolve()) for p in allowed_cwds)
        self.drop_to_user = drop_to_user
        self.env = env

    def verdict(self, command: str, *, is_owner: bool) -> ShellVerdict:
        return self.policy.command_verdict(command, is_owner=is_owner)

    def run(self, command: str, *, is_owner: bool, cwd: str | None = None) -> ExecutionResult:
        started = time.monotonic()
        verdict = self.policy.command_verdict(command, is_owner=is_owner)
        if not verdict.allowed:
            return ExecutionResult("denied", None, "", "",
                                   time.monotonic() - started, verdict.reason)
        cwd_resolved = self._resolve_cwd(cwd)
        if cwd_resolved is None:
            return ExecutionResult("denied", None, "", "",
                                   time.monotonic() - started,
                                   f"cwd not in whitelist: {cwd!r}")
        preexec = None
        if self.drop_to_user:
            try:
                preexec = _demote_fn(self.drop_to_user)
            except KeyError:
                return ExecutionResult("error", None, "", "",
                                       time.monotonic() - started,
                                       f"unknown drop user: {self.drop_to_user!r}")
        try:
            proc = subprocess.Popen(
                ["/bin/sh", "-c", command],
                cwd=cwd_resolved,
                env=_scrub_env(self.env),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                errors="replace",  # 非法字节替换——解码异常不得逃逸 run() 契约
                start_new_session=True,  # 独立进程组，超时整组强杀
                preexec_fn=preexec,
            )
        except OSError as exc:
            return ExecutionResult("error", None, "", "",
                                   time.monotonic() - started, type(exc).__name__)
        try:
            out, err = proc.communicate(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                out, err = proc.communicate(timeout=5)  # 强杀后收集残余输出
            except subprocess.TimeoutExpired:
                out, err = "", ""
            return ExecutionResult("timeout", None,
                                   (out or "")[: self.output_chars],
                                   (err or "")[: self.output_chars],
                                   time.monotonic() - started, f"killed after {self.timeout_s}s")
        duration = time.monotonic() - started
        status = "success" if proc.returncode == 0 else "failed"
        return ExecutionResult(status, proc.returncode,
                               (out or "")[: self.output_chars],
                               (err or "")[: self.output_chars],
                               duration)

    def _resolve_cwd(self, cwd: str | None) -> str | None:
        """cwd 为空 → 临时目录；给了 cwd 时必须落在白名单内（未配白名单一律拒）。"""
        if cwd is None:
            return tempfile.gettempdir()
        resolved = str(Path(cwd).resolve())
        if not self.allowed_cwds:
            return None
        for base in self.allowed_cwds:
            if resolved == base or resolved.startswith(base + os.sep):
                return resolved
        return None
