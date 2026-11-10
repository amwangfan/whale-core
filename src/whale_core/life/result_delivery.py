"""统一结果投递路由（ResultDelivery）。

场景：agent 做完事产出文字/图片后，按通道汇总投递——
- QQ 在线：优先 WeQ owner 私聊（可定向群/非 owner 私聊）；
- QQ 离线：记录并尝试手机侧图库（无现成 API 时 stub + 日志 + 可选 ADB/MediaStore 探测）。

本模块只做投递路由与回执，不负责任务执行本身。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

log = logging.getLogger("whale_core.result_delivery")


@dataclass(frozen=True)
class DeliveryItem:
    kind: str  # text | image | audio
    text: str = ""
    image_bytes: bytes | None = None
    filename: str = "result.png"
    # 2026-11-11：语音产物（WAV 字节）——kind="audio" 时经 send_voice_to 投递
    audio_bytes: bytes | None = None


@dataclass
class DeliveryReceipt:
    channel: str  # qq_c2c | qq_group | whale_notification | phone_gallery | stub_log
    ok: bool
    detail: str = ""
    qq_online: bool | None = None
    weq_receipt: dict | None = None
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "channel": self.channel,
            "ok": self.ok,
            "detail": self.detail,
            "qq_online": self.qq_online,
            "weq_receipt": self.weq_receipt,
            "ts": self.ts,
        }


OnlineProbe = Callable[[], bool]
PhoneGallerySink = Callable[[bytes, str], "tuple[bool, str]"]
WhalePushSink = Callable[[Sequence[DeliveryItem], str, str], Sequence[DeliveryReceipt]]


def _unwrap_send_port(port):
    """剥 WindowedSendPort / ConversationRoutingPort 等到真正的 WeQWebSendPort。"""
    seen = set()
    cur = port
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        inner = getattr(cur, "_inner", None)
        if inner is None:
            break
        cur = inner
    return cur


def probe_weq_qq_online(port) -> bool:
    """经 WeQ ``groupFeedback.status`` 探测本机 QQ 是否在线。

    失败（网络/未登录）视为离线——投递会走手机侧兜底，不假装在线硬发。
    """
    port = _unwrap_send_port(port)
    get = getattr(port, "_get", None)
    login = getattr(port, "login", None)
    cookie = getattr(port, "_cookie", None)
    if not callable(get):
        # 无查询能力时：改用进程启发（/opt/QQ/qq）。
        return _fallback_qq_process_online()
    try:
        if cookie is None and callable(login):
            login()
        status, body = get("/trpc/groupFeedback.status", auth=True)
        if status == 401 and callable(login):
            login()
            status, body = get("/trpc/groupFeedback.status", auth=True)
        if status != 200 or not isinstance(body, dict):
            return False
        data = ((body.get("result") or {}).get("data") or {}).get("json")
        if isinstance(data, dict):
            return bool(data.get("online"))
    except Exception as exc:
        log.warning("qq online probe failed: %s", type(exc).__name__)
    return False


def _fallback_qq_process_online() -> bool:
    """无 WeQ 查询面时的弱启发：本机是否有 QQ 进程（不保证可发送）。"""
    try:
        import subprocess
        out = subprocess.check_output(
            ["pgrep", "-f", r"/opt/QQ/qq|/opt/QQ/QQ|QQ\.exe"],
            stderr=subprocess.DEVNULL, text=True, timeout=3)
        return bool(out.strip())
    except Exception:
        return False


class ResultDelivery:
    """把一组 DeliveryItem 投到 QQ 或手机图库。"""

    def __init__(
        self,
        send_port,
        *,
        owner_uid: str,
        online_probe: OnlineProbe | None = None,
        phone_gallery: PhoneGallerySink | None = None,
        whale_push: WhalePushSink | None = None,
        prefer_owner_dm: bool = True,
    ) -> None:
        self.port = send_port
        self.owner_uid = str(owner_uid or "").strip()
        self.online_probe = online_probe or (lambda: probe_weq_qq_online(send_port))
        self.phone_gallery = phone_gallery
        self.whale_push = whale_push
        self.prefer_owner_dm = prefer_owner_dm
        self.last_receipts: list[DeliveryReceipt] = []

    def deliver(
        self,
        items: Sequence[DeliveryItem],
        *,
        conversation: str = "",
        summary: str = "",
    ) -> list[DeliveryReceipt]:
        items = [i for i in items if i is not None]
        if summary and not any(i.kind == "text" and i.text.strip() for i in items):
            items = [DeliveryItem(kind="text", text=summary), *items]
        if not items:
            receipt = DeliveryReceipt(
                channel="stub_log", ok=False, detail="empty-items", qq_online=None)
            self.last_receipts = [receipt]
            return self.last_receipts

        online = False
        try:
            online = bool(self.online_probe())
        except Exception as exc:
            log.warning("online_probe raised: %s", type(exc).__name__)
            online = False

        if online:
            receipts = self._deliver_qq(items, conversation=conversation)
        else:
            # QQ 离线时先走 Whale 原生通知；图片仍可独立回落图库。
            whale_receipts = self._deliver_whale(
                items, conversation=conversation, summary=summary or "")
            whale_text_accepted = any(
                receipt.ok and receipt.channel == "whale_notification"
                for receipt in whale_receipts)
            images = [item for item in items
                      if item.kind == "image" and item.image_bytes]
            if images:
                phone_receipts = self._deliver_phone(
                    images, summary="" if whale_text_accepted else (summary or ""))
            elif not whale_text_accepted:
                phone_receipts = self._deliver_phone(items, summary=summary or "")
            else:
                phone_receipts = []
            receipts = [*whale_receipts, *phone_receipts]
            # 离线也落一条文字摘要到日志，便于事后补发。
            log.info(
                "result_delivery qq_offline summary=%r items=%d whale_accepted=%s",
                (summary or "")[:120], len(items), whale_text_accepted)
        self.last_receipts = receipts
        return receipts

    def _resolve_qq_target(self, conversation: str) -> tuple[str, str]:
        """返回 (peer_type, target)。owner 私聊优先。"""
        conv = str(conversation or "").strip()
        if self.prefer_owner_dm and self.owner_uid:
            return "c2c", self.owner_uid
        if conv.startswith("group:"):
            return "group", conv.split(":", 1)[1]
        if conv.startswith("private:"):
            uid = conv.split(":", 1)[1]
            return "c2c", uid or self.owner_uid
        return "c2c", self.owner_uid

    def _deliver_qq(
        self, items: Sequence[DeliveryItem], *, conversation: str
    ) -> list[DeliveryReceipt]:
        # owner 私聊优先（默认）；关闭 prefer 时按 conversation 路由群/私聊。
        if self.prefer_owner_dm and self.owner_uid:
            peer_type, target = "c2c", self.owner_uid
        else:
            peer_type, target = self._resolve_qq_target(conversation)

        receipts: list[DeliveryReceipt] = []
        for item in items:
            if item.kind == "text":
                text = (item.text or "").strip()
                if not text:
                    continue
                ok = False
                detail = ""
                weq = None
                try:
                    if peer_type == "group" and hasattr(self.port, "send_group_text"):
                        ok = bool(self.port.send_group_text(target, text))
                    elif hasattr(self.port, "send_c2c"):
                        ok = bool(self.port.send_c2c(target, text))
                    else:
                        detail = "no-send-c2c"
                    weq = getattr(_unwrap_send_port(self.port), "last_receipt", None)
                    if not detail:
                        detail = "sent" if ok else "send_failed"
                except Exception as exc:
                    detail = f"{type(exc).__name__}"
                    ok = False
                receipts.append(DeliveryReceipt(
                    channel=("qq_group" if peer_type == "group" else "qq_c2c"),
                    ok=ok, detail=detail, qq_online=True,
                    weq_receipt=weq if isinstance(weq, dict) else None))
                continue

            if item.kind == "image":
                data = item.image_bytes or b""
                if not data:
                    receipts.append(DeliveryReceipt(
                        channel=("qq_group" if peer_type == "group" else "qq_c2c"),
                        ok=False, detail="empty-image", qq_online=True))
                    continue
                ok = False
                detail = ""
                weq = None
                try:
                    if hasattr(self.port, "send_image_to"):
                        ok = bool(self.port.send_image_to(
                            peer_type, target, data, filename=item.filename or "result.png"))
                    elif peer_type == "c2c" and hasattr(self.port, "send_image"):
                        ok = bool(self.port.send_image(
                            data, filename=item.filename or "result.png"))
                    else:
                        detail = "no-send-image"
                    weq = getattr(_unwrap_send_port(self.port), "last_receipt", None)
                    if not detail:
                        detail = "sent" if ok else "send_failed"
                except Exception as exc:
                    detail = f"{type(exc).__name__}"
                    ok = False
                receipts.append(DeliveryReceipt(
                    channel=("qq_group" if peer_type == "group" else "qq_c2c"),
                    ok=ok, detail=detail, qq_online=True,
                    weq_receipt=weq if isinstance(weq, dict) else None))
                continue

            if item.kind == "audio":
                data = item.audio_bytes or b""
                if not data:
                    receipts.append(DeliveryReceipt(
                        channel=("qq_group" if peer_type == "group" else "qq_c2c"),
                        ok=False, detail="empty-audio", qq_online=True))
                    continue
                ok = False
                detail = ""
                weq = None
                try:
                    if hasattr(self.port, "send_voice_to"):
                        ok = bool(self.port.send_voice_to(
                            peer_type, target, data,
                            filename=item.filename or "voice.wav"))
                    else:
                        detail = "no-send-voice"
                    weq = getattr(_unwrap_send_port(self.port), "last_receipt", None)
                    if not detail:
                        detail = "sent" if ok else "send_failed"
                except Exception as exc:
                    detail = f"{type(exc).__name__}"
                    ok = False
                receipts.append(DeliveryReceipt(
                    channel=("qq_group" if peer_type == "group" else "qq_c2c"),
                    ok=ok, detail=detail, qq_online=True,
                    weq_receipt=weq if isinstance(weq, dict) else None))
                continue

            receipts.append(DeliveryReceipt(
                channel="stub_log", ok=False,
                detail=f"unsupported-kind:{item.kind}", qq_online=True))
        return receipts

    def _deliver_whale(
        self, items: Sequence[DeliveryItem], *, conversation: str, summary: str
    ) -> list[DeliveryReceipt]:
        if self.whale_push is None:
            return []
        try:
            result = self.whale_push(items, conversation, summary)
            return list(result or ())
        except Exception as exc:
            log.warning("whale notification push failed: %s", type(exc).__name__)
            return [DeliveryReceipt(
                channel="whale_notification", ok=False,
                detail=f"push_failed:{type(exc).__name__}", qq_online=False)]

    def _deliver_phone(
        self, items: Sequence[DeliveryItem], *, summary: str
    ) -> list[DeliveryReceipt]:
        sink = self.phone_gallery
        receipts: list[DeliveryReceipt] = []
        if summary:
            log.info("phone_fallback summary=%s", summary[:500])
        images = [i for i in items if i.kind == "image" and i.image_bytes]
        texts = [i for i in items if i.kind == "text" and (i.text or "").strip()]
        if not images and texts:
            # 纯文字离线：只能记日志（手机通知/剪贴板后续再接）
            blob = "\n".join(t.text for t in texts)[:2000]
            log.info("phone_fallback text_only=%r", blob[:200])
            receipts.append(DeliveryReceipt(
                channel="stub_log", ok=True,
                detail="logged-text-offline", qq_online=False))
            return receipts
        if sink is None:
            from whale_core.life.phone_gallery import stub_save_to_gallery
            sink = stub_save_to_gallery
        for item in images:
            try:
                ok, detail = sink(item.image_bytes or b"", item.filename or "result.png")
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}"
            receipts.append(DeliveryReceipt(
                channel=("phone_gallery" if ok and not str(detail).startswith("stub")
                         else "stub_log"),
                ok=bool(ok), detail=str(detail)[:300], qq_online=False))
        if not receipts:
            receipts.append(DeliveryReceipt(
                channel="stub_log", ok=False,
                detail="offline-nothing-to-deliver", qq_online=False))
        return receipts


def summarize_receipts(receipts: Sequence[DeliveryReceipt]) -> dict:
    return {
        "ok": all(r.ok for r in receipts) if receipts else False,
        "count": len(receipts),
        "channels": [r.channel for r in receipts],
        "receipts": [r.as_dict() for r in receipts],
    }
