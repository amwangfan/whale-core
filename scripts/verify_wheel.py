#!/usr/bin/env python3
"""Install a wheel outside the checkout and verify its packaged console + API."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile

SMOKE = r'''
import json
from pathlib import Path
import sys
import urllib.request
sys.path.insert(0, sys.argv[1])
import whale_core
from whale_core.cli.main import _repo_static_dir
from whale_core.cli.desktop import run_desktop
assert callable(run_desktop)  # optional GUI must not be an import-time dependency
from whale_core.api.server import ApiContext, WhaleApiServer
from whale_core.memory.store import InMemoryStore
package = Path(whale_core.__file__).resolve().parent
assert package.parent == Path(sys.argv[1]).resolve(), package
from whale_core.devices.schema import ContractValidator, contracts_dir
from whale_core.devices.capabilities import all_capabilities, action_capabilities, query_operations
assert package in contracts_dir().parents, contracts_dir()
assert ContractValidator().validators
assert all_capabilities() and action_capabilities() and query_operations()
static = _repo_static_dir()
assert static is not None and package in static.parents, static
assert (static / "index.html").is_file()
from whale_core.profiles import assemble_runtime
from whale_core.service.workflows import WorkflowsService
bundle = assemble_runtime('local', store=InMemoryStore(), env={})
assert WorkflowsService(bundle.orchestrator).list_skills()
assert bundle.manager.status()
from whale_core.cli.desktop import run_desktop
assert callable(run_desktop)
server = WhaleApiServer(ApiContext(store=InMemoryStore()), token="wheel-smoke-token", port=0,
                        static_dir=str(static))
server.start()
try:
    base = server.url.rsplit("/api/", 1)[0]
    with urllib.request.urlopen(base + "/", timeout=5) as response:
        html = response.read().decode()
        assert response.status == 200 and '<div id="root">' in html
    # Lazy pages have separate JS/CSS files. Loading only index.html can miss
    # broken asset routing or SPA fallbacks that return HTML for a chunk URL.
    assets = sorted((static / "assets").rglob("*"))
    checked = 0
    for asset in assets:
        if asset.suffix not in {".js", ".css"}:
            continue
        path = asset.relative_to(static).as_posix()
        with urllib.request.urlopen(base + "/" + path, timeout=5) as response:
            assert response.status == 200, path
            assert "text/html" not in response.headers.get("Content-Type", ""), path
            assert response.read() == asset.read_bytes(), path
        checked += 1
    assert checked, "no packaged console assets"
    print(f"Served {checked} packaged JavaScript/CSS assets with exact bytes.")
    request = urllib.request.Request(server.url + "/health",
                                     headers={"Authorization": "Bearer wheel-smoke-token"})
    with urllib.request.urlopen(request, timeout=5) as response:
        assert json.load(response)["result"]["ok"] is True
finally:
    server.stop()
print("Installed wheel loads V2 contracts and serves the packaged console and authenticated API.")
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", help="path to a built .whl")
    args = parser.parse_args()
    wheel = Path(args.wheel).resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        parser.error("provide an existing wheel")
    with tempfile.TemporaryDirectory(prefix="whale-install-") as temporary:
        target = Path(temporary) / "site"
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--target",
                        str(target), str(wheel)], check=True)
        subprocess.run([sys.executable, "-I", "-c", SMOKE, str(target)],
                       cwd=temporary, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
