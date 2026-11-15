import json
from urllib.request import urlopen
from urllib.error import HTTPError

import pytest

from whale_core.api.server import ApiContext, WhaleApiServer
from whale_core.memory.store import InMemoryStore


@pytest.fixture
def console(tmp_path):
    (tmp_path / "index.html").write_text('<div id="root">console</div>', encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "page.js").write_bytes(b"export default 42;")
    server = WhaleApiServer(ApiContext(store=InMemoryStore()), token="test", port=0, static_dir=str(tmp_path))
    server.start()
    yield server.url.rsplit("/api/", 1)[0], tmp_path
    server.stop()


@pytest.mark.parametrize("path", ["/", "/workspace/devices"])
def test_entry_and_extensionless_routes_keep_spa_fallback(console, path):
    base, _ = console
    with urlopen(base + path, timeout=5) as response:
        assert response.status == 200
        assert response.headers.get_content_type() == "text/html"
        assert b"console" in response.read()


@pytest.mark.parametrize("path", ["/assets/stale.js", "/assets/missing", "/icon.png", "/missing.css"])
def test_missing_resources_never_return_successful_html(console, path):
    base, _ = console
    with pytest.raises(HTTPError) as error:
        urlopen(base + path, timeout=5)
    assert error.value.code == 404
    assert json.load(error.value)["error"]["code"] == "not_found"


def test_existing_chunk_serves_exact_bytes(console):
    base, _ = console
    with urlopen(base + "/assets/page.js", timeout=5) as response:
        assert response.status == 200
        assert response.read() == b"export default 42;"
        assert response.headers["Cache-Control"] == "no-store"


def test_resource_removed_between_stat_and_read_returns_404(console, monkeypatch):
    from pathlib import Path
    base, _ = console
    original = Path.read_bytes
    def gone(path):
        if path.name == "page.js":
            raise FileNotFoundError("private filesystem details")
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", gone)
    with pytest.raises(HTTPError) as error:
        urlopen(base + "/assets/page.js", timeout=5)
    assert error.value.code == 404
    assert "private" not in error.value.read().decode()
