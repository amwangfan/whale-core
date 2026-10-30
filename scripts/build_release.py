"""Build frontend, include it in a wheel, and validate the packaged static assets.

Run after installing web dependencies: python scripts/build_release.py
No production deployment is performed.
"""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import zipfile

root = Path(__file__).resolve().parents[1]
web = root / "web"
subprocess.run(["pnpm", "run", "build"], cwd=web, check=True)
target = root / "src" / "whale_core" / "web_dist"
if target.exists():
    shutil.rmtree(target)
shutil.copytree(web / "dist", target)
try:
    subprocess.run(
        [sys.executable, "-m", "hatchling", "build", "-t", "wheel"],
        cwd=root,
        check=True,
    )
    wheel = max((root / "dist").glob("*.whl"), key=lambda p: p.stat().st_mtime)
    with zipfile.ZipFile(wheel) as archive:
        assert "whale_core/web_dist/index.html" in archive.namelist()
        assert any(
            p.startswith("whale_core/web_dist/assets/") for p in archive.namelist()
        )
    print("Packaged console verified:", wheel.name)
finally:
    shutil.rmtree(target)
