"""SSH Transport 单元测试（fake asyncssh，不依赖真机/网络）。

覆盖：命令超时、输出截断与 output_ref、退出码非零→failed、su 包裹转义、
SFTP 大小上限、并发上限 ≤3、host key pinning/拒绝、取消（device.stop /
disconnect）、observe 降级、未知能力。

环境无 pytest-asyncio：每个用例用 run()（asyncio.run）驱动。
"""

import asyncio
import base64
import json
import re
import shlex
import stat as statmod

from types import SimpleNamespace

import pytest

from whale_core.devices.transports import ssh as sshmod
from whale_core.devices.transports.base import (ObserveRequest, ReceiptStatus,
                                                ActionRequest)
from whale_core.devices.transports.ssh import (MemoryOutputStore,
                                               SshDeviceTarget,
                                               SshTransport,
                                               SshTransportError,
                                               unpack_streams)

HOST_KEY = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFake"
            "FakeFakeFakeFakeFakeFakeFakeFakeFakeF")


def run(coro):
    return asyncio.run(coro)


def make_action(capability, arguments=None, action_id="act_TEST01",
                timeout_ms=10_000):
    return ActionRequest(action_id=action_id, capability=capability,
                         arguments=arguments or {},
                         idempotency_key="idem-" + action_id,
                         timeout_ms=timeout_ms)


# ---------------------------------------------------------------------------
# Fake asyncssh 连接
# ---------------------------------------------------------------------------

class FakeCompleted:
    def __init__(self, exit_status, stdout, stderr):
        self.exit_status = exit_status
        self.stdout = stdout
        self.stderr = stderr


class FakeProc:
    """模拟 SSHClientProcess：delay 秒后完成；terminate/kill 立即结束。"""

    def __init__(self, conn, spec):
        self._conn = conn
        self._spec = spec
        self.exit_status = spec.get("ec", 0)
        self.terminated = False
        self.killed = False
        self._counted = True
        self._done = asyncio.Event()
        self._timer = None
        delay = spec.get("delay")
        if delay is None:
            self._done.set()
        else:
            self._timer = asyncio.get_running_loop().call_later(
                delay, self._done.set)

    def _finish(self, exit_status):
        if self._timer is not None:
            self._timer.cancel()
        self.exit_status = exit_status
        self._done.set()

    def terminate(self):
        self.terminated = True
        self._finish(-15)

    def kill(self):
        self.killed = True
        self._finish(-9)

    async def wait(self, check=False, timeout=None):
        # shield：transport 超时时 wait_for 取消不应取消 _done 等待本身
        await asyncio.wait_for(asyncio.shield(self._done.wait()), timeout)
        if self._counted:
            self._counted = False
            self._conn._active -= 1
        return FakeCompleted(self.exit_status, self._spec.get("out", b""),
                             self._spec.get("err", b""))


class FakeSFTPFile:
    def __init__(self, sftp, path, mode):
        self._sftp = sftp
        self._path = path
        self._mode = mode
        self._pos = 0
        self._buf = bytearray(sftp.files.get(path, b""))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        if "w" in self._mode:
            self._sftp.files[self._path] = bytes(self._buf)
        return False

    async def seek(self, offset):
        self._pos = offset

    async def read(self, size=-1):
        data = bytes(self._buf[self._pos:] if size < 0
                     else self._buf[self._pos:self._pos + size])
        self._pos += len(data)
        return data

    async def write(self, data):
        end = self._pos + len(data)
        if end > len(self._buf):
            self._buf.extend(b"\x00" * (end - len(self._buf)))
        self._buf[self._pos:end] = data
        self._pos = end


class FakeSFTP:
    def __init__(self):
        self.files = {}        # path -> bytes
        self.dirs = {}         # path -> [name]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def stat(self, path):
        if path in self.files:
            return SimpleNamespace(size=len(self.files[path]), mtime=1,
                                   permissions=statmod.S_IFREG | 0o644)
        if path in self.dirs:
            return SimpleNamespace(size=0, mtime=1,
                                   permissions=statmod.S_IFDIR | 0o755)
        raise FileNotFoundError(path)

    def open(self, path, mode):
        # 与真 asyncssh 一致：同步返回 async context manager
        if "r" in mode and path not in self.files:
            raise FileNotFoundError(path)
        return FakeSFTPFile(self, path, mode)

    async def readdir(self, path):
        if path not in self.dirs:
            raise FileNotFoundError(path)
        return [SimpleNamespace(filename=n,
                                attrs=SimpleNamespace(
                                    size=1, mtime=2,
                                    permissions=statmod.S_IFREG | 0o644))
                for n in self.dirs[path]]


