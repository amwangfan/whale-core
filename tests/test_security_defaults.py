"""安全默认快照与只读 API。"""
from pathlib import Path

from whale_core.security.defaults import security_defaults_snapshot


def test_snapshot_deny_by_default_and_classification_rule():
    snap = security_defaults_snapshot()
    assert snap["deny_by_default"] is True
    assert snap["classification_is_not_authorization"] is True
    assert snap["intent_router_cannot_grant"] is True
    assert "tool.shell.safe" in snap["shell_acl"]["scope"]
    assert "chat.basic" in snap["grant_boundaries"]["grantable_scopes"]
    assert snap["grant_boundaries"]["transaction_never_auto"] is True


def test_security_defaults_route_registered():
    import whale_core.api.routes.system as system
    assert callable(system.security_defaults)
    server_src = Path(system.__file__).resolve().parents[1] / "server.py"
    text = server_src.read_text(encoding="utf-8")
    assert '["security", "defaults"]' in text
    assert "security_defaults" in text
