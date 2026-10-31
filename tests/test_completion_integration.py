"""Host-level regression coverage for settings, lifecycle and API boundaries."""
from __future__ import annotations
import base64
import json
from http.client import HTTPConnection
import pytest
from whale_core.api.server import ApiContext, WhaleApiServer
from whale_core.configuration import SettingsController
from whale_core.core.deepseek import DeepSeekCompanion
from whale_core.profiles import assemble_runtime


def request(server, method, route, body=None, *, token='test', origin=None):
    conn = HTTPConnection('127.0.0.1', server.port, timeout=5)
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
    if origin:
        headers['Origin'] = origin
    conn.request(method, '/api/v1/' + route, json.dumps(body or {}) if method == 'POST' else None, headers)
    resp = conn.getresponse()
    data = resp.read()
    status = resp.status
    conn.close()
    return status, json.loads(data) if 'application/json' in resp.getheader('Content-Type', '') else data


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(DeepSeekCompanion, 'reply', lambda self, text, **kw: 'connected ' + self.model)
    bundle = assemble_runtime('local', config_dir=str(tmp_path), env={})
    controller = SettingsController(tmp_path, bundle, env={})
    ctx = ApiContext(bundle.store, manager=bundle.manager, orchestrator=bundle.orchestrator,
                     curator=bundle.companion.curator, settings=controller, optional=bundle.optional)
    server = WhaleApiServer(ctx, port=0, token='test')
    server.start()
    yield server, controller
    server.stop()
    bundle.store.close()


def test_settings_persist_and_activate_without_returning_credentials(host, tmp_path):
    server, controller = host
    code, response = request(server, 'POST', 'settings', {'profile': 'cloud', 'model': {
        'base_url': 'https://example.com/v1', 'model': 'chosen-model', 'api_key': 'PRIVATE-KEY'}})
    assert code == 200
    assert response['result']['model']['model'] == 'chosen-model'
    assert response['result']['model']['api_key_configured'] is True
    assert 'PRIVATE-KEY' not in json.dumps(response)
    assert 'PRIVATE-KEY' not in (tmp_path/'config.json').read_text()
    assert (tmp_path/'secrets/settings.json').stat().st_mode & 0o077 == 0
    assert request(server, 'POST', 'providers/test')[1]['result']['ok'] is True
    assert request(server, 'POST', 'chat', {'text': 'hello'})[1]['result']['reply'] == 'connected chosen-model'
    reloaded = assemble_runtime('cloud', config_dir=str(tmp_path), env={})
    assert reloaded.companion.model.model == 'chosen-model'
    assert reloaded.companion.model.api_key == 'PRIVATE-KEY'
    reloaded.store.close()


def test_disabled_plugin_is_removed_from_tools_and_persists(host, tmp_path):
    server, controller = host
    before = request(server, 'GET', 'plugins')[1]['result']['plugins']
    assert {'local_model', 'web_fetch'} <= {p['plugin_id'] for p in before}
    assert request(server, 'POST', 'plugins/web_fetch/disable')[0] == 200
    assert controller.bundle.tools.get('fetch_url') is None
    assert 'web.fetch' not in request(server, 'GET', 'settings')[1]['result']['capabilities']
    reloaded = assemble_runtime('local', config_dir=str(tmp_path), env={})
    assert reloaded.tools.get('fetch_url') is None
    reloaded.store.close()
    assert request(server, 'POST', 'plugins/web_fetch/enable')[0] == 200
    assert controller.bundle.tools.get('fetch_url') is not None
    assert request(server, 'POST', 'plugins/local_model/disable')[0] == 200
    assert request(server, 'POST', 'chat', {'text': 'hello'})[0] == 503
    assert 'llm.chat' not in request(server, 'GET', 'settings')[1]['result']['capabilities']


def test_settings_validation_is_nonmutating_and_busy_rejected(host, tmp_path):
    server, controller = host
    store = controller.bundle.store
    store.put('task:active', {'status': 'running', 'in_flight': True})
    assert request(server, 'POST', 'settings', {'model': {'model': 'new'}})[0] == 422
    assert store.get('task:active')['status'] == 'running'
    store.delete('task:active')
    assert request(server, 'POST', 'settings', {'model': {'base_url': 'https://user:password@example.com'}})[0] == 422
    assert not (tmp_path/'config.json').exists()


def test_memory_cannot_delete_internal_configuration(host):
    server, controller = host
    controller.bundle.store.put('plugin:state:local_model', {'enabled': True})
    assert request(server, 'DELETE', 'memory/plugin%3Astate%3Alocal_model')[0] == 404
    assert controller.bundle.store.get('plugin:state:local_model') == {'enabled': True}
    assert request(server, 'POST', 'chat', {'text': '记住：测试偏好'})[0] == 200
    hits = request(server, 'GET', 'memory/search')[1]['result']['hits']
    assert hits


def test_origin_auth_and_private_artifact(host):
    server, controller = host
    assert request(server, 'GET', 'settings', token='wrong')[0] == 401
    assert request(server, 'POST', 'settings', {}, origin='https://evil.example')[0] == 403
    artifact = controller.bundle.optional.artifacts.save_bytes(b'fake image bytes', 'image/png')
    assert request(server, 'GET', 'artifacts/' + artifact.artifact_id, token='wrong')[0] == 401
    assert request(server, 'GET', 'artifacts/' + artifact.artifact_id) == (200, b'fake image bytes')
    assert request(server, 'GET', 'artifacts/../../secrets')[0] != 200


def test_explicit_capability_uses_persistent_task(host):
    server, controller = host
    from whale_core.core.runtime import ToolSpec
    controller.bundle.tools.register(ToolSpec(name='echo_test', capability='test.echo', handler=lambda p: 'hello '+p['text']))
    code, result = request(server, 'POST', 'capabilities/test.echo', {'params': {'text': 'world'}})
    assert code == 200
    assert result['result']['status'] == 'answered'
    assert result['result']['output'] == 'hello world'
    task_id = result['result']['task_id']
    assert request(server, 'GET', 'tasks/' + task_id)[1]['result']['status'] == 'answered'
    assert request(server, 'POST', 'capabilities/unknown', {'params': {}})[0] == 404


def test_workflow_api_and_diagnostics(host):
    server, _ = host
    assert request(server, 'GET', 'diagnostics')[0] == 200
    assert request(server, 'GET', 'workflows/skills')[1]['result']['skills']
    assert request(server, 'POST', 'workflows/settings', {'paused': True})[0] == 200
    assert request(server, 'GET', 'workflows/settings')[1]['result']['paused'] is True
