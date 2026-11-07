"""SSH Transport（contracts/v2 第一轮闭环 · Device Gateway）。

以显式注入的 SSH 参数（host/port/username/key_path/jump/host key pinning）
管理每设备一条 SSH 连接，在其上实现 base.DeviceTransport：

- device.shell         远端 shell 执行（stdout/stderr 分流、退出码、可选 su -c root）
- device.file.read     SFTP 读（单次 ≤16MB，超限用 offset/length 分块）
- device.file.write    SFTP 写（≤16MB）
- device.file.list     SFTP 列目录
- device.logcat        Android logcat -d（设备可用时）
- device.stop          终止该设备全部在飞远端进程（取消语义）
- device.observe       降级观察：仅 Android 尽力截图，空元素表 + truncated；
                       非 Android 设备 FAILED/unavailable（不伪造观察）

安全约束：
- 仅密钥认证（password_auth / kbdint_auth 关闭，ssh-agent 不启用）；
- 主机密钥必须 pinning：构造参数给 expected_host_key（OpenSSH 公钥行）或
  host_key_provider 回调；二者皆无 → 构造期 ValueError，绝不 accept-any；
- 不读 ~/.ssh/config，不依赖运行环境隐式配置。

回执纪律（contracts/v2 §7）：超时回 TIMEOUT 不抛异常；连接失败回
FAILED/unavailable；未知能力回 FAILED/capability_unknown；取消回 CANCELLED。

大输出纪律（base.py docstring / §10）：stdout+stderr 超过 MAX_INLINE_OUTPUT
（64KiB）时尾部截断写入 OutputStore，回执只带 output_ref；SFTP 文件内容一律
落 OutputStore 不进 summary。本模块内置 MemoryOutputStore；生产可注入其它
实现（满足 OutputStore 协议即可）。

OutputStore 中 shell 类输出的打包格式（_pack_streams）：
    b"WSH1" + uint32be(len(stdout)) + stdout + stderr
read_output 返回的是该打包字节流的分页；用 unpack_streams() 还原。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import re
import shlex
import stat as statmod
import time

from dataclasses import dataclass, field
from typing import (Any, Awaitable, Callable, Dict, Mapping, Optional,
                    Protocol, Sequence, Tuple, runtime_checkable)

from .base import (ActionReceipt, ActionRequest, Observation, ObserveRequest,
                   ReceiptStatus, TransportHealth, UiElement, Verification,
                   VerificationStatus, new_id, now_ms)

try:  # 懒加载：asyncssh 是可选依赖（pyproject [project.optional-dependencies].ssh）
    import asyncssh  # type: ignore
except ImportError:  # pragma: no cover - 由构造期检查覆盖
    asyncssh = None  # type: ignore


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MAX_INLINE_OUTPUT = 64 * 1024          # shell 输出内联上限；超过尾部截断落 OutputStore
MAX_FILE_BYTES = 16 * 1024 * 1024      # SFTP 单次读写上限（分块边界）
MAX_SHELL_SESSIONS = 3                 # 每设备并发 shell 会话上限
SUMMARY_MAX = 2000                     # contracts/v2 §7.3：summary ≤2000 字符
_FALLBACK_CHUNK = 768 * 1024           # shell 降级写分块（768KiB 原始 → 1MiB base64）
_STORED_MAGIC = b"WSH1"                # OutputStore 打包格式头
_SENTINEL_RE_TEMPLATE = r"\n?{sentinel}(\d+):(\S+)\n?$"


class SshTransportError(Exception):
    """SSH transport 层错误。code 取 contracts/v2 §14 错误码枚举值。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# OutputStore（大输出外置存储；生产可替换为持久化实现）
# ---------------------------------------------------------------------------

@runtime_checkable
class OutputStore(Protocol):
    """大输出存储协议：ref → bytes。ref 形如 ``agent:out:<action_id>``。"""

    async def put(self, ref: str, data: bytes) -> None: ...

    async def get(self, ref: str) -> Optional[bytes]: ...


class MemoryOutputStore:
    """进程内 LRU OutputStore（默认实现；测试与单机部署够用）。"""

    def __init__(self, *, max_entries: int = 256, max_total_bytes: int = 64 * 1024 * 1024):
        self._max_entries = max_entries
        self._max_total = max_total_bytes
        self._data: Dict[str, bytes] = {}
        self._total = 0

    async def put(self, ref: str, data: bytes) -> None:
        if ref in self._data:
            self._total -= len(self._data.pop(ref))
        self._data[ref] = data
        self._total += len(data)
        # LRU 逐出最旧条目
        while len(self._data) > self._max_entries or self._total > self._max_total:
            oldest = next(iter(self._data))
            self._total -= len(self._data.pop(oldest))

    async def get(self, ref: str) -> Optional[bytes]:
        data = self._data.get(ref)
        if data is None:
            return None
        self._data[ref] = self._data.pop(ref)  # touch：移到最新
        return data


def _pack_streams(stdout: bytes, stderr: bytes) -> bytes:
    """shell 输出打包（stdout/stderr 分流不丢失）。"""
    return _STORED_MAGIC + len(stdout).to_bytes(4, "big") + stdout + stderr


def unpack_streams(data: bytes) -> Tuple[bytes, bytes]:
    """还原 _pack_streams 打包的 (stdout, stderr)。"""
    if not data.startswith(_STORED_MAGIC):
        raise ValueError("not a packed stream blob")
    n = int.from_bytes(data[4:8], "big")
    return data[8:8 + n], data[8 + n:]


# ---------------------------------------------------------------------------
# 设备目标配置（显式注入，不读 ssh config）
# ---------------------------------------------------------------------------

