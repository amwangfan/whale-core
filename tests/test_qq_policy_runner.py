"""issue #12（审批/dry-run 分离、双开关）+ issue #13（双策略默认与 env 优先级）。

运行器按文件路径加载（examples 不是包）；全部离线：不触网、不起监听、
真实发送端仅在构造层面断言（WeQWebSendPort 构造不发起请求）。
"""

from __future__ import annotations

import importlib.util
import urllib.request
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from whale_core.life.channels import OwnerEvent
from whale_core.life.initiative import ProactiveDraft, ProactiveOutbox, ProactivePrefs
from whale_core.memory.store import InMemoryStore
from weq_channel.weq_send import WeQWebSendPort

RUNNER_PATH = Path(__file__).resolve().parents[1] / "examples" / "qq_live_workflow.py"

ENV_KEYS = (
    "DAILY_LIMIT", "QUIET_START", "QUIET_END", "PASSIVE_DAILY_LIMIT",
    "PASSIVE_REPLY_IN_QUIET", "QQ_LIVE_AUTOSEND", "WEQ_WEB_TOKEN",
    "WEQ_WEB_URL", "OWNER_UID", "QQ_SELF_UIN", "WEQ_WEB_ALLOWED_HOSTS",
)


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("qq_live_workflow_under_test", RUNNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_KEYS:
        monkeypatch.delenv(name, raising=False)


def set_weq_env(monkeypatch):
    monkeypatch.setenv("WEQ_WEB_TOKEN", "synthetic-web-token")
    monkeypatch.setenv("WEQ_WEB_URL", "http://127.0.0.1:7690")
    monkeypatch.setenv("OWNER_UID", "u_Synthetic123")


def propose_draft(store, draft_id="qq-reply:m1", text="回复内容"):
    outbox = ProactiveOutbox(store)
    outbox.propose(ProactiveDraft(draft_id, text, "r", ("e",), channel="direct_message"))
    return outbox


class TruePort:
    def send(self, draft):
        return True


# ── issue #13：默认值、env 优先级、双策略 ────────────────────────────


def test_risk_prefs_defaults_are_conservative(runner):
    prefs = runner.risk_prefs()
    assert prefs.daily_limit == 10                      # 主动：与文档 §6.5 一致
    assert prefs.quiet_start == dtime(22) and prefs.quiet_end == dtime(7)
    assert prefs.passive_daily_limit == 200             # 被动：独立保险丝
    assert prefs.passive_reply_in_quiet is True         # 显式产品决策，非隐式关静默
    assert prefs.allowed_channels == frozenset({"direct_message"})


def test_risk_prefs_env_overrides(runner, monkeypatch):
    monkeypatch.setenv("DAILY_LIMIT", "3")
    monkeypatch.setenv("QUIET_START", "23")
    monkeypatch.setenv("PASSIVE_REPLY_IN_QUIET", "0")
    monkeypatch.setenv("PASSIVE_DAILY_LIMIT", "50")
    prefs = runner.risk_prefs()
    assert (prefs.daily_limit, prefs.quiet_start.hour) == (3, 23)
    assert prefs.passive_reply_in_quiet is False
    assert prefs.passive_daily_limit == 50


def test_policy_summary_prints_no_secrets(runner, monkeypatch):
    set_weq_env(monkeypatch)
    monkeypatch.setenv("WEQ_SSE_TOKEN", "SUPER-SECRET-VALUE")
    line = runner.policy_summary(runner.risk_prefs())
    assert "SUPER-SECRET-VALUE" not in line
    assert "synthetic-web-token" not in line
    assert "被动回复" in line and "主动" in line


def test_passive_quiet_exemption_matrix():
    """issue #22（owner 决策 2026-11-01）：被动回复**一律**豁免静默——
    与 passive_reply_in_quiet 旧开关无关（该字段已废弃，仅为配置兼容保留）；
    主动打扰（unsolicited）在静默时段仍然 deferred。"""
    store = InMemoryStore()
    outbox = ProactiveOutbox(store)
    night = datetime(2026, 10, 29, 23, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    base = dict(enabled=True, allowed_channels=frozenset({"direct_message"}),
                daily_limit=10, quiet_start=dtime(22), quiet_end=dtime(7))
    exempt = ProactivePrefs(passive_reply_in_quiet=True, **base)
    strict = ProactivePrefs(passive_reply_in_quiet=False, **base)

    for did in ("p1", "p2", "a1", "c1", "x1", "a2"):
        outbox.propose(ProactiveDraft(did, "回复", "r", ("e",), channel="direct_message"))

    # 被动回复：无论旧开关 True/False，都不再被静默拦截（政策固定豁免）
    assert outbox.inspect("p1", exempt, now=night, passive_reply=True).status == "needs_approval"
    assert outbox.inspect("p2", strict, now=night, passive_reply=True).status == "needs_approval"
    # 主动打扰：静默时段仍 deferred
    assert outbox.inspect("a1", exempt, now=night, passive_reply=False).status == "deferred"
    # response_origin 显式语义：reactive_completion（任务续答）同样豁免
    default_prefs = ProactivePrefs(enabled=True,
                                   allowed_channels=frozenset({"direct_message"}))
    assert outbox.inspect("c1", default_prefs, now=night,
                          response_origin="reactive_completion").status == "needs_approval"
    assert outbox.inspect("a2", default_prefs, now=night,
                          response_origin="unsolicited").status == "deferred"
    # 未知 origin fail closed → unsolicited
    assert outbox.inspect("x1", default_prefs, now=night,
                          response_origin="bogus").status == "deferred"


def test_passive_quota_separate_from_proactive():
    store = InMemoryStore()
    outbox = ProactiveOutbox(store)
    day = datetime(2026, 10, 29, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    prefs = ProactivePrefs(enabled=True, allowed_channels=frozenset({"direct_message"}),
                           daily_limit=1, passive_daily_limit=2,
                           quiet_start=dtime(0), quiet_end=dtime(0))
    for i in range(4):
        outbox.propose(ProactiveDraft(f"d{i}", "t", "r", ("e",), channel="direct_message"))

    v = [outbox.dispatch(f"d{i}", prefs, now=day, port=TruePort(),
                         explicitly_approved=True, passive_reply=True) for i in range(3)]
    assert [x.status for x in v] == ["accepted", "accepted", "over_budget"]
    # 主动额度未被被动派发占用
    v3 = outbox.dispatch("d3", prefs, now=day, port=TruePort(), explicitly_approved=True)
    assert v3.status == "accepted"
    assert store.get("life:quota:passive:2026-10-29") == 2
    assert store.get("life:quota:2026-10-29") == 1


def test_qq_workflow_reply_uses_passive_policy(runner):
    from whale_core.life.qq_workflow import QqInboundWorkflow
    store = InMemoryStore()
    prefs = ProactivePrefs(enabled=True, allowed_channels=frozenset({"direct_message"}),
                           daily_limit=10, quiet_start=dtime(22), quiet_end=dtime(7),
                           passive_reply_in_quiet=True, passive_daily_limit=200)
    wf = QqInboundWorkflow(
        store, runner.StubBrain(), prefs=prefs,
        now_fn=lambda: datetime(2026, 10, 29, 23, 30, tzinfo=ZoneInfo("Asia/Shanghai")))
    result = wf.handle_owner_event(OwnerEvent(
        owner_id="owner", channel="qq", conversation="private:c1",
        message_id="m99", text="在吗"))
    # 深夜被动回复不被静默拦截（豁免生效），但仍需人工批准
    assert result["verdict"] == "needs_approval"


# ── issue #12：审批默认、dry-run 隔离、双开关 ────────────────────────


def test_env_configured_does_not_implicitly_enable_live_send(runner, monkeypatch):
    """P0 核心：环境变量配齐 ≠ 自动切到真实发送端。"""
    set_weq_env(monkeypatch)
    port = runner.build_send_port(live_ok=False)
    assert isinstance(port, runner.ConsoleSendPort)
    assert not isinstance(port, WeQWebSendPort)


def test_live_send_port_only_when_explicit(runner, monkeypatch):
    set_weq_env(monkeypatch)
    port = runner.build_send_port(live_ok=True)
    assert isinstance(port, WeQWebSendPort)


def test_live_auto_send_requires_both_switches(runner, monkeypatch):
    args = SimpleNamespace(live_auto_send=True)
    assert runner.live_auto_send_enabled(args) is False          # 只有 CLI 开关
    monkeypatch.setenv("QQ_LIVE_AUTOSEND", "1")
    assert runner.live_auto_send_enabled(args) is True           # 双开关齐
    assert runner.live_auto_send_enabled(
        SimpleNamespace(live_auto_send=False)) is False          # 只有 env



def test_dry_run_and_live_auto_send_are_mutually_exclusive(runner, capsys):
    args = SimpleNamespace(auto_approve=True, live_auto_send=True)
    assert runner.cmd_run(args) == 2
    assert "互斥" in capsys.readouterr().out


def test_shared_cli_paths_are_accepted_after_subcommand(runner, monkeypatch):
    import sys
    seen = {}
    def fake_run(args):
        seen.update(secrets=args.secrets, store=args.store, inbox=args.inbox)
        return 0
    monkeypatch.setattr(runner, "cmd_run", fake_run)
    monkeypatch.setattr(sys, "argv", [
        str(RUNNER_PATH), "run", "--secrets", "/tmp/qq.env",
        "--store", "/tmp/qq.sqlite3", "--inbox", "/tmp/inbox.sqlite3",
    ])
    assert runner.main() == 0
    assert seen == {
        "secrets": "/tmp/qq.env", "store": "/tmp/qq.sqlite3",
        "inbox": "/tmp/inbox.sqlite3",
    }


def test_dry_run_leaves_real_ledger_and_quota_untouched(runner):
    store = InMemoryStore()
    outbox = propose_draft(store)
    status = runner.dispatch_dry_run(store, "qq-reply:m1")
    assert status == "accepted"
    ledger = outbox.ledger("qq-reply:m1")
    assert ledger["status"] == "draft" and ledger["attempts"] == 0   # 真实账本零写入
    assert not any(k.startswith("life:quota") for k in store.keys()) # 额度未占用
    # 草稿仍可人工真批
    verdict = outbox.inspect("qq-reply:m1", runner.risk_prefs(),
                             now=datetime.now(timezone.utc), passive_reply=True)
    assert verdict.status == "needs_approval"


def test_dry_run_makes_no_http(runner, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("dry-run must not touch network")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    store = InMemoryStore()
    propose_draft(store, "qq-reply:m2")
    assert runner.dispatch_dry_run(store, "qq-reply:m2") == "accepted"


def test_dry_run_port_is_print_only(runner):
    port = runner.DryRunSendPort()
    draft = ProactiveDraft("d", "文本", "r", ("e",), channel="direct_message")
    assert port.send(draft) is True and port.calls == ["d"]
    assert not hasattr(port, "base_url") and not hasattr(port, "_opener")


def test_manual_approve_without_live_config_records_unknown(runner):
    """人工 approve 但未配真实端：console stub 如实记 unknown，不冒充受理。"""
    store = InMemoryStore()
    outbox = propose_draft(store, "qq-reply:m3")
    status = runner.dispatch_approved(store, "qq-reply:m3", port=runner.ConsoleSendPort())
    assert status == "unknown"
    assert outbox.ledger("qq-reply:m3")["status"] == "unknown"
    # unknown 不自动重试：再次 dispatch → already_attempted
    again = runner.dispatch_approved(store, "qq-reply:m3", port=runner.ConsoleSendPort())
    assert again == "already_attempted"


def test_multibubble_segments_each_need_outbox_entry():
    """2026-11-01 生产事故：多气泡分段用 ``主草稿id#i`` 派发，但台账只有主草稿
    → 网关 outbox 模式查不到分段 → fail:missing:not found 全段丢发。
    修复纪律：分段派发前各自 propose 入台账。"""
    store = InMemoryStore()
    outbox = ProactiveOutbox(store)
    day = datetime(2026, 11, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    prefs = ProactivePrefs(enabled=True, allowed_channels=frozenset({"direct_message"}),
                           quiet_start=dtime(0), quiet_end=dtime(0))
    base = "qq-reply:multi-1"
    segments = ["第一段", "第二段"]
    outbox.propose(ProactiveDraft(base, "第一段\n第二段", "r", ("e",),
                                  channel="direct_message"))
    # 反事实：未入台账的分段 id 派发被拒（事故形态）
    missing = outbox.dispatch(f"{base}#0", prefs, now=day, port=TruePort(),
                              explicitly_approved=True, passive_reply=True)
    assert "missing" in f"{missing.status}:{missing.reason}" or missing.status != "accepted"
    # 修复后：分段各自 propose → 各段 accepted
    for i, seg in enumerate(segments):
        outbox.propose(ProactiveDraft(f"{base}#{i}", seg, "多气泡分段", ("e",),
                                      channel="direct_message"))
    verdicts = [outbox.dispatch(f"{base}#{i}", prefs, now=day, port=TruePort(),
                                explicitly_approved=True, passive_reply=True)
                for i in range(2)]
    assert [v.status for v in verdicts] == ["accepted", "accepted"]