_SENTINEL_RE = re.compile(r"printf '%s%s:%s\\n' '([^']+)'")


def shell_spec(command, *, ec=0, out=b"", err=b"", uid="0", delay=None):
    """按 transport 包装后的命令，模拟远端执行结果（含哨兵行）。"""
    m = _SENTINEL_RE.search(command)
    if m:  # device.shell/logcat 路径：stderr 尾部补哨兵
        err = err + f"\n{m.group(1)}{ec}:{uid}\n".encode()
    return {"ec": ec, "out": out, "err": err, "delay": delay}


def make_handler(shell_responder=None, *, android=True):
    """shell_responder(command) -> spec dict；默认 exit 0 空输出。"""

    def handler(command):
        if "command -v getprop" in command:      # connect 时的 Android 探测
            out = b"android\nlogcat\nscreencap\n" if android else b""
            return {"ec": 0, "out": out}
        if shell_responder is not None:
            return shell_responder(command)
        return shell_spec(command)

    return handler


class FakeConn:
    def __init__(self, handler, *, sftp_broken=False):
        self.handler = handler
        self.commands = []       # 捕获全部 create_process 命令
        self.procs = []
        self._active = 0
        self.max_active = 0
        self.sftp = FakeSFTP()
        self.sftp_broken = sftp_broken   # 模拟 dropbear 无 sftp-server
        self.closed = False

    async def create_process(self, command, **kw):
        self.commands.append(command)
        proc = FakeProc(self, self.handler(command))
        self.procs.append(proc)
        self._active += 1
        self.max_active = max(self.max_active, self._active)
        return proc

    async def start_sftp_client(self):
        if self.sftp_broken:
            raise RuntimeError("SFTPConnectionLost: no sftp-server")
        return self.sftp

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


def make_transport(handler=None, *, android=True, extra_targets=None,
                   sftp_broken=False):
    """构造带 fake 连接的 transport；返回 (transport, conns, connect_calls)。"""
    conns = {}
    connect_calls = []

    async def factory(**kwargs):
        connect_calls.append(kwargs)
        conn = FakeConn(handler or make_handler(android=android),
                        sftp_broken=sftp_broken)
        conns[kwargs["host"]] = conn
        return conn

    targets = {"dev1": SshDeviceTarget(
        host="fakehost", port=2222, username="root", key_path="/root/.ssh/t",
        expected_host_key=HOST_KEY)}
    if extra_targets:
        targets.update(extra_targets)
    return SshTransport(targets, connect_factory=factory), conns, connect_calls


# ---------------------------------------------------------------------------
# 安全：host key pinning / 仅密钥认证
# ---------------------------------------------------------------------------

def test_host_key_required_at_construction():
    with pytest.raises(ValueError):
        SshTransport({"d": SshDeviceTarget(host="h", key_path="/k")})


def test_host_key_provider_rejects():
    target = SshDeviceTarget(host="h", key_path="/k",
                             host_key_provider=lambda h, p: None)
    t = SshTransport({"d": target}, connect_factory=None)
    with pytest.raises(SshTransportError) as ei:
        run(t.connect("d"))
    assert ei.value.code == "auth_failed"


def test_connect_key_only_auth_and_pinning():
    t, _, calls = make_transport()
    run(t.connect("dev1"))
    assert len(calls) == 1
    kw = calls[0]
    assert kw["password_auth"] is False
    assert kw["kbdint_auth"] is False
    assert kw["agent_path"] is None               # 不启用 ssh-agent
    assert kw["client_keys"] == ["/root/.ssh/t"]
    # known_hosts 是 pinning 数据，绝不为 None（None = accept-any）
    kh = kw["known_hosts"]
    assert isinstance(kh, bytes)
    assert b"[fakehost]:2222 ssh-ed25519" in kh
    assert HOST_KEY.split()[1].encode() in kh


