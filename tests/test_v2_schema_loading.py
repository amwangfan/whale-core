import json

import pytest

from whale_core.devices.schema import ContractValidator, SchemaUnsupported, _Validator


@pytest.mark.parametrize("schema", [
    {"properties": {"unused": {"oneOf": []}}},
    {"items": {"$ref": "other.json"}},
    {"additionalProperties": {"format": "email"}},
    {"properties": {"nested": {"type": "number"}}},
])
def test_unsupported_nested_rules_fail_at_load(schema):
    with pytest.raises(SchemaUnsupported):
        _Validator(schema)


def test_empty_contracts_fail_at_load(tmp_path):
    with pytest.raises(ValueError, match="schema"):
        ContractValidator(tmp_path)


def test_canonical_messages_and_examples_load():
    from whale_core.devices.schema import contracts_dir

    validator = ContractValidator()
    for path in (contracts_dir().parent / "examples").glob("*.json"):
        message = json.loads(path.read_text(encoding="utf-8"))
        assert validator.validate(message["type"], message) is None, path.name