# host_key_provider(host, port) -> OpenSSH 公钥行（"ssh-ed25519 AAAA..."）；
# 返回 None 表示拒绝该主机（等价 host key 不匹配）。
HostKeyProvider = Callable[[str, int], Optional[str]]


@dataclass(frozen=True)
class SshDeviceTarget:
    """一台设备的 SSH 接入参数。"""

    host: str
    port: int = 22
    username: str = "root"
    key_path: str = ""
    expected_host_key: Optional[str] = None        # OpenSSH 公钥行，pinning
    host_key_provider: Optional[HostKeyProvider] = None  # 动态接受回调
    jump: Optional[str] = None                     # 跳板：另一 device_id
    env_prefix: str = ""                           # 每条命令前置 shell 片段
                                                   # （如 "export PATH=/system/bin:..."）
    connect_timeout: float = 10.0
    keepalive_interval: float = 30.0
    keepalive_count_max: int = 3


# ---------------------------------------------------------------------------
# 内部运行时状态
# ---------------------------------------------------------------------------

@dataclass
class _Inflight:
    """一个在飞远端进程（取消/断连时 terminate）。"""

    proc: Any
    cancelled: bool = False


@dataclass
class _ConnState:
    conn: Any
    sem: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(MAX_SHELL_SESSIONS))
    inflight: Dict[str, _Inflight] = field(default_factory=dict)
    is_android: Optional[bool] = None              # connect 时探测
    has_logcat: bool = False
    has_screencap: bool = False
    sftp_ok: Optional[bool] = None   # None=未探测；False=无 sftp-server（dropbear），
                                     # 文件操作降级为 shell base64 通道


@dataclass
class _RemoteResult:
    """一次远端命令执行结果（含超时/取消终态标记）。"""

    timed_out: bool = False
    cancelled: bool = False
    exit_code: Optional[int] = None
    uid: str = ""
    stdout: bytes = b""
    stderr: bytes = b""
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# SshTransport
# ---------------------------------------------------------------------------

