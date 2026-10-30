#!/usr/bin/env python3
"""Install a user-local Linux launcher or macOS .app wrapper.

Run using the Python environment where whale-core is installed. This adds only
an entry point; it never installs dependencies or deletes user data.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import plistlib
import shlex
import sys


def desktop_quote(value: str) -> str:
    # Desktop Entry Exec fields use their own quoting rules, not shell quoting.
    value = value.replace("%", "%%")
    for char in ("\\", '"', "`", "$"):
        value = value.replace(char, "\\" + char)
    return '"' + value + '"'


def install_linux(destination: Path, python: str) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / "whale-core.desktop"
    target.write_text("[Desktop Entry]\nType=Application\nName=Whale Core\n"
                      "Comment=Whale companion runtime and control console\n"
                      f"Exec={desktop_quote(python)} -m whale_core.cli.desktop\n"
                      "Terminal=true\nCategories=Utility;\nStartupNotify=false\n",
                      encoding="utf-8")
    target.chmod(0o644)
    return target


def install_macos(destination: Path, python: str) -> Path:
    bundle = destination / "Whale Core.app"
    contents = bundle / "Contents"
    executable = contents / "MacOS" / "WhaleCore"
    executable.parent.mkdir(parents=True, exist_ok=True)
    command = shlex.join([python, "-m", "whale_core.cli.desktop"])
    command = command.replace("\\", "\\\\").replace('"', '\\"')
    executable.write_text("#!/bin/sh\n/usr/bin/osascript <<'WHALE_APPLESCRIPT'\n"
                          'tell application "Terminal"\n    activate\n'
                          f'    do script "exec {command}"\n'
                          "end tell\nWHALE_APPLESCRIPT\n", encoding="utf-8")
    executable.chmod(0o755)
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleName": "Whale Core", "CFBundleDisplayName": "Whale Core",
                      "CFBundleIdentifier": "io.whale.core.desktop",
                      "CFBundleExecutable": "WhaleCore", "CFBundlePackageType": "APPL",
                      "CFBundleVersion": "0.1.0", "LSUIElement": True}, stream)
    return bundle


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path,
                        help="launcher parent directory; defaults to the platform's user applications directory")
    args = parser.parse_args(argv)
    if sys.platform == "linux":
        default = Path.home() / ".local" / "share" / "applications"
        target = install_linux((args.destination or default).expanduser(), sys.executable)
    elif sys.platform == "darwin":
        target = install_macos((args.destination or Path.home() / "Applications").expanduser(), sys.executable)
    else:
        parser.error("only Linux and macOS launchers are implemented")
    print(f"Installed launcher: {target}")
    print("The launcher uses this Python environment; reinstall it if the environment moves.")
    print("Removing the launcher leaves all Whale data intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
