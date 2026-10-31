#!/usr/bin/env python3
"""Smoke-test a frozen executable outside the checkout without using its Python.

The verifier itself uses the build machine's Python, while the child executable
receives an empty PATH and no Python environment variables.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    args = parser.parse_args(argv)
    executable = args.executable.expanduser().resolve()
    if not executable.is_file():
        parser.error("executable does not exist")
    with tempfile.TemporaryDirectory(prefix="whale-frozen-smoke-") as temporary:
        root = Path(temporary)
        environment = {k: v for k, v in os.environ.items()
                       if not k.startswith("PYTHON") and k not in (
                           "VIRTUAL_ENV", "CONDA_PREFIX", "SSL_CERT_FILE", "SSL_CERT_DIR",
                           "OPENSSL_CONF", "CODEX_PROXY_CERT")}
        environment.update(PATH="", WHALE_API_TOKEN="frozen-smoke-local-token")
        version = subprocess.check_output([str(executable), "--version"], env=environment,
                                          cwd=root, text=True, timeout=30)
        assert "Whale Core " in version, version
        help_text = subprocess.check_output([str(executable), "--help"], env=environment,
                                            cwd=root, text=True, timeout=30)
        assert "No separately installed Python" in help_text, help_text
        process = subprocess.Popen([str(executable), "--serve", "--config-dir", str(root / "data")],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, cwd=root, env=environment)
        output: queue.Queue = queue.Queue()
        def read_output():
            for line in process.stdout:
                output.put(line)
            output.put(None)
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        url = None
        try:
            deadline = time.monotonic() + 60
            lines = []
            while time.monotonic() < deadline:
                try:
                    line = output.get(timeout=max(0.1, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if line is None:
                    break
                lines.append(line)
                try:
                    ready = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ready, dict) and ready.get("ready"):
                    url = ready["url"]
                    break
            assert url, "frozen API did not become ready: " + "".join(lines)[-2000:]
            assert url.startswith("http://127.0.0.1:")
            def get(path):
                request = urllib.request.Request(url + path, headers={"Authorization": "Bearer frozen-smoke-local-token"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    return json.load(response)["result"]
            assert get("/health")["ok"] is True
            skills = get("/workflows/skills")["skills"]
            assert any(skill["name"] == "search-and-read" for skill in skills), skills
            base = url.rsplit("/api/", 1)[0]
            with urllib.request.urlopen(base + "/", timeout=10) as response:
                html = response.read().decode()
                assert '<div id="root">' in html
            asset = re.search(r'src="(/assets/[^" ]+\.js)"', html)
            assert asset, "compiled console asset missing"
            with urllib.request.urlopen(base + asset.group(1), timeout=10) as response:
                assert "javascript" in response.headers["Content-Type"]
                assert len(response.read()) > 1000
            try:
                urllib.request.urlopen(url + "/health", timeout=10)
            except urllib.error.HTTPError as exc:
                assert exc.code == 401
            else:
                raise AssertionError("API accepted a missing token")
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
        assert process.returncode == 0, process.returncode
        if url:
            try:
                urllib.request.urlopen(url + "/health", timeout=1)
            except urllib.error.URLError:
                pass
            else:
                raise AssertionError("API still listening after executable termination")
    print("Frozen executable passed: empty PATH, version/help, console/assets, auth, workflow data, graceful shutdown.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
