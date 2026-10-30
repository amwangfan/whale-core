#!/usr/bin/env python3
"""Freeze a local Linux/macOS executable containing Python and the console.

Build on the target OS/architecture. Install PyInstaller in a private virtual
environment first; this script never installs tools or deploys the result.
"""
from __future__ import annotations

import argparse
import importlib.util
import importlib.metadata
import os
import json
import platform
from pathlib import Path
import plistlib
import shlex
import shutil
import ssl
import subprocess
import sys
import sysconfig
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def _mac_wrapper(output: Path, executable: Path, *, onefile: bool = False) -> Path:
    bundle = output / "Whale Core.app"
    contents = bundle / "Contents"
    binary = contents / "MacOS" / "WhaleCore"
    binary.parent.mkdir(parents=True, exist_ok=True)
    # Copy the full distribution into the app so it stays self-contained when
    # the app is moved. A launcher resolves its own path without using Python.
    runtime = contents / "Resources" / "runtime"
    if runtime.exists():
        shutil.rmtree(runtime)
    runtime.parent.mkdir(parents=True, exist_ok=True)
    if not onefile:  # one-dir distribution
        shutil.copytree(executable.parent, runtime)
    else:
        runtime.mkdir()
        shutil.copy2(executable, runtime / "whale-core")
    binary.write_text('''#!/bin/sh
app_binary="$(CDPATH= cd -- "$(dirname -- "$0")/../Resources/runtime" && pwd)/whale-core"
# argv is passed to osascript separately; app paths never become source code.
/usr/bin/osascript - "$app_binary" <<'WHALE_APPLESCRIPT'
on run argv
    set executablePath to item 1 of argv
    tell application "Terminal"
        activate
        do script "exec " & quoted form of executablePath
    end tell
end run
WHALE_APPLESCRIPT
''', encoding="utf-8")
    binary.chmod(0o755)
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleName": "Whale Core", "CFBundleDisplayName": "Whale Core",
                      "CFBundleIdentifier": "io.whale.core.desktop",
                      "CFBundleExecutable": "WhaleCore", "CFBundlePackageType": "APPL",
                      "CFBundleVersion": "0.1.0", "LSUIElement": True}, stream)
    return bundle


