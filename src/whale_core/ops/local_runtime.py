"""本地运行时归属管理（issue #47）：一个 canonical checkout，可审计的 start/status/stop。

背景：N100 上曾出现 API 进程从 ``whale-core-dev`` clone 启动、QQ 进程从主
clone 启动的「进程归属分裂」。GitHub 上的代码无法证明本地进程来源，必须用
PID、cwd、启动命令和端口实测。本模块把这件事做成工具：

- ``status``：对每个登记的服务报告 systemd 单元状态、MainPID、进程 cwd、
  命令行、监听端口、cwd 所在 git 树的 HEAD，并判定 cwd 是否落在 canonical
  checkout 内；同时给出 canonical checkout 自身的 HEAD，便于发现
  「运行中代码 ≠ 工作树代码」的漂移。
- ``start``：启动前检查端口占用——若端口被**不属于** canonical checkout 的
  进程占用，拒绝启动（防止重复/分裂实例）；已运行且健康时幂等返回。
- ``stop``：只停止「本工具登记、且 MainPID 的 cwd 确认落在 canonical
  checkout 内、且命令行包含登记标记」的进程；否则拒绝，绝不误杀其他
  Python/QQ/Flutter 进程。

纪律：纯标准库；不读写任何密钥；所有外部命令（systemctl/ss/git）、
``/proc`` 读取与 HTTP/TCP 探测都是**可注入**的，测试全离线。
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

# ---------------------------------------------------------------- 可注入依赖

Runner = Callable[[Sequence[str]], "tuple[int, str, str]"]
CwdOfPid = Callable[[int], "str | None"]
# (address, port, pid) 三元组——按绑定地址区分，避免把 tailscale 代理等
# 绑在不同地址上的同端口进程误判为冲突。
PortListeners = Callable[[], "list[tuple[str, int, int]]"]
HttpProbe = Callable[[str, float], "tuple[int, str]"]
TcpProbe = Callable[[str, int, float], bool]

WILDCARD_ADDRS = frozenset({"0.0.0.0", "::", "[::]", "*"})


def _default_runner(cmd: Sequence[str]) -> "tuple[int, str, str]":
    try:
        proc = subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=15,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover - 防御
        return 127, "", str(exc)


def _default_cwd_of_pid(pid: int) -> "str | None":
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def _default_port_listeners() -> "list[tuple[str, int, int]]":
    """解析 ``ss -tlnpH`` 为 (address, port, pid) 列表（失败返回空）。"""
    rc, out, _ = _default_runner(["ss", "-tlnpH"])
    if rc != 0:
        return []
    listeners: list[tuple[str, int, int]] = []
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 5:
            continue
        # 行格式：LISTEN <recv-q> <send-q> <local addr:port> <peer> [users:...]
        local = cols[3]
        addr, _, port_s = local.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            continue
        addr = addr.strip("[]").split("%", 1)[0]  # 去掉 IPv6/链路本地 %iface 后缀
        for pid_s in re.findall(r"pid=(\d+)", line):
            listeners.append((addr, port, int(pid_s)))
    return listeners


def _default_http_probe(url: str, timeout: float) -> "tuple[int, str]":
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - 仅本机回环
            body = resp.read(4096).decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:  # 401/404 也代表进程活着
        return exc.code, ""
    except Exception:
        return 0, ""


def _default_tcp_probe(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ------------------------------------------------------------------- 数据模型


@dataclass(frozen=True)
class ServiceSpec:
    """一个受管服务的登记信息（不含任何密钥）。"""

    name: str                      # 逻辑名，如 "whale-api"
    unit: str                      # systemd 单元名
    ports: tuple = ()              # 应监听的 TCP 端口
    bind_addrs: tuple = ("127.0.0.1",)  # 预期绑定地址；冲突判定只算这些地址与通配
    cmdline_marker: str = ""       # stop 前要求进程命令行包含该子串
    health_url: str = ""           # http(s)://… 健康检查；空则跳过
    health_tcp: tuple = ()         # (host, port) TCP 存活探测；空则跳过


@dataclass
class PortView:
    port: int
    pids: list = field(default_factory=list)
    foreign_pids: list = field(default_factory=list)  # cwd 不在 canonical 内的 pid


@dataclass
class ServiceStatus:
    name: str
    unit: str
    active: bool
    main_pid: "int | None"
    cwd: "str | None"
    cmdline: "str | None"
    cwd_in_canonical: bool
    process_head: "str | None"      # 进程 cwd 所在 git 树的 HEAD
    head_matches_canonical: "bool | None"
    ports: list = field(default_factory=list)  # list[PortView]
    health: str = "skip"            # ok / fail / skip
    problems: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "unit": self.unit,
            "active": self.active,
            "main_pid": self.main_pid,
            "cwd": self.cwd,
            "cmdline": self.cmdline,
            "cwd_in_canonical": self.cwd_in_canonical,
            "process_head": self.process_head,
            "head_matches_canonical": self.head_matches_canonical,
            "ports": [
                {"port": p.port, "pids": p.pids, "foreign_pids": p.foreign_pids}
                for p in self.ports
            ],
            "health": self.health,
            "problems": self.problems,
        }


@dataclass
class RuntimeReport:
    checkout: str
    canonical_head: "str | None"
    generated_at: float
    services: list = field(default_factory=list)  # list[ServiceStatus]

    def to_dict(self) -> dict:
        return {
            "checkout": self.checkout,
            "canonical_head": self.canonical_head,
            "generated_at": self.generated_at,
            "services": [s.to_dict() for s in self.services],
        }

    @property
    def ok(self) -> bool:
        return all(not s.problems for s in self.services)


# ------------------------------------------------------------------- 管理器

# N100 部署的默认登记（端口、单元名均为本机部署事实，不含密钥）。
DEFAULT_SERVICES: "tuple[ServiceSpec, ...]" = (
    ServiceSpec(
        name="whale-api",
        unit="whale-api.service",
        ports=(8180,),
        cmdline_marker="whale-api",
        health_url="http://127.0.0.1:8180/api/v1/health",
    ),
    ServiceSpec(
        name="qq-bot",
        unit="qq-bot.service",
        ports=(18797,),
        cmdline_marker="whale_core.cli.main",
        health_tcp=("127.0.0.1", 18797),
    ),
)


class RuntimeError_(RuntimeError):
    """start/stop 安全拒绝（端口被外来进程占用、cwd 不在 canonical 内等）。"""


class LocalRuntimeManager:
    def __init__(
        self,
        checkout: "str | Path",
        services: Sequence[ServiceSpec] = DEFAULT_SERVICES,
        *,
        runner: Runner = _default_runner,
        cwd_of_pid: CwdOfPid = _default_cwd_of_pid,
        port_listeners: PortListeners = _default_port_listeners,
        http_probe: HttpProbe = _default_http_probe,
        tcp_probe: TcpProbe = _default_tcp_probe,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.checkout = Path(checkout).resolve()
        self.services = list(services)
        self._run = runner
        self._cwd_of_pid = cwd_of_pid
        self._port_listeners = port_listeners
        self._http = http_probe
        self._tcp = tcp_probe
        self._now = now

    def _conflicting_listeners(
        self, spec: ServiceSpec, port: int,
    ) -> "list[tuple[str, int, int]]":
        """该服务关注的绑定地址（或通配）上的同端口监听。"""
        watched = set(spec.bind_addrs) | WILDCARD_ADDRS
        return [
            (addr, p, pid)
            for addr, p, pid in self._port_listeners()
            if p == port and addr in watched
        ]

    # ------------------------------------------------------------ 内部工具

    def _git_head(self, path: Path) -> "str | None":
        rc, out, _ = self._run(["git", "-C", str(path), "rev-parse", "HEAD"])
        return out.strip() if rc == 0 and out.strip() else None

    def _unit_state(self, unit: str) -> "tuple[bool, int | None]":
        rc, out, _ = self._run([
            "systemctl", "show", unit,
            "-p", "ActiveState,MainPID", "--no-pager",
        ])
        if rc != 0:
            return False, None
        kv = dict(
            line.split("=", 1) for line in out.splitlines() if "=" in line
        )
        active = kv.get("ActiveState") == "active"
        try:
            pid = int(kv.get("MainPID", "0"))
        except ValueError:
            pid = 0
        return active, (pid or None)

    def _cmdline_of(self, pid: int) -> "str | None":
        rc, out, _ = self._run(["ps", "-o", "args=", "-p", str(pid)])
        if rc != 0 or not out.strip():
            return None
        return " ".join(out.split())

    def _in_canonical(self, cwd: "str | None") -> bool:
        if not cwd:
            return False
        try:
            Path(cwd).resolve().relative_to(self.checkout)
            return True
        except ValueError:
            return False

    # ------------------------------------------------------------ 查询

    def status(self) -> RuntimeReport:
        canonical_head = self._git_head(self.checkout)
        report = RuntimeReport(
            checkout=str(self.checkout),
            canonical_head=canonical_head,
            generated_at=self._now(),
        )
        for spec in self.services:
            report.services.append(self._service_status(spec, canonical_head))
        return report

    def _service_status(
        self, spec: ServiceSpec, canonical_head: "str | None",
    ) -> ServiceStatus:
        active, pid = self._unit_state(spec.unit)
        cwd = self._cwd_of_pid(pid) if pid else None
        cmdline = self._cmdline_of(pid) if pid else None
        in_canon = self._in_canonical(cwd)
        proc_head: "str | None" = None
        head_match: "bool | None" = None
        if pid and cwd:
            proc_head = self._git_head(Path(cwd))
            if proc_head and canonical_head:
                head_match = proc_head == canonical_head

        st = ServiceStatus(
            name=spec.name, unit=spec.unit, active=active, main_pid=pid,
            cwd=cwd, cmdline=cmdline, cwd_in_canonical=in_canon,
            process_head=proc_head, head_matches_canonical=head_match,
        )

        if active and pid and not in_canon:
            st.problems.append(
                f"进程 cwd={cwd} 不在 canonical checkout {self.checkout} 内"
            )
        if active and head_match is False:
            st.problems.append(
                f"运行中代码 {proc_head[:12]} 与工作树 {canonical_head[:12]} 不一致"
            )

        for port in spec.ports:
            listeners = self._conflicting_listeners(spec, port)
            pids = sorted({p for _, _, p in listeners})
            foreign = sorted({
                p for _, _, p in listeners
                if p != pid and not self._in_canonical(self._cwd_of_pid(p))
            })
            view = PortView(port=port, pids=pids, foreign_pids=foreign)
            if foreign:
                st.problems.append(
                    f"端口 {port} 被外来进程占用: pid={foreign}"
                )
            if active and not pids:
                st.problems.append(f"服务 active 但端口 {port} 无人监听")
            st.ports.append(view)

        if spec.health_url:
            code, _ = self._http(spec.health_url, 3.0)
            # 端口上有 HTTP 服务应答（含 401/404）即视为存活
            st.health = "ok" if code > 0 else "fail"
        elif spec.health_tcp:
            st.health = (
                "ok" if self._tcp(spec.health_tcp[0], spec.health_tcp[1], 3.0)
                else "fail"
            )
        if active and st.health == "fail":
            st.problems.append("健康检查失败")
        return st

    # ------------------------------------------------------------ 动作

    def _find(self, name: str) -> ServiceSpec:
        for spec in self.services:
            if spec.name == name:
                return spec
        known = ", ".join(s.name for s in self.services)
        raise RuntimeError_(f"未知服务 {name!r}（已登记：{known}）")

    def start(self, name: str) -> ServiceStatus:
        spec = self._find(name)
        active, pid = self._unit_state(spec.unit)
        if active:
            return self._service_status(spec, self._git_head(self.checkout))
        # 防重复/防分裂：端口被 canonical 之外的进程占用时拒绝启动
        for port in spec.ports:
            foreign = sorted({
                p for _, _, p in self._conflicting_listeners(spec, port)
                if not self._in_canonical(self._cwd_of_pid(p))
            })
            if foreign:
                raise RuntimeError_(
                    f"拒绝启动 {spec.name}：端口 {port} 被外来进程 {foreign} 占用；"
                    f"先确认其归属（cwd 不在 {self.checkout} 内）"
                )
        rc, _, err = self._run(["systemctl", "start", spec.unit])
        if rc != 0:
            raise RuntimeError_(f"systemctl start {spec.unit} 失败: {err.strip()}")
        return self._service_status(spec, self._git_head(self.checkout))

    def stop(self, name: str) -> ServiceStatus:
        spec = self._find(name)
        active, pid = self._unit_state(spec.unit)
        if not active or not pid:
            return self._service_status(spec, self._git_head(self.checkout))
        cwd = self._cwd_of_pid(pid)
        if not self._in_canonical(cwd):
            raise RuntimeError_(
                f"拒绝停止 {spec.name}：MainPID {pid} 的 cwd={cwd} 不在 "
                f"canonical checkout {self.checkout} 内——不是本工具登记的进程"
            )
        cmdline = self._cmdline_of(pid) or ""
        if spec.cmdline_marker and spec.cmdline_marker not in cmdline:
            raise RuntimeError_(
                f"拒绝停止 {spec.name}：MainPID {pid} 命令行不含登记标记 "
                f"{spec.cmdline_marker!r}（实际：{cmdline[:120]}）"
            )
        rc, _, err = self._run(["systemctl", "stop", spec.unit])
        if rc != 0:
            raise RuntimeError_(f"systemctl stop {spec.unit} 失败: {err.strip()}")
        return self._service_status(spec, self._git_head(self.checkout))


# ------------------------------------------------------------------- CLI 渲染


def render_text(report: RuntimeReport) -> str:
    lines = [
        f"canonical checkout: {report.checkout} @ "
        f"{(report.canonical_head or '?')[:12]}",
    ]
    for s in report.services:
        head = (s.process_head or "-")[:12]
        match = {True: "=", False: "≠", None: "?"}[s.head_matches_canonical]
        state = "active" if s.active else "inactive"
        lines.append(
            f"[{s.name}] {state} pid={s.main_pid or '-'} health={s.health}"
        )
        lines.append(f"  cwd: {s.cwd or '-'}")
        lines.append(f"  HEAD: {head} {match} checkout")
        if s.cmdline:
            lines.append(f"  cmd: {s.cmdline[:100]}")
        for p in s.ports:
            owners = ",".join(str(x) for x in p.pids) or "-"
            lines.append(f"  port {p.port}: pids={owners}")
        for prob in s.problems:
            lines.append(f"  [PROBLEM] {prob}")
    return "\n".join(lines)


def render_json(report: RuntimeReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