def test_host_key_mismatch_connect_fails():
    import asyncssh

    async def factory(**kwargs):
        raise asyncssh.HostKeyNotVerifiable("host key mismatch")

    target = SshDeviceTarget(host="h", key_path="/k",
                             expected_host_key=HOST_KEY)
    t = SshTransport({"d": target}, connect_factory=factory)
    with pytest.raises(SshTransportError) as ei:
        run(t.connect("d"))
    assert ei.value.code == "unavailable"
    # execute 路径同样映射为 FAILED/unavailable
    receipt = run(t.execute("d", make_action("device.shell",
                                             {"command": "id"})))
    assert receipt.status is ReceiptStatus.FAILED
    assert receipt.error_code == "unavailable"


# ---------------------------------------------------------------------------
# device.shell
# ---------------------------------------------------------------------------

def test_shell_success_streams_split():
    def responder(command):
        return shell_spec(command, out=b"hello\n", err=b"warn-msg\n", uid="0")

    t, _, _ = make_transport(make_handler(shell_responder=responder))
    receipt = run(t.execute("dev1", make_action("device.shell",
                                                {"command": "echo hello"})))
    assert receipt.status is ReceiptStatus.SUCCEEDED
    summary = json.loads(receipt.summary)
    assert summary["exit_code"] == 0
    assert summary["uid"] == "0"
    assert summary["stdout_tail"] == "hello\n"
    assert "warn-msg" in summary["stderr_tail"]
    assert "__WHALE_RC_" not in summary["stderr_tail"]   # 哨兵已剥离
    assert summary["truncated"] is False
    assert receipt.output_ref is None
    assert "uid=0" in receipt.provider_ref               # 真实执行身份
    assert receipt.completed_at >= receipt.started_at


def test_shell_timeout_returns_timeout_and_kills():
    def responder(command):
        return shell_spec(command, delay=60)

    t, conns, _ = make_transport(make_handler(shell_responder=responder))
    receipt = run(t.execute(
        "dev1", make_action("device.shell",
                            {"command": "sleep 60", "timeout_ms": 200})))
    assert receipt.status is ReceiptStatus.TIMEOUT
    assert receipt.error_code == "timeout"
    proc = conns["fakehost"].procs[-1]
    assert proc.terminated or proc.killed               # 远端进程被终止


def test_shell_output_truncation_to_output_store():
    big = b"H" * 100 + b"x" * 70_000 + b"TAIL-MARKER"

    def responder(command):
        return shell_spec(command, out=big)

    t, _, _ = make_transport(make_handler(shell_responder=responder))
    action = make_action("device.shell", {"command": "yes"})
    receipt = run(t.execute("dev1", action))
    assert receipt.status is ReceiptStatus.SUCCEEDED
    assert receipt.output_ref == f"agent:out:{action.action_id}"
    summary = json.loads(receipt.summary)
    assert summary["truncated"] is True
    assert summary["original_bytes"] > sshmod.MAX_INLINE_OUTPUT
    # OutputStore 里是尾部截断后的打包流
    out, err = run(_get_streams(t, receipt.output_ref))
    assert len(out) + len(err) <= sshmod.MAX_INLINE_OUTPUT
    assert out.endswith(b"TAIL-MARKER")
    assert b"H" * 100 not in out                        # 头部被截掉
    # read_output 分页
    async def page():
        chunk, more = await t.read_output("dev1", receipt.output_ref,
                                          offset=0, max_bytes=100)
        full, trunc = await t.read_output("dev1", receipt.output_ref,
                                          offset=0, max_bytes=1 << 20)
        return chunk, more, full, trunc
    chunk, more, full, trunc = run(page())
    assert len(chunk) == 100
    assert more is True                                 # 只读了前 100 字节
    assert trunc is False
    assert full == (b"WSH1" + len(out).to_bytes(4, "big") + out + err)


async def _get_streams(t, ref):
    data = await t._store.get(ref)
    return unpack_streams(data)


