"""端云协议 V1 帧校验器（纯 stdlib，不依赖 jsonschema 库）。

协议事实源是 whale-mobile ``contracts/v1/``；本仓在 ``contracts/v1/`` 持有
版本化副本（见该目录 README）。V1 全部 16 个 schema 只用到一个很小的
JSON Schema 子集（draft 2020-12）：type / const / enum / pattern /
minLength / maxLength / minimum / maximum / minItems / maxItems /
uniqueItems / items / properties / required / additionalProperties:false。
本模块实现且仅实现该子集；遇到子集之外的关键字会抛 ``SchemaUnsupported``
（fail closed——宁可拒绝校验也不静默跳过约束）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

SUPPORTED_MAJOR = 1
PROTOCOL_VERSION = "1.0"

_SCHEMA_DIR_ENV = "WHALE_CONTRACTS_DIR"

_KEYWORDS = {
    "type", "const", "enum", "pattern", "minLength", "maxLength",
    "minimum", "maximum", "minItems", "maxItems", "uniqueItems", "items",
    "properties", "required", "additionalProperties",
    # 仅元信息，无校验语义
    "$schema", "$id", "title", "description",
}


class SchemaUnsupported(ValueError):
    """schema 用到了本校验器不支持的关键字（fail closed）。"""


def contracts_dir() -> Path:
    """定位 contracts/v1/messages：环境变量优先，否则按仓库布局向上找。"""
    env = os.environ.get(_SCHEMA_DIR_ENV)
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "contracts" / "v1" / "messages"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "contracts/v1/messages 不存在；可用 WHALE_CONTRACTS_DIR 显式指定")


def parse_major(version: Any) -> int | None:
    """从 ``major.minor`` 提取 major；无法解析返回 None。"""
    if isinstance(version, str):
        head, _, _ = version.partition(".")
        if head.isdigit():
            return int(head)
    return None


class _Validator:
    __slots__ = ("schema",)

    def __init__(self, schema: dict):
        unknown = set(schema) - _KEYWORDS
        if unknown:
            raise SchemaUnsupported(f"不支持的 schema 关键字: {sorted(unknown)}")
        self.schema = schema

    def validate(self, instance: Any) -> str | None:
        """返回第一条违例描述；通过返回 None。"""
        return _check(self.schema, instance, "$")


def _check(schema: dict, value: Any, path: str) -> str | None:
    stype = schema.get("type")
    if stype == "object":
        if not isinstance(value, dict):
            return f"{path}: 期望 object"
    elif stype == "array":
        if not isinstance(value, list):
            return f"{path}: 期望 array"
    elif stype == "string":
        if not isinstance(value, str):
            return f"{path}: 期望 string"
    elif stype == "integer":
        # bool 是 int 子类，JSON Schema 里 integer 不含 true/false
        if not isinstance(value, int) or isinstance(value, bool):
            return f"{path}: 期望 integer"
    elif stype == "boolean":
        if not isinstance(value, bool):
            return f"{path}: 期望 boolean"

    if "const" in schema and value != schema["const"]:
        return f"{path}: 必须等于 {schema['const']!r}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path}: 不在枚举 {schema['enum']!r} 内"

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            return f"{path}: 长度 < minLength {schema['minLength']}"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{path}: 长度 > maxLength {schema['maxLength']}"
        if "pattern" in schema and not re.search(schema["pattern"], value):
            return f"{path}: 不匹配 pattern {schema['pattern']!r}"
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path}: 小于 minimum {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path}: 大于 maximum {schema['maximum']}"
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            return f"{path}: 元素数 < minItems {schema['minItems']}"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"{path}: 元素数 > maxItems {schema['maxItems']}"
        if schema.get("uniqueItems"):
            seen = [json.dumps(item, sort_keys=True, ensure_ascii=False)
                    for item in value]
            if len(set(seen)) != len(seen):
                return f"{path}: 元素不唯一"
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(value):
                err = _check(item_schema, item, f"{path}[{i}]")
                if err:
                    return err
    if isinstance(value, dict):
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                return f"{path}: 缺少必需字段 {key!r}"
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                return f"{path}: 存在未声明字段 {sorted(extra)}"
        for key, subschema in properties.items():
            if isinstance(subschema, dict) and key in value:
                err = _check(subschema, value[key], f"{path}.{key}")
                if err:
                    return err
    return None


class ContractValidator:
    """加载 contracts/v1/messages 下逐消息 schema，按 type 索引。"""

    def __init__(self, messages_dir: Path | None = None) -> None:
        directory = messages_dir if messages_dir is not None else contracts_dir()
        self.validators: dict[str, _Validator] = {}
        self.envelope: _Validator | None = None
        for path in sorted(directory.glob("*.schema.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))
            validator = _Validator(schema)
            msg_type = schema.get("properties", {}).get("type", {}).get("const")
            if msg_type:
                self.validators[msg_type] = validator
            elif schema.get("title") == "envelope":
                self.envelope = validator

    def validate(self, msg_type: str, message: Any) -> str | None:
        """校验完整信封帧；未知 type 返回 None（由调用方决定忽略/拒绝）。"""
        validator = self.validators.get(msg_type)
        if validator is None:
            return None
        return validator.validate(message)
