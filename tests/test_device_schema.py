"""The lightweight wire validator must agree with canonical JSON Schema rules."""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from whale_core.devices.schema import (
    ContractValidator, SchemaUnsupported, _Validator, contracts_dir,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / 'contracts/v1/examples').glob('*.json'))


@pytest.mark.parametrize('path', EXAMPLES, ids=lambda p: p.stem)
def test_all_canonical_examples_pass_both_validators(path):
    message = json.loads(path.read_text(encoding='utf-8'))
    schema = json.loads((contracts_dir() / (message['type'] + '.schema.json')).read_text(encoding='utf-8'))
    assert Draft202012Validator(schema).is_valid(message)
    assert ContractValidator().validate(message['type'], message) is None


@pytest.mark.parametrize('schema,value', [
    ({'type': 'object', 'maxProperties': 1}, {'a': 1, 'b': 2}),
    ({'type': 'object', 'minProperties': 1}, {}),
    ({'type': 'object', 'additionalProperties': {'type': 'integer'}}, {'a': 'bad'}),
    ({'type': 'object', 'additionalProperties': {'type': 'integer'}}, {'a': 1}),
    ({'type': 'array', 'uniqueItems': True}, [1, 1.0]),
    ({'type': 'array', 'uniqueItems': True}, [True, 1]),
    ({'type': 'array', 'uniqueItems': True}, [{'a': [1]}, {'a': [1.0]}]),
    ({'enum': [1]}, True),
    ({'const': True}, 1),
    ({'type': 'integer'}, 1.0),
    ({'type': 'integer'}, 1.5),
    ({'type': 'number', 'maximum': 2}, 2.5),
    ({'type': 'number'}, True),
    ({'type': 'null'}, None),
    ({'type': 'null'}, ''),
    ({'properties': {'a': False}}, {'a': 1}),
])
def test_supported_rules_match_reference(schema, value):
    assert (_Validator(schema).validate(value) is None) == Draft202012Validator(schema).is_valid(value)


@pytest.mark.parametrize('schema', [
    {'properties': {'unused': {'anyOf': [{'type': 'string'}]}}},
    {'type': 'array', 'items': {'format': 'email'}},
    {'additionalProperties': {'not': {'type': 'null'}}},
    {'properties': {'unused': {'type': ['string', 'null']}}},
])
def test_unsupported_nested_rules_rejected_at_load_even_without_value(schema):
    with pytest.raises(SchemaUnsupported):
        _Validator(schema)


def test_action_parameter_limit_is_actually_enforced():
    message = json.loads((ROOT / 'contracts/v1/examples/action.request.json').read_text(encoding='utf-8'))
    message['payload']['args'] = {str(i): i for i in range(33)}
    assert 'maxProperties' in ContractValidator().validate('action.request', message)


def test_missing_schema_directory_cannot_silently_accept_messages(tmp_path):
    with pytest.raises(FileNotFoundError):
        ContractValidator(tmp_path)
