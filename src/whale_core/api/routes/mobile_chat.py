"""移动端聊天 v2 面路由：/api/v2/conversations、/api/v2/chat/*、/api/v2/attachments/*。

实现移动端开发规划 Phase 4 的 Core 侧 REST 契约（contracts/v2 §11）：

- ``GET  /conversations``                会话列表（预览 + persona_id + updated_at，
                                         不透明 cursor 分页）；
- ``GET  /conversations/{id}/messages``  历史消息倒序分页（cursor 不透明）；
- ``POST /chat/attachments``             multipart 上传 → 201 返回 AttachmentRef
                                         （严格符合 objects/attachment-ref schema）；
- ``GET  /attachments/{id}``             下载（Content-Type/Length 如实；
                                         从未存在 404 / 已删除 410）；
- ``DELETE /attachments/{id}``           删除（幂等语义：已删 → 410）；
- ``POST /chat/stream``                  SSE 流式回复。事件词汇表（规划 §5.5）：
                                         phase / delta / tool.started /
                                         tool.progress / tool.result /
                                         attachment.created / done / error。
                                         当前同步编排器只暴露 phase/delta 真实
                                         回调；tool.result 由真实 receipts 在
                                         done 前补发；tool.started/progress 与
                                         attachment.created 在事件写器中定义但
                                         无真实来源时**不伪造**（编排器接入任务
                                         时间线后再补）。

鉴权与 v1 完全一致：``Authorization: Bearer <console token>``（server.py 统一
拦截 /api/*）；sender 语义沿用 chat 面先例（source="api"、sender_id="console"，
身份解析仍由服务端 resolver 决定）。

幂等：``client_message_id``（§3 idempotency_key 同款 pattern）在会话内去重；
重复提交重放首个 done（duplicate=true），**不产生第二条消息、不二次执行**。
"""
from __future__ import annotations

import base64
import binascii
import json
import re

from whale_core.api.server import _error, _json_bytes
from whale_core.cloud.attachments import (
    ATTACHMENT_ID_RE,
    MULTIPART_OVERHEAD_BYTES,
    MAX_ATTACHMENT_BYTES,
    AttachmentError,
    AttachmentStore,
    default_root,
)
from whale_core.devices.transports.base import new_id, now_ms
from whale_core.memory.history import conversation_lock, load_history

# §3 idempotency_key：^[0-9A-Za-z:_-]{8,128}$
_CLIENT_MESSAGE_ID_RE = re.compile(r"^[0-9A-Za-z:_-]{8,128}$")

MAX_STREAM_BODY_BYTES = 1024 * 1024     # 流式请求只带文本+附件引用
MAX_ATTACHMENTS_PER_MESSAGE = 9         # 与主流移动端一致的单条上限
MAX_IMAGES_TO_MODEL = 4                 # 进多模态链路的图片数上限（其余仅引用）
MOBILE_LOG_CAP = 500                    # 每会话移动端消息日志上限
IDEM_TABLE_CAP = 200                    # 每会话幂等表上限（FIFO）

_MOBILE_LOG_PREFIX = "mobile:chat:"
_MOBILE_IDEM_PREFIX = "mobile:idem:"
_HISTORY_PREFIX = "chat:history:"

# AttachmentRef schema 允许的键（additionalProperties: false）
_REF_KEYS = ("attachment_id", "type", "mime", "size", "sha256",
             "width", "height", "duration_ms")


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------

def _attachment_store(ctx) -> AttachmentStore:
    """懒构建并缓存在 ctx 上；目录 WHALE_ATTACHMENTS_DIR 可配。"""
    store = getattr(ctx, "attachments", None)
    if store is None:
        store = AttachmentStore(default_root())
        ctx.attachments = store
    return store


