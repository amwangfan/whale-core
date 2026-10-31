"""WeQ SSE 推送 → whale-core 入站桥（W09 QQ 渠道的"收"侧）。

WeQ 桌面端（完全离线模式亦可）实时监听本地 nt_msg.db，把新消息 POST 到本模块的
HTTP 监听器（载荷 `{events:[...]}`，Bearer 鉴权，失败指数退避重试=至少一次投递）。
本模块只做三件事：鉴权+解析 → 映射 `Incoming` → `OwnerChannelRegistry` 配对产出
`OwnerEvent`。业务决策（AI/记忆/草稿）在 `qq_workflow`，这里不含任何策略。

边界：
- 默认只监听 127.0.0.1；未配置 token 直接拒绝启动（fail closed）；token 恒定时间比较。
- 至少一次投递 → 按 msgId 有界去重，重复推送不会重复进管道。
- `self_uin`（bot 所在账号自己）发的消息不回灌，防未来自发回复形成自激回路。
- 群聊默认不收（`accept_group=False`）：群内容永远按不可信输入处理。
- HTTP 立即 ACK，事件经有界队列由单一工作线程串行消费：AI 耗时不阻塞 WeQ 推送
  （其超时仅 10s），且同一所有者的事件天然按到达序处理。
- 不打日志正文；回调只拿到配对结果，未配对事件仅暴露元数据。
- **持久化 inbox（#14）**：配置 ``inbox=SqliteInbox(...)`` 后，owner 消息先落
  SQLite 任务队列（message_id 主键幂等）再派发；处理失败按 attempts 上限受控
  重试、超限落 failed 可观察；进程重启 ``recover_stale`` 收回租约过期的任务——
  「已 ACK 未处理」窗口崩溃不丢消息。未配对事件与 mass 元数据**不落盘**。
"""
from __future__ import annotations

import json
import queue
import sys
import threading
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from whale_core.life.channels import Incoming, OwnerChannelRegistry, OwnerEvent
from whale_core.life.inbox import SqliteInbox
from whale_core.life.qq_onebot import MAX_TEXT_CHARS, bearer_authenticated

DEFAULT_WQ_PUSH_HOST = "127.0.0.1"
DEFAULT_WQ_PUSH_PORT = 18797
MAX_BODY_BYTES = 4_000_000
DEDUP_WINDOW = 2048
EVENT_QUEUE_CAPACITY = 256


def parse_push_events(body: bytes | None) -> list[dict] | None:
    """校验推送载荷并返回事件列表；结构非法返回 None（不抛异常）。"""
    if body is None or len(body) > MAX_BODY_BYTES:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    events = payload.get("events")
    if not isinstance(events, list):
        return None
    return [e for e in events if isinstance(e, dict)]


# QQ 多媒体 CDN：相对路径需补多媒体域名 + 活 rkey（rkey 由 weq 账号监控
# 刷新，约 57 分钟过期）。只读 weq 配置目录，不写。
_QQ_MEDIA_HOST = "https://multimedia.nt.qq.com.cn"
# rkey type 10=c2c 图（appid 1406）、20=群图（appid 1407）
_RKEY_APPID = {10: "1406", 20: "1407"}


