"""whale-core 管理 API（roadmap §5.3/§5.4）。

设计要点：
- 纯 stdlib（ThreadingHTTPServer），沿用 wq_push/qq_onebot 的回环监听先例；
  **默认只绑 127.0.0.1**，云端部署须显式配监听地址 + TLS 反代（宿主职责）。
- 所有 /api/ 请求强制 ``Authorization: Bearer <token>``（hmac.compare_digest
  常量时间比较）；token 由宿主注入，本模块不生成、不回显、不进日志。
- 统一错误格式 ``{"error": {"code", "message"}}``；成功 ``{"result": ...}``。
- 配置类输出一律过 PluginManager.config_view 脱敏；本层不回显任何 secret。
- 前端只读本 API，不直接碰 SQLite/文件系统/密钥（roadmap §5.1）。

结构（issue #30）：本模块保留中间件/鉴权/异常映射/静态服务/生命周期与根
路由表；各 API surface 的路由本体拆到 ``whale_core.api.routes``（system /
chat / plugins / media / workflows / artifacts / operations），studio/auth
与 devices 沿用 ``api.studio`` / ``api.devices_api`` 独立模块。
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
    manager: Any = None        # PluginManager
    orchestrator: Any = None   # ConversationOrchestrator
    settings: Any = None       # SettingsController
    worker: Any = None
    transcript_recorder: Any = None
    transcript_ids: Any = field(default_factory=set)
    voice_bridge: Any = None
    pcm_voice_gateway: Any = None  # api.voice.VoiceGateway（懒创建，可选 websockets）
    device_registry: Any = None    # devices.registry.DeviceRegistry（懒构建）
    device_gateway: Any = None     # devices.gateway.DeviceGateway（懒创建，可选 websockets）
    device_adapter: Any = None     # devices.perception.DevicePerceptionAdapter
    workflows: Any = None
    optional: Any = None       # OptionalCapabilities
    studio: Any = None         # api.studio.StudioServices（S5；缺省由 ctx.store 现场构建）
    lock: Any = field(default_factory=threading.RLock)
    stopping: Any = field(default_factory=threading.Event)
    curator: Any = None        # MemoryCurator
    started_at: float = field(default_factory=time.time)
    events: list[dict] = field(default_factory=list)  # bounded, non-durable log
    event_seq: int = 0
    trace_ledger: Any = None  # issue #39：惰性构建（api/routes/traces.py）

    def log_event(self, kind: str, detail: str = "") -> None:
        with self.lock:
            self.event_seq += 1
            event = {"at": time.time(), "seq": self.event_seq,
                     "kind": kind, "detail": detail[:200]}
            self.events.append(event)
            del self.events[:-200]

    def apply_bundle(self, bundle) -> None:
        with self.lock:
            self.profile, self.manager = bundle.profile, bundle.manager
            self.orchestrator, self.curator = bundle.orchestrator, bundle.companion.curator
            self.optional = bundle.optional
            from whale_core.service.workflows import WorkflowsService
            self.workflows = WorkflowsService(bundle.orchestrator)
            if self.worker is not None:
                self.worker.set_service(self.workflows)
            if self.voice_bridge is not None:
                self.voice_bridge.shutdown()
                self.voice_bridge = None



def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _error(code: str, message: str) -> bytes:
    return _json_bytes({"error": {"code": code, "message": message}})


class _Handler(BaseHTTPRequestHandler):
    server_version = "whale-api/0.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

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
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header('Referrer-Policy', 'no-referrer')
        if content_type == 'text/html':
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body_json(self, max_bytes: int = MAX_BODY_BYTES) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > max_bytes:
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

    def do_PUT(self) -> None:
        self._route("PUT")

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
        origin = self.headers.get('Origin')
        if origin:
            origin_parts = urlparse(origin)
            if origin_parts.scheme not in ('http', 'https') or origin_parts.netloc != self.headers.get('Host'):
                self._send(403, _error('forbidden', 'cross-origin request denied'))
                return
        rel = path[len(f"/api/{API_VERSION}/"): ]
        parts = [p for p in rel.split("/") if p]
        query = parse_qs(parsed.query)
        try:
            self._dispatch(method, parts, query)
        except Exception:
            self._send(500, _error("internal", "internal error"))

    def _dispatch(self, method: str, parts: list[str], query: dict) -> None:
        # 根路由表（匹配顺序即契约）；路由本体在 whale_core.api.routes.*。
        from whale_core.api import routes
        if not parts:
            self._send(404, _error("not_found", "unknown endpoint"))
            return
        head = parts[0]
        if parts == ["health"] and method == "GET":
            routes.system.health(self)
        elif parts == ["status"] and method == "GET":
            routes.system.status(self)
        elif head == 'settings' and len(parts) == 1:
            routes.system.settings(self, method)
        elif head == 'diagnostics' and method == 'GET' and len(parts) == 1:
            routes.system.diagnostics(self)
        elif head == 'providers' and parts == ['providers', 'test'] and method == 'POST':
            routes.system.provider_test(self)
        elif head == 'capabilities' and len(parts) == 2 and method == 'POST':
            routes.media.capability(self, parts[1])
        elif head == 'video':
            routes.media.video(self, method, parts)
        elif head == 'voice':
            routes.media.voice(self, method, parts)
        elif head == 'workflows':
            routes.workflows.workflows(self, method, parts)
        elif head == 'artifacts' and len(parts) == 2 and method == 'GET':
            routes.artifacts.artifact(self, parts[1])
        elif head == "plugins":
            routes.plugins.plugins(self, method, parts)
        elif head == "conversations":
            routes.chat.conversations(self, method, parts)
        elif head == "studio":
            from whale_core.api import studio as _studio
            _studio.dispatch_studio(self, method, parts, query)
        elif head == "devices":
            from whale_core.api import devices_api as _devices
            _devices.dispatch_devices(self, method, parts)
        elif head == "auth":
            from whale_core.api import studio as _studio
            _studio.dispatch_auth(self, method, parts)
        elif head == "memory":
            routes.chat.memory(self, method, parts, query)
        elif head == "traces":
            routes.traces.traces(self, method, parts, query)
        elif head == "activity":
            routes.traces.activity(self, method, parts, query)
        elif parts == ["chat", "stream"] and method == "POST":
            routes.chat.chat_stream(self)
        elif parts == ["chat"] and method == "POST":
            routes.chat.chat(self)
        elif head == "tasks":
            routes.operations.tasks(self, method, parts)
        elif head == "approvals":
            routes.operations.approvals(self, method, parts)
        elif head == "life":
            routes.operations.life(self, method, parts)
        elif parts == ["events"] and method == "GET":
            routes.operations.events_sse(self)
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


class _BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


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
        if ctx.settings is not None:
            ctx.settings.on_apply = ctx.apply_bundle
        self.token = token
        self._httpd = _BoundedServer((host, port), _Handler)
        self._httpd.ctx = ctx       # type: ignore[attr-defined]
        self._httpd.token = token   # type: ignore[attr-defined]
        # PCM 语音中继（可选 websockets 依赖）懒监听在 API 端口 + 1（沿用 PR#4
        # 约定：8080 → 8081）；port=0（临时端口）时中继也用临时端口。
        self._httpd.voice_relay_host = host                    # type: ignore[attr-defined]
        self._httpd.voice_relay_port = port + 1 if port else 0  # type: ignore[attr-defined]
        # 设备面 WSS 长连（D2）懒监听在 API 端口 + 2（voice +1 的顺延约定）；
        # port=0（临时端口）时同样用临时端口。
        self._httpd.device_relay_host = host                    # type: ignore[attr-defined]
        self._httpd.device_relay_port = port + 2 if port else 0  # type: ignore[attr-defined]
        # 静态目录词法归一 + resolve，供穿越校验
        from pathlib import Path
        self._httpd.static_dir = (  # type: ignore[attr-defined]
            Path(static_dir).resolve() if static_dir else None
        )
        if ctx.orchestrator is not None:
            from whale_core.service.workflows import WorkflowsService
            from whale_core.service.worker import WorkflowsWorker
            ctx.workflows = ctx.workflows or WorkflowsService(ctx.orchestrator)
            ctx.worker = WorkflowsWorker(ctx.workflows)
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
        if self.ctx.worker:
            self.ctx.worker.start()
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="whale-api", daemon=True)
        self._thread.start()

    def serve_forever(self) -> None:
        if self.ctx.worker:
            self.ctx.worker.start()
        try:
            self._httpd.serve_forever()
        finally:
            if self.ctx.worker:
                self.ctx.worker.stop()

    def stop(self) -> None:
        self.ctx.stopping.set()
        if self.ctx.worker:
            self.ctx.worker.stop()
        if self.ctx.voice_bridge is not None:
            self.ctx.voice_bridge.shutdown()
        if self.ctx.pcm_voice_gateway is not None:
            self.ctx.pcm_voice_gateway.close()
            self.ctx.pcm_voice_gateway = None
        if self.ctx.device_gateway is not None:
            self.ctx.device_gateway.close()
            self.ctx.device_gateway = None
        if self.ctx.transcript_recorder is not None:
            self.ctx.transcript_recorder.close()
            self.ctx.transcript_recorder = None
        # shutdown() 只能在 serve_forever 进行中调用（否则永久阻塞）；
        # 仅 start() 起过线程时才走 shutdown。
        if self._thread is not None:
            self._httpd.shutdown()
            self._thread.join(timeout=3)
        self._httpd.server_close()