def _cursor_encode(offset: int) -> str:
    """不透明 cursor：base64url(JSON)。内容无密级，不透明仅为演进自由。"""
    raw = json.dumps({"offset": offset}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _cursor_decode(raw: str) -> int | None:
    try:
        obj = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")))
        offset = obj.get("offset")
    except (ValueError, binascii.Error, UnicodeDecodeError, AttributeError):
        return None
    return offset if isinstance(offset, int) and offset >= 0 else None


def _limit(query: dict, default: int, ceiling: int = 100) -> int | None:
    raw = (query.get("limit") or [""])[0]
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 1 <= value <= ceiling else None


def _ref_view(meta: dict) -> dict:
    """元数据 → 严格 schema 子集（不多一个字段）。"""
    return {k: meta[k] for k in _REF_KEYS if k in meta}


# ---------------------------------------------------------------------------
# 会话列表 / 消息分页
# ---------------------------------------------------------------------------

def _conversation_entry(ctx, cid: str) -> dict:
    store = ctx.store
    log = store.get(_MOBILE_LOG_PREFIX + cid)
    log = log if isinstance(log, list) else []
    pdata = store.get(f"bind:persona:{cid}")
    persona_id = str(pdata.get("persona_id")) if isinstance(pdata, dict) \
        and pdata.get("persona_id") else None
    updated_at = None
    preview = None
    if log:
        last = log[-1]
        updated_at = last.get("created_at")
        preview = {"role": last.get("role"),
                   "text": str(last.get("text") or "")[:200],
                   "at": last.get("created_at")}
    else:
        # 非移动端来源（console/QQ 等）的会话：预览取 chat:history 末条
        history = load_history(store, cid)
        if history:
            preview = {"role": history[-1]["role"],
                       "text": history[-1]["content"][:200],
                       "at": None}
    return {
        "conversation_id": cid,
        "persona_id": persona_id,
        "updated_at": updated_at,
        "last_message": preview,
        "message_count": len(log),
    }


def conversations_list(handler, query: dict) -> None:
    """GET /api/v2/conversations?cursor=&limit=（按 updated_at 倒序）。"""
    ctx = handler.ctx
    limit = _limit(query, default=20)
    if limit is None:
        handler._send(400, _error("bad_request", "limit out of range"))
        return
    cursor_raw = (query.get("cursor") or [""])[0]
    offset = 0
    if cursor_raw:
        offset = _cursor_decode(cursor_raw)
        if offset is None:
            handler._send(400, _error("bad_request", "invalid cursor"))
            return
    cids: set[str] = set()
    for key in ctx.store.keys():
        if key.startswith(_MOBILE_LOG_PREFIX):
            cids.add(key[len(_MOBILE_LOG_PREFIX):])
        elif key.startswith(_HISTORY_PREFIX):
            cids.add(key[len(_HISTORY_PREFIX):])
    entries = [_conversation_entry(ctx, cid) for cid in cids]
    # updated_at 未知（纯历史会话）排最后；同刻按 id 字典序保证稳定
    entries.sort(key=lambda e: (e["updated_at"] or 0, e["conversation_id"]),
                 reverse=True)
    page = entries[offset:offset + limit]
    next_cursor = _cursor_encode(offset + limit) \
        if offset + limit < len(entries) else None
    handler._send(200, _json_bytes({"result": {
        "conversations": page, "next_cursor": next_cursor}}))


def conversation_messages(handler, conversation_id: str, query: dict) -> None:
    """GET /api/v2/conversations/{id}/messages?cursor=&limit=（新→旧倒序）。"""
    from urllib.parse import unquote
    ctx = handler.ctx
    cid = unquote(conversation_id)[:160]
    if not cid:
        handler._send(400, _error("bad_request", "conversation id required"))
        return
    limit = _limit(query, default=50)
    if limit is None:
        handler._send(400, _error("bad_request", "limit out of range"))
        return
    cursor_raw = (query.get("cursor") or [""])[0]
    offset = 0
    if cursor_raw:
        offset = _cursor_decode(cursor_raw)
        if offset is None:
            handler._send(400, _error("bad_request", "invalid cursor"))
            return
    log = ctx.store.get(_MOBILE_LOG_PREFIX + cid)
    if isinstance(log, list) and log:
        ordered = list(reversed(log))  # 存储为时间升序，接口返回新→旧
        page = ordered[offset:offset + limit]
        next_cursor = _cursor_encode(offset + limit) \
            if offset + limit < len(ordered) else None
        handler._send(200, _json_bytes({"result": {
            "conversation_id": cid, "messages": page,
            "next_cursor": next_cursor}}))
        return
    # 回落：非移动端会话只有 chat:history（无 id/时间戳，不伪造，如实标 null）
    history = load_history(ctx.store, cid)
    if not history:
        handler._send(404, _error("not_found", "no such conversation"))
        return
    messages = [{"message_id": None, "role": m["role"], "text": m["content"],
                 "created_at": None, "attachments": [], "via": "history"}
                for m in reversed(history)]
    handler._send(200, _json_bytes({"result": {
        "conversation_id": cid, "messages": messages, "next_cursor": None}}))


# ---------------------------------------------------------------------------
# 附件上传 / 下载 / 删除
# ---------------------------------------------------------------------------

def _multipart_boundary(content_type: str) -> str | None:
    for part in content_type.split(";")[1:]:
        key, sep, value = part.partition("=")
        if sep and key.strip().lower() == "boundary":
            value = value.strip()
            if value.startswith('"') and value.endswith('"') and len(value) >= 2:
                value = value[1:-1]
            # RFC 2046 boundary 字符集；拒绝怪字符防注入
            if 1 <= len(value) <= 200 and re.match(
                    r"^[0-9A-Za-z'()+_,\-./:=? ]+$", value):
                return value
    return None


def _parse_disposition_params(value: str) -> dict:
    params = {}
    for part in value.split(";")[1:]:
        key, sep, raw = part.partition("=")
        if not sep:
            continue
        raw = raw.strip()
        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            raw = raw[1:-1]
        params[key.strip().lower()] = raw
    return params


def _parse_multipart(body: bytes, boundary: str) -> tuple[dict, list]:
    """最小 multipart/form-data 解析：字段 → dict，文件 → (name, filename, mime, data)。"""
    fields: dict[str, str] = {}
    files: list[tuple[str, str, str, bytes]] = []
    delimiter = b"--" + boundary.encode("latin-1")
    for segment in body.split(delimiter)[1:]:
        if segment.startswith(b"--"):
            break  # 终止边界
        if not segment.startswith(b"\r\n"):
            continue
        segment = segment[2:]
        if segment.endswith(b"\r\n"):
            segment = segment[:-2]
        header_blob, sep, content = segment.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers: dict[str, str] = {}
        for line in header_blob.split(b"\r\n"):
            key, sep2, value = line.partition(b":")
            if sep2:
                headers[key.decode("latin-1").strip().lower()] = \
                    value.decode("latin-1").strip()
        disposition = headers.get("content-disposition", "")
        if "form-data" not in disposition:
            continue
        params = _parse_disposition_params(disposition)
        name = params.get("name", "")
        filename = params.get("filename")
        if filename is None:
            fields[name] = content.decode("utf-8", "replace")
        else:
            files.append((name, filename,
                          headers.get("content-type",
                                      "application/octet-stream"),
                          content))
    return fields, files


def attachment_upload(handler) -> None:
    """POST /api/v2/chat/attachments（multipart）→ 201 AttachmentRef。"""
    ctx = handler.ctx
    content_type = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type.lower():
        handler._send(400, _error("bad_request", "multipart/form-data required"))
        return
    boundary = _multipart_boundary(content_type)
    if boundary is None:
        handler._send(400, _error("bad_request", "multipart boundary missing"))
        return
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        length = 0
    if length <= 0:
        handler._send(400, _error("bad_request", "empty body"))
        return
    # 预检：文件本体 ≤50MB，加上 multipart 开销兜底（§11 超限拒绝）
    if length > MAX_ATTACHMENT_BYTES + MULTIPART_OVERHEAD_BYTES:
        handler._send(413, _error(
            "too_large", f"attachment exceeds {MAX_ATTACHMENT_BYTES} bytes"))
        return
    body = handler.rfile.read(length)
    fields, files = _parse_multipart(body, boundary)
    if not files:
        handler._send(400, _error("bad_request", "file part required"))
        return
    _, filename, mime, data = files[0]  # 一次一个附件（契约语义）
    try:
        ref = _attachment_store(ctx).save(
            data, mime, filename=filename,
            declared_type=(fields.get("type") or "").strip())
    except AttachmentError as exc:
        status = 413 if exc.kind == "too_large" else 400
        handler._send(status, _error(exc.kind, exc.message))
        return
    ctx.log_event("attachment:upload", ref["attachment_id"])
    handler._send(201, _json_bytes(ref))  # 响应体即 AttachmentRef（§11）


def attachment_download(handler, attachment_id: str) -> None:
    """GET /api/v2/attachments/{id}：Content-Type/Length 如实；404/410 语义。"""
    from urllib.parse import unquote
    try:
        meta, data = _attachment_store(handler.ctx).read(
            unquote(attachment_id))
    except AttachmentError as exc:
        status = {"not_found": 404, "gone": 410}.get(exc.kind, 400)
        handler._send(status, _error(exc.kind, exc.message))
        return
    # 二进制不走 handler._send（它会给 Content-Type 追加 charset）
    handler.send_response(200)
    handler.send_header("Content-Type",
                        str(meta.get("mime") or "application/octet-stream"))
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    try:
        handler.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


def attachment_delete(handler, attachment_id: str) -> None:
    """DELETE /api/v2/attachments/{id}：成功 200；从未存在 404；已删 410。"""
    from urllib.parse import unquote
    ctx = handler.ctx
    try:
        result = _attachment_store(ctx).delete(unquote(attachment_id))
    except AttachmentError as exc:
        status = {"not_found": 404, "gone": 410}.get(exc.kind, 400)
        handler._send(status, _error(exc.kind, exc.message))
        return
    ctx.log_event("attachment:delete", result["attachment_id"])
    handler._send(200, _json_bytes({"result": result}))


# ---------------------------------------------------------------------------
# 流式聊天（SSE）
# ---------------------------------------------------------------------------

def _append_mobile_messages(ctx, cid: str, messages: list[dict]) -> None:
    """追加移动端消息日志（调用方须已持有 conversation_lock）。"""
    store = ctx.store
    key = _MOBILE_LOG_PREFIX + cid
    log = store.get(key)
    log = log if isinstance(log, list) else []
    log.extend(messages)
    store.put(key, log[-MOBILE_LOG_CAP:])


def chat_stream_v2(handler) -> None:
    """POST /api/v2/chat/stream：SSE 流式回复 + client_message_id 幂等。

    请求：``{conversation_id, client_message_id, text, attachments?:
    [{attachment_id}]}``。text 为空但带附件合法（纯图消息）。
    """
    ctx = handler.ctx
    if ctx.orchestrator is None:
        handler._send(503, _error("unavailable", "orchestrator not wired"))
        return
    body = handler._body_json(MAX_STREAM_BODY_BYTES)
    if body is None:
        handler._send(400, _error("bad_request", "invalid JSON body"))
        return
    conversation_id = str(body.get("conversation_id") or "").strip()[:160]
    client_message_id = str(body.get("client_message_id") or "").strip()
    text = str(body.get("text") or "")
    attachments_in = body.get("attachments") or []
    if not conversation_id:
        handler._send(400, _error("bad_request", "conversation_id required"))
        return
    if not _CLIENT_MESSAGE_ID_RE.match(client_message_id):
        handler._send(400, _error(
            "bad_request",
            "client_message_id must match ^[0-9A-Za-z:_-]{8,128}$"))
        return
    if not isinstance(attachments_in, list) \
            or len(attachments_in) > MAX_ATTACHMENTS_PER_MESSAGE:
        handler._send(400, _error("bad_request", "invalid attachments"))
        return
    if not text.strip() and not attachments_in:
        handler._send(400, _error("bad_request", "empty text"))
        return
    # attachment_id 格式先过严格 pattern（拼路径前的唯一闸口在存储层，
    # 这里提前拒绝只为给出 400 而非 404）
    att_ids: list[str] = []
    for item in attachments_in:
        aid = str(item.get("attachment_id") or "") \
            if isinstance(item, dict) else ""
        if not ATTACHMENT_ID_RE.match(aid):
            handler._send(400, _error("bad_request", "invalid attachment_id"))
            return
        att_ids.append(aid)

    store = ctx.store
    idem_key = _MOBILE_IDEM_PREFIX + conversation_id

    def _start_sse() -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.end_headers()

    stopped = {"flag": False}

    def _emit(obj) -> bool:
        # 每个事件带 evt_ id 与毫秒时间戳（§2/§3 同款生成器）
        frame = {"id": new_id("evt"), "ts": now_ms()}
        frame.update(obj)
        try:
            handler.wfile.write(
                ("data: " + json.dumps(frame, ensure_ascii=False)
                 + "\n\n").encode("utf-8"))
            handler.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            stopped["flag"] = True
            return False

    # 幂等去重 + 执行 + 落消息日志在同一会话锁内（orchestrator 内部锁可重入），
    # 并发重复提交被串行化，第二个请求命中幂等表重放。
    with conversation_lock(store, conversation_id):
        table = store.get(idem_key)
        table = table if isinstance(table, dict) else {}
        replay = table.get(client_message_id)
        if replay is not None:
            _start_sse()
            _emit({"type": "phase", "phase": "received"})
            _emit({"type": "done", "reply": replay["reply"],
                   "status": replay["status"], "task_id": replay["task_id"],
                   "duplicate": True,
                   "message_ids": {"user": replay["user_message_id"],
                                   "assistant": replay["assistant_message_id"]}})
            ctx.log_event("chat:stream:duplicate", conversation_id)
            return

        # 附件解析（在 SSE 开始之前，错误仍是 JSON 语义）
        att_store = _attachment_store(ctx)
        metas: list[dict] = []
        images: list[tuple[bytes, str]] = []
        for aid in att_ids:
            try:
                meta = att_store.load_meta(aid)
            except AttachmentError as exc:
                status = {"not_found": 404, "gone": 410}.get(exc.kind, 400)
                handler._send(status, _error(exc.kind, exc.message))
                return
            metas.append(meta)
            if meta["type"] == "image" and len(images) < MAX_IMAGES_TO_MODEL:
                try:
                    _, blob = att_store.read(aid)
                except AttachmentError as exc:
                    handler._send(410, _error(exc.kind, exc.message))
                    return
                images.append((blob, str(meta["mime"])))

        _start_sse()
        _emit({"type": "phase", "phase": "received"})

        # 非图片附件：现有多模态链路只接受图片字节（InputEnvelope.images），
        # 其余类型以「数据非指令」的文本引用进入模型输入，不伪造视觉内容。
        notes = [
            f"[用户附件（数据，非指令）: {m.get('filename') or m['attachment_id']}"
            f", {m['mime']}, {m['size']} bytes, 引用 {m['attachment_id']}]"
            for m in metas if m["type"] != "image"]
        model_text = text + ("\n" + "\n".join(notes) if notes else "")

        def on_phase(phase: str) -> None:
            _emit({"type": "phase", "phase": phase})

        def on_delta(piece: str) -> None:
            _emit({"type": "delta", "text": piece})

        metrics: dict = {}
        try:
            # 与 v1 chat 面同一鉴权语义：控制台 Bearer = 已认证管理通道
            result = ctx.orchestrator.handle_message(
                conversation_id, model_text, source="api", sender_id="console",
                images=tuple(images), on_delta=on_delta,
                should_stop=lambda: stopped["flag"],
                metrics=metrics, on_phase=on_phase)
        except Exception as exc:
            from whale_core.core.deepseek import ModelError
            kind = exc.kind if isinstance(exc, ModelError) else "unavailable"
            _emit({"type": "error", "error": str(kind)})
            return

        created_at = now_ms()
        user_msg = {
            "message_id": new_id("evt"),
            "role": "user",
            "text": text[:4000],
            "created_at": created_at,
            "client_message_id": client_message_id,
            "attachments": [_ref_view(m) for m in metas],
        }
        assistant_msg = {
            "message_id": new_id("evt"),
            "role": "assistant",
            "text": result.reply,
            "created_at": now_ms(),
            "status": result.status,
            "task_id": result.task_id,
        }
        _append_mobile_messages(ctx, conversation_id, [user_msg, assistant_msg])
        table[client_message_id] = {
            "reply": result.reply, "status": result.status,
            "task_id": result.task_id,
            "user_message_id": user_msg["message_id"],
            "assistant_message_id": assistant_msg["message_id"],
            "created_at": created_at,
        }
        # 幂等表 FIFO 封顶（按入库顺序淘汰最旧）
        while len(table) > IDEM_TABLE_CAP:
            table.pop(next(iter(table)))
        store.put(idem_key, table)

    # receipts 是本轮真实工具回执（同步路径无进行中回调，故在 done 前补发；
    # tool.started/tool.progress 无真实来源不伪造）
    for receipt in result.receipts:
        _emit({"type": "tool.result", "receipt": str(receipt)})
    _emit({"type": "done", "reply": result.reply, "status": result.status,
           "task_id": result.task_id, "metrics": metrics,
           "stopped": bool(metrics.get("stopped")), "duplicate": False,
           "message_ids": {"user": user_msg["message_id"],
                           "assistant": assistant_msg["message_id"]}})
    ctx.log_event("chat:stream:v2", conversation_id)