class SshTransport:
    """base.DeviceTransport 的 SSH 实现。kind="ssh"。"""

    kind = "ssh"

    _BASE_CAPS = ("device.shell", "device.file.read", "device.file.write",
                  "device.file.list", "device.stop")

    def __init__(
        self,
        targets: Mapping[str, SshDeviceTarget],
        *,
        output_store: Optional[OutputStore] = None,
        connect_factory: Optional[Callable[..., Awaitable[Any]]] = None,
    ):
        """targets: device_id → SshDeviceTarget。

        connect_factory: 测试注入点，签名兼容 asyncssh.connect（async）。
        """
        self._targets = dict(targets)
        # 安全：主机密钥 pinning 是硬要求，缺配置直接构造失败（fail closed）
        for device_id, t in self._targets.items():
            if not t.expected_host_key and not t.host_key_provider:
                raise ValueError(
                    f"device {device_id!r}: 必须提供 expected_host_key 或 "
                    "host_key_provider（禁止 accept-any）")
            if t.jump and t.jump not in self._targets:
                raise ValueError(f"device {device_id!r}: 未知跳板 {t.jump!r}")
        self._store: OutputStore = output_store or MemoryOutputStore()
        self._connect_factory = connect_factory
        self._conns: Dict[str, _ConnState] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    # -- 连接管理 ----------------------------------------------------------

    def _target(self, device_id: str) -> SshDeviceTarget:
        try:
            return self._targets[device_id]
        except KeyError:
            raise SshTransportError("unavailable", f"未知设备 {device_id!r}")

    @staticmethod
    def _known_hosts_bytes(t: SshDeviceTarget) -> bytes:
        """把 pinning 的主机公钥构造成 known_hosts 数据（bytes）。

        known_hosts 绝不传 None（None 在 asyncssh 里等于不校验）。
        """
        key = t.expected_host_key
        if key is None and t.host_key_provider is not None:
            key = t.host_key_provider(t.host, t.port)
            if not key:
                raise SshTransportError(
                    "auth_failed", f"host key provider 拒绝 {t.host}:{t.port}")
        assert key is not None  # 构造期已校验
        key = key.strip()
        if " " not in key:
            raise ValueError("expected_host_key 须为 OpenSSH 公钥行 "
                             "（如 'ssh-ed25519 AAAA...'）")
        hostpat = t.host if t.port == 22 else f"[{t.host}]:{t.port}"
        return f"{hostpat} {key}\n".encode("ascii")

    async def _open_connection(self, device_id: str) -> _ConnState:
        t = self._target(device_id)
        if asyncssh is None and self._connect_factory is None:
            raise SshTransportError(
                "unavailable", "asyncssh 未安装（pip install whale-core[ssh]）")
        factory = self._connect_factory or asyncssh.connect
        kwargs: Dict[str, Any] = dict(
            host=t.host,
            port=t.port,
            username=t.username,
            client_keys=[t.key_path] if t.key_path else [],
            # 仅密钥认证：密码 / 键盘交互 / agent 全部关闭
            password_auth=False,
            kbdint_auth=False,
            agent_path=None,
            # 主机密钥 pinning（bytes 形式的 known_hosts 数据）
            known_hosts=self._known_hosts_bytes(t),
            connect_timeout=t.connect_timeout,
            keepalive_interval=t.keepalive_interval,
            keepalive_count_max=t.keepalive_count_max,
            login_timeout=t.connect_timeout,
        )
        if t.jump:
            jump_state = await self._ensure(device_id=t.jump)
            kwargs["tunnel"] = jump_state.conn
        try:
            conn = await factory(**kwargs)
        except SshTransportError:
            raise
        except Exception as exc:  # 连接失败（网络/认证/host key 拒绝）统一归类
            raise SshTransportError(
                "unavailable", f"SSH 连接 {t.host}:{t.port} 失败: {exc}") from exc
        st = _ConnState(conn=conn)
        await self._probe(st)
        return st

    async def _probe(self, st: _ConnState) -> None:
        """连接建立后探测 Android 环境（决定 logcat/observe 能力）。"""
        try:
            res = await self._run_remote_raw(
                st, probe_id="probe",
                command="command -v getprop >/dev/null 2>&1 && echo android; "
                        "command -v logcat >/dev/null 2>&1 && echo logcat; "
                        "command -v screencap >/dev/null 2>&1 && echo screencap",
                timeout_ms=5000)
        except SshTransportError:
            return  # 探测失败不阻断连接，按非 Android 处理
        out = res.stdout.decode("utf-8", "replace")
        st.is_android = "android" in out
        st.has_logcat = "logcat" in out
        st.has_screencap = "screencap" in out

    async def _ensure(self, device_id: str) -> _ConnState:
        st = self._conns.get(device_id)
        if st is not None:
            return st
        # 并发 _ensure 只建一条连接（每设备一条 SSH 连接是硬约束）
        lock = self._locks.setdefault(device_id, asyncio.Lock())
        async with lock:
            st = self._conns.get(device_id)
            if st is None:
                st = await self._open_connection(device_id)
                self._conns[device_id] = st
            return st

    async def connect(self, device_id: str) -> None:
        await self._ensure(device_id)

    async def disconnect(self, device_id: str) -> None:
        st = self._conns.pop(device_id, None)
        if st is None:
            return
        await self._cancel_inflight(st)          # 在飞命令 → CANCELLED
        try:
            st.conn.close()
            await asyncio.wait_for(st.conn.wait_closed(), timeout=5)
        except Exception:
            pass

    async def health(self, device_id: str) -> TransportHealth:
        st = self._conns.get(device_id)
        if st is None:
            return TransportHealth(connected=False, detail="not connected",
                                   checked_at=now_ms())
        try:
            res = await self._run_remote_raw(st, probe_id="health",
                                             command="true", timeout_ms=5000)
            ok = res.exit_code == 0
            return TransportHealth(connected=ok,
                                   detail="" if ok else "probe failed",
                                   checked_at=now_ms())
        except Exception as exc:
            self._conns.pop(device_id, None)     # 连接已死，下次用时重建
            return TransportHealth(connected=False, detail=str(exc),
                                   checked_at=now_ms())

    async def capabilities(self, device_id: str) -> Sequence[str]:
        caps = list(self._BASE_CAPS)
        st = self._conns.get(device_id)
        if st is not None and st.is_android:
            if st.has_logcat:
                caps.append("device.logcat")
            if st.has_screencap:
                caps.append("device.observe")
        return tuple(caps)

    # -- 远端命令执行核心 ----------------------------------------------------

    def _build_script(self, t: SshDeviceTarget, command: str,
                      working_dir: Optional[str], allow_root: bool,
                      sentinel: str) -> str:
        """组装远端脚本：env 前缀 + uid 捕获 + cd + 命令 + 退出码/uid 哨兵。

        哨兵行打印到 stderr，客户端据此拿到「真实退出码 + 真实执行 uid」，
        即使通道层 exit_status 丢失（su/被kill）也能对账。
        """
        prelude = []
        if t.env_prefix:
            prelude.append(t.env_prefix)
        prelude.append("_wu=$(id -u 2>/dev/null || echo x)")
        body = command
        if working_dir:
            body = f"cd {shlex.quote(working_dir)} && {{ {command} ; }}"
        inner = "; ".join(prelude) + "; " + body
        if allow_root:
            # shlex.quote：单引号包裹 + 内嵌单引号转义为 '"'"'，POSIX 安全
            run = f"su -c {shlex.quote(inner)}"
        else:
            run = inner
        return (f"{run}; _ec=$?; "
                f"printf '%s%s:%s\\n' '{sentinel}' \"$_ec\" \"$_wu\" >&2; "
                f"exit $_ec")

    @staticmethod
    def _parse_sentinel(stderr: bytes, sentinel: str,
                        fallback_exit: Optional[int]) -> Tuple[int, str, bytes]:
        """从 stderr 尾部剥离哨兵行，返回 (exit_code, uid, 干净 stderr)。"""
        text = stderr.decode("utf-8", "replace")
        m = re.search(_SENTINEL_RE_TEMPLATE.format(sentinel=re.escape(sentinel)),
                      text)
        if not m:
            # 哨兵丢失（进程被杀/通道异常）：退化为通道 exit_status
            return (fallback_exit if fallback_exit is not None else -1), "", stderr
        clean = text[:m.start()] + text[m.end():]
        return int(m.group(1)), m.group(2), clean.encode("utf-8", "replace")

    async def _terminate_proc(self, proc: Any) -> None:
        """取消语义：先 SIGTERM，1s 内不退出则 kill（关通道）。"""
        try:
            proc.terminate()
            await asyncio.wait_for(asyncio.shield(proc.wait(check=False)),
                                   timeout=1.0)
            return
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass

    async def _cancel_inflight(self, st: _ConnState) -> int:
        """终止该连接全部在飞进程；返回取消数量。"""
        n = 0
        for inf in list(st.inflight.values()):
            inf.cancelled = True
            await self._terminate_proc(inf.proc)
            n += 1
        return n

    async def _run_remote_raw(self, st: _ConnState, *, probe_id: str,
                              command: str, timeout_ms: int) -> _RemoteResult:
        """不经哨兵包装的裸执行（probe/health 用，无 uid 解析需求）。"""
        probe_id = f"{probe_id}-{time.monotonic_ns()}"  # 防并发健康检查撞键
        sentinel = f"__WHALE_RC_{hashlib.sha256(probe_id.encode()).hexdigest()[:8]}__"
        full = (f"{command}; _ec=$?; "
                f"printf '%s%s\\n' '{sentinel}' \"$_ec\" >&2; exit $_ec")
        res = await self._run_script(st, action_id=probe_id, full=full,
                                     timeout_ms=timeout_ms)
        if res.timed_out or res.cancelled:
            return res
        text = res.stderr.decode("utf-8", "replace")
        idx = text.rfind(sentinel)
        if idx >= 0:
            line = text[idx + len(sentinel):].strip()
            res.exit_code = int(line) if line.isdigit() else -1
            res.stderr = text[:idx].encode("utf-8", "replace")
        return res

    async def _run_script(self, st: _ConnState, *, action_id: str, full: str,
                          timeout_ms: int) -> _RemoteResult:
        """执行已组装脚本：并发上限、超时 kill、取消 terminate。"""
        started = time.monotonic()
        async with st.sem:
            try:
                proc = await st.conn.create_process(full, encoding=None)
            except SshTransportError:
                raise
            except Exception as exc:
                raise SshTransportError("unavailable",
                                        f"创建远端进程失败: {exc}") from exc
            inf = _Inflight(proc=proc)
            st.inflight[action_id] = inf
            try:
                completed = await proc.wait(check=False,
                                            timeout=max(timeout_ms, 1) / 1000)
                res = _RemoteResult(
                    exit_code=completed.exit_status,
                    stdout=completed.stdout or b"",
                    stderr=completed.stderr or b"",
                    duration_ms=int((time.monotonic() - started) * 1000))
                if inf.cancelled:
                    res.cancelled = True
                return res
            except asyncio.TimeoutError:
                await self._terminate_proc(proc)
                return _RemoteResult(
                    timed_out=True, cancelled=inf.cancelled,
                    duration_ms=int((time.monotonic() - started) * 1000))
            except asyncio.CancelledError:
                inf.cancelled = True
                await self._terminate_proc(proc)
                raise
            except Exception as exc:
                if inf.cancelled:
                    return _RemoteResult(cancelled=True)
                raise SshTransportError("unavailable",
                                        f"远端执行中断: {exc}") from exc
            finally:
                st.inflight.pop(action_id, None)

    async def _run_shell(self, st: _ConnState, t: SshDeviceTarget,
                         action: ActionRequest, command: str, *,
                         working_dir: Optional[str], allow_root: bool,
                         timeout_ms: int) -> _RemoteResult:
        sentinel = f"__WHALE_RC_{action.action_id.replace('-', '')[-12:]}__"
        full = self._build_script(t, command, working_dir, allow_root, sentinel)
        res = await self._run_script(st, action_id=action.action_id,
                                     full=full, timeout_ms=timeout_ms)
        if res.timed_out or res.cancelled:
            return res
        ec, uid, clean_err = self._parse_sentinel(res.stderr, sentinel,
                                                  res.exit_code)
        res.exit_code, res.uid, res.stderr = ec, uid, clean_err
        return res

    # -- 回执构造 ------------------------------------------------------------

    @staticmethod
    def _receipt(action: ActionRequest, status: ReceiptStatus, *,
                 summary: str = "", output_ref: Optional[str] = None,
                 error_code: Optional[str] = None,
                 provider_ref: Optional[str] = None,
                 verification: Optional[Verification] = None,
                 started_at: Optional[int] = None) -> ActionReceipt:
        return ActionReceipt(
            action_id=action.action_id, status=status,
            task_id=action.task_id, summary=summary[:SUMMARY_MAX],
            output_ref=output_ref, error_code=error_code,
            provider_ref=provider_ref, verification=verification,
            started_at=started_at, completed_at=now_ms())

    async def _shell_receipt(self, action: ActionRequest, res: _RemoteResult,
                             *, provider_ref: str,
                             started_at: int) -> ActionReceipt:
        if res.cancelled:
            return self._receipt(action, ReceiptStatus.CANCELLED,
                                 summary="已取消", error_code="cancelled",
                                 provider_ref=provider_ref,
                                 started_at=started_at)
        if res.timed_out:
            return self._receipt(action, ReceiptStatus.TIMEOUT,
                                 summary="执行超时（远端进程已终止）",
                                 error_code="timeout",
                                 provider_ref=provider_ref,
                                 started_at=started_at)
        stdout, stderr = res.stdout, res.stderr
        total = len(stdout) + len(stderr)
        truncated = total > MAX_INLINE_OUTPUT
        output_ref = None
        if truncated:
            # 尾部截断：保留 stderr 末尾 ≤16KiB，其余额度给 stdout 末尾
            err_keep = stderr[-16 * 1024:]
            out_keep = stdout[-(MAX_INLINE_OUTPUT - len(err_keep)):]
            output_ref = f"agent:out:{action.action_id}"
            await self._store.put(output_ref, _pack_streams(out_keep, err_keep))
        exit_code = res.exit_code if res.exit_code is not None else -1
        summary = json.dumps({
            "exit_code": exit_code,
            "uid": res.uid,
            "duration_ms": res.duration_ms,
            "stdout_tail": stdout.decode("utf-8", "replace")[-500:],
            "stderr_tail": stderr.decode("utf-8", "replace")[-300:],
            "truncated": truncated,
            "original_bytes": total,
        }, ensure_ascii=False)
        status = ReceiptStatus.SUCCEEDED if exit_code == 0 else ReceiptStatus.FAILED
        return self._receipt(
            action, status, summary=summary, output_ref=output_ref,
            error_code=None if exit_code == 0 else "internal",
            provider_ref=provider_ref, started_at=started_at)

    # -- execute 分发 ---------------------------------------------------------

    async def execute(self, device_id: str, action: ActionRequest) -> ActionReceipt:
        started_at = now_ms()
        handler = {
            "device.shell": self._exec_shell,
            "device.file.read": self._exec_file_read,
            "device.file.write": self._exec_file_write,
            "device.file.list": self._exec_file_list,
            "device.logcat": self._exec_logcat,
            "device.stop": self._exec_stop,
            "device.observe": self._exec_observe,
        }.get(action.capability)
        if handler is None:
            return self._receipt(action, ReceiptStatus.FAILED,
                                 summary=f"SSH transport 不支持 {action.capability}",
                                 error_code="capability_unknown",
                                 started_at=started_at)
        try:
            return await handler(device_id, action, started_at)
        except SshTransportError as exc:
            status = (ReceiptStatus.FAILED if exc.code != "timeout"
                      else ReceiptStatus.TIMEOUT)
            return self._receipt(action, status, summary=str(exc),
                                 error_code=exc.code, started_at=started_at)
        except Exception as exc:  # 兜底：execute 绝不把异常抛给调用方
            return self._receipt(action, ReceiptStatus.FAILED,
                                 summary=f"内部错误: {exc}",
                                 error_code="internal", started_at=started_at)

    # -- device.shell ---------------------------------------------------------

    async def _exec_shell(self, device_id: str, action: ActionRequest,
                          started_at: int) -> ActionReceipt:
        args = action.arguments
        command = args.get("command")
        if not isinstance(command, str) or not command:
            return self._receipt(action, ReceiptStatus.FAILED,
                                 summary="缺少 arguments.command",
                                 error_code="invalid_message",
                                 started_at=started_at)
        t = self._target(device_id)
        st = await self._ensure(device_id)
        timeout_ms = int(args.get("timeout_ms") or action.timeout_ms)
        allow_root = bool(args.get("allow_root", False))
        working_dir = args.get("working_dir")
        res = await self._run_shell(st, t, action, command,
                                    working_dir=working_dir,
                                    allow_root=allow_root,
                                    timeout_ms=timeout_ms)
        # provider_ref 诚实性注解：SSH 登录身份 + 真实执行 uid + 是否经 su
        via = " su" if allow_root else ""
        provider_ref = f"ssh://{t.username}@{t.host}:{t.port} uid={res.uid}{via}"
        return await self._shell_receipt(action, res,
                                         provider_ref=provider_ref,
                                         started_at=started_at)

    # -- device.logcat ---------------------------------------------------------

    async def _exec_logcat(self, device_id: str, action: ActionRequest,
                           started_at: int) -> ActionReceipt:
        st = await self._ensure(device_id)
        if not st.has_logcat:
            return self._receipt(action, ReceiptStatus.FAILED,
                                 summary="设备无 logcat 命令（非 Android 环境）",
                                 error_code="unavailable",
                                 started_at=started_at)
        args = action.arguments
        lines = int(args.get("lines", 500))
        lines = max(1, min(lines, 100_000))
        extra = args.get("args") or []
        if isinstance(extra, str):
            extra = shlex.split(extra)
        cmd = " ".join(["logcat", "-d", "-t", str(lines)]
                       + [shlex.quote(str(a)) for a in extra])
        t = self._target(device_id)
        timeout_ms = int(args.get("timeout_ms") or action.timeout_ms)
        res = await self._run_shell(st, t, action, cmd, working_dir=None,
                                    allow_root=bool(args.get("allow_root")),
                                    timeout_ms=timeout_ms)
        return await self._shell_receipt(
            action, res,
            provider_ref=f"ssh://{t.username}@{t.host}:{t.port} logcat",
            started_at=started_at)

    # -- device.stop -------------------------------------------------------------

    async def _exec_stop(self, device_id: str, action: ActionRequest,
                         started_at: int) -> ActionReceipt:
        st = self._conns.get(device_id)
        n = await self._cancel_inflight(st) if st is not None else 0
        return self._receipt(action, ReceiptStatus.SUCCEEDED,
                             summary=json.dumps({"cancelled": n}),
                             started_at=started_at)

    # -- device.file.*（SFTP，无 sftp-server 时降级 shell base64 通道） --------------

    async def _get_sftp(self, st: _ConnState):
        """返回可用 SFTPClient；子系统不可用返回 None 并缓存探测结果。"""
        if st.sftp_ok is False:
            return None
        try:
            sftp = await st.conn.start_sftp_client()
            st.sftp_ok = True
            return sftp
        except Exception:
            st.sftp_ok = False  # dropbear 等无 sftp-server → 走 shell 降级
            return None

    async def _exec_file_read(self, device_id: str, action: ActionRequest,
                              started_at: int) -> ActionReceipt:
        args = action.arguments
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return self._receipt(action, ReceiptStatus.FAILED,
                                 summary="缺少 arguments.path",
                                 error_code="invalid_message",
                                 started_at=started_at)
        offset = max(0, int(args.get("offset", 0)))
        length = min(max(1, int(args.get("length", MAX_FILE_BYTES))),
                     MAX_FILE_BYTES)  # 单次 ≤16MB；更大文件用 offset/length 分块
        t = self._target(device_id)
        st = await self._ensure(device_id)
        timeout_ms = int(args.get("timeout_ms") or action.timeout_ms)
        sftp = await self._get_sftp(st)
        if sftp is None:
            return await self._file_read_shell(st, t, action, path, offset,
                                               length, timeout_ms, started_at)
        timeout_s = max(timeout_ms, 1) / 1000

        async def _read() -> Tuple[int, bytes]:
            attrs = await sftp.stat(path)
            if attrs.permissions is not None and statmod.S_ISDIR(
                    attrs.permissions):
                raise SshTransportError("invalid_message",
                                        f"{path} 是目录，请用 device.file.list")
            async with sftp.open(path, "rb") as f:
                await f.seek(offset)
                return attrs.size or 0, await f.read(length)

        try:
            async with sftp:
                size, data = await asyncio.wait_for(_read(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return self._receipt(action, ReceiptStatus.TIMEOUT,
                                 summary="SFTP 读超时", error_code="timeout",
                                 started_at=started_at)
        except SshTransportError:
            raise
        except Exception as exc:
            return self._receipt(action, ReceiptStatus.FAILED,
                                 summary=f"SFTP 读失败: {exc}",
                                 error_code="unavailable", started_at=started_at)
        return await self._file_read_receipt(action, path, size, offset, data,
                                             provider_ref="sftp",
                                             started_at=started_at)

    async def _file_read_shell(self, st: _ConnState, t: SshDeviceTarget,
                               action: ActionRequest, path: str, offset: int,
                               length: int, timeout_ms: int,
                               started_at: int) -> ActionReceipt:
        """SFTP 不可用时的降级读：tail/head 切片 + base64 通道。"""
        q = shlex.quote(path)
        res = await self._run_shell(st, t, action, f"test -d {q}",
                                    working_dir=None, allow_root=False,
                                    timeout_ms=timeout_ms)
        if res.timed_out or res.cancelled:
            return await self._shell_receipt(
                action, res, provider_ref="degraded:shell_fallback",
                started_at=started_at)
        if res.exit_code == 0:
            return self._receipt(action, ReceiptStatus.FAILED,
                                 summary=f"{path} 是目录，请用 device.file.list",
                                 error_code="invalid_message",
                                 started_at=started_at)
        res = await self._run_shell(
            st, t, action, f"stat -c %s {q} 2>/dev/null || wc -c < {q}",
            working_dir=None, allow_root=False, timeout_ms=timeout_ms)
        try:
            size = int(res.stdout.decode("utf-8", "replace").strip())
        except ValueError:
            return self._receipt(action, ReceiptStatus.FAILED,
                                 summary=f"无法读取 {path}（不存在或无权限）",
                                 error_code="unavailable",
                                 started_at=started_at)
        res = await self._run_shell(
            st, t, action,
            f"tail -c +{offset + 1} -- {q} | head -c {length} | base64",
            working_dir=None, allow_root=False, timeout_ms=timeout_ms)
        if res.timed_out or res.cancelled:
            return await self._shell_receipt(
                action, res, provider_ref="degraded:shell_fallback",
                started_at=started_at)
        if res.exit_code != 0:
            return self._receipt(action, ReceiptStatus.FAILED,
                                 summary=f"读取失败: "
                                         f"{res.stderr.decode('utf-8', 'replace')[-200:]}",
                                 error_code="unavailable",
                                 started_at=started_at)
        try:
            data = base64.b64decode(res.stdout)
        except (binascii.Error, ValueError):
            return self._receipt(action, ReceiptStatus.FAILED,
                                 summary="base64 通道解码失败",
                                 error_code="internal", started_at=started_at)
        return await self._file_read_receipt(
            action, path, size, offset, data,
            provider_ref="degraded:shell_fallback", started_at=started_at)

    async def _file_read_receipt(self, action: ActionRequest, path: str,
                                 size: int, offset: int, data: bytes,
                                 *, provider_ref: str,
                                 started_at: int) -> ActionReceipt:
        eof = offset + len(data) >= size
        ref = f"agent:out:{action.action_id}"
        await self._store.put(ref, data)  # 文件内容一律外置（二进制安全）
        summary = json.dumps({
            "path": path, "size": size, "offset": offset,
            "length": len(data), "eof": eof,
            "truncated": not eof,          # 未读到尾 → 调用方用 offset 续读
            "sha256": hashlib.sha256(data).hexdigest(),
        }, ensure_ascii=False)
        return self._receipt(action, ReceiptStatus.SUCCEEDED, summary=summary,
                             output_ref=ref, provider_ref=provider_ref,
                             started_at=started_at)

    async def _exec_file_write(self, device_id: str, action: ActionRequest,
                               started_at: int) -> ActionReceipt:
        args = action.arguments
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return self._receipt(action, ReceiptStatus.FAILED,
                                 summary="缺少 arguments.path",
                                 error_code="invalid_message",
                                 started_at=started_at)
        if "data_b64" in args:
            try:
                data = base64.b64decode(args["data_b64"], validate=True)
            except (binascii.Error, ValueError):
                return self._receipt(action, ReceiptStatus.FAILED,
                                     summary="data_b64 非法",
                                     error_code="invalid_message",
                                     started_at=started_at)
        elif "text" in args:
            data = str(args["text"]).encode("utf-8")
        else:
            return self._receipt(action, ReceiptStatus.FAILED,
                                 summary="需要 data_b64 或 text",
                                 error_code="invalid_message",
                                 started_at=started_at)
        if len(data) > MAX_FILE_BYTES:
            return self._receipt(action, ReceiptStatus.FAILED,
                                 summary=f"文件超过 {MAX_FILE_BYTES} 字节上限",
                                 error_code="invalid_message",
                                 started_at=started_at)
        t = self._target(device_id)
        st = await self._ensure(device_id)
        timeout_ms = int(args.get("timeout_ms") or action.timeout_ms)
        sftp = await self._get_sftp(st)
        if sftp is None:
            return await self._file_write_shell(st, t, action, path, data,
                                                timeout_ms, started_at)
        timeout_s = max(timeout_ms, 1) / 1000

        async def _write() -> None:
            async with sftp.open(path, "wb") as f:
                await f.write(data)

        try:
            async with sftp:
                await asyncio.wait_for(_write(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return self._receipt(action, ReceiptStatus.TIMEOUT,
                                 summary="SFTP 写超时", error_code="timeout",
                                 started_at=started_at)
        except Exception as exc:
            return self._receipt(action, ReceiptStatus.FAILED,
                                 summary=f"SFTP 写失败: {exc}",
                                 error_code="unavailable", started_at=started_at)
        summary = json.dumps({"path": path, "bytes": len(data),
                              "sha256": hashlib.sha256(data).hexdigest()},
                             ensure_ascii=False)
        return self._receipt(action, ReceiptStatus.SUCCEEDED, summary=summary,
                             provider_ref="sftp", started_at=started_at)

    async def _file_write_shell(self, st: _ConnState, t: SshDeviceTarget,
                                action: ActionRequest, path: str, data: bytes,
                                timeout_ms: int,
                                started_at: int) -> ActionReceipt:
        """SFTP 不可用时的降级写：base64 分块（每块 768KiB 原始）经 shell 管道。"""
        q = shlex.quote(path)
        first = True
        for off in range(0, max(len(data), 1), _FALLBACK_CHUNK):
            chunk = data[off:off + _FALLBACK_CHUNK]
            b64 = base64.b64encode(chunk).decode("ascii")  # 仅 [A-Za-z0-9+/=]，单引号安全
            redir = ">" if first else ">>"
            first = False
            res = await self._run_shell(
                st, t, action,
                f"printf '%s' '{b64}' | base64 -d {redir} {q}",
                working_dir=None, allow_root=False, timeout_ms=timeout_ms)
            if res.timed_out or res.cancelled:
                return await self._shell_receipt(
                    action, res, provider_ref="degraded:shell_fallback",
                    started_at=started_at)
            if res.exit_code != 0:
                return self._receipt(
                    action, ReceiptStatus.FAILED,
                    summary=f"降级写失败: "
                            f"{res.stderr.decode('utf-8', 'replace')[-200:]}",
                    error_code="unavailable", started_at=started_at)
        summary = json.dumps({"path": path, "bytes": len(data),
                              "sha256": hashlib.sha256(data).hexdigest()},
                             ensure_ascii=False)
        return self._receipt(action, ReceiptStatus.SUCCEEDED, summary=summary,
                             provider_ref="degraded:shell_fallback",
                             started_at=started_at)

    async def _exec_file_list(self, device_id: str, action: ActionRequest,
                              started_at: int) -> ActionReceipt:
        args = action.arguments
        path = args.get("path") or "."
        t = self._target(device_id)
        st = await self._ensure(device_id)
        timeout_ms = int(args.get("timeout_ms") or action.timeout_ms)
        sftp = await self._get_sftp(st)
        if sftp is None:
            return await self._file_list_shell(st, t, action, path,
                                               timeout_ms, started_at)
        timeout_s = max(timeout_ms, 1) / 1000

        async def _list() -> list:
            return await sftp.readdir(path)

        try:
            async with sftp:
                entries = await asyncio.wait_for(_list(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return self._receipt(action, ReceiptStatus.TIMEOUT,
                                 summary="SFTP 列目录超时", error_code="timeout",
                                 started_at=started_at)
        except Exception as exc:
            return self._receipt(action, ReceiptStatus.FAILED,
                                 summary=f"SFTP 列目录失败: {exc}",
                                 error_code="unavailable", started_at=started_at)
        items = []
        for e in entries:
            a = e.attrs
            perm = a.permissions
            kind = ("dir" if perm is not None and statmod.S_ISDIR(perm)
                    else "file")
            items.append({"name": e.filename, "type": kind,
                          "size": a.size, "mtime": a.mtime})
        return await self._file_list_receipt(action, path, items,
                                             provider_ref="sftp",
                                             started_at=started_at)

    async def _file_list_receipt(self, action: ActionRequest, path: str,
                                 items: list, *, provider_ref: str,
                                 started_at: int) -> ActionReceipt:
        """列目录回执：条目多时完整 JSON 落 OutputStore，summary 保持 ≤2000 合法 JSON。"""
        payload = json.dumps({"path": path, "count": len(items),
                              "entries": items}, ensure_ascii=False)
        output_ref = None
        if len(payload) > SUMMARY_MAX - 200:   # 留余量，保证 summary 是完整 JSON
            output_ref = f"agent:out:{action.action_id}"
            await self._store.put(output_ref, payload.encode("utf-8"))
            payload = json.dumps({"path": path, "count": len(items),
                                  "truncated": True}, ensure_ascii=False)
        return self._receipt(action, ReceiptStatus.SUCCEEDED, summary=payload,
                             output_ref=output_ref, provider_ref=provider_ref,
                             started_at=started_at)

    async def _file_list_shell(self, st: _ConnState, t: SshDeviceTarget,
                               action: ActionRequest, path: str,
                               timeout_ms: int,
                               started_at: int) -> ActionReceipt:
        """SFTP 不可用时的降级列目录：stat 循环（toybox 兼容）。"""
        q = shlex.quote(path)
        cmd = (f"cd {q} && for f in ./* ./.[!.]*; do "
               f"[ -e \"$f\" ] || continue; "
               f"stat -c '%n|%s|%Y|%F' \"$f\" 2>/dev/null; done")
        res = await self._run_shell(st, t, action, cmd, working_dir=None,
                                    allow_root=False, timeout_ms=timeout_ms)
        if res.timed_out or res.cancelled:
            return await self._shell_receipt(
                action, res, provider_ref="degraded:shell_fallback",
                started_at=started_at)
        if res.exit_code != 0:
            return self._receipt(action, ReceiptStatus.FAILED,
                                 summary=f"列目录失败: "
                                         f"{res.stderr.decode('utf-8', 'replace')[-200:]}",
                                 error_code="unavailable", started_at=started_at)
        items = []
        for line in res.stdout.decode("utf-8", "replace").splitlines():
            parts = line.rsplit("|", 3)   # 名字里可能含 '|'，从右切
            if len(parts) != 4:
                continue
            name, size_s, mtime_s, kind = parts
            name = name[2:] if name.startswith("./") else name
            try:
                size_v, mtime_v = int(size_s), int(mtime_s)
            except ValueError:
                size_v, mtime_v = None, None
            items.append({"name": name,
                          "type": "dir" if "dir" in kind else "file",
                          "size": size_v, "mtime": mtime_v})
        return await self._file_list_receipt(
            action, path, items, provider_ref="degraded:shell_fallback",
            started_at=started_at)

    # -- observe / device.observe -------------------------------------------------

    async def observe(self, device_id: str,
                      request: ObserveRequest) -> Observation:
        """降级观察：SSH 无 UI 语义树。

        Android（有 screencap）：尽力返回截图 ref + 空元素表 + truncated=True；
        非 Android：抛 SshTransportError("unavailable")，由上层映射为
        FAILED/unavailable，绝不伪造观察。
        """
        t = self._target(device_id)
        st = await self._ensure(device_id)
        if not st.has_screencap:
            raise SshTransportError(
                "unavailable", "设备无 screencap（非 Android），observe 不可用")
        obs_id = new_id("obs")
        screenshot_ref = None
        screenshot_sig = None
        if request.screenshot:
            res = await self._run_shell(st, t, _NullAction(obs_id),
                                        "screencap -p", working_dir=None,
                                        allow_root=False, timeout_ms=15000)
            if res.exit_code == 0 and res.stdout:
                screenshot_ref = f"agent:shot:{obs_id}"
                await self._store.put(screenshot_ref, res.stdout)
                screenshot_sig = self._screenshot_signature(res.stdout)
        width, height = await self._screen_size(st, t)
        package, activity = await self._foreground(st, t)
        # 空元素表：SSH 通道拿不到语义树，诚实标记 truncated
        ui_sig = hashlib.sha256(json.dumps([]).encode()).hexdigest()[:16]
        return Observation(
            observation_id=obs_id, device_id=device_id, taken_at=now_ms(),
            foreground_package=package, activity=activity,
            elements=(), ui_signature=ui_sig,
            screenshot_ref=screenshot_ref,
            screenshot_signature=screenshot_sig,
            screen_width=width, screen_height=height,
            truncated=True)

    async def _screen_size(self, st: _ConnState, t: SshDeviceTarget) -> Tuple[int, int]:
        try:
            res = await self._run_shell(st, t, _NullAction("size"),
                                        "wm size", working_dir=None,
                                        allow_root=False, timeout_ms=8000)
            m = re.search(r"(\d+)x(\d+)", res.stdout.decode("utf-8", "replace"))
            if m:
                return int(m.group(1)), int(m.group(2))
        except SshTransportError:
            pass
        return 0, 0

    async def _foreground(self, st: _ConnState,
                          t: SshDeviceTarget) -> Tuple[str, Optional[str]]:
        try:
            res = await self._run_shell(
                st, t, _NullAction("fg"),
                "dumpsys activity activities 2>/dev/null | "
                "grep -m1 -E 'mResumedActivity|topResumedActivity'",
                working_dir=None, allow_root=False, timeout_ms=8000)
            # 形如: mResumedActivity: ActivityRecord{... u0 com.pkg/.MainActivity t123}
            m = re.search(r"([a-zA-Z0-9_.]+)/([a-zA-Z0-9_.$]+)",
                          res.stdout.decode("utf-8", "replace"))
            if m:
                return m.group(1), m.group(2)
        except SshTransportError:
            pass
        return "", None

    @staticmethod
    def _screenshot_signature(png: bytes) -> str:
        """32x32 灰度签名（屏稳检测）；无 PIL 时退化为内容哈希。"""
        try:
            import io
            from PIL import Image  # type: ignore
            img = Image.open(io.BytesIO(png)).convert("L").resize((32, 32))
            return bytes(img.tobytes()).hex()
        except Exception:
            return hashlib.sha256(png).hexdigest()

    async def _exec_observe(self, device_id: str, action: ActionRequest,
                            started_at: int) -> ActionReceipt:
        args = action.arguments
        req = ObserveRequest(
            screenshot=bool(args.get("screenshot", True)),
            ui_tree=bool(args.get("ui_tree", True)),
            max_elements=int(args.get("max_elements", 400)))
        try:
            obs = await self.observe(device_id, req)
        except SshTransportError as exc:
            return self._receipt(action, ReceiptStatus.FAILED,
                                 summary=str(exc), error_code=exc.code,
                                 started_at=started_at)
        summary = json.dumps({
            "observation_id": obs.observation_id,
            "screenshot_ref": obs.screenshot_ref,
            "screen": f"{obs.screen_width}x{obs.screen_height}",
            "foreground_package": obs.foreground_package,
            "activity": obs.activity,
            "elements": 0,
            "truncated": True,   # 空元素表：UI 语义树不可用，如实标记
        }, ensure_ascii=False)
        return self._receipt(
            action, ReceiptStatus.SUCCEEDED, summary=summary,
            verification=Verification(VerificationStatus.SKIPPED,
                                      "SSH 通道无 UI 语义树"),
            started_at=started_at)

    # -- read_output ---------------------------------------------------------------

    async def read_output(self, device_id: str, output_ref: str, *,
                          offset: int = 0,
                          max_bytes: int = 65536) -> Tuple[bytes, bool]:
        data = await self._store.get(output_ref)
        if data is None:
            raise SshTransportError("unavailable",
                                    f"未知 output_ref {output_ref!r}")
        chunk = data[offset:offset + max_bytes]
        return chunk, offset + len(chunk) < len(data)


class _NullAction:
    """observe 内部复用 _run_shell 时的伪 action（仅需 action_id）。"""

    __slots__ = ("action_id", "timeout_ms", "task_id")

    def __init__(self, action_id: str):
        self.action_id = action_id
        self.timeout_ms = 10_000
        self.task_id = None
