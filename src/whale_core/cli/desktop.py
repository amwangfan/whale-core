"""Linux/macOS desktop host for the same authenticated local management API.

The optional pywebview window owns the API lifetime. If no GUI backend is
available, use the system browser and keep the terminal running until Ctrl+C.
The per-launch bearer token is printed only to the invoking terminal, never
placed in a URL, JavaScript source, or ordinary log file.
"""
from __future__ import annotations

import argparse
import importlib
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import signal
import sys
import threading
import webbrowser


_SUPPORTED = ("linux", "darwin")


def desktop_data_dir() -> Path:
    """Keep desktop data independent from the CLI's ~/.whale directory."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Whale Core"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "whale-core"


def desktop_log_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "Whale Core"
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "whale-core" / "logs"


def _logger(path: Path) -> logging.Logger:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    target = path / "desktop.log"
    # Create the file privately before FileHandler opens it; no sensitive
    # content is recorded here, but lifecycle logs still belong to the owner.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    os.chmod(target, 0o600)
    logger = logging.Logger("whale-desktop", level=logging.INFO)
    handler = RotatingFileHandler(target, maxBytes=256 * 1024, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def _create_server(data_dir: Path, token: str, static_dir: Path):
    from whale_core.api.server import ApiContext, WhaleApiServer
    from whale_core.configuration import SettingsController, read_config
    from whale_core.profiles import assemble_runtime

    config = read_config(data_dir)
    bundle = assemble_runtime(str(config.get("profile") or "local"), config_dir=str(data_dir))
    try:
        context = ApiContext(store=bundle.store, profile=bundle.profile,
                             manager=bundle.manager, orchestrator=bundle.orchestrator,
                             curator=bundle.companion.curator, optional=bundle.optional,
                             settings=SettingsController(data_dir, bundle))
        return (WhaleApiServer(context, host="127.0.0.1", port=0,
                               token=token, static_dir=str(static_dir)), bundle.store)
    except Exception:
        bundle.store.close()
        raise


def _load_webview():
    return importlib.import_module("webview")


def _wait_for_interrupt() -> None:
    # Browser windows are external processes: closing a tab cannot prove the
    # user is finished. The hosting terminal explicitly controls shutdown.
    threading.Event().wait()


def run_desktop(config_dir: str | Path | None = None, *, browser_only: bool = False) -> int:
    """Open a native window when available, otherwise an authenticated web UI.

    Call this from the main thread, as required by pywebview's GUI loop.
    Closing the native window or interrupting browser mode stops the API and
    closes the SQLite connection. No sidecar child process is left behind.
    """
    if sys.platform not in _SUPPORTED:
        print("桌面入口目前支持 Linux 和 macOS；Windows 不在本轮范围。", file=sys.stderr)
        return 2
    from whale_core.cli.main import _repo_static_dir
    static_dir = _repo_static_dir()
    if static_dir is None:
        print("控制台尚未构建。请安装包含控制台的 wheel，或先运行 pnpm -C web build。", file=sys.stderr)
        return 2
    data_dir = Path(config_dir).expanduser().resolve() if config_dir else desktop_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(data_dir, 0o700)
    logger = _logger(desktop_log_dir())
    server = store = None
    previous_sigterm = None
    lease = None
    try:
        from whale_core.core.lease import RuntimeLease
        lease = RuntimeLease(data_dir)
        token = secrets.token_urlsafe(36)
        server, store = _create_server(data_dir, token, static_dir)
        server.start()
        url = server.url.rsplit("/api/", 1)[0] + "/"
        logger.info("desktop started; interface=loopback")
        print(f"Whale Core 桌面服务：{url}")
        print(f"本次访问 token（粘贴到窗口连接框）：{token}", flush=True)
        print(f"桌面数据目录：{data_dir}")
        if threading.current_thread() is threading.main_thread():
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, _interrupt)
        if not browser_only:
            try:
                webview = _load_webview()
                webview.create_window("Whale Core · 鲸鱼", url=url,
                                      width=1180, height=820, min_size=(760, 560),
                                      background_color="#f4f7f9")
                webview.start(debug=False, private_mode=True)
                return 0
            except ImportError:
                logger.info("native window unavailable; dependency missing")
                print("未安装桌面窗口依赖，已转为系统浏览器。原生窗口可安装：python -m pip install 'whale-core[desktop]'。")
            except Exception as exc:
                # Backend exceptions may contain local private paths. Log only
                # the class, then offer an actionable standard browser fallback.
                logger.warning("native window unavailable; kind=%s", type(exc).__name__)
                print("原生窗口启动失败（请检查系统 WebView 依赖），已转为系统浏览器。")
        print("浏览器模式：保持此终端运行，按 Ctrl+C 停止服务。")
        try:
            opened = webbrowser.open(url, new=1)
        except Exception:
            opened = False
        if not opened:
            print("未能自动打开浏览器，请手动打开上方本机地址。")
        _wait_for_interrupt()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logger.error("desktop failed; kind=%s", type(exc).__name__)
        print(f"桌面服务无法启动：{type(exc).__name__}。请检查配置和日志。", file=sys.stderr)
        return 1
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        try:
            if server is not None:
                server.stop()
        except Exception as exc:
            logger.error("API shutdown failed; kind=%s", type(exc).__name__)
        finally:
            try:
                if store is not None:
                    store.close()
            finally:
                if lease is not None:
                    lease.close()
                logger.info("desktop stopped")
                for handler in tuple(logger.handlers):
                    handler.close()
                    logger.removeHandler(handler)


def _interrupt(_signal, _frame) -> None:
    raise KeyboardInterrupt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Whale Core Linux/macOS desktop launcher")
    parser.add_argument("--config-dir", help="desktop data directory (separate from ~/.whale by default)")
    parser.add_argument("--browser", action="store_true", help="use system browser instead of a native window")
    args = parser.parse_args(argv)
    return run_desktop(args.config_dir, browser_only=args.browser)


if __name__ == "__main__":
    raise SystemExit(main())
