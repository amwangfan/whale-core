"""Linux namespace sandbox. Fail closed when user namespaces are unavailable.

No host home/config mounts, network, device access or writable system paths. A
private /work may be explicitly mounted; all child processes share resource limits
and are killed on timeout/cancel. No daemon/socket or root privileges required.
"""

from __future__ import annotations
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from whale_core.cloud.shell_policy import ShellPolicy


def bounded_process(
    argv, *, timeout=30, output_bytes=16000, cancel=None, env=None, cwd=None, **kwargs
):
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=env,
            cwd=cwd,
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "status": "error",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "error": type(exc).__name__,
            "duration_s": time.monotonic() - started,
        }
    captures = {"stdout": bytearray(), "stderr": bytearray()}
    status = "success"
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
        try:
            while selector.get_map():
                if time.monotonic() - started >= timeout or (
                    cancel and cancel.is_set()
                ):
                    status = "cancelled" if cancel and cancel.is_set() else "timeout"
                    break
                for key, _ in selector.select(timeout=0.05):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target = captures[key.data]
                    target.extend(chunk[: max(0, output_bytes - len(target))])
            if status != "success":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            proc.wait(timeout=3)
        finally:
            # Descendants must not outlive the managed command, even if they have
            # closed stdout/stderr. start_new_session owns the whole group.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.stdout.close()
            proc.stderr.close()
    if status == "success" and proc.returncode != 0:
        status = "failed"
    return {
        "status": status,
        "exit_code": proc.returncode,
        **{
            name: data.decode("utf-8", errors="replace")
            for name, data in captures.items()
        },
        "duration_s": time.monotonic() - started,
    }


class ContainerShellExecutor:
    def __init__(
        self, *, timeout_s=30, output_chars=4000, workspace="", tier="managed"
    ):
        if not 0 < timeout_s <= 120 or not 0 < output_chars <= 20000:
            raise ValueError("invalid execution limits")
        self.timeout_s, self.output_chars = timeout_s, output_chars
        self.workspace = str(Path(workspace).resolve()) if workspace else ""
        self.policy = ShellPolicy(tier=tier)

    def argv(self, command):
        bwrap = shutil.which("bwrap")
        if sys.platform != "linux" or not bwrap:
            raise ValueError("Linux bubblewrap is required")
        args = [
            bwrap,
            "--unshare-all",
            "--unshare-user",
            "--disable-userns",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--uid",
            "65534",
            "--gid",
            "65534",
            "--ro-bind",
            "/usr",
            "/usr",
        ]
        for folder in ("/bin", "/lib", "/lib64"):
            if Path(folder).exists():
                args += ["--ro-bind", folder, folder]
        args += [
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/work",
        ]
        if self.workspace:
            if not Path(self.workspace).is_dir():
                raise ValueError("workspace must exist")
            args += ["--bind", self.workspace, "/work"]
        args += [
            "--chdir",
            "/work",
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--setenv",
            "HOME",
            "/tmp",
            "--setenv",
            "LANG",
            "C.UTF-8",
            "/bin/sh",
            "-c",
            command,
        ]
        # Resource setup runs in a fresh single-thread process, avoiding preexec_fn
        # in the multithreaded API. The worker does not execute the command itself.
        return [
            sys.executable,
            str(Path(__file__).with_name("sandbox_worker.py")),
            *args,
        ]

    def run(self, command, *, is_owner, cancel=None):
        if os.geteuid() == 0:
            return {
                "status": "denied",
                "reason": "run the core under a dedicated non-root account",
            }
        verdict = self.policy.command_verdict(command, is_owner=is_owner)
        if not verdict.allowed:
            return {"status": "denied", "reason": verdict.reason}
        try:
            argv = self.argv(command)
        except ValueError:
            return {
                "status": "unavailable",
                "reason": "Linux bubblewrap/workspace unavailable",
            }
        return bounded_process(
            argv,
            timeout=self.timeout_s,
            output_bytes=self.output_chars * 4,
            cancel=cancel,
            env={"PATH": "/usr/bin:/bin"},
        )

    def healthcheck(self):
        result = self.run("true", is_owner=True)
        return result["status"] == "success"
