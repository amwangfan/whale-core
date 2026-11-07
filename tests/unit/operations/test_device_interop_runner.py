"""The joint gate must fail promptly and preserve each run's evidence."""
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / 'scripts/check_device_interop.py'


@pytest.fixture
def gate(tmp_path):
    spec = importlib.util.spec_from_file_location('device_gate', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    mobile = tmp_path / 'mobile'
    for rel in ('scripts/generate_capabilities.py', 'tests/contract_validate.py'):
        file = mobile / rel
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('')
    logs = tmp_path / 'logs'
    return module, mobile, logs


def invoke(gate, *extra):
    module, mobile, logs = gate
    return module.main(['--mobile', str(mobile), '--log-dir', str(logs), *extra])


def test_successful_runs_keep_distinct_logs_and_temp_roots(gate, monkeypatch):
    module, _, logs = gate
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        assert kwargs['timeout'] == 180
        return SimpleNamespace(returncode=0, stdout='ok\n', stderr='')
    monkeypatch.setattr(module.subprocess, 'run', run)
    assert invoke(gate) == 0
    assert invoke(gate) == 0
    runs = list(logs.glob('run-*'))
    assert len(runs) == 2
    for root in runs:
        summary = json.loads((root / 'summary.json').read_text())
        assert summary['ok'] and len(summary['stages']) == 5
    temp_paths = [cmd[cmd.index('--basetemp') + 1] for cmd in commands if '--basetemp' in cmd]
    assert len(set(temp_paths)) == 4


def test_failed_stage_stops_and_writes_partial_summary(gate, monkeypatch):
    module, _, logs = gate
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=7 if len(calls) == 2 else 0, stdout='details', stderr='')
    monkeypatch.setattr(module.subprocess, 'run', run)
    assert invoke(gate) == 7
    summary = json.loads(next(logs.glob('run-*/summary.json')).read_text())
    assert not summary['ok'] and len(calls) == 2
    assert summary['stages'][-1]['status'] == 'failed'


@pytest.mark.parametrize('kind,code,status', [('timeout', 124, 'timed_out'), ('launch', 127, 'launch_error')])
def test_execution_errors_return_explicit_status(gate, monkeypatch, kind, code, status):
    module, _, logs = gate
    def run(command, **kwargs):
        if kind == 'timeout':
            raise subprocess.TimeoutExpired(command, 1, output=b'partial output')
        raise OSError('cannot start')
    monkeypatch.setattr(module.subprocess, 'run', run)
    assert invoke(gate) == code
    summary = json.loads(next(logs.glob('run-*/summary.json')).read_text())
    assert not summary['ok'] and summary['stages'][0]['status'] == status
    assert next(logs.glob('run-*/01-*.log')).read_text()


def test_contracts_only_really_stops_after_three_stages(gate, monkeypatch):
    module, _, logs = gate
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='', stderr=''))
    assert invoke(gate, '--contracts-only') == 0
    summary = json.loads(next(logs.glob('run-*/summary.json')).read_text())
    assert summary['ok'] and len(summary['stages']) == 3


@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf'])
def test_invalid_stage_timeout_rejected_before_any_run(gate, value):
    with pytest.raises(SystemExit) as error:
        invoke(gate, '--stage-timeout', value)
    assert error.value.code == 2
    assert not gate[2].exists()
