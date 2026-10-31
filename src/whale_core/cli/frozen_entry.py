"""Entry point for a self-contained Linux/macOS executable.

No arguments starts the desktop browser host; normal whale CLI commands remain
available. --serve is a loopback-only integration/diagnostic host, with the
bearer token supplied through WHALE_API_TOKEN or the private token file.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading

from whale_core import __version__


def _serve(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="whale-core --serve")
    parser.add_argument("--config-dir", required=True, help="private persistent data directory")
    parser.add_argument("--port", type=int, default=0, help="loopback port (default: automatic)")
    args = parser.parse_args(argv)
    args.host = "127.0.0.1"
    from whale_core.cli.main import _build_api_server
    server = None
    previous_sigterm = None
    try:
        server, _token, _source = _build_api_server(args)
        server.start()
        def interrupt(_signal, _frame):
            raise KeyboardInterrupt
        previous_sigterm = signal.signal(signal.SIGTERM, interrupt)
        print(json.dumps({"ready": True, "url": server.url,
                          "console": server.url.rsplit("/api/", 1)[0] + "/"}), flush=True)
        threading.Event().wait()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Unable to start local server: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        if server is not None:
            try:
                server.stop()
            finally:
                server.ctx.store.close()
    return 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        print(f"Whale Core {__version__}")
        return 0
    if args and args[0] in ("--help", "-h"):
        print("Whale Core self-contained Linux/macOS runtime\n\n"
              "Usage: whale-core [COMMAND] [OPTIONS]\n\n"
              "Without arguments, opens the desktop console in your browser.\n"
              "Commands: desktop, api, setup, doctor, run, plugins, memory\n"
              "  --version                         show packaged version\n"
              "  --serve --config-dir PATH         start loopback API on a random port\n"
              "  COMMAND --help                    show command options\n\n"
              "No separately installed Python or Node is required.")
        return 0
    if args and args[0] == "--serve":
        return _serve(args[1:])
    if not args:
        from whale_core.cli.desktop import run_desktop
        return run_desktop(browser_only=True)
    from whale_core.cli.main import main as cli_main
    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