def _linux_wrapper(output: Path, executable: Path) -> None:
    launcher = output / "launch-whale-core.sh"
    relative = executable.relative_to(output)
    launcher.write_text('#!/bin/sh\nbase_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n'
                        f'exec "$base_dir"/{shlex.quote(str(relative))} "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)
    # Absolute desktop entries are machine-local. Generate one next to the
    # archive and tell users to rerun this installer after moving the folder.
    installer = output / "install-launcher.sh"
    installer.write_text(r'''#!/bin/sh
set -eu
base_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
apps_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$apps_dir"
# Desktop Entry Exec is not shell input. Escape its quoted string and percent.
escaped=$(printf '%s' "$base_dir/launch-whale-core.sh" | sed 's/\\/\\\\/g; s/"/\\"/g; s/%/%%/g; s/`/\\`/g; s/\$/\\$/g')
cat > "$apps_dir/whale-core-frozen.desktop" <<WHALE_DESKTOP
[Desktop Entry]
Type=Application
Name=Whale Core
Exec="$escaped"
Terminal=true
Categories=Utility;
WHALE_DESKTOP
printf 'Installed %s\n' "$apps_dir/whale-core-frozen.desktop"
''', encoding="utf-8")
    installer.chmod(0o755)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="output directory (dist/frozen or dist/frozen-onefile)")
    parser.add_argument("--onefile", action="store_true", help="single executable instead of a one-dir runtime")
    parser.add_argument("--skip-web-build", action="store_true", help="reuse current web/dist")
    args = parser.parse_args(argv)
    if sys.platform not in ("linux", "darwin"):
        parser.error("only native Linux and macOS builds are supported")
    if importlib.util.find_spec("PyInstaller") is None:
        parser.error("PyInstaller missing; install it in a build venv: python -m pip install 'pyinstaller>=6,<7'")
    if not args.skip_web_build:
        subprocess.run(["pnpm", "-C", str(ROOT / "web"), "build"], check=True)
    web = ROOT / "web" / "dist"
    skills = ROOT / "src" / "whale_core" / "workflow_skills"
    if not (web / "index.html").is_file() or not skills.is_dir():
        parser.error("console build or workflow_skills missing")
    output = (args.output or ROOT / "dist" / ("frozen-onefile" if args.onefile else "frozen")).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="whale-freeze-") as temporary:
        notices = Path(temporary) / "THIRD_PARTY_NOTICES"
        notices.mkdir()
        python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
        if python_license.is_file():
            shutil.copy2(python_license, notices / "Python-LICENSE.txt")
        for package in ("pyinstaller", "websockets"):
            try:
                distribution = importlib.metadata.distribution(package)
            except importlib.metadata.PackageNotFoundError:
                continue
            for member in distribution.files or ():
                if member.name.lower().startswith(("license", "copying")):
                    source = distribution.locate_file(member)
                    if source.is_file():
                        shutil.copy2(source, notices / (package + "-" + member.name))
        for package in ("react", "react-dom", "scheduler"):
            source = ROOT / "web" / "node_modules" / package / "LICENSE"
            if source.is_file():
                shutil.copy2(source, notices / (package + "-LICENSE.txt"))
        (notices / "README.txt").write_text(
            "Internal evaluation build. Project-level license remains owner-pending.\n"
            "Includes Python, PyInstaller bootloader, optional websockets and React notices.\n"
            "Verify target-platform runtime library notices before public redistribution.\n", encoding="utf-8")
        command = [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", "--console",
                   "--name", "whale-core", "--onefile" if args.onefile else "--onedir",
                   "--distpath", str(output), "--workpath", str(Path(temporary) / "work"),
                   "--specpath", temporary, "--paths", str(ROOT / "src"),
                   "--collect-submodules", "whale_core",
                   "--add-data", f"{web}:whale_core/static",
                   "--add-data", f"{skills}:whale_core/workflow_skills",
                   "--add-data", f"{ROOT / 'licenses'}:licenses",
                   "--add-data", f"{notices}:THIRD_PARTY_NOTICES"]
        # Some managed Python builds ship an SSL policy data file beside
        # ssl.py. Preserve that policy in the frozen runtime instead of
        # disabling certificate validation or leaving a missing-file startup.
        ssl_policy = Path(ssl.__file__).with_name("codex-rejected-cas.pem")
        if ssl_policy.is_file():
            command += ["--add-data", f"{ssl_policy}:."]
        if importlib.util.find_spec("websockets") is not None:
            command += ["--collect-submodules", "websockets"]
        # Browser desktop mode keeps the default executable independent of the
        # OS GUI toolkit, while still packaging the full authenticated UI.
        command += ["--exclude-module", "webview",
                    str(ROOT / "src" / "whale_core" / "cli" / "frozen_entry.py")]
        subprocess.run(command, check=True, env={**os.environ, "PYINSTALLER_CONFIG_DIR": str(Path(temporary) / "cache")})
    executable = output / "whale-core" if args.onefile else output / "whale-core" / "whale-core"
    if sys.platform == "darwin":
        _mac_wrapper(output, executable, onefile=args.onefile)
    else:
        _linux_wrapper(output, executable)
    (output / "build-info.json").write_text(json.dumps({
        "system": platform.system(), "architecture": platform.machine(),
        "libc": platform.libc_ver(), "python_bundled": platform.python_version(),
        "pyinstaller": importlib.metadata.version("pyinstaller"),
        "mode": "onefile" if args.onefile else "onedir", "internal_evaluation": True,
    }, indent=2) + "\n", encoding="utf-8")
    (output / "README.txt").write_text(
        "Whale Core internal evaluation package (project license pending).\n"
        "No separate Python or Node installation is required.\n"
        "Linux: run ./launch-whale-core.sh in a terminal; optional ./install-launcher.sh adds a menu item.\n"
        "macOS: open Whale Core.app, or run the bundled whale-core executable in Terminal.\n"
        "Copy the per-launch token from Terminal into the console connection field.\n"
        "Close browser-mode service with Ctrl+C; closing a browser tab does not stop it.\n"
        "Data is stored outside this folder; deleting or replacing the app preserves it.\n"
        "Back up the complete data directory before an upgrade.\n"
        "Built for this OS and architecture; check build-info.json and system library compatibility.\n"
        "Not signed or notarized. Public redistribution requires license and platform review.\n",
        encoding="utf-8")
    print(f"Self-contained executable: {executable}")
    print("Internal evaluation only: project-level distribution license remains owner-pending.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