def test_shell_nonzero_exit_failed():
    def responder(command):
        return shell_spec(command, ec=3, err=b"boom\n")

    t, _, _ = make_transport(make_handler(shell_responder=responder))
    receipt = run(t.execute("dev1", make_action("device.shell",
                                                {"command": "false-ish"})))
    assert receipt.status is ReceiptStatus.FAILED
    assert receipt.error_code == "internal"
    assert json.loads(receipt.summary)["exit_code"] == 3


def test_shell_su_quoting():
    command = "echo 'it'\"'\"'s \"quoted\"' && id"

    def responder(c):
        return shell_spec(c, out=b"uid=0(root)\n", uid="0")

    t, conns, _ = make_transport(make_handler(shell_responder=responder))
    receipt = run(t.execute(
        "dev1", make_action("device.shell",
                            {"command": command, "allow_root": True})))
    assert receipt.status is ReceiptStatus.SUCCEEDED
    sent = conns["fakehost"].commands[-1]
    assert "su -c " in sent
    # 取出 su -c 的引号参数，必须能还原为原始 inner（单引号转义正确）
    quoted = sent.split("su -c ", 1)[1].split("; _ec=$?", 1)[0]
    inner = shlex.split(quoted)
    assert len(inner) == 1
    assert command in inner[0]
    assert "'\"'\"'" in quoted                             # 单引号转义形态
    assert " su" in receipt.provider_ref                   # 注明经 su


def test_shell_working_dir_and_env_prefix():
    def responder(c):
        return shell_spec(c)

    extra = {"nova": SshDeviceTarget(
        host="fake2", port=22, username="root", key_path="/k",
        expected_host_key=HOST_KEY,
        env_prefix="export PATH=/system/bin:/system/xbin:$PATH")}
    t, conns, _ = make_transport(make_handler(shell_responder=responder),
                                 extra_targets=extra)
    run(t.execute("nova", make_action(
        "device.shell", {"command": "getprop ro.product.model",
                         "working_dir": "/data/local/tmp"})))
    sent = conns["fake2"].commands[-1]
    assert sent.startswith("export PATH=/system/bin:/system/xbin:$PATH; ")
    assert "cd /data/local/tmp && { getprop ro.product.model ; }" in sent


def test_shell_concurrency_capped_at_3():
    def responder(command):
        return shell_spec(command, delay=0.3)

    t, conns, calls = make_transport(make_handler(shell_responder=responder))

    async def main():
        return await asyncio.gather(*[
            t.execute("dev1", make_action("device.shell", {"command": "s"},
                                          action_id=f"act_C{i}"))
            for i in range(5)])

    receipts = run(main())
    assert all(r.status is ReceiptStatus.SUCCEEDED for r in receipts)
    conn = conns["fakehost"]
    # 前 5 条是 probe？probe 只有 1 条且在信号量内；用户命令并发 ≤3
    assert conn.max_active <= sshmod.MAX_SHELL_SESSIONS
    assert len(calls) == 1                                # 并发 _ensure 只建一条连接


# ---------------------------------------------------------------------------
# 取消：device.stop / disconnect
# ---------------------------------------------------------------------------

def test_device_stop_cancels_inflight():
    def responder(command):
        return shell_spec(command, delay=30)

    t, conns, _ = make_transport(make_handler(shell_responder=responder))

    async def main():
        shell_task = asyncio.create_task(t.execute(
            "dev1", make_action("device.shell", {"command": "sleep 30"},
                                timeout_ms=30_000)))
        await asyncio.sleep(0.2)
        stop = await t.execute("dev1", make_action("device.stop", {},
                                                   action_id="act_STOP"))
        shell_receipt = await shell_task
        return stop, shell_receipt

    stop, shell_receipt = run(main())
    assert stop.status is ReceiptStatus.SUCCEEDED
    assert json.loads(stop.summary)["cancelled"] == 1
    assert shell_receipt.status is ReceiptStatus.CANCELLED
    assert shell_receipt.error_code == "cancelled"
    assert conns["fakehost"].procs[-1].terminated


def test_disconnect_cancels_inflight():
    def responder(command):
        return shell_spec(command, delay=30)

    t, conns, _ = make_transport(make_handler(shell_responder=responder))

    async def main():
        shell_task = asyncio.create_task(t.execute(
            "dev1", make_action("device.shell", {"command": "sleep 30"},
                                timeout_ms=30_000)))
        await asyncio.sleep(0.2)
        await t.disconnect("dev1")
        return await shell_task

    receipt = run(main())
    assert receipt.status is ReceiptStatus.CANCELLED
    assert conns["fakehost"].closed


