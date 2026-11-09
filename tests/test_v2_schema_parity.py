import copy
import json

import pytest
from jsonschema import Draft202012Validator

from whale_core.devices.schema import ContractValidator, SchemaUnsupported, _Validator, contracts_dir


@pytest.mark.parametrize("schema,values", [
    ({"const": 1}, [1, 1.0, True, False, "1"]),
    ({"enum": [True, 2]}, [1, True, 2, 2.0, False, "2"]),
    ({"type": "array", "uniqueItems": True}, [[1, 1.0], [True, 1],
        [{"n": 1}, {"n": 1.0}], [[1], [1.0]], [0, -0.0], [False, 0]]),
    ({"type": "integer", "minimum": 0, "maximum": 4}, [0, 1, 1.0, 1.1, True, -1, 4.0, 5]),
    ({"minimum": 1, "maximum": 4}, [0.5, 1.5, 4.0, 4.5, "value"]),
    ({"enum": []}, [None, 0, "", [], {}]),
])
def test_json_equality_and_number_rules_match_reference(schema, values):
    ours, reference = _Validator(schema), Draft202012Validator(schema)
    for value in values:
        assert (ours.validate(value) is None) == reference.is_valid(value), (schema, value)


@pytest.mark.parametrize("schema", [
    {"properties": []}, {"properties": {"nested": False}}, {"required": "field"},
    {"required": ["field", "field"]}, {"minItems": True}, {"minLength": -1},
    {"uniqueItems": 1}, {"enum": "value"}, {"items": False}, {"additionalProperties": []},
    {"minimum": float("inf")}, {"pattern": "["}, {"type": None},
])
def test_unsupported_or_malformed_rules_fail_at_load(schema):
    with pytest.raises(SchemaUnsupported):
        _Validator(schema)


def test_canonical_frames_and_mutated_sequence_match_reference():
    validator = ContractValidator()
    for path in (contracts_dir().parent / "examples").glob("*.json"):
        frame = json.loads(path.read_text(encoding="utf-8"))
        schema = json.loads((contracts_dir() / f'{frame["type"]}.schema.json').read_text(encoding="utf-8"))
        reference = Draft202012Validator(schema)
        for value in (True, -1, 0, 1.0, 1.5, 9007199254740992):
            mutated = copy.deepcopy(frame)
            mutated["seq"] = value
            assert (validator.validate(frame["type"], mutated) is None) == reference.is_valid(mutated), path.name