def _load_weq_rkeys(config_dir: str = "") -> list[tuple[str, str]]:
    """从 weq 账号配置读未过期的图片 rkey，返回 [(appid, "&rkey=...")]。

    配置目录默认 ~/.config/weq（WHALE_WEQ_CONFIG_DIR 可覆盖）；读不到/过期
    一律返回空——调用方按「无可用下载链」处理，不伪造 URL。
    """
    import glob
    import json
    import os
    import time

    base = (config_dir or os.environ.get("WHALE_WEQ_CONFIG_DIR", "")
            or os.path.expanduser("~/.config/weq"))
    now = time.time()
    out: list[tuple[str, str]] = []

    def _walk(obj):
        if isinstance(obj, dict):
            if isinstance(obj.get("rkeys"), list):
                return obj["rkeys"]
            for value in obj.values():
                found = _walk(value)
                if found is not None:
                    return found
        return None

    for path in glob.glob(os.path.join(base, "config", "accounts", "*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                rkeys = _walk(json.load(fh))
        except Exception:
            continue
        for rk in rkeys or ():
            if not isinstance(rk, dict):
                continue
            appid = _RKEY_APPID.get(rk.get("type"))
            value = str(rk.get("rkey") or "")
            try:
                fresh = float(rk.get("createTime", 0)) + float(
                    rk.get("ttlSeconds", 0)) > now
            except (TypeError, ValueError):
                fresh = False
            if appid and value.startswith("&rkey=") and fresh:
                out.append((appid, value))
    return out


def _pic_download_urls(data: Mapping, *, chat_type: str,
                       rkeys: list[tuple[str, str]]) -> tuple[str, ...]:
    """pic 元素 → 候选下载 URL（有序，最坏情况为空 tuple）。

    - 绝对 https URL：直接可用；
    - 相对 /download 路径 或仅 fileToken：补多媒体域名 + 按场景匹配 appid
      的活 rkey（c2c=1406/type10，group=1407/type20；两者都试，场景优先）。
    """
    url = str(data.get("originalUrl") or "")
    token = str(data.get("fileToken") or "")
    out: list[str] = []
    if url.startswith("https://"):
        out.append(url[:2000])
    path = ""
    if url.startswith("/"):
        path = url
    elif token and not out:
        scene_appid = "1407" if chat_type == "group" else "1406"
        path = f"/download?appid={scene_appid}&fileid={token}&spec=0"
    if path and rkeys:
        import re as _re

        # 场景匹配的 appid 优先（c2c=1406，group=1407），另一场景兜底
        scene_first = ("1407", "1406") if chat_type == "group" else ("1406", "1407")
        ordered = ([rk for rk in rkeys if rk[0] == scene_first[0]]
                   + [rk for rk in rkeys if rk[0] == scene_first[1]])
        for rk_appid, rk in ordered:
            candidate = _QQ_MEDIA_HOST + path
            if "appid=" in candidate:
                candidate = _re.sub(r"appid=\d+", f"appid={rk_appid}", candidate)
            out.append((candidate + rk)[:2000])
    seen: set[str] = set()
    deduped: list[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return tuple(deduped[:4])


def sse_message_to_incoming(event: Mapping, *, self_uin: str,
                            accept_group: bool = False,
                            group_allow: frozenset | None = None) -> Incoming | None:
    """WeQ SSE 单条 message 事件 → Incoming；不收/解析不了的一律 None（宁缺勿错）。

    - 只认 type=message（mass 合并事件走独立回调，不进消息管道）；
    - self_uin 自己发的消息过滤（防自激回路）；
    - 群聊默认拒收；``group_allow`` 非空时只收白名单内的群
      （S7 灰度：先只开一个测试群）；
    - c2c → conversation=private:<convId>，group → group:<convId>；
    - transport_authenticated=True：调用前提是 HTTP 层 Bearer 已通过。
    """
    if event.get("type") != "message":
        return None
    sender = str(event.get("senderUin") or "")
    if not sender or sender == str(self_uin):
        return None
    msg_id = str(event.get("msgId") or "")
    if not msg_id:
        return None
    chat_type = event.get("chatType")
    if chat_type == "c2c":
        conversation_kind = "private"
    elif chat_type == "group" and accept_group:
        conversation_kind = "group"
    else:
        return None
    conv_id = str(event.get("convId") or "")
    if not conv_id:
        return None
    if conversation_kind == "group" and group_allow is not None \
            and conv_id not in group_allow:
        return None  # 灰度白名单外的群一律不收
    text = event.get("text")
    if not isinstance(text, str):
        text = ""
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
    # G1：@/引用元数据（平台事件元素，不从正文猜）
    mentions: list[str] = []
    reply_to: tuple = ()
    elements = event.get("elements")
    if isinstance(elements, list):
        for el in elements:
            if not isinstance(el, dict):
                continue
            data = el.get("data") or {}
            if el.get("type") == "at" and data.get("atTargetUid"):
                mentions.append(str(data["atTargetUid"]))
            elif el.get("type") == "reply":
                reply_to = (str(data.get("origSenderUin") or ""),
                            str(data.get("origSenderUid") or ""),
                            str(data.get("origMsgId") or ""),
                            str(data.get("origMsgSeq") or ""))
    # 附件：pic 元素带本机缓存路径（filePath）与 CDN 链接（originalUrl）
    attachments = []
    rkeys: list[tuple[str, str]] = []
    if isinstance(elements, list) and any(
            isinstance(el, dict) and el.get("type") == "pic" for el in elements):
        rkeys = _load_weq_rkeys()
    if isinstance(elements, list):
        for el in elements[:8]:
            if not isinstance(el, dict) or el.get("type") != "pic":
                continue
            data = el.get("data") or {}
            urls = _pic_download_urls(data, chat_type=str(chat_type or "c2c"),
                                      rkeys=rkeys)
            attachments.append({
                "kind": "pic",
                "file_path": str(data.get("filePath") or "")[:500],
                "url": urls[0] if urls else "",
                "urls": urls,
                "name": str(data.get("fileName") or "image")[:100],
            })
    # 诊断（结构性，不含正文/URL）：图片链路排障用——记录元素类型序列与
    # pic 元素实际携带的字段名，便于确认上游推送是否含附件。
    if elements:
        kinds = [str(el.get("type")) for el in elements if isinstance(el, dict)][:8]
        pic_fields = sorted({k for el in elements if isinstance(el, dict)
                             and el.get("type") == "pic"
                             for k in (el.get("data") or {}).keys()
                             if (el.get("data") or {}).get(k)})[:12]
        print(f"  [调试] msg={msg_id} elements={kinds} pic_fields={pic_fields}",
              flush=True)
    return Incoming(channel="qq", account=sender,
                    conversation=f"{conversation_kind}:{conv_id}",
                    message_id=msg_id, text=text, transport_authenticated=True,
                    attachments=tuple(attachments),
                    sender_uid=str(event.get("senderUid") or ""),
                    send_time=str(event.get("sendTime") or ""),
                    mentions=tuple(mentions), reply_to=reply_to)


def mass_event_metadata(event: Mapping) -> dict:
    """mass 合并事件的元数据摘要（不含预览正文，正文属于消息内容）。"""
    preview = event.get("preview")
    return {
        "chatType": event.get("chatType"),
        "convId": str(event.get("convId") or ""),
        "fromSeq": str(event.get("fromSeq") or ""),
        "toSeq": str(event.get("toSeq") or ""),
        "count": event.get("count"),
        "hasPreview": isinstance(preview, dict),
    }


class WeQSsePushListener:
    """接收 WeQ「SSE 消息推送」的 stdlib HTTP 监听器。

    回调契约：
    - `on_owner_event(OwnerEvent)`：配对成功的所有者消息（工作线程串行调用）；
    - `on_unpaired(Incoming)`：鉴权通过但未配对/非消息管道的事件元数据（可选）；
    - `on_mass(dict)`：mass 合并事件元数据（可选，见 mass_event_metadata）。
    `async_dispatch=False` 时全部同步调用（测试用）。
    """

    def __init__(self, registry: OwnerChannelRegistry, *, expected_token: str, self_uin: str,
                 on_owner_event: Callable[[OwnerEvent], None],
                 on_unpaired: Callable[[Incoming], None] | None = None,
                 on_mass: Callable[[dict], None] | None = None,
                 accept_group: bool = False,
                 group_allow: frozenset | None = None,
                 timeline=None,
                 member_extractor=None,
                 host: str = DEFAULT_WQ_PUSH_HOST, port: int = DEFAULT_WQ_PUSH_PORT,
                 async_dispatch: bool = True,
                 inbox: SqliteInbox | None = None) -> None:
        if not expected_token:
            raise ValueError("WeQSsePushListener requires a non-empty token (fail closed)")
        if not str(self_uin):
            raise ValueError("self_uin required")
        self.registry = registry
        self.expected_token = expected_token
        self.self_uin = str(self_uin)
        self.on_owner_event = on_owner_event
        self.on_unpaired = on_unpaired
        self.on_mass = on_mass
        self.accept_group = accept_group
        self.group_allow = frozenset(group_allow) if group_allow else None
        self.timeline = timeline  # G1：群消息时间线（可选 GroupTimelineStore）
        # G2：成员记忆提炼器（可选 MemberMemoryExtractor）；时间线追加后
        # 触发本人自述提炼（consent 无效即 0 写入，管道内 fail closed）
        self.member_extractor = member_extractor
        self.host = host
        self.requested_port = port
        self.async_dispatch = async_dispatch
        self.inbox = inbox
        self.recovered = 0  # start() 时收回的过期租约任务数（崩溃恢复可见性）
        self._seen: dict[str, None] = {}
        self._ingest_lock = threading.RLock()  # ThreadingHTTPServer may ingest retries concurrently.
        self._dropped = 0
        self._events: queue.Queue = queue.Queue(maxsize=EVENT_QUEUE_CAPACITY)
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None

    # ── 纯逻辑（可单测） ────────────────────────────────────────

    def _mark_seen(self, msg_id: str) -> bool:
        """首次出现返回 True；调用方需持有 _ingest_lock。"""
        if msg_id in self._seen:
            return False
        self._seen[msg_id] = None
        while len(self._seen) > DEDUP_WINDOW:
            self._seen.pop(next(iter(self._seen)))
        return True

    def ingest_events(self, events: list[dict]) -> int:
        """把事件送进管道，返回已消费/入队数量；过载详情由 HTTP 层使用私有结果。"""
        accepted, _overloaded = self._ingest_events(events)
        return accepted

    def _ingest_events(self, events: list[dict]) -> tuple[int, bool]:
        """串行去重并入队；队列满时不记 seen，令发送方可安全重试。"""
        accepted = 0
        overloaded = False
        with self._ingest_lock:
            for event in events:
                if event.get("type") == "mass":
                    if self._emit_mass(mass_event_metadata(event)):
                        accepted += 1
                    else:
                        overloaded = True
                    continue
                incoming = sse_message_to_incoming(event, self_uin=self.self_uin,
                                                   accept_group=self.accept_group,
                                                   group_allow=self.group_allow)
                if incoming is None or incoming.message_id in self._seen:
                    continue
                # G1：群消息先入时间线（短期公共证据；不等于个人长期记忆）
                # G6 红队高-2：sudo 明文永不落时间线/提炼（N17）——截获先于
                # 任何持久化；但私聊的 sudo 仍须正常流到工作流做验证。
                from whale_core.life.temporary_auth import maybe_intercept_sudo
                is_sudo = maybe_intercept_sudo(incoming.text) is not None
                if (self.timeline is not None and not is_sudo
                        and incoming.conversation.startswith("group:")):
                    self.timeline.append(
                        incoming.conversation, message_id=incoming.message_id,
                        sender_id=incoming.account, text=incoming.text,
                        sender_uid=incoming.sender_uid,
                        send_time=incoming.send_time,
                        mentions=incoming.mentions, reply_to=incoming.reply_to)
                    if (self.member_extractor is not None and incoming.account
                            and not is_sudo):
                        try:
                            self.member_extractor.extract_for_principal(
                                f"anon:qq:{incoming.account}",
                                incoming.conversation)
                        except Exception:
                            pass  # 提炼失败不阻断消息管道
                owner_event = self.registry.accept(incoming)
                if owner_event is None:
                    # Unpaired messages are intentionally consumed if no callback was configured.
                    if self._emit_unpaired(incoming):
                        self._mark_seen(incoming.message_id)
                        accepted += 1
                    else:
                        overloaded = True
                    continue
                if self.inbox is not None:
                    # 持久化优先（#14）：先落库（message_id 主键幂等），再派发。
                    # 重复投递（上游重试 / 重启后重推）不会二次入队，只消化；
                    # 唤醒只是降低延迟的提示，库才是真源（队列满也不丢任务）。
                    self.inbox.enqueue(
                        message_id=owner_event.message_id,
                        conversation=owner_event.conversation,
                        account=incoming.account,
                        text=owner_event.text,
                        attachments=list(owner_event.attachments or ()),
                        meta={"sender_uid": incoming.sender_uid,
                              "send_time": incoming.send_time,
                              "mentions": list(incoming.mentions or ()),
                              "reply_to": list(incoming.reply_to or ())},
                    )
                    self._emit_wake(owner_event.message_id)
                    self._mark_seen(incoming.message_id)
                    accepted += 1
                    continue
                if self._emit_owner(owner_event):
                    self._mark_seen(incoming.message_id)
                    accepted += 1
                else:
                    # Do not mark seen: the upstream retry must be able to enqueue this event.
                    overloaded = True
        return accepted, overloaded

    def _emit_owner(self, event: OwnerEvent) -> bool:
        if self.async_dispatch:
            try:
                self._events.put_nowait(("owner", event))
                return True
            except queue.Full:
                self._dropped += 1
                return False
        self.on_owner_event(event)
        return True

    def _emit_wake(self, message_id: str) -> None:
        """持久化 inbox 模式的派发唤醒：队列只是提示，**库才是真源**。

        同步模式（测试）直接排空；异步模式放一个 ("job", id) 唤醒项，
        队列满也不丢任务——工作线程空转轮询会兜底消化（含重试与恢复）。
        """
        if not self.async_dispatch:
            self._drain_inbox()
            return
        try:
            self._events.put_nowait(("job", message_id))
        except queue.Full:
            pass

    def _drain_inbox(self) -> None:
        """从持久化 inbox 认领并处理任务；失败只记错误**类名**（不落正文）。"""
        if self.inbox is None:
            return
        while not self._stop.is_set():
            job = self.inbox.claim_next()
            if job is None:
                return
            event = OwnerEvent(
                owner_id=self.registry.owner_id,
                channel="qq",
                conversation=job.conversation,
                message_id=job.message_id,
                text=job.text,
                attachments=tuple(
                    a for a in (job.attachments or ())
                    if isinstance(a, dict) and a.get("kind") == "pic"),
                # S1/S7：sender 必须穿透持久化排水路径——inbox 的 account
                # 即真实发送者，丢失会把主人降级为匿名（记忆失效根因）。
                sender_id=job.account,
                # G1：@/引用/时间元数据穿透持久化路径
                sender_uid=str((job.meta or {}).get("sender_uid") or ""),
                send_time=str((job.meta or {}).get("send_time") or ""),
                mentions=tuple((job.meta or {}).get("mentions") or ()),
                reply_to=tuple((job.meta or {}).get("reply_to") or ()),
            )
            try:
                self.on_owner_event(event)
            except Exception as exc:
                # 受控重试：attempts 未达上限回 pending，超限落 failed（可观察、可 requeue）
                self.inbox.fail(job.message_id, error_class=type(exc).__name__)
                continue
            self.inbox.succeed(job.message_id)

    def _emit_unpaired(self, incoming: Incoming) -> bool:
        if self.on_unpaired is None:
            return True
        if self.async_dispatch:
            try:
                self._events.put_nowait(("unpaired", incoming))
                return True
            except queue.Full:
                self._dropped += 1
                return False
        self.on_unpaired(incoming)
        return True

    def _emit_mass(self, meta: dict) -> bool:
        if self.on_mass is None:
            return True
        if self.async_dispatch:
            try:
                self._events.put_nowait(("mass", meta))
                return True
            except queue.Full:
                self._dropped += 1
                return False
        self.on_mass(meta)
        return True

    def process_payload(self, headers: Mapping[str, str],
                        body: bytes | None) -> tuple[int, int]:
        """HTTP 请求 → (状态码, 受理事件数)。401 未鉴权；413 缺失/超限；400 非法载荷；503 队列过载可重试。"""
        authorization: str | None = None
        for key, value in headers.items():
            if key.lower() == "authorization":
                authorization = value
                break
        if not bearer_authenticated(authorization, self.expected_token):
            return 401, 0
        if body is None or len(body) > MAX_BODY_BYTES:
            return 413, 0
        events = parse_push_events(body)
        if events is None:
            return 400, 0
        accepted, overloaded = self._ingest_events(events)
        return (503 if overloaded else 200), accepted

    # ── 运行时（HTTP 服务 + 工作线程） ─────────────────────────

    @property
    def port(self) -> int:
        return int(self._server.server_address[1]) if self._server else self.requested_port

    @property
    def dropped(self) -> int:
        """队列满被丢弃的事件数（过载可见性；不静默）。"""
        return self._dropped

    def start(self) -> None:
        if self._server is not None:
            return
        if self.inbox is not None:
            # 崩溃恢复（#14）：收回租约过期的 processing 任务，启动即续处理
            self.recovered = self.inbox.recover_stale()
        if self.async_dispatch and self._worker is None:
            self._worker = threading.Thread(target=self._consume, name="wq-push-worker",
                                            daemon=True)
            self._worker.start()
        listener = self

        class _Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                exc = sys.exc_info()[1]
                if isinstance(exc, ConnectionError):
                    return  # 客户端提前断开（探活/keep-alive 关闭）：正常现象，不打异常栈
                super().handle_error(request, client_address)

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                body = self._read_capped_body()
                code, count = listener.process_payload(dict(self.headers), body)
                payload = json.dumps({"accepted": count}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _read_capped_body(self) -> bytes | None:
                raw = self.headers.get("Content-Length")
                try:
                    length = int(raw) if raw is not None else -1
                except ValueError:
                    return None
                if length < 0 or length > MAX_BODY_BYTES:
                    return None
                return self.rfile.read(length) if length > 0 else b""

            def log_message(self, *args):
                pass  # 静默 access log：不含正文、不含 token

        self._server = _Server((self.host, self.requested_port), _Handler)
        self._server_thread = threading.Thread(target=self._server.serve_forever,
                                               name="wq-push-listener", daemon=True)
        self._server_thread.start()

    def _consume(self) -> None:
        while not self._stop.is_set() or not self._events.empty():
            try:
                kind, payload = self._events.get(timeout=0.1)
            except queue.Empty:
                # 空转轮询持久化 inbox：重试任务与崩溃恢复不依赖新消息到达
                self._drain_inbox()
                continue
            try:
                if kind == "owner":
                    self.on_owner_event(payload)
                elif kind == "job":
                    self._drain_inbox()
                elif kind == "unpaired" and self.on_unpaired is not None:
                    self.on_unpaired(payload)
                elif kind == "mass" and self.on_mass is not None:
                    self.on_mass(payload)
            except Exception:
                # 单条事件的业务失败不杀死消费线程；不记录正文。
                pass
            finally:
                self._events.task_done()

    def wait_idle(self, timeout: float | None = None) -> None:
        """等待队列排空（测试/优雅停机用）。"""
        self._events.join() if timeout is None else _join_with_timeout(self._events, timeout)

    def stop(self) -> None:
        self._stop.set()
        server, thread = self._server, self._server_thread
        self._server, self._server_thread = None, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        if self._worker is not None:
            self._worker.join(timeout=5)
            self._worker = None


def _join_with_timeout(q: queue.Queue, timeout: float) -> None:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with q.mutex:
            if not q.unfinished_tasks:
                return
        time.sleep(0.01)
