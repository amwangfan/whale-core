"""Desktop lifetime and credential handling without a GUI or external services."""
import types

import pytest

from whale_core.cli import desktop


@pytest.fixture
def host(monkeypatch, tmp_path):
    import importlib
    main_module = importlib.import_module("whale_core.cli.main")
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<div id='root'></div>")
    monkeypatch.setattr(main_module, "_repo_static_dir", lambda: static)
    monkeypatch.setattr(desktop, "desktop_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    state = {"started": 0, "stopped": 0, "closed": 0}
    class Server:
        url = "http://127.0.0.1:43321/api/v1"
        def start(self):
            state["started"] += 1
        def stop(self):
            state["stopped"] += 1
    class Store:
        def close(self):
            state["closed"] += 1
    def create(path, token, static_dir):
        state.update(data=path, token=token, static=static_dir)
        return Server(), Store()
    monkeypatch.setattr(desktop, "_create_server", create)
    return state, tmp_path


def test_native_close_stops_api_and_store(host, monkeypatch, capsys):
    state, path = host
    def window(title, **kwargs):
        state["window"] = kwargs
    def start(**kwargs):
        state["gui_options"] = kwargs
    monkeypatch.setattr(desktop, "_load_webview", lambda: types.SimpleNamespace(create_window=window, start=start))
    assert desktop.run_desktop(path / "data") == 0
    assert (state["started"], state["stopped"], state["closed"]) == (1, 1, 1)
    assert state["window"]["url"] == "http://127.0.0.1:43321/"
    assert "js_api" not in state["window"]
    assert state["token"] not in state["window"]["url"]
    assert state["gui_options"] == {"debug": False, "private_mode": True}
    assert state["token"] in capsys.readouterr().out
    assert state["token"] not in (path / "logs" / "desktop.log").read_text()
    assert (path / "data").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("failure", [ImportError("missing"), RuntimeError("private backend details")])
def test_missing_or_failed_gui_uses_browser(host, monkeypatch, failure, capsys):
    state, path = host
    def unavailable():
        raise failure
    monkeypatch.setattr(desktop, "_load_webview", unavailable)
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url, new: state.update(browser=url) or True)
    monkeypatch.setattr(desktop, "_wait_for_interrupt", lambda: None)
    assert desktop.run_desktop(path / "data") == 0
    assert state["browser"] == "http://127.0.0.1:43321/"
    assert state["stopped"] == state["closed"] == 1
    output = capsys.readouterr().out
    assert "转为系统浏览器" in output
    assert "private backend details" not in output
    assert "private backend details" not in (path / "logs" / "desktop.log").read_text()


def test_browser_interrupt_cleans_up_and_token_changes(host, monkeypatch):
    state, path = host
    tokens = []
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url, new: True)
    def interrupt():
        tokens.append(state["token"])
        raise KeyboardInterrupt
    monkeypatch.setattr(desktop, "_wait_for_interrupt", interrupt)
    assert desktop.run_desktop(path / "data", browser_only=True) == 0
    assert desktop.run_desktop(path / "data", browser_only=True) == 0
    assert state["stopped"] == state["closed"] == 2
    assert len(set(tokens)) == 2


def test_windows_is_out_of_scope(monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    assert desktop.run_desktop() == 2


def test_missing_static_fails_before_start(host, monkeypatch):
    import importlib
    main_module = importlib.import_module("whale_core.cli.main")
    monkeypatch.setattr(main_module, "_repo_static_dir", lambda: None)
    state, path = host
    assert desktop.run_desktop(path / "data") == 2
    assert state["started"] == 0


def test_real_loopback_host_uses_dynamic_port_and_shuts_down(tmp_path, monkeypatch):
    import importlib
    import json
    import urllib.error
    import urllib.request
    main_module = importlib.import_module("whale_core.cli.main")
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<div id='root'></div>")
    monkeypatch.setattr(main_module, "_repo_static_dir", lambda: static)
    monkeypatch.setattr(desktop, "desktop_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    monkeypatch.setattr(desktop.secrets, "token_urlsafe", lambda _: "desktop-unit-token")
    observed = {}
    def window(title, **kwargs):
        observed["url"] = kwargs["url"]
    def start(**kwargs):
        request = urllib.request.Request(observed["url"] + "api/v1/health",
                                         headers={"Authorization": "Bearer desktop-unit-token"})
        with urllib.request.urlopen(request, timeout=3) as response:
            assert json.load(response)["result"]["ok"]
        with urllib.request.urlopen(observed["url"], timeout=3) as response:
            assert b"root" in response.read()
    monkeypatch.setattr(desktop, "_load_webview", lambda: types.SimpleNamespace(create_window=window, start=start))
    assert desktop.run_desktop(tmp_path / "data") == 0
    assert ":0/" not in observed["url"]
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(observed["url"], timeout=1)
    assert (tmp_path / "data" / "memory.sqlite3").is_file()
