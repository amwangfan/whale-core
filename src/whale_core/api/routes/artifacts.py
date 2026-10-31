"""artifacts 面路由：GET /artifacts/{id} 按 content_type 取回产物字节。

从 api/server.py 拆分（issue #30），逻辑逐行等价。
"""
from __future__ import annotations

from whale_core.api.server import _error


def artifact(handler, artifact_id: str) -> None:
    artifacts = getattr(handler.ctx.optional, 'artifacts', None)
    if artifacts is None:
        handler._send(503, _error('unavailable', 'artifact store not configured'))
        return
    try:
        artifact, data = artifacts.read(artifact_id)
    except (KeyError, FileNotFoundError, ValueError):
        handler._send(404, _error('not_found', 'artifact missing or expired'))
        return
    handler._send(200, data, content_type=artifact.content_type or 'application/octet-stream')
