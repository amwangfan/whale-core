"""移动端聊天 v2 REST 面测试（开发规划 Phase 4 Core 侧）。

覆盖：会话列表 / 消息倒序分页 / 附件上传→下载→删除全链 /
AttachmentRef schema 合规（contracts/v2 镜像 + whale-mobile vendor 的
jsonschema）/ SSE 事件序 / client_message_id 幂等 / 越界 attachment_id 拒绝 /
超限拒绝。全部真实回环 HTTP（127.0.0.1 + 临时端口），不触外网。
"""

from __future__ import annotations

import json
import struct
import sys
from http.client import HTTPConnection
from pathlib import Path

import pytest

from whale_core.api.server import ApiContext, WhaleApiServer
from whale_core.core.companion import WhaleCompanion
from whale_core.core.runtime import ConversationOrchestrator
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.store import InMemoryStore

# whale-mobile 的 tests/_vendor（jsonschema 及其依赖），与本仓平级；
# 缺邻仓时仅 schema 合规用例跳过，其余照跑（沿用 test_notifications_contract 先例）
_WHALE_MOBILE_VENDOR = (
    Path(__file__).resolve().parents[2] / "whale-mobile" / "tests" / "_vendor")
if _WHALE_MOBILE_VENDOR.is_dir():
    sys.path.insert(0, str(_WHALE_MOBILE_VENDOR))

jsonschema = pytest.importorskip(
    "jsonschema",
    reason="需要 whale-mobile/tests/_vendor 的 jsonschema（邻仓缺失时跳过）")

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts" / "v2" / "objects"
TOKEN = "mobile-test-token"


class FakeBrain:
    """记录 images 透传的假模型（验证附件走现有多模态链路）。"""

    def __init__(self):
        self.seen_images = ()

    def reply(self, text, *, persona="", memories=(), history=(), images=()):
        self.seen_images = tuple(images)
        return f"[fake] {text}"


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("WHALE_ATTACHMENTS_DIR", str(tmp_path / "attachments"))
    store = InMemoryStore()
    brain = FakeBrain()
    curator = MemoryCurator(store)
    companion = WhaleCompanion(store, brain, curator=curator)
    orch = ConversationOrchestrator(store, companion)
    ctx = ApiContext(store=store, curator=curator, orchestrator=orch,
                     profile="demo")
    server = WhaleApiServer(ctx, port=0, token=TOKEN)
    server.brain = brain  # 测试探针
    server.start()
    yield server
    server.stop()


def _req(server, method, path, *, token=TOKEN, body=None, raw_body=None,
         content_type="application/json"):
    conn = HTTPConnection("127.0.0.1", server.port, timeout=10)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = None
    if raw_body is not None:
        payload = raw_body
        headers["Content-Type"] = content_type
    elif body is not None:
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    result = (resp.status, data, resp.getheader("Content-Type"),
              resp.getheader("Content-Length"))
    conn.close()
    return result


def _json(raw):
    return json.loads(raw.decode("utf-8"))


def _sse_events(raw: bytes) -> list[dict]:
    events = []
    for block in raw.decode("utf-8").split("\n\n"):
        block = block.strip()
        if block.startswith("data: "):
            events.append(json.loads(block[len("data: "):]))
    return events


def _png(width: int, height: int) -> bytes:
    """最小 PNG 头部（签名 + IHDR 宽高），足以让尺寸解析器取到真实宽高。"""
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
            + b"\x00" * 32)


def _multipart(filename: str, data: bytes, content_type: str,
               fields: dict | None = None,
               boundary: str = "----whaletestboundary") -> tuple[bytes, str]:
    parts = []
    for key, value in (fields or {}).items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8"))
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
        + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _upload(api, filename="a.png", data=None, content_type="image/png",
            fields=None):
    body, ctype = _multipart(filename, data if data is not None else _png(4, 3),
                             content_type, fields=fields)
    return _req(api, "POST", "/api/v2/chat/attachments",
                raw_body=body, content_type=ctype)


def _stream(api, cid="conv-1", cmid="msg-00001", text="你好", attachments=None):
    body = {"conversation_id": cid, "client_message_id": cmid, "text": text}
    if attachments is not None:
        body["attachments"] = attachments
    status, raw, _, _ = _req(api, "POST", "/api/v2/chat/stream", body=body)
    return status, _sse_events(raw)


# ---------------------------------------------------------------------------
# 鉴权 / 路由
# ---------------------------------------------------------------------------

def test_v2_requires_bearer(api):
    status, raw, _, _ = _req(api, "GET", "/api/v2/conversations", token="")
    assert status == 401
    assert _json(raw)["error"]["code"] == "unauthorized"


