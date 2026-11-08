"""Normalize Mobile Agent2 and transport observations at one boundary."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import re

from whale_core.devices.transports.base import Observation, ObserveRequest, UiElement, new_id, now_ms


def bounds(value) -> tuple[int, int, int, int]:
    if isinstance(value, str):
        match = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", value)
        if match is None:
            raise ValueError("观察元素 bounds 格式无效")
        value = [int(n) for n in match.groups()]
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(type(n) is not int or abs(n) > 2**31 - 1 for n in value)):
        raise ValueError("观察元素 bounds 坐标无效")
    left, top, right, bottom = value
    if right < left or bottom < top:
        raise ValueError("观察元素 bounds 矩形反向")
    return left, top, right, bottom


def element(data: dict) -> UiElement:
    element_id = data.get("element_id")
    if type(element_id) is not int or element_id < 0:
        raise ValueError("观察元素 ID 无效")
    def text(key):
        return data[key] if isinstance(data.get(key), str) else ""
    return UiElement(element_id=element_id, bounds=bounds(data.get("bounds")),
        class_name=text("class_name"), text=text("text"), resource_id=text("resource_id"),
        content_desc=text("content_desc"), fingerprint=text("fingerprint"),
        **{key: data.get(key) is True for key in (
            "clickable", "long_clickable", "scrollable", "editable")},
        enabled=data.get("enabled", True) is True)


def validate_request(request: ObserveRequest) -> None:
    if (type(request.screenshot) is not bool or type(request.ui_tree) is not bool
            or type(request.max_elements) is not int or not 1 <= request.max_elements <= 2**31 - 1):
        raise ValueError("观察开关须为布尔值，max_elements 须为正整数")


def observation(device_id: str, payload: dict, request: ObserveRequest) -> Observation:
    validate_request(request)
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise ValueError("观察负载必须包含 elements 数组")
    parsed = []
    damaged = False
    ids = set()
    for raw in payload["elements"]:
        try:
            if not isinstance(raw, dict):
                raise ValueError("元素不是对象")
            item = element(raw)
        except ValueError:
            damaged = True
            continue
        if item.element_id in ids:
            raise ValueError("观察元素 ID 重复")
        ids.add(item.element_id)
        parsed.append(item)
    if payload["elements"] and not parsed:
        raise ValueError("观察负载没有可用的元素")
    truncated = payload.get("truncated") is True or damaged or len(parsed) > request.max_elements
    parsed = parsed[:request.max_elements] if request.ui_tree else []
    screen = payload.get("screen") if isinstance(payload.get("screen"), dict) else {}
    def integer(value, default=0):
        return value if type(value) is int and value >= 0 else default
    def text(key):
        return payload[key] if isinstance(payload.get(key), str) and payload[key] else None
    normalized = [{k: v for k, v in asdict(item).items() if k != "element_id"} for item in parsed]
    signature = hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return Observation(
        observation_id=text("observation_id") or new_id("obs"), device_id=device_id,
        taken_at=integer(payload.get("taken_at", payload.get("captured_at")), now_ms()),
        foreground_package=text("foreground_package") or "", activity=text("activity"),
        elements=tuple(parsed), ui_signature=signature,
        screenshot_ref=text("screenshot_ref") if request.screenshot else None,
        screenshot_signature=text("screenshot_signature") if request.screenshot else None,
        screen_width=integer(payload.get("screen_width", screen.get("w"))),
        screen_height=integer(payload.get("screen_height", screen.get("h"))),
        keyboard_shown=payload.get("keyboard_shown") is True,
        locked=payload.get("locked") is True, truncated=truncated)
