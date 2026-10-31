#!/usr/bin/env python3
"""Build an installable wheel containing the current web console.

Run from any directory. Build tools: Python/pip/hatchling, Node 24 and pnpm 12.
No generated frontend files are written into the source package.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "dist"), help="wheel output directory")
    parser.add_argument("--skip-web-build", action="store_true", help="use an existing web/dist build")
    parser.add_argument("--no-build-isolation", action="store_true", help="use already installed hatchling")
    args = parser.parse_args()
    if not args.skip_web_build:
        subprocess.run(["pnpm", "-C", str(ROOT / "web"), "build"], check=True)
    web = ROOT / "web" / "dist"
    if not (web / "index.html").is_file():
        parser.error("web/dist/index.html missing; install frontend dependencies and build first")
    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="whale-release-") as temporary:
        stage = Path(temporary)
        for name in ("pyproject.toml", "README.md", "README_EN.md", "CONTRIBUTING.md"):
            shutil.copy2(ROOT / name, stage / name)
        for name in ("src", "licenses", "docs", "plugins"):
            shutil.copytree(ROOT / name, stage / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "static"))
        shutil.copytree(web, stage / "src" / "whale_core" / "static")
        command = [sys.executable, "-m", "pip", "wheel", "--no-deps",
                   "--wheel-dir", str(output)]
        if args.no_build_isolation:
            command.append("--no-build-isolation")
        subprocess.run(command + [str(stage)], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