def test_v2_unknown_path_404(api):
    status, _, _, _ = _req(api, "GET", "/api/v2/plugins")
    assert status == 404  # v2 面不泄漏 v1 路由


# ---------------------------------------------------------------------------
# 附件全链
# ---------------------------------------------------------------------------

def test_attachment_upload_matches_contract_schema(api):
    status, raw, _, _ = _upload(api)
    assert status == 201
    ref = _json(raw)
    schema = json.loads(
        (CONTRACTS / "attachment-ref.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(ref, schema)  # 严格过 contracts/v2 镜像
    assert ref["attachment_id"].startswith("att_")
    assert ref["type"] == "image" and ref["mime"] == "image/png"
    assert ref["size"] > 0 and len(ref["sha256"]) == 64
    assert (ref["width"], ref["height"]) == (4, 3)  # 图片解码取宽高


def test_attachment_download_and_delete_cycle(api):
    payload = _png(8, 6)
    status, raw, _, _ = _upload(api, data=payload)
    ref = _json(raw)
    aid = ref["attachment_id"]

    status, raw, ctype, clen = _req(api, "GET", f"/api/v2/attachments/{aid}")
    assert status == 200
    assert ctype == "image/png"  # 不带 charset 后缀
    assert int(clen) == len(payload)
    assert raw == payload

    status, raw, _, _ = _req(api, "DELETE", f"/api/v2/attachments/{aid}")
    assert status == 200 and _json(raw)["result"]["deleted"] is True

    # 删除后：410 Gone（区别于从未存在的 404）；blob 文件不留孤儿
    status, raw, _, _ = _req(api, "GET", f"/api/v2/attachments/{aid}")
    assert status == 410 and _json(raw)["error"]["code"] == "gone"
    status, _, _, _ = _req(api, "DELETE", f"/api/v2/attachments/{aid}")
    assert status == 410
    root = Path(api.ctx.attachments.root)
    assert not (root / f"{aid}.bin").exists()
    assert json.loads((root / f"{aid}.json").read_text())["deleted_at"]


def test_attachment_never_existed_404(api):
    aid = "att_" + "A" * 26  # 格式合法但从未存在
    status, raw, _, _ = _req(api, "GET", f"/api/v2/attachments/{aid}")
    assert status == 404 and _json(raw)["error"]["code"] == "not_found"


def test_attachment_out_of_pattern_id_rejected(api):
    """越界/畸形 id（含穿越形态）一律拒绝，且不得拼路径。"""
    for bad in ("att_01J9XK3EM8TQ2ZP0VN6W4RA8F!",  # 非法字符
                "att_short",                       # 长度不足
                "..%2F..%2Fetc%2Fpasswd",          # 路径穿越（URL 编码）
                "att_01j9xk3em8tq2zP0VN6W4RA8FF"):  # 小写不在 Crockford 集
        status, raw, _, _ = _req(api, "GET", f"/api/v2/attachments/{bad}")
        assert status == 404, bad
        assert _json(raw)["error"]["code"] == "not_found"
        status, _, _, _ = _req(api, "DELETE", f"/api/v2/attachments/{bad}")
        assert status == 404, bad


def test_attachment_type_classification_and_declared_mismatch(api):
    status, raw, _, _ = _upload(api, filename="a.pdf", data=b"%PDF-1.4 fake",
                                content_type="application/pdf")
    assert _json(raw)["type"] == "file"
    status, raw, _, _ = _upload(api, filename="a.mp3", data=b"ID3xxxx",
                                content_type="audio/mpeg")
    assert _json(raw)["type"] == "audio"
    # 声明类型与 MIME 侦测不一致 → 400
    status, raw, _, _ = _upload(api, filename="a.png",
                                content_type="image/png",
                                fields={"type": "video"})
    assert status == 400


def test_attachment_corrupt_image_rejected(api):
    # 声称 image/png 却解不出 IHDR：拒绝入库，不伪造宽高
    status, raw, _, _ = _upload(api, data=b"not a real png at all",
                                content_type="image/png")
    assert status == 400
    assert _json(raw)["error"]["code"] == "bad_request"


def test_attachment_oversize_rejected(api, monkeypatch):
    import whale_core.cloud.attachments as att_mod
    monkeypatch.setattr(att_mod, "MAX_ATTACHMENT_BYTES", 100)
    status, raw, _, _ = _upload(api, data=b"x" * 200,
                                content_type="application/octet-stream")
    assert status == 413
    assert _json(raw)["error"]["code"] == "too_large"


def test_attachment_upload_non_multipart_400(api):
    status, _, _, _ = _req(api, "POST", "/api/v2/chat/attachments",
                           body={"not": "multipart"})
    assert status == 400


# ---------------------------------------------------------------------------
# 流式聊天 / SSE 事件序 / 幂等
# ---------------------------------------------------------------------------

def test_stream_sse_event_sequence(api):
    status, events = _stream(api)
    assert status == 200
    types = [e["type"] for e in events]
    # 词汇表（规划 §5.5）：phase/delta/tool.* /attachment.created/done/error
    vocab = {"phase", "delta", "tool.started", "tool.progress",
             "tool.result", "attachment.created", "done", "error"}
    assert set(types) <= vocab
    assert types[0] == "phase" and events[0]["phase"] == "received"
    assert types[-1] == "done"
    done = events[-1]
    assert done["reply"] == "[fake] 你好" and done["duplicate"] is False
    assert done["message_ids"]["user"].startswith("evt_")
    # 每个事件带 evt_ id 与毫秒 ts
    for event in events:
        assert event["id"].startswith("evt_") and isinstance(event["ts"], int)
    # 消息落移动端日志
    log = api.ctx.store.get("mobile:chat:conv-1")
    assert [m["role"] for m in log] == ["user", "assistant"]


def test_stream_idempotent_replay(api):
    status1, events1 = _stream(api, cmid="msg-dup01")
    status2, events2 = _stream(api, cmid="msg-dup01")
    assert status1 == status2 == 200
    done1, done2 = events1[-1], events2[-1]
    assert done1["duplicate"] is False and done2["duplicate"] is True
    assert done2["reply"] == done1["reply"]
    assert done2["message_ids"] == done1["message_ids"]
    # 重复提交不产生两条消息
    log = api.ctx.store.get("mobile:chat:conv-1")
    assert len(log) == 2
    # 新 client_message_id → 正常追加
    _stream(api, cmid="msg-dup02")
    assert len(api.ctx.store.get("mobile:chat:conv-1")) == 4


def test_stream_validation_errors(api):
    # client_message_id 不符合 §3 idempotency_key pattern
    status, _, _, _ = _req(api, "POST", "/api/v2/chat/stream",
                           body={"conversation_id": "c", "text": "hi",
                                 "client_message_id": "短"})
    assert status == 400
    # 空文本且无附件
    status, _, _, _ = _req(api, "POST", "/api/v2/chat/stream",
                           body={"conversation_id": "c", "text": "  ",
                                 "client_message_id": "msg-00001"})
    assert status == 400
    # 引用不存在的附件 → 404
    status, _, _, _ = _req(
        api, "POST", "/api/v2/chat/stream",
        body={"conversation_id": "c", "text": "看图",
              "client_message_id": "msg-00001",
              "attachments": [{"attachment_id": "att_" + "B" * 26}]})
    assert status == 404
    # 附件 id 越界格式 → 400
    status, _, _, _ = _req(
        api, "POST", "/api/v2/chat/stream",
        body={"conversation_id": "c", "text": "看图",
              "client_message_id": "msg-00001",
              "attachments": [{"attachment_id": "../../etc/passwd"}]})
    assert status == 400


def test_stream_image_attachment_reaches_multimodal_chain(api):
    status, raw, _, _ = _upload(api)
    aid = _json(raw)["attachment_id"]
    status, events = _stream(api, cmid="msg-img01", text="看这张",
                             attachments=[{"attachment_id": aid}])
    assert status == 200 and events[-1]["type"] == "done"
    # 附件字节经现有多模态链路（InputEnvelope.images）到达模型层
    assert len(api.brain.seen_images) == 1
    blob, mime = api.brain.seen_images[0]
    assert mime == "image/png" and blob.startswith(b"\x89PNG")
    # 用户消息记录携带严格 schema 子集的附件引用
    log = api.ctx.store.get("mobile:chat:conv-1")
    assert log[0]["attachments"][0]["attachment_id"] == aid
    assert set(log[0]["attachments"][0]) <= {
        "attachment_id", "type", "mime", "size", "sha256",
        "width", "height", "duration_ms"}


# ---------------------------------------------------------------------------
# 会话列表 / 消息分页
# ---------------------------------------------------------------------------

def test_conversations_list_and_cursor_pagination(api):
    _stream(api, cid="conv-a", cmid="msg-00001", text="一")
    _stream(api, cid="conv-b", cmid="msg-00001", text="二")
    _stream(api, cid="conv-b", cmid="msg-00002", text="三")  # conv-b 更新
    api.ctx.store.put("bind:persona:conv-b", {"persona_id": "persona-x"})
    # 纯历史会话（非移动端来源）也应可见
    from whale_core.memory.history import append_turn
    append_turn(api.ctx.store, "console", "旧", "旧回复")

    status, raw, _, _ = _req(api, "GET", "/api/v2/conversations?limit=2")
    assert status == 200
    result = _json(raw)["result"]
    assert len(result["conversations"]) == 2
    assert result["next_cursor"]  # 还有一页
    first = result["conversations"][0]
    assert first["conversation_id"] == "conv-b"  # updated_at 最新者在前
    assert first["persona_id"] == "persona-x"
    assert first["last_message"]["text"] == "[fake] 三"
    assert first["message_count"] == 4
    # 翻页
    from urllib.parse import quote
    status, raw, _, _ = _req(
        api, "GET",
        f"/api/v2/conversations?limit=2&cursor={quote(result['next_cursor'])}")
    page2 = _json(raw)["result"]
    assert [c["conversation_id"] for c in page2["conversations"]] == ["console"]
    assert page2["next_cursor"] is None
    # 坏 cursor → 400
    status, _, _, _ = _req(api, "GET", "/api/v2/conversations?cursor=!!bad!!")
    assert status == 400


def test_messages_reverse_chronological_pagination(api):
    for i in range(3):
        _stream(api, cid="conv-p", cmid=f"msg-0000{i + 1}", text=f"第{i + 1}句")
    status, raw, _, _ = _req(
        api, "GET", "/api/v2/conversations/conv-p/messages?limit=2")
    assert status == 200
    result = _json(raw)["result"]
    assert len(result["messages"]) == 2
    # 倒序：最新（assistant 回复「第3句」）在最前
    assert result["messages"][0]["text"] == "[fake] 第3句"
    assert result["messages"][0]["role"] == "assistant"
    assert result["messages"][1]["text"] == "第3句"
    seen = list(result["messages"])
    cursor = result["next_cursor"]
    while cursor:
        from urllib.parse import quote
        status, raw, _, _ = _req(
            api, "GET",
            f"/api/v2/conversations/conv-p/messages?limit=2&cursor={quote(cursor)}")
        result = _json(raw)["result"]
        seen.extend(result["messages"])
        cursor = result["next_cursor"]
    assert len(seen) == 6  # 3 用户 + 3 助手，无重复
    assert len({m["message_id"] for m in seen}) == 6
    # 坏 cursor → 400
    status, _, _, _ = _req(
        api, "GET", "/api/v2/conversations/conv-p/messages?cursor=xx")
    assert status == 400


def test_messages_fallback_to_history_and_unknown_404(api):
    from whale_core.memory.history import append_turn
    append_turn(api.ctx.store, "legacy-conv", "你好", "你好呀")
    status, raw, _, _ = _req(
        api, "GET", "/api/v2/conversations/legacy-conv/messages")
    assert status == 200
    messages = _json(raw)["result"]["messages"]
    # 历史回落如实标注（无 id/时间戳时不伪造）
    assert messages[0]["via"] == "history" and messages[0]["created_at"] is None
    assert [m["role"] for m in messages] == ["assistant", "user"]
    status, _, _, _ = _req(
        api, "GET", "/api/v2/conversations/nope/messages")
    assert status == 404


# ---------------------------------------------------------------------------
# 存储层单元（原子性 / tombstone / 防穿越）
# ---------------------------------------------------------------------------

def test_store_unit_atomic_write_and_traversal(tmp_path):
    from whale_core.cloud.attachments import AttachmentError, AttachmentStore
    store = AttachmentStore(tmp_path / "atts")
    ref = store.save(b"hello-bytes", "text/plain", filename="a.txt")
    assert ref["type"] == "file"
    assert ref["sha256"] == (
        __import__("hashlib").sha256(b"hello-bytes").hexdigest())
    # 原子提交后无临时文件残留
    assert not list((tmp_path / "atts").glob(".tmp-*"))
    meta, data = store.read(ref["attachment_id"])
    assert data == b"hello-bytes" and meta["filename"] == "a.txt"
    # 畸形 id 连路径拼接都不允许发生
    for bad in ("../x", "att_bad", "", "att_" + "a" * 26):
        with pytest.raises(AttachmentError):
            store.read(bad)
    store.delete(ref["attachment_id"])
    with pytest.raises(AttachmentError) as exc:
        store.read(ref["attachment_id"])
    assert exc.value.kind == "gone"
