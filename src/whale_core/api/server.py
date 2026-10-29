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
    profile: str = "demo"
    manager: Any = None        # PluginManager
    orchestrator: Any = None   # ConversationOrchestrator
    curator: Any = None        # MemoryCurator
    started_at: float = field(default_factory=time.time)
    events: list[dict] = field(default_factory=list)  # 近期事件环（API 层可观测性）

    def log_event(self, kind: str, detail: str = "") -> None:
        self.events.append({"at": time.time(), "kind": kind, "detail": detail[:200]})
        del self.events[:-200]


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _error(code: str, message: str) -> bytes:
    return _json_bytes({"error": {"code": code, "message": message}})


class _Handler(BaseHTTPRequestHandler):
    server_version = "whale-api/0.1"

    # ---- 基础设施 ----
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

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY_BYTES:
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
        rel = path[len(f"/api/{API_VERSION}/"):]
        parts = [p for p in rel.split("/") if p]
        query = parse_qs(parsed.query)
        try:
            self._dispatch(method, parts, query)
        except Exception:
            self._send(500, _error("internal", "internal error"))

    def _dispatch(self, method: str, parts: list[str], query: dict) -> None:
        if not parts:
            self._send(404, _error("not_found", "unknown endpoint"))
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
        ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
        ".json": "application/json", ".svg": "image/svg+xml",
        ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2",
        ".txt": "text/plain", ".map": "application/json",
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
            target = root / "index.html"  # SPA 回落
            if not target.is_file():
                self._send(404, _error("not_found", "console not built (web/dist missing)"))
                return
        ctype = self._STATIC_TYPES.get(target.suffix.lower(), "application/octet-stream")
        data = target.read_bytes()
        self._send(200, data, content_type=ctype)

    # ---- 路由实现 ----
    def _status(self) -> None:
        from whale_core.memory.store import MemoryKind
        store = self.ctx.store
        plugins = {"loaded": 0, "enabled": 0}
        if self.ctx.manager is not None:
            status = self.ctx.manager.status()
            plugins["loaded"] = len(status)
            plugins["enabled"] = sum(1 for p in status if p.get("enabled"))
        drafts = sum(1 for k in store.keys() if k.startswith("life:draft:"))
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
        }
        self._send(200, _json_bytes({"result": result}))

    def _plugins(self, method: str, parts: list[str]) -> None:
        if self.ctx.manager is None:
            self._send(503, _error("unavailable", "plugin manager not wired"))
            return
        mgr = self.ctx.manager
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
            info["config"] = mgr.config_view(pid)  # 脱敏视图
            self._send(200, _json_bytes({"result": info}))
            return
        if len(parts) == 3:
            pid, action = parts[1], parts[2]
            if action == "config" and method == "POST":
                body = self._body_json()
                if body is None or not isinstance(body.get("config"), dict):
                    self._send(400, _error("bad_request", "body must contain config object"))
                    return
                try:
                    mgr.set_config(pid, body["config"])
                except KeyError:
                    self._send(404, _error("not_found", f"unknown plugin {pid!r}"))
                    return
                except ValueError as exc:
                    self._send(422, _error("invalid_config", str(exc)[:200]))
                    return
                self.ctx.log_event("plugin:config", pid)
                self._send(200, _json_bytes({"result": {"plugin_id": pid, "saved": True}}))
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
                self.ctx.log_event(f"plugin:{action}", pid)
                self._send(200, _json_bytes({"result": {"plugin_id": pid, "status": action + "d"}}))
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
            result = [{
                "key": h.record.key, "kind": h.record.kind.value,
                "summary": str(h.record.content.get("summary", "")),
                "score": round(h.score, 4), "reasons": list(h.reasons),
                "created_at": h.record.created_at.isoformat(),
            } for h in hits]
            self._send(200, _json_bytes({"result": {"hits": result}}))
            return
        if len(parts) == 2 and parts[1] == "export" and method == "POST":
            import io
            from whale_core.memory.export import export_jsonl
            buf = io.StringIO()
            count = export_jsonl(store, buf)
            self.ctx.log_event("memory:export", f"{count} records")
            self._send(200, buf.getvalue().encode("utf-8"),
                       content_type="application/x-ndjson")
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
                self._send(200, _json_bytes({"result": {
                    "key": record.key, "kind": record.kind.value,
                    "content": dict(record.content), "tags": list(record.tags),
                    "created_at": record.created_at.isoformat(),
                }}))
                return
            if method == "DELETE":
                existed = store.get_record(key) is not None or store.get(key) is not None
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
        text = str(body.get("text") or "").strip()
        conversation_id = str(body.get("conversation_id") or "api").strip()[:160]
        if not text:
            self._send(400, _error("bad_request", "empty text"))
            return
        result = self.ctx.orchestrator.handle_message(conversation_id, text, source="api")
        self.ctx.log_event("chat", conversation_id)
        self._send(200, _json_bytes({"result": {
            "reply": result.reply, "status": result.status,
            "receipts": list(result.receipts), "task_id": result.task_id,
        }}))

    def _tasks(self, method: str, parts: list[str]) -> None:
        store = self.ctx.store
        if len(parts) == 1 and method == "GET":
            index = store.get("tasks:recent") or []
            tasks = []
            for task_id in reversed(index[-50:]):
                record = store.get(f"task:{task_id}")
                if isinstance(record, dict):
                    tasks.append(record)
            self._send(200, _json_bytes({"result": {"tasks": tasks}}))
            return
        if len(parts) == 2 and method == "GET":
            record = store.get(f"task:{parts[1]}")
            if not isinstance(record, dict):
                self._send(404, _error("not_found", "no such task"))
                return
            self._send(200, _json_bytes({"result": record}))
            return
        self._send(404, _error("not_found", "unknown endpoint"))

    def _approvals(self, method: str, parts: list[str]) -> None:
        store = self.ctx.store
        if len(parts) == 1 and method == "GET":
            pending = []
            for key in store.keys():
                if not key.startswith("life:draft:"):
                    continue
                draft = store.get(key)
                ledger = store.get("life:ledger:" + key.split(":", 2)[2]) or {}
                if isinstance(draft, dict):
                    pending.append({"draft_id": draft.get("draft_id", key.split(":", 2)[2]),
                                    "text": str(draft.get("text", ""))[:200],
                                    "channel": draft.get("channel", ""),
                                    "status": ledger.get("status", "unknown")})
            self._send(200, _json_bytes({"result": {"approvals": pending}}))
            return
        if len(parts) == 3 and parts[2] == "decision" and method == "POST":
            body = self._body_json()
            decision = str((body or {}).get("decision") or "")
            if decision not in ("approve", "reject"):
                self._send(400, _error("bad_request", "decision must be approve|reject"))
                return
            draft_id = parts[1]
            key = f"life:decision:{draft_id}"
            store.put(key, {"decision": decision, "at": time.time()})
            self.ctx.log_event(f"approval:{decision}", draft_id)
            self._send(200, _json_bytes({"result": {"draft_id": draft_id, "decision": decision}}))
            return
        self._send(404, _error("not_found", "unknown endpoint"))

    def _events_sse(self) -> None:
        """SSE：先回放近期事件，之后每 15s 心跳（MVP 语义，客户端断线重连拉新）。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        for event in self.ctx.events[-SSE_REPLAY_LIMIT:]:
            self.wfile.write(b"data: " + _json_bytes(event) + b"\n\n")
        self.wfile.write(b": ping\n\n")
        self.wfile.flush()


class WhaleApiServer:
    """管理 API 服务：默认 127.0.0.1，token 必填。

    ``static_dir`` 给定时（指向 web/dist 构建产物），GET 非 /api/ 路径按 SPA
    方式提供静态文件（防目录穿越；非文件路径回落 index.html）。
    """

    def __init__(self, ctx: ApiContext, *, host: str = "127.0.0.1", port: int = 8080,
                 token: str = "", static_dir: str | None = None) -> None:
        if not token:
            raise ValueError("API token is required (management API must not be unauthenticated)")
        self.ctx = ctx
        self.token = token
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.ctx = ctx       # type: ignore[attr-defined]
        self._httpd.token = token   # type: ignore[attr-defined]
        # 静态目录词法归一 + resolve，供穿越校验
        from pathlib import Path
        self._httpd.static_dir = (  # type: ignore[attr-defined]
            Path(static_dir).resolve() if static_dir else None
        )
        self._thread: threading.Thread | None = None

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
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="whale-api", daemon=True)
        self._thread.start()

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def stop(self) -> None:
        # shutdown() 只能在 serve_forever 进行中调用（否则永久阻塞）；
        # 仅 start() 起过线程时才走 shutdown。
        if self._thread is not None:
            self._httpd.shutdown()
            self._thread.join(timeout=3)
        self._httpd.server_close()
