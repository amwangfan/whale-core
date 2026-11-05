"""Owner「做 xxx」轻量意图：规则匹配 + 简单结果图 + ResultDelivery。"""
from whale_core.life.do_request import (
    match_do_request, try_owner_do, make_simple_result_png,
    build_result_image_items, execute_do_request, is_owner_private_conversation)
from whale_core.life.result_delivery import DeliveryItem


def test_match_result_image_phrases():
    for text in (
        "做一张简单结果图发我",
        "帮我做一张结果图",
        "生成一张测试图给我",
        "给我发一张简单结果图",
        "画一张示意图发我",
    ):
        req = match_do_request(text)
        assert req is not None, text
        assert req.kind == "result_image", text


def test_reject_chitchat_and_group_only_private():
    assert match_do_request("你在做什么") is None
    assert match_do_request("做梦吧") is None
    assert match_do_request("今天天气怎样") is None
    assert is_owner_private_conversation("private:u1")
    assert not is_owner_private_conversation("group:1124744261")


def test_try_owner_do_skips_group():
    assert try_owner_do("做一张简单结果图发我",
                        conversation="group:1", owner_uid="u") is None


def test_execute_result_image_via_fake_deliverer():
    seen = {}

    def deliverer(items, summary):
        seen["summary"] = summary
        seen["kinds"] = [i.kind for i in items]
        assert any(i.kind == "image" and i.image_bytes for i in items)
        return {"ok": True, "count": len(items), "channels": ["qq_c2c"], "receipts": []}

    req = match_do_request("做一张简单结果图发我")
    out = execute_do_request(req, deliverer=deliverer, owner_uid="u_owner")
    assert out["handled"] is True
    assert out["delivery"]["ok"] is True
    assert seen["kinds"] == ["text", "image"]
    assert "结果图" in out["reply"] or "好了" in out["reply"]


def test_png_is_valid_signature():
    png = make_simple_result_png()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    summary, items = build_result_image_items("做一张简单结果图发我")
    assert "结果图" in summary
    assert isinstance(items[1], DeliveryItem)
    assert items[1].image_bytes.startswith(b"\x89PNG")


def test_qq_workflow_private_do_short_circuit():
    """owner 私聊命中时走 ResultDelivery，不调 companion。"""
    from whale_core.life.qq_workflow import QqInboundWorkflow
    from whale_core.life.channels import OwnerEvent
    from whale_core.life.initiative import ProactivePrefs
    from whale_core.memory.store import InMemoryStore

    class BoomBrain:
        def reply(self, *a, **k):
            raise AssertionError("companion should not run for do_request")

    store = InMemoryStore()
    wf = QqInboundWorkflow(
        store, BoomBrain(),
        prefs=ProactivePrefs(enabled=True, allowed_channels=("direct_message",),
                             auto_send=True, daily_limit=0, passive_daily_limit=0))
    seen = {}

    def deliverer(items, summary):
        seen["n"] = len(items)
        return {"ok": True, "count": len(items), "channels": ["qq_c2c"], "receipts": []}

    wf._result_deliverer = deliverer
    wf._result_owner_uid = "u_owner"
    event = OwnerEvent(
        channel="qq", conversation="private:u_owner", owner_id="u_owner",
        sender_id="u_owner", message_id="m-do-1", text="做一张简单结果图发我")
    result = wf.handle_owner_event(event)
    assert result.get("delivered_via") == "result_delivery"
    assert result["do_request"]["handled"] is True
    assert seen["n"] == 2
    # 群聊不短路
    event_g = OwnerEvent(
        channel="qq", conversation="group:1124744261", owner_id="u_owner",
        sender_id="u_owner", message_id="m-do-2", text="做一张简单结果图发我")
    # 无编排器时会打 companion → BoomBrain；群应回落 companion，故预期炸
    try:
        wf.handle_owner_event(event_g)
        raised = False
    except AssertionError:
        raised = True
    assert raised, "group must not short-circuit do_request"
