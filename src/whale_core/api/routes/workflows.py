"""workflows 面路由：/workflows/skills、/workflows/settings、/workflows/explore、
技能运行与工作流恢复。

从 api/server.py 拆分（issue #30），逻辑逐行等价。
"""
from __future__ import annotations

from whale_core.api.server import _error, _json_bytes


def workflows(handler, method: str, parts: list[str]) -> None:
    ctx = handler.ctx
    if ctx.workflows is None and ctx.orchestrator:
        from whale_core.service.workflows import WorkflowsService
        ctx.workflows = WorkflowsService(ctx.orchestrator)
    service = ctx.workflows
    if service is None:
        handler._send(503, _error('unavailable', 'workflows not wired'))
        return
    body = None
    if method == 'POST':
        body = handler._body_json()
        if body is None:
            handler._send(400, _error('bad_request', 'JSON body required'))
            return
    try:
        if parts == ['workflows', 'skills'] and method == 'GET':
            result = {'skills': service.list_skills()}
        elif parts == ['workflows', 'settings'] and method == 'GET':
            result = service.get_settings()
        elif parts == ['workflows', 'settings'] and method == 'POST':
            result = service.update_settings(body)
        elif parts == ['workflows', 'explore'] and method == 'POST':
            result = service.explore(body.get('topics'))
        elif len(parts) == 4 and parts[1] == 'skills' and parts[3] == 'run' and method == 'POST':
            result = service.run_skill(parts[2], str(body.get('input_text', '')))
        elif len(parts) == 3 and parts[2] == 'resume' and method == 'POST':
            result = service.resume_workflow(parts[1])
        else:
            handler._send(404, _error('not_found', 'unknown endpoint'))
            return
    except KeyError:
        handler._send(404, _error('not_found', 'workflow or skill not found'))
        return
    except ValueError:
        handler._send(422, _error('bad_request', 'invalid workflow configuration or state'))
        return
    handler._send(200, _json_bytes({'result': result}))
