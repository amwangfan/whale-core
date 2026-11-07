"""确定性前后验证（借鉴 dsh-mobile-gui-agent src/agent/verifier.ts）。

验证不了必须返回 skipped，禁止伪报 passed（contracts/v2 §7.3）。
"""
from __future__ import annotations

from typing import Any, Mapping

from whale_core.devices.transports.base import (
    Observation,
    Verification,
    VerificationStatus,
)


def verify_action(
    *,
    kind: str,
    arguments: Mapping[str, Any],
    before: Observation | None,
    after: Observation | None,
) -> Verification:
    """before/after 双快照确定性验证。"""
    if after is None:
        return Verification(VerificationStatus.SKIPPED, "无 after 观察，无法验证")

    if kind == "device.wait" or kind == "observe":
        return Verification(VerificationStatus.SKIPPED, "等待/观察类动作不验证")

    if kind == "device.app.launch":
        want = str(arguments.get("package") or "")
        if not want:
            return Verification(VerificationStatus.SKIPPED, "未指定目标包名")
        if after.foreground_package == want:
            return Verification(VerificationStatus.PASSED,
                                f"前台已切换到 {want}")
        return Verification(VerificationStatus.FAILED,
                            f"前台为 {after.foreground_package or '未知'}，非 {want}")

    if kind == "device.key":
        key = str(arguments.get("key") or "")
        if key == "back":
            # back 的可验证信号：activity 变化或 UI 签名变化
            if before and (before.ui_signature != after.ui_signature
                           or before.activity != after.activity):
                return Verification(VerificationStatus.PASSED, "页面已变化")
            return Verification(VerificationStatus.FAILED, "页面无变化")
        return Verification(VerificationStatus.SKIPPED, "该按键无确定性验证")

    if kind in ("device.tap", "device.long_press", "device.swipe"):
        changed = bool(before) and (
            before.ui_signature != after.ui_signature
            or before.screenshot_signature != after.screenshot_signature
            or before.foreground_package != after.foreground_package
        )
        if changed:
            return Verification(VerificationStatus.PASSED, "界面已变化")
        if before is None:
            return Verification(VerificationStatus.SKIPPED, "无 before 观察")
        return Verification(VerificationStatus.FAILED, "界面无可见变化")

    if kind == "device.input_text":
        # 目标字段文本包含输入值（对 editable 元素）
        text = str(arguments.get("text") or "")
        if not text:
            return Verification(VerificationStatus.SKIPPED, "空输入")
        for el in after.elements:
            if el.editable and text[:20] in (el.text or ""):
                return Verification(VerificationStatus.PASSED, "文本已进入输入框")
        # 中文经 IME 的场景 after 文本未必回显，降为 skipped 而非 failed
        return Verification(VerificationStatus.SKIPPED, "输入结果无法确定性判定")

    return Verification(VerificationStatus.SKIPPED, f"{kind} 无验证规则")