# ---------------------------------------------------------------------------
# device.file.*
# ---------------------------------------------------------------------------

def test_file_write_size_limit(monkeypatch):
    monkeypatch.setattr(sshmod, "MAX_FILE_BYTES", 16)
    t, conns, _ = make_transport()
    big = base64.b64encode(b"x" * 17).decode()
    receipt = run(t.execute("dev1", make_action(
        "device.file.write", {"path": "/t.bin", "data_b64": big})))
    assert receipt.status is ReceiptStatus.FAILED
    assert receipt.error_code == "invalid_message"
    ok = run(t.execute("dev1", make_action(
        "device.file.write", {"path": "/t.bin",
                              "data_b64": base64.b64encode(b"x" * 16).decode()})))
    assert ok.status is ReceiptStatus.SUCCEEDED
    assert conns["fakehost"].sftp.files["/t.bin"] == b"x" * 16


def test_file_read_chunked_via_output_ref(monkeypatch):
    monkeypatch.setattr(sshmod, "MAX_FILE_BYTES", 32)
    t, conns, _ = make_transport()
    payload = bytes(range(100))
    run(t.connect("dev1"))
    conns["fakehost"].sftp.files["/big.bin"] = payload

    r1 = run(t.execute("dev1", make_action(
        "device.file.read", {"path": "/big.bin"}, action_id="act_R1")))
    assert r1.status is ReceiptStatus.SUCCEEDED
    s1 = json.loads(r1.summary)
    assert s1["length"] == 32 and s1["eof"] is False and s1["truncated"] is True
    chunk1, _ = run(t.read_output("dev1", r1.output_ref, max_bytes=1 << 20))
    assert chunk1 == payload[:32]

    r2 = run(t.execute("dev1", make_action(
        "device.file.read", {"path": "/big.bin", "offset": 32, "length": 1000},
        action_id="act_R2")))
    s2 = json.loads(r2.summary)
    assert s2["length"] == 32                              # 仍被 16MB(=32) 上限钳制
    chunk2, _ = run(t.read_output("dev1", r2.output_ref, max_bytes=1 << 20))
    assert chunk2 == payload[32:64]

    r3 = run(t.execute("dev1", make_action(
        "device.file.read", {"path": "/big.bin", "offset": 68, "length": 32},
        action_id="act_R3")))
    s3 = json.loads(r3.summary)
    assert s3["eof"] is True and s3["length"] == 32      # 68+32 = 100 = 文件大小

    # 目录读 → invalid_message
    conns["fakehost"].sftp.dirs["/d"] = []
    rd = run(t.execute("dev1", make_action("device.file.read",
                                           {"path": "/d"}, action_id="act_RD")))
    assert rd.status is ReceiptStatus.FAILED
    assert rd.error_code == "invalid_message"


def test_file_list():
    t, conns, _ = make_transport()
    run(t.connect("dev1"))
    conns["fakehost"].sftp.dirs["/data/local/tmp"] = ["a.txt", "b.bin"]
    receipt = run(t.execute("dev1", make_action(
        "device.file.list", {"path": "/data/local/tmp"})))
    assert receipt.status is ReceiptStatus.SUCCEEDED
    summary = json.loads(receipt.summary)
    assert summary["count"] == 2
    assert [e["name"] for e in summary["entries"]] == ["a.txt", "b.bin"]


def test_file_list_large_spills_to_output_ref():
    """条目多时：summary 保持完整 JSON（≤2000），全量条目落 output_ref。"""
    t, conns, _ = make_transport()
    run(t.connect("dev1"))
    conns["fakehost"].sftp.dirs["/d"] = [f"file-{i:04d}.txt" for i in range(300)]
    receipt = run(t.execute("dev1", make_action("device.file.list",
                                                {"path": "/d"})))
    assert receipt.status is ReceiptStatus.SUCCEEDED
    assert len(receipt.summary) <= sshmod.SUMMARY_MAX
    summary = json.loads(receipt.summary)       # 必须是完整 JSON（曾被截断破坏）
    assert summary["count"] == 300
    assert summary["truncated"] is True
    assert receipt.output_ref
    blob, _ = run(t.read_output("dev1", receipt.output_ref, max_bytes=1 << 22))
    full = json.loads(blob.decode())
    assert len(full["entries"]) == 300


