"""whale CLI. ``whale`` alone starts the agent and opens the console; also token / doctor."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path


def default_data_dir() -> Path:
    env = os.environ.get("WHALE_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".whale"


def _port_free(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1" if host in ("0.0.0.0", "") else host, port))
        except OSError:
            return False
    return True


def cmd_serve(args: argparse.Namespace) -> int:
    """Run Whale. Restarts in-process when the console asks (e.g. after enabling LAN access)."""
    import threading
    import webbrowser

    import uvicorn

    from .app import App
    from .server.http import create_server

    opened = False
    while True:
        app = App(args.data)
        host = args.host or app.config.value.server.host
        port = args.port or app.config.value.server.port
        if not _port_free(host, port):
            for candidate in range(port + 1, port + 20):
                if _port_free(host, candidate):
                    print(f"  端口 {port} 已被占用，改用 {candidate}")
                    port = candidate
                    break
        local = f"http://127.0.0.1:{port}/?token={app.config.api_token}"
        print(f"\n  🐋 Whale {app.version}  ·  数据目录 {app.data_dir}")
        print(f"  打开控制台：{local}")
        if host in ("0.0.0.0", "::"):
            from .setup import lan_addresses

            for ip in lan_addresses():
                print(f"  手机连接地址：http://{ip}:{port}")
        print("  关闭这个窗口（或按 Ctrl+C）即可停止。\n")
        server = uvicorn.Server(uvicorn.Config(create_server(app), host=host, port=port, log_level=args.log_level,
                                               ws="websockets" if _has("websockets") else "auto"))
        restart = threading.Event()

        def request_restart(_server: uvicorn.Server = server, _flag: threading.Event = restart) -> None:
            _flag.set()
            _server.should_exit = True

        app.setup.restart_hook = request_restart
        app.setup.bound = (host, port)
        if args.open and not opened:
            opened = True
            threading.Timer(1.2, lambda url=local: webbrowser.open(url)).start()
        server.run()
        if not restart.is_set():
            return 0
        print("  正在重启…")


def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


def cmd_token(args: argparse.Namespace) -> int:
    from .kernel.config import Config

    cfg = Config(args.data)
    if args.rotate:
        import secrets

        cfg.set_secret("api_token", secrets.token_urlsafe(24))
    print(cfg.api_token)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .app import App

    app = App(args.data)
    ok = True
    print(f"Whale {app.version} · Python {sys.version.split()[0]} · data {app.data_dir}")
    print(f"  SQLite FTS5: {'yes' if app.db.fts5 else 'no (keyword recall falls back to LIKE)'}")
    status = app.models.status()
    for role, info in status["roles"].items():
        mark = "✓" if info["configured"] else "·"
        print(f"  model {role:<9} {mark} {info['provider'] or '-'} {info['model'] or ''}")
    if status["roles"]["chat"]["configured"]:
        res = asyncio.run(app.models.test("chat"))
        print(f"  chat probe: {res}")
        ok = ok and res.get("ok", False)
    for mod in ("uvicorn", "starlette", "httpx", "pydantic", "websockets", "cryptography"):
        print(f"  {mod:<13} {'✓' if _has(mod) else '✗ missing'}")
    return 0 if ok else 1


COMMANDS = ("serve", "token", "doctor")


def normalize_argv(raw: list[str]) -> list[str]:
    """``whale`` / ``whale --no-open`` / ``whale --data D:/x`` all mean ``serve``."""
    if any(a in (*COMMANDS, "-h", "--help") for a in raw):
        return raw
    head: list[str] = []
    tail: list[str] = []
    k = 0
    while k < len(raw):
        if raw[k] == "--data" and k + 1 < len(raw):
            head += raw[k:k + 2]
            k += 2
            continue
        (head if raw[k].startswith("--data=") else tail).append(raw[k])
        k += 1
    return head + ["serve"] + tail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="whale", description="Whale personal agent core")
    parser.add_argument("--data", type=Path, default=default_data_dir(), help="data directory (default ~/.whale)")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("serve", help="run the agent and console")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--log-level", default="warning")
    p.add_argument("--open", dest="open", action="store_true", default=True, help="open the console in a browser")
    p.add_argument("--no-open", dest="open", action="store_false")
    p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("token", help="print (or --rotate) the API token")
    p.add_argument("--rotate", action="store_true")
    p.set_defaults(fn=cmd_token)
    p = sub.add_parser("doctor", help="check environment and models")
    p.set_defaults(fn=cmd_doctor)
    raw = normalize_argv(list(sys.argv[1:] if argv is None else argv))
    args = parser.parse_args(raw)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
