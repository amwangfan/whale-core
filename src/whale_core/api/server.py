"""whale-core 管理 API（roadmap §5.3/§5.4）。

设计要点：
- 纯 stdlib（ThreadingHTTPServer），沿用 wq_push/qq_onebot 的回环监听先例；
  **默认只绑 127.0.0.1**，云端部署须显式配监听地址 + TLS 反代（宿主职责）。
- 所有 /api/ 请求强制 ``Authorization: Bearer <token>``（hmac.compare_digest
  常量时间比较）；token 由宿主注入，本模块不生成、不回显、不进日志。
- 统一错误格式 ``{"error": {"code", "message"}}``；成功 ``{"result": ...}``。
- 配置类输出一律过 PluginManager.config_view 脱敏；本层不回显任何 secret。
- 前端只读本 API，不直接碰 SQLite/文件系统/密钥（roadmap §5.1）。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from whale_core.memory.store import MemoryStore

API_VERSION = "v1"
MAX_BODY_BYTES = 64 * 1024
SSE_REPLAY_LIMIT = 50


@dataclass
class ApiContext:
    """宿主注入的能力集合（全部为可选；缺能力的路由返回 503）。"""

    store: MemoryStore
    version: str = "0.1.0"
    profile: str = "local"
    manager: Any = None  # PluginManager
    orchestrator: Any = None  # ConversationOrchestrator
    curator: Any = None  # MemoryCurator
    started_at: float = field(default_factory=time.time)
    events: list[dict] = field(default_factory=list)  # bounded, non-durable log
    event_seq: int = 0
    bundle: Any = None
    jobs: Any = None
    artifacts: Any = None
    config_dir: str = ""
    voice: Any = None
    voice_host: str = "127.0.0.1"
    voice_port: int = 8081
    control_lock: Any = field(default_factory=threading.RLock)
    event_lock: Any = field(default_factory=threading.RLock)
    stopping: Any = field(default_factory=threading.Event)

    def log_event(self, kind: str, detail: str = "") -> None:
        with self.event_lock:
            self.event_seq += 1
            self.events.append(
                {
                    "at": time.time(),
                    "seq": self.event_seq,
                    "kind": kind,
                    "detail": detail[:200],
                }
            )
            del self.events[:-200]


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _error(code: str, message: str) -> bytes:
    return _json_bytes({"error": {"code": code, "message": message}})


class _Handler(BaseHTTPRequestHandler):
    server_version = "whale-api/0.1"

    # ---- 基础设施 ----
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, fmt, *args):  # 静默访问日志（宿主可自行接管）
        pass

    @property
    def ctx(self) -> ApiContext:
        return self.server.ctx  # type: ignore[attr-defined]

    def _authorized(self) -> bool:
        import hmac

        expected = self.server.token  # type: ignore[attr-defined]
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].encode(), expected.encode())

    def _send(
        self, status: int, body: bytes, content_type: str = "application/json"
    ) -> None:
        self.send_response(status)
        self.send_header(
            "Content-Type",
            content_type
            + (
                "; charset=utf-8"
                if content_type.startswith("text/")
                or content_type == "application/json"
                else ""
            ),
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        limit = (
            36 * 1024 * 1024
            if self.path.split("?")[0] in ("/api/v1/jobs", "/api/v1/memory/import")
            else MAX_BODY_BYTES
        )
        if length <= 0 or length > limit:
            return None
        try:
            data = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    # ---- 入口 ----
    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def do_DELETE(self) -> None:
        self._route("DELETE")

    def _route(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith(f"/api/{API_VERSION}/"):
            # 非 API 路径：静态控制台（web/dist）。静态资源不鉴权（API 全部鉴权；
            # 绑定默认回环；SPA 需先加载才能走登录流程）。
            if method == "GET" and self.server.static_dir:  # type: ignore[attr-defined]
                self._serve_static(path)
                return
            self._send(404, _error("not_found", "unknown endpoint"))
            return
        if not self._authorized():
            self._send(401, _error("unauthorized", "missing or invalid bearer token"))
            return
        rel = path[len(f"/api/{API_VERSION}/") :]
        parts = [p for p in rel.split("/") if p]
        query = parse_qs(parsed.query)
        try:
            # Control mutations are serialized; long jobs/SSE run independently.
            if method in ("POST", "DELETE") and parts and parts[0] != "chat":
                with self.ctx.control_lock:
                    self._dispatch(method, parts, query)
            else:
                self._dispatch(method, parts, query)
        except KeyError:
            self._send(404, _error("not_found", "resource not found"))
        except (ValueError, TypeError):
            self._send(
                422,
                _error(
                    "invalid_request",
                    "Invalid parameters or configuration; check field types and limits",
                ),
            )
        except Exception:
            self._send(500, _error("internal", "internal error"))

    def _dispatch(self, method: str, parts: list[str], query: dict) -> None:
        if not parts:
            self._send(404, _error("not_found", "unknown endpoint"))
            return
        from whale_core.api.console import dispatch

        if dispatch(self, method, parts, query):
            return
        head = parts[0]
        if head == "health" and method == "GET":
            self._send(200, _json_bytes({"result": {"ok": True}}))
        elif head == "status" and method == "GET":
            self._status()
        elif head == "plugins":
            self._plugins(method, parts)
        elif head == "memory":
            self._memory(method, parts, query)
        elif head == "chat" and method == "POST":
            self._chat()
        elif head == "tasks":
            self._tasks(method, parts)
        elif head == "approvals":
            self._approvals(method, parts)
        elif head == "events" and method == "GET":
            self._events_sse()
        else:
            self._send(404, _error("not_found", "unknown endpoint"))

    # ---- 静态控制台（SPA） ----
    _STATIC_TYPES = {
        ".html": "text/html",
        ".js": "text/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
        ".woff2": "font/woff2",
        ".txt": "text/plain",
        ".map": "application/json",
    }

    def _serve_static(self, path: str) -> None:
        from pathlib import Path

        root: Path = self.server.static_dir  # type: ignore[attr-defined]
        rel = path.lstrip("/") or "index.html"
        target = (root / rel).resolve()
        # 防目录穿越：必须落在 root 内
        if root not in target.parents and target != root:
            self._send(403, _error("forbidden", "path outside static root"))
            return
        if not target.is_file():
            if target.suffix:
                self._send(404, _error("not_found", "static resource not found"))
                return
            target = root / "index.html"  # SPA 回落
            if not target.is_file():
                self._send(
                    404, _error("not_found", "console not built (web/dist missing)")
                )
                return
        ctype = self._STATIC_TYPES.get(
            target.suffix.lower(), "application/octet-stream"
        )
        data = target.read_bytes()
        self._send(200, data, content_type=ctype)

    # ---- 路由实现 ----
    def _status(self) -> None:
        from whale_core.memory.store import MemoryKind

        store = self.ctx.store
        plugins = {"loaded": 0, "enabled": 0}
        if self.ctx.manager is not None:
            self.ctx.manager.refresh()
            status = self.ctx.manager.status()
            plugins["loaded"] = len(status)
            plugins["enabled"] = sum(1 for p in status if p.get("enabled"))
        drafts = sum(
            1
            for k in store.keys()
            if k.startswith("life:draft:")
            and not (store.get("life:decision:" + k[11:]) or {}).get("decision")
            and not (store.get("life:ledger:" + k[11:]) or {}).get("attempts")
        )
        if self.ctx.jobs:
            drafts += sum(
                j["status"] == "pending_approval" for j in self.ctx.jobs.list()
            )
        result = {
            "ok": True,
            "version": self.ctx.version,
            "profile": self.ctx.profile,
            "uptime_s": int(time.time() - self.ctx.started_at),
            "plugins": plugins,
            "memory": {
                "episodes": len(store.list_by_kind(MemoryKind.EPISODE)),
                "preferences": len(store.list_by_kind(MemoryKind.PREFERENCE)),
            },
            "drafts_pending": drafts,
            "capabilities": [spec.capability for spec in self.ctx.bundle.tools.specs()]
            if self.ctx.bundle
            else [],
            "initiative_paused": (store.get("settings:life") or {}).get("paused", True),
            "tasks_running": sum(
                j["status"] in ("running", "queued", "cancelling")
                for j in self.ctx.jobs.list()
            )
            if self.ctx.jobs
            else 0,
        }
        self._send(200, _json_bytes({"result": result}))

    def _plugins(self, method: str, parts: list[str]) -> None:
        if self.ctx.manager is None:
            self._send(503, _error("unavailable", "plugin manager not wired"))
            return
        mgr = self.ctx.manager
        mgr.refresh()
        if len(parts) == 1 and method == "GET":
            self._send(200, _json_bytes({"result": {"plugins": mgr.status()}}))
            return
        if len(parts) == 2 and method == "GET":
            pid = parts[1]
            info = next((p for p in mgr.status() if p.get("plugin_id") == pid), None)
            if info is None:
                self._send(404, _error("not_found", f"unknown plugin {pid!r}"))
                return
            info = dict(info)
            info["config_schema"] = mgr.schema(pid)
            info["health"] = mgr.healthcheck(pid)
            info["config"] = mgr.config_view(pid)  # 脱敏视图
            self._send(200, _json_bytes({"result": info}))
            return
        if len(parts) == 3:
            pid, action = parts[1], parts[2]
            if action == "test" and method == "POST":
                self._plugin_test(pid)
                return
            if action == "config" and method == "POST":
                body = self._body_json()
                if body is None or not isinstance(body.get("config"), dict):
                    self._send(
                        400, _error("bad_request", "body must contain config object")
                    )
                    return
                try:
                    mgr.patch_config(pid, body["config"])
                except KeyError:
                    self._send(404, _error("not_found", f"unknown plugin {pid!r}"))
                    return
                except ValueError as exc:
                    self._send(422, _error("invalid_config", str(exc)[:200]))
                    return
                self.ctx.log_event("plugin:config", pid)
                self._send(
                    200, _json_bytes({"result": {"plugin_id": pid, "saved": True}})
                )
                return
            if action in ("enable", "disable") and method == "POST":
                try:
                    if action == "enable":
                        mgr.enable(pid)
                    else:
                        mgr.disable(pid)
                except KeyError:
                    self._send(404, _error("not_found", f"unknown plugin {pid!r}"))
                    return
                except Exception as exc:
                    self._send(500, _error("internal", f"{type(exc).__name__}"))
                    return
                info = next(p for p in mgr.status() if p["plugin_id"] == pid)
                if info["status"] == "error":
                    self._send(
                        422,
                        _error(
                            "activation_failed",
                            "Plugin could not activate; check configuration",
                        ),
                    )
                    return
                self.ctx.log_event(f"plugin:{action}", pid)
                self._send(
                    200,
                    _json_bytes(
                        {"result": {"plugin_id": pid, "status": info["status"]}}
                    ),
                )
                return
            if action == "health" and method == "GET":
                self._send(200, _json_bytes({"result": mgr.healthcheck(pid)}))
                return
        self._send(404, _error("not_found", "unknown endpoint"))

    def _memory(self, method: str, parts: list[str], query: dict) -> None:
        store = self.ctx.store
        if len(parts) == 2 and parts[1] == "search" and method == "GET":
            if self.ctx.curator is None:
                self._send(503, _error("unavailable", "memory curator not wired"))
                return
            q = (query.get("q") or [""])[0]
            hits = self.ctx.curator.recall(q, limit=10)
            result = [
                {
                    "key": h.record.key,
                    "kind": h.record.kind.value,
                    "summary": str(h.record.content.get("summary", "")),
                    "score": round(h.score, 4),
                    "reasons": list(h.reasons),
                    "created_at": h.record.created_at.isoformat(),
                }
                for h in hits
            ]
            self._send(200, _json_bytes({"result": {"hits": result}}))
            return
        if len(parts) == 2 and parts[1] == "export" and method == "POST":
            import io
            from whale_core.memory.export import export_jsonl

            buf = io.StringIO()
            count = export_jsonl(store, buf)
            self.ctx.log_event("memory:export", f"{count} records")
            self._send(
                200, buf.getvalue().encode("utf-8"), content_type="application/x-ndjson"
            )
            return
        if len(parts) == 2:
            key = parts[1]
            from urllib.parse import unquote

            key = unquote(key)
            if method == "GET":
                record = store.get_record(key)
                if record is None:
                    self._send(404, _error("not_found", "no such memory"))
                    return
                self._send(
                    200,
                    _json_bytes(
                        {
                            "result": {
                                "key": record.key,
                                "kind": record.kind.value,
                                "content": dict(record.content),
                                "tags": list(record.tags),
                                "created_at": record.created_at.isoformat(),
                            }
                        }
                    ),
                )
                return
            if method == "DELETE":
                existed = (
                    store.get_record(key) is not None or store.get(key) is not None
                )
                store.delete(key)
                self.ctx.log_event("memory:delete", key)
                self._send(200, _json_bytes({"result": {"deleted": existed}}))
                return
        self._send(404, _error("not_found", "unknown endpoint"))

    def _chat(self) -> None:
        if self.ctx.orchestrator is None:
            self._send(503, _error("unavailable", "orchestrator not wired"))
            return
        body = self._body_json()
        if body is None:
            self._send(400, _error("bad_request", "invalid JSON body"))
            return
        if not isinstance(body.get("text"), str) or not isinstance(
            body.get("conversation_id", "api"), str
        ):
            self._send(
                422,
                _error("invalid_request", "text and conversation_id must be strings"),
            )
            return
        text = body["text"].strip()
        conversation_id = str(body.get("conversation_id") or "api").strip()[:160]
        if not text:
            self._send(400, _error("bad_request", "empty text"))
            return
        try:
            result = self.ctx.orchestrator.handle_message(
                conversation_id, text, source="api"
            )
        except Exception as exc:
            from whale_core.core.deepseek import ModelError

            kind = exc.kind if isinstance(exc, ModelError) else "provider_error"
            self.ctx.log_event("chat:error", kind)
            self._send(
                502, _error(kind, "模型暂不可用，请检查设置与连接（" + kind + "）")
            )
            return
        self.ctx.log_event("chat", conversation_id)
        self._send(
            200,
            _json_bytes(
                {
                    "result": {
                        "reply": result.reply,
                        "status": result.status,
                        "receipts": list(result.receipts),
                        "task_id": result.task_id,
                    }
                }
            ),
        )

    def _plugin_test(self, pid: str) -> None:
        """POST /plugins/{id}/test：用户点击=同意的一次最小真实连通性测试。

        - provider 有 ``reply()``（llm.chat 类）→ 最小 ping（短超时）；
        - 否则回落 manager.healthcheck（不触网）。
        结果只含 ok/latency/错误分类名，**绝不含密钥与异常原文**。
        """
        mgr = self.ctx.manager
        if mgr is None:
            self._send(503, _error("unavailable", "plugin manager not wired"))
            return
        registry = getattr(mgr, "registry", None)
        provider = registry.provider_of(pid) if registry is not None else None
        if provider is None or not hasattr(provider, "reply"):
            health = mgr.healthcheck(pid)
            self._send(
                200,
                _json_bytes(
                    {
                        "result": {
                            "plugin_id": pid,
                            "ok": health.get("status") == "healthy",
                            "latency_ms": None,
                            "via": "healthcheck",
                            "detail": health.get("status", ""),
                        }
                    }
                ),
            )
            return
        started = time.monotonic()
        try:
            from whale_core.core.budget import reserve

            reserve(self.ctx.store, "model_calls")
            reply = provider.reply("只回复 OK")  # 最小调用；成本=一次短请求
            latency = int((time.monotonic() - started) * 1000)
            ok = isinstance(reply, str) and bool(reply)
            self._send(
                200,
                _json_bytes(
                    {
                        "result": {
                            "plugin_id": pid,
                            "ok": ok,
                            "latency_ms": latency,
                            "via": "ping",
                        }
                    }
                ),
            )
        except Exception as exc:
            latency = int((time.monotonic() - started) * 1000)
            kind = getattr(exc, "kind", type(exc).__name__)
            self._send(
                200,
                _json_bytes(
                    {
                        "result": {
                            "plugin_id": pid,
                            "ok": False,
                            "latency_ms": latency,
                            "via": "ping",
                            "error_kind": str(kind)[:40],
                        }
                    }
                ),
            )

    def _tasks(self, method: str, parts: list[str]) -> None:
        store = self.ctx.store
        if len(parts) == 1 and method == "GET":
            index = store.get("tasks:recent") or []
            tasks = []
            for task_id in reversed(index[-50:]):
                record = store.get(f"task:{task_id}")
                if isinstance(record, dict):
                    tasks.append(record)
            if self.ctx.jobs:
                tasks += self.ctx.jobs.list()
                tasks.sort(key=lambda t: t.get("at", 0), reverse=True)
            self._send(200, _json_bytes({"result": {"tasks": tasks[:100]}}))
            return
        if len(parts) == 2 and method == "GET":
            record = (
                self.ctx.jobs.get(parts[1])
                if self.ctx.jobs and parts[1].startswith("job-")
                else store.get(f"task:{parts[1]}")
            )
            if not isinstance(record, dict):
                self._send(404, _error("not_found", "no such task"))
                return
            self._send(200, _json_bytes({"result": record}))
            return
        self._send(404, _error("not_found", "unknown endpoint"))

    def _approvals(self, method: str, parts: list[str]) -> None:
        """Decisions authorize later dispatch; they do NOT send a message."""
        store = self.ctx.store
        if len(parts) == 1 and method == "GET":
            pending = []
            for key in store.keys():
                if not key.startswith("life:draft:"):
                    continue
                draft_id = key.split(":", 2)[2]
                draft = store.get(key)
                ledger = store.get("life:ledger:" + draft_id) or {}
                decision = store.get("life:decision:" + draft_id) or {}
                if not isinstance(draft, dict):
                    continue
                status = ledger.get("status", "draft")
                if ledger.get("attempts", 0) > 0 or status in ("accepted", "unknown"):
                    continue
                pending.append(
                    {
                        "draft_id": draft.get("draft_id", draft_id),
                        "text": str(draft.get("text", ""))[:200],
                        "channel": draft.get("channel", ""),
                        "status": status,
                        "decision": decision.get("decision", ""),
                        "execution_status": "not_dispatched",
                    }
                )
            if self.ctx.jobs:
                pending += [
                    {
                        "draft_id": j["task_id"],
                        "text": j["goal"],
                        "channel": j["kind"],
                        "status": j["status"],
                        "args": j["args"],
                        "decision": j["decision"],
                        "execution_status": "not_dispatched",
                    }
                    for j in self.ctx.jobs.list()
                    if j["status"] == "pending_approval"
                ]
            self._send(200, _json_bytes({"result": {"approvals": pending}}))
            return
        if len(parts) == 3 and parts[2] == "decision" and method == "POST":
            body = self._body_json()
            decision = str((body or {}).get("decision") or "")
            if decision not in ("approve", "reject"):
                self._send(
                    400, _error("bad_request", "decision must be approve|reject")
                )
                return
            draft_id = parts[1]
            if draft_id.startswith("job-") and self.ctx.jobs:
                job = self.ctx.jobs.decide(draft_id, decision)
                self._send(
                    200,
                    _json_bytes(
                        {
                            "result": {
                                "draft_id": draft_id,
                                "decision": decision,
                                "execution_status": job["status"],
                            }
                        }
                    ),
                )
                return
            if not draft_id or not isinstance(
                store.get(f"life:draft:{draft_id}"), dict
            ):
                self._send(404, _error("not_found", "no such draft"))
                return
            ledger = store.get(f"life:ledger:{draft_id}") or {}
            if ledger.get("attempts", 0) > 0:
                self._send(409, _error("conflict", "draft already attempted"))
                return
            key = f"life:decision:{draft_id}"
            existing = store.get(key) or {}
            if existing.get("decision"):
                if existing.get("decision") != decision:
                    self._send(
                        409, _error("conflict", "draft decision already recorded")
                    )
                    return
                self._send(
                    200,
                    _json_bytes(
                        {
                            "result": {
                                "draft_id": draft_id,
                                "decision": decision,
                                "execution_status": "not_dispatched",
                            }
                        }
                    ),
                )
                return
            store.put(key, {"decision": decision, "at": time.time()})
            execution_status = "not_dispatched"
            if self.ctx.jobs and decision == "approve":
                job = self.ctx.jobs.submit(
                    "outbox",
                    {"draft_id": draft_id},
                    request_id="dispatch-"
                    + __import__("hashlib").sha256(draft_id.encode()).hexdigest(),
                )
                queued = self.ctx.jobs.decide(job["task_id"], "approve")
                execution_status = queued["status"]
            self.ctx.log_event(f"approval:{decision}", draft_id)
            self._send(
                200,
                _json_bytes(
                    {
                        "result": {
                            "draft_id": draft_id,
                            "decision": decision,
                            "execution_status": execution_status,
                        }
                    }
                ),
            )
            return
        self._send(404, _error("not_found", "unknown endpoint"))

    def _events_sse(self) -> None:
        """Keep an authenticated SSE stream open, replaying buffered events."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        # Snapshot by index; events are capped at 200, so a slow client may lose
        # old entries. No promise of durable event delivery.
        last = 0
        last_heartbeat = 0.0
        try:
            while not self.ctx.stopping.is_set():
                with self.ctx.event_lock:
                    snapshot = list(self.ctx.events)
                if last == 0:
                    batch = snapshot[-SSE_REPLAY_LIMIT:]
                else:
                    batch = [event for event in snapshot if event.get("seq", 0) > last]
                for event in batch:
                    self.wfile.write(b"data: " + _json_bytes(event) + b"\n\n")
                    last = max(last, int(event.get("seq", 0)))
                now = time.monotonic()
                if now - last_heartbeat >= 15.0 or (last_heartbeat == 0 and not batch):
                    self.wfile.write(b": ping\n\n")
                    last_heartbeat = now
                if batch or last_heartbeat == now:
                    self.wfile.flush()
                self.ctx.stopping.wait(0.5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class WhaleApiServer:
    """管理 API 服务：默认 127.0.0.1，token 必填。

    ``static_dir`` 给定时（指向 web/dist 构建产物），GET 非 /api/ 路径按 SPA
    方式提供静态文件（防目录穿越；非文件路径回落 index.html）。
    """

    def __init__(
        self,
        ctx: ApiContext,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        token: str = "",
        static_dir: str | None = None,
    ) -> None:
        if not token:
            raise ValueError(
                "API token is required (management API must not be unauthenticated)"
            )
        self.ctx = ctx
        ctx.voice_host, ctx.voice_port = host, (port + 1 if port else 0)
        self.token = token
        self._temporary = None
        self._lease = None
        if ctx.config_dir:
            from whale_core.core.lease import RuntimeLease

            self._lease = RuntimeLease(ctx.config_dir)
        if ctx.bundle and ctx.jobs is None:
            from whale_core.core.jobs import JobService
            from whale_core.core.artifacts import ArtifactStore

            if ctx.config_dir:
                directory = __import__("pathlib").Path(ctx.config_dir) / "artifacts"
            else:
                import tempfile

                self._temporary = tempfile.TemporaryDirectory()
                directory = self._temporary.name
            ctx.artifacts = ArtifactStore(ctx.store, directory)
            ctx.artifacts.cleanup()
            ctx.jobs = JobService(ctx.bundle, ctx.artifacts, on_event=ctx.log_event)
        try:
            self._httpd = ThreadingHTTPServer((host, port), _Handler)
        except OSError:
            if self._lease:
                self._lease.close()
            if ctx.jobs:
                ctx.jobs.close()
            raise
        self._httpd.ctx = ctx  # type: ignore[attr-defined]
        self._httpd.token = token  # type: ignore[attr-defined]
        # 静态目录词法归一 + resolve，供穿越校验
        from pathlib import Path

        self._httpd.static_dir = (  # type: ignore[attr-defined]
            Path(static_dir).resolve() if static_dir else None
        )
        self._thread: threading.Thread | None = None

    def _maintenance(self):
        """Only explicit life opt-in enables read-only scheduled exploration."""
        next_cleanup = 0.0
        next_explore = 0.0
        while not self.ctx.stopping.wait(30):
            now = time.monotonic()
            if self.ctx.artifacts and now >= next_cleanup:
                self.ctx.artifacts.cleanup()
                next_cleanup = now + 3600
            if self.ctx.jobs and self.ctx.bundle and now >= next_explore:
                from whale_core.life.service import settings
                from whale_core.life.exploration import InterestTracker

                config = settings(self.ctx.store)
                if (
                    not config["paused"]
                    and config["allowed_hosts"]
                    and InterestTracker(self.ctx.store).topics()
                ):
                    try:
                        if not any(
                            j["kind"] == "explore"
                            and j["status"] in ("queued", "running")
                            for j in self.ctx.jobs.list()
                        ):
                            self.ctx.jobs.submit("explore", {})
                    except ValueError:
                        pass
                next_explore = now + 300

    def _start_maintenance(self):
        if self.ctx.bundle and not hasattr(self, "_maintenance_thread"):
            self._maintenance_thread = threading.Thread(
                target=self._maintenance, name="whale-maintenance", daemon=True
            )
            self._maintenance_thread.start()

    @property
    def host(self) -> str:
        return self._httpd.server_address[0]

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/api/{API_VERSION}"

    def start(self) -> None:
        """后台线程启动（不阻塞宿主主循环）。"""
        self._start_maintenance()
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="whale-api", daemon=True
        )
        self._thread.start()

    def serve_forever(self) -> None:
        self._start_maintenance()
        self._httpd.serve_forever()

    def stop(self) -> None:
        # shutdown() 只能在 serve_forever 进行中调用（否则永久阻塞）；
        # 仅 start() 起过线程时才走 shutdown。
        self.ctx.stopping.set()
        if self._thread is not None:
            self._httpd.shutdown()
            self._thread.join(timeout=3)
        self.ctx.stopping.set()
        self._httpd.server_close()
        if self.ctx.voice:
            self.ctx.voice.close()
        if self.ctx.jobs:
            self.ctx.jobs.close()
        if self._temporary:
            self._temporary.cleanup()
        if self._lease:
            self._lease.close()