def test_file_write_read_roundtrip():
    t, _, _ = make_transport()
    data = b"whale-ssh-roundtrip\n"
    w = run(t.execute("dev1", make_action(
        "device.file.write", {"path": "/data/local/tmp/whale_ssh_test.txt",
                              "data_b64": base64.b64encode(data).decode()})))
    assert w.status is ReceiptStatus.SUCCEEDED
    r = run(t.execute("dev1", make_action(
        "device.file.read", {"path": "/data/local/tmp/whale_ssh_test.txt"})))
    assert r.status is ReceiptStatus.SUCCEEDED
    got, _ = run(t.read_output("dev1", r.output_ref, max_bytes=1 << 20))
    assert got == data
    assert json.loads(r.summary)["sha256"] == json.loads(w.summary)["sha256"]


# ---------------------------------------------------------------------------
# logcat / observe / 未知能力
# ---------------------------------------------------------------------------

def test_logcat_android_and_unavailable():
    def responder(command):
        assert command.startswith("")                    # 命令已组装
        return shell_spec(command, out=b"log-line\n")

    t, _, _ = make_transport(make_handler(shell_responder=responder))
    r = run(t.execute("dev1", make_action(
        "device.logcat", {"lines": 10, "args": ["-s", "Whale:D"]})))
    assert r.status is ReceiptStatus.SUCCEEDED
    assert "log-line" in json.loads(r.summary)["stdout_tail"]
    assert "device.logcat" in run(t.capabilities("dev1"))

    t2, _, _ = make_transport(android=False)
    r2 = run(t2.execute("dev1", make_action("device.logcat", {})))
    assert r2.status is ReceiptStatus.FAILED
    assert r2.error_code == "unavailable"
    assert "device.logcat" not in run(t2.capabilities("dev1"))


def test_observe_android_degraded():
    def responder(command):
        if "screencap -p" in command:
            return shell_spec(command, out=b"\x89PNG-fake-bytes")
        if "wm size" in command:
            return shell_spec(command, out=b"Physical size: 1080x2400\n")
        if "dumpsys activity" in command:
            return shell_spec(
                command,
                out=b"mResumedActivity: ActivityRecord{1 u0 com.x/.Main t7}\n")
        return shell_spec(command)

    t, _, _ = make_transport(make_handler(shell_responder=responder))
    obs = run(t.observe("dev1", ObserveRequest()))
    assert obs.screenshot_ref and obs.screenshot_ref.startswith("agent:shot:obs_")
    assert obs.elements == ()                              # 空元素表
    assert obs.truncated is True
    assert (obs.screen_width, obs.screen_height) == (1080, 2400)
    assert obs.foreground_package == "com.x"
    shot, _ = run(t.read_output("dev1", obs.screenshot_ref, max_bytes=1 << 20))
    assert shot == b"\x89PNG-fake-bytes"

    r = run(t.execute("dev1", make_action("device.observe", {})))
    assert r.status is ReceiptStatus.SUCCEEDED
    assert json.loads(r.summary)["truncated"] is True


def test_observe_non_android_unavailable():
    t, _, _ = make_transport(android=False)
    with pytest.raises(SshTransportError) as ei:
        run(t.observe("dev1", ObserveRequest()))
    assert ei.value.code == "unavailable"
    r = run(t.execute("dev1", make_action("device.observe", {})))
    assert r.status is ReceiptStatus.FAILED
    assert r.error_code == "unavailable"


def test_unknown_capability():
    t, _, _ = make_transport()
    r = run(t.execute("dev1", make_action("device.tap", {"element_id": 1})))
    assert r.status is ReceiptStatus.FAILED
    assert r.error_code == "capability_unknown"


def test_health_and_reconnect():
    t, _, calls = make_transport()
    bad = run(t.health("dev1"))
    assert bad.connected is False
    run(t.connect("dev1"))
    good = run(t.health("dev1"))
    assert good.connected is True
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# SFTP 不可用（dropbear 无 sftp-server）→ shell base64 降级通道
# ---------------------------------------------------------------------------

