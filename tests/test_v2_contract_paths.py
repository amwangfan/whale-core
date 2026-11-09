import json
from pathlib import Path
import shutil
import pytest
from whale_core.devices import capabilities
from whale_core.devices.schema import contracts_dir

SOURCE = Path(__file__).resolve().parents[1] / 'contracts/v2/messages'

@pytest.mark.parametrize('suffix', ['', 'v2', 'v2/messages'])
def test_schema_and_capabilities_accept_the_same_override(tmp_path, monkeypatch, suffix):
    messages = tmp_path / 'v2/messages'
    shutil.copytree(SOURCE, messages)
    monkeypatch.setenv('WHALE_CONTRACTS_DIR', str(tmp_path / suffix))
    assert contracts_dir() == messages.resolve()
    assert capabilities.all_capabilities()
    assert capabilities.action_capabilities()
    assert capabilities.query_operations()

def test_switching_override_does_not_reuse_previous_directory_cache(tmp_path, monkeypatch):
    for name in ('one', 'two'):
        folder = tmp_path / name
        folder.mkdir()
        schema = {'properties': {'payload': {'properties': {'capabilities': {'items': {'enum': [name]}}}}}}
        (folder / 'device.hello.schema.json').write_text(json.dumps(schema), encoding='utf-8')
        monkeypatch.setenv('WHALE_CONTRACTS_DIR', str(folder))
        assert capabilities.all_capabilities() == (name,)

def test_bad_override_is_rejected_by_both_consumers(tmp_path, monkeypatch):
    monkeypatch.setenv('WHALE_CONTRACTS_DIR', str(tmp_path))
    for operation in (contracts_dir, capabilities.all_capabilities):
        with pytest.raises(FileNotFoundError):
            operation()
