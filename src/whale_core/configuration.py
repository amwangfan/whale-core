"""Persistent host settings. Public config and private credentials never share a file."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

SERVICE_FIELDS = frozenset('WHALE_EMBEDDING_BASE_URL WHALE_EMBEDDING_API_KEY WHALE_EMBEDDING_MODEL WHALE_SHELL_ENABLED WHALE_SHELL_TIER WHALE_SEARCH_PROVIDER TAVILY_API_KEY BRAVE_API_KEY WHALE_IMAGE_BASE_URL WHALE_IMAGE_API_KEY WHALE_IMAGE_MODEL WHALE_SPEECH_BASE_URL WHALE_SPEECH_API_KEY WHALE_ASR_MODEL WHALE_TTS_MODEL WHALE_TTS_VOICE WHALE_VIDEO_BASE_URL WHALE_VIDEO_API_KEY DASHSCOPE_API_KEY WHALE_VIDEO_MODEL WHALE_REALTIME_URL WHALE_REALTIME_API_KEY WHALE_REALTIME_MODEL WHALE_REALTIME_VOICE WHALE_REALTIME_PROTOCOL'.split())
SECRET_FIELDS = frozenset(k for k in SERVICE_FIELDS if k.endswith('API_KEY'))


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.whale-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _private_snapshot(config_dir: str | Path | None) -> dict:
    if config_dir is None:
        return {}
    try:
        data = json.loads((Path(config_dir) / 'secrets' / 'settings.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:
        raise ValueError('private settings cannot be read; restore the configuration') from exc
    if not isinstance(data, dict):
        raise ValueError('private settings must be an object')
    if 'schema_version' in data and data['schema_version'] != 1:
        raise ValueError('unsupported private settings version')
    return data


def read_settings_pair(config_dir: str | Path | None) -> tuple[dict, dict]:
    """Read credentials and endpoints from one generation, even during replace."""
    if config_dir is None:
        return {}, {}
    snapshot = _private_snapshot(config_dir)
    if snapshot.get('schema_version') == 1:
        if not isinstance(snapshot.get('config'), dict) or not isinstance(snapshot.get('private'), dict):
            raise ValueError('invalid settings snapshot')
        return snapshot['config'], snapshot['private']
    path = Path(config_dir) / 'config.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        data = {}
    except (ValueError, OSError) as exc:
        raise ValueError('config.json cannot be read; restore or repair the configuration') from exc
    if not isinstance(data, dict):
        raise ValueError('config.json must be an object')
    # Migration may have atomically installed the first canonical snapshot while
    # a legacy reader loaded the public projection. Prefer the complete snapshot.
    latest = _private_snapshot(config_dir)
    if latest.get('schema_version') == 1:
        if not isinstance(latest.get('config'), dict) or not isinstance(latest.get('private'), dict):
            raise ValueError('invalid settings snapshot')
        return latest['config'], latest['private']
    return data, snapshot


def read_config(config_dir: str | Path | None) -> dict:
    return read_settings_pair(config_dir)[0]


def private_settings(config_dir: str | Path | None) -> dict:
    return read_settings_pair(config_dir)[1]


def configured_env(profile: str, config_dir, env: Mapping[str, str]) -> dict[str, str]:
    result = dict(env)
    config, secrets = read_settings_pair(config_dir)
    model = config.get('model') or {}
    if not isinstance(model, dict):
        raise ValueError('model must be an object')
    prefix = 'DEEPSEEK' if profile == 'cloud' else 'LOCAL_MODEL'
    for key, suffix in (('base_url', 'BASE_URL'), ('model', 'MODEL' if profile == 'cloud' else 'NAME')):
        if model.get(key):
            result[f'{prefix}_{suffix}'] = str(model[key])
    credential = secrets.get('model_api_key')
    if credential is None and model.get('api_key_env'):
        credential = env.get(str(model['api_key_env']))
    if credential is not None:
        result['DEEPSEEK_API_KEY' if profile == 'cloud' else 'LOCAL_MODEL_API_KEY'] = str(credential)
    for name, value in (config.get('services') or {}).items():
        if name in SERVICE_FIELDS and name not in SECRET_FIELDS:
            result[name] = str(value)
    for name, value in (secrets.get('services') or {}).items():
        if name in SECRET_FIELDS:
            result[name] = str(value)
    if 'intent_model_enabled' in config:
        result['WHALE_INTENT_MODEL_ENABLED'] = '1' if config['intent_model_enabled'] else '0'
    return result


def validate_endpoint(url: str, *, realtime: bool = False) -> None:
    from whale_core.core.deepseek import _is_local_or_private_http
    try:
        parts = urlsplit(url)
        allowed = parts.scheme == ('wss' if realtime else 'https') or (
            parts.scheme == ('ws' if realtime else 'http') and
            _is_local_or_private_http(('http' + url[2:]) if realtime else url))
        if not allowed or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError
        _ = parts.port
    except ValueError:
        raise ValueError('endpoint must use HTTPS (or local/private HTTP), without credentials, query or fragment') from None


class SettingsController:
    """Composition root owned by the host; each change is validated before publication."""
    def __init__(self, config_dir: str | Path, bundle, *, env: Mapping[str, str] | None = None):
        self.config_dir = Path(config_dir)
        self.bundle = bundle
        self.env = dict(os.environ if env is None else env)
        self.lock = threading.RLock()

    def view(self) -> dict:
        from whale_core.plugins.model_provider import resolve_chat_model
        bundle = self.bundle
        model = resolve_chat_model(bundle.registry)
        env = configured_env(bundle.profile, self.config_dir, self.env)
        return {
            'profile': bundle.profile,
            'model': {'base_url': getattr(model, 'base_url', ''), 'model': getattr(model, 'model', ''),
                      'api_key_configured': bool(getattr(model, 'api_key', ''))},
            'intent_model_enabled': env.get('WHALE_INTENT_MODEL_ENABLED') == '1',
            'capabilities': sorted({s.capability for s in bundle.tools.specs()} | {c for m in bundle.registry.manifests() for c in m.provides}),
            'services': {key: (bool(env.get(key)) if key in SECRET_FIELDS else env.get(key, ''))
                         for key in sorted(SERVICE_FIELDS)},
            'notes': list(bundle.notes),
        }

    def update(self, patch: dict):
        # Match lock order used by workflows: execution lock then runtime task lock.
        # No new task may slip between the busy check and publication.
        from whale_core.service.workflows import WorkflowsService
        gate = WorkflowsService(self.bundle.orchestrator)._execution_lock
        if not gate.acquire(blocking=False):
            raise ValueError('settings cannot change while a workflow is running')
        try:
            with self.bundle.orchestrator._task_lock:
                return self._update_locked(patch)
        finally:
            gate.release()

    def _update_locked(self, patch: dict):
        from whale_core.profiles import assemble_runtime
        if set(patch) - {'profile', 'model', 'intent_model_enabled', 'services'}:
            raise ValueError('unknown setting')
        with self.lock:
            if any(isinstance(r := self.bundle.store.get(k), dict) and (r.get('status') == 'running' or r.get('in_flight'))
                   for k in self.bundle.store.keys() if k.startswith('task:')):
                raise ValueError('settings cannot change while a task is running')
            config, private = read_settings_pair(self.config_dir)
            profile = patch.get('profile', config.get('profile', self.bundle.profile))
            if profile not in ('cloud', 'local'):
                raise ValueError('profile must be cloud or local')
            model_patch = patch.get('model', {})
            if not isinstance(model_patch, dict) or set(model_patch) - {'base_url', 'model', 'api_key'}:
                raise ValueError('invalid model settings')
            changing_profile = profile != self.bundle.profile
            model = {} if changing_profile else dict(config.get('model') or {})
            if changing_profile:
                private.pop('model_api_key', None)
            for key in ('base_url', 'model'):
                if key in model_patch:
                    value = model_patch[key]
                    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
                        raise ValueError(f'invalid model {key}')
                    if key == 'base_url':
                        validate_endpoint(value)
                    model[key] = value.strip()
            if 'api_key' in model_patch:
                key = model_patch['api_key']
                if key is not None and not isinstance(key, str):
                    raise ValueError('api_key must be a string or null')
                if key is None:
                    private['model_api_key'] = ''
                elif key:
                    private['model_api_key'] = key
            config.update(profile=profile, model=model)
            if 'intent_model_enabled' in patch:
                if not isinstance(patch['intent_model_enabled'], bool):
                    raise ValueError('intent_model_enabled must be boolean')
                config['intent_model_enabled'] = patch['intent_model_enabled']
            services = patch.get('services', {})
            if not isinstance(services, dict) or set(services) - SERVICE_FIELDS:
                raise ValueError('unknown service setting')
            for name, value in services.items():
                if name == 'WHALE_REALTIME_PROTOCOL' and value not in ('ga', 'legacy', '', None):
                    raise ValueError('realtime protocol must be ga or legacy')
                if name == 'WHALE_SHELL_TIER' and value not in ('managed', 'safe', 'personal', '', None):
                    raise ValueError('invalid shell tier')
                if value is not None and not isinstance(value, str):
                    raise ValueError('service setting must be a string or null')
                if value and (name.endswith('BASE_URL') or name == 'WHALE_REALTIME_URL'):
                    validate_endpoint(value, realtime=name == 'WHALE_REALTIME_URL')
                destination = private.setdefault('services', {}) if name in SECRET_FIELDS else config.setdefault('services', {})
                if name in SECRET_FIELDS and value == '':
                    continue
                destination[name] = value or ''
            # Construct using a projected environment before touching persisted settings.
            projected = dict(self.env)
            prefix = 'DEEPSEEK' if profile == 'cloud' else 'LOCAL_MODEL'
            if model.get('base_url'):
                projected[f'{prefix}_BASE_URL'] = model['base_url']
            if model.get('model'):
                projected['DEEPSEEK_MODEL' if profile == 'cloud' else 'LOCAL_MODEL_NAME'] = model['model']
            if 'model_api_key' in private:
                projected['DEEPSEEK_API_KEY' if profile == 'cloud' else 'LOCAL_MODEL_API_KEY'] = private['model_api_key']
            projected.update(config.get('services') or {})
            projected.update(private.get('services') or {})
            if 'intent_model_enabled' in config:
                projected['WHALE_INTENT_MODEL_ENABLED'] = '1' if config['intent_model_enabled'] else '0'
            # Validate in an isolated store, not the active journal. Candidate construction
            # must not mark live tasks recovered or alter enabled states during validation.
            from whale_core.memory.store import InMemoryStore
            assemble_runtime(profile, store=InMemoryStore(), env=projected,
                             use_saved_config=False)
            secret_dir = self.config_dir / 'secrets'
            secret_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(secret_dir, 0o700)
            # One atomic source of truth binds endpoint/model and credentials.
            # The public file is a convenience projection; never a recovery fallback
            # once a canonical private snapshot exists.
            atomic_json(secret_dir / 'settings.json', {
                'schema_version': 1, 'config': config, 'private': private})
            try:
                atomic_json(self.config_dir / 'config.json', config)
            except OSError:
                pass  # canonical settings are already durably committed
            candidate = assemble_runtime(profile, store=self.bundle.store, env=projected,
                                         config_dir=str(self.config_dir), use_saved_config=False)
            previous = self.bundle
            self.bundle = candidate
            # Revoke cached references held by the previous composition root.
            for manifest in list(previous.registry.manifests()):
                previous.registry.unregister(manifest.plugin_id)
            callback = getattr(self, 'on_apply', None)
            if callback:
                callback(candidate)
            return candidate
