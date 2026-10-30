import pytest

from whale_core.core.companion import WhaleCompanion
from whale_core.core.runtime import ConversationOrchestrator, ToolRegistry, ToolSpec
from whale_core.memory.store import InMemoryStore
from whale_core.service.workflows import WorkflowsService

class Brain:
    def reply(self, text, **kw): return "summary"

def service(tmp_path=None):
    store, tools = InMemoryStore(), ToolRegistry()
    runtime = ConversationOrchestrator(store, WhaleCompanion(store, Brain()), tools=tools)
    return WorkflowsService(runtime, skill_dir=tmp_path)

def test_builtin_skill_executes_search_and_reads_real_returned_url():
    svc = service()
    calls = []
    svc.runtime.tools.register(ToolSpec("search_web", "web.search", handler=lambda a: "资料（https://example.com/page）"))
    svc.runtime.tools.register(ToolSpec("fetch_url", "web.fetch", handler=lambda a: calls.append(a["url"]) or "source text"))
    result = svc.run_skill("search-and-read", "技术资料")
    assert result["status"] == "succeeded"
    assert result["outputs"]["page"] == "source text"
    assert calls == ["https://example.com/page"]
    assert len(result["tasks"]) == 2
    assert svc.resume_workflow(result["workflow_id"]) == result

def test_skill_path_and_symlink_escape_rejected(tmp_path):
    svc = service(tmp_path)
    with pytest.raises(ValueError): svc.run_skill("../escape", "input")
    secret = tmp_path.parent / "outside.md"
    secret.write_text("# skill: escape\n## step: x\ntool: x\n")
    (tmp_path / "escape.md").symlink_to(secret)
    assert svc.list_skills() == []
    with pytest.raises(KeyError): svc.run_skill("escape", "input")

def test_workflow_pause_and_settings_validation():
    svc = service()
    svc.update_settings({"paused": True})
    with pytest.raises(ValueError): svc.run_skill("search-and-read", "input")
    assert svc.explore(["FPGA"])["status"] == "paused"
    with pytest.raises(ValueError): svc.update_settings({"quiet_start": "25:00"})
    with pytest.raises(ValueError): svc.update_settings({"daily_limit": True})
    assert svc.get_settings()["paused"] is True

def test_exploration_fetches_sources_then_proposes_without_sending():
    svc = service()
    svc.update_settings({"quiet_start": "00:00", "quiet_end": "00:00", "daily_limit": 1})
    calls = []
    svc.runtime.tools.register(ToolSpec("search_web", "web.search", handler=lambda a: "来源（https://example.com/fpga）"))
    svc.runtime.tools.register(ToolSpec("fetch_url", "web.fetch", handler=lambda a: calls.append(a["url"]) or "FPGA hardware evidence"))
    result = svc.explore(["FPGA"])
    assert result["sources_read"] == 1
    assert len(result["drafts"]) == 1
    assert result["execution_status"] == "not_dispatched"
    draft = svc.store.get("life:draft:" + result["drafts"][0]["draft_id"])
    assert draft["evidence"] == ["https://example.com/fpga"]
    assert svc.explore()["status"] == "quota_exhausted"
    assert len(calls) == 1

def test_exploration_does_not_mark_failed_fetch_as_inspected():
    svc = service()
    svc.update_settings({"quiet_start": "00:00", "quiet_end": "00:00"})
    svc.runtime.tools.register(ToolSpec("search_web", "web.search", handler=lambda a: "https://example.com/x"))
    def bad(args): raise OSError("network error")
    svc.runtime.tools.register(ToolSpec("fetch_url", "web.fetch", handler=bad))
    result = svc.explore(["FPGA"])
    assert result["sources_read"] == 0
    assert result["drafts"] == []

def test_pending_workflow_continues_only_after_exact_task_approval(tmp_path):
    (tmp_path / "notify.md").write_text("# skill: notify\n## step: first\ntool: send\nsave_as: sent\n## step: after\ntool: read\nsave_as: after\n")
    svc = service(tmp_path)
    approved, calls = [False], []
    class Gateway:
        def dispatch(self, intent):
            if not approved[0]: return "plugin-hold:mail:send"
            calls.append("send")
            return "plugin:mail:send:ok:sent"
    svc.runtime.gateway = Gateway()
    svc.runtime.tools.register(ToolSpec("send", "mail", risk_class="communicate", plugin_id="mail", action="send"))
    svc.runtime.tools.register(ToolSpec("read", "read", handler=lambda a: calls.append("read") or "read"))
    first = svc.run_skill("notify", "input")
    assert first["status"] == "pending_approval"
    assert svc.resume_workflow(first["workflow_id"])["status"] == "pending_approval"
    task_id = first["tasks"][0]
    svc.runtime.decide_task(task_id, "approve")
    approved[0] = True
    svc.runtime.resume_task(task_id)
    final = svc.resume_workflow(first["workflow_id"])
    assert final["status"] == "succeeded"
    assert calls == ["send", "read"]