def _fallback_responder(sftp):
    """模拟降级通道的远端命令：base64 写 / tail+head+base64 读 / stat 列目录。"""
    # 路径经 shlex.quote：无特殊字符时不加引号，两种形态都匹配
    write_re = re.compile(r"printf '%s' '([A-Za-z0-9+/=]*)' \| base64 -d "
                          r"(>>?) (?:'([^']+)'|([^\s;]+))")

    def responder(command):
        if "test -d " in command:
            m = re.search(r"test -d (?:'([^']+)'|([^\s;]+))", command)
            path = m.group(1) or m.group(2)
            return shell_spec(command, ec=0 if path in sftp.dirs else 1)
        if "stat -c %s" in command:
            m = re.search(r"stat -c %s (?:'([^']+)'|([^\s;]+))", command)
            path = m.group(1) or m.group(2)
            if path in sftp.files:
                return shell_spec(command,
                                  out=str(len(sftp.files[path])).encode())
            return shell_spec(command, ec=1)  # || wc 分支也不成功
        if " | base64" in command and "tail -c" in command:
            m = re.search(r"tail -c \+(\d+) -- (?:'([^']+)'|([^\s;]+)) \| "
                          r"head -c (\d+)", command)
            skip = int(m.group(1)) - 1
            path = m.group(2) or m.group(3)
            length = int(m.group(4))
            data = sftp.files.get(path, b"")[skip:skip + length]
            return shell_spec(command, out=base64.b64encode(data))
        m = write_re.search(command)
        if m:
            b64, redir = m.group(1), m.group(2)
            path = m.group(3) or m.group(4)
            data = base64.b64decode(b64)
            if redir == ">":
                sftp.files[path] = data
            else:
                sftp.files[path] = sftp.files.get(path, b"") + data
            return shell_spec(command)
        if "for f in ./*" in command:
            m = re.search(r"cd (?:'([^']+)'|([^\s;]+)) &&", command)
            path = m.group(1) or m.group(2)
            lines = "".join(
                f"./{n}|1|2|regular file\n"
                for n in sftp.dirs.get(path, []))
            return shell_spec(command, out=lines.encode())
        return shell_spec(command)

    return responder


def test_file_fallback_write_read_list():
    holder = {}

    def responder(command):
        # 延迟绑定：conn 在首次 execute/connect 时才由 factory 创建
        return _fallback_responder(holder["conn"].sftp)(command)

    t, conns, _ = make_transport(
        make_handler(shell_responder=responder), sftp_broken=True)

    async def _bind():
        await t.connect("dev1")
        holder["conn"] = conns["fakehost"]
    run(_bind())
    data = b"fallback-channel-\x00\xff\n"
    w = run(t.execute("dev1", make_action(
        "device.file.write", {"path": "/f.bin",
                              "data_b64": base64.b64encode(data).decode()})))
    assert w.status is ReceiptStatus.SUCCEEDED
    assert w.provider_ref == "degraded:shell_fallback"     # 诚实标注降级
    conn = conns["fakehost"]
    assert conn.sftp.files["/f.bin"] == data

    r = run(t.execute("dev1", make_action("device.file.read",
                                          {"path": "/f.bin"})))
    assert r.status is ReceiptStatus.SUCCEEDED
    assert r.provider_ref == "degraded:shell_fallback"
    got, _ = run(t.read_output("dev1", r.output_ref, max_bytes=1 << 20))
    assert got == data

    conn.sftp.dirs["/d"] = ["x", "y"]
    l = run(t.execute("dev1", make_action("device.file.list", {"path": "/d"})))
    assert l.status is ReceiptStatus.SUCCEEDED
    summary = json.loads(l.summary)
    assert summary["count"] == 2
    assert l.provider_ref == "degraded:shell_fallback"


def test_memory_output_store_lru():
    store = MemoryOutputStore(max_entries=2, max_total_bytes=1 << 20)
    run(store.put("a", b"1"))
    run(store.put("b", b"2"))
    run(store.put("c", b"3"))
    assert run(store.get("a")) is None
    assert run(store.get("c")) == b"3"
