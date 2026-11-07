"""风险动作分类与审批端口（借鉴 dsh-mobile-gui-agent src/agent/approval.ts）。

对 6 类中英正则判高风险：发送/发布/回复、删除/卸载、支付/购买/转账、
改密/账号安全、拨打电话、安装应用。低风险动作不打扰用户。
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Protocol

from whale_core.devices.transports.base import Observation

_PATTERNS = [
    # 发送/发布/回复
    re.compile(r"发送|发布|发表|回复|提交|分享|send|publish|post|reply|share|submit", re.I),
    # 删除/卸载
    re.compile(r"删除|卸载|清空|清除数据|delete|uninstall|remove|clear data", re.I),
    # 支付/购买/转账
    re.compile(r"支付|付款|购买|转账|充值|下单|pay|purchase|transfer|checkout|buy", re.I),
    # 改密/账号安全
    re.compile(r"修改密码|更改密码|账号安全|账户安全|change password|password|security", re.I),
    # 拨打电话
    re.compile(r"拨打电话|呼叫|拨打|call|dial", re.I),
    # 安装应用
    re.compile(r"安装|install", re.I),
]

# device.app.install 无条件高风险（参考实现 setup_unicode_input 同款语义）
_ALWAYS_RISKY = frozenset({"device.app.install", "device.app.uninstall"})


class ApprovalPort(Protocol):
    async def request(self, *, action_kind: str, arguments: Mapping[str, Any],
                      reason: str, observation_summary: str) -> bool:
        """返回 True = allowed-once；其他一律视为拒绝。"""
        ...


def _target_label(kind: str, arguments: Mapping[str, Any],
                  observation: Observation | None) -> str:
    """拼目标标签：元素动作取目标元素文本，否则取参数文本。"""
    parts: list[str] = []
    el_id = arguments.get("element_id")
    if el_id is not None and observation is not None:
        for el in observation.elements:
            if el.element_id == el_id:
                parts += [el.text, el.content_desc, el.resource_id, el.class_name]
                break
    for key in ("text", "package", "key"):
        v = arguments.get(key)
        if isinstance(v, str):
            parts.append(v)
    return " ".join(p for p in parts if p)[:200]


def classify_risk(kind: str, arguments: Mapping[str, Any],
                  observation: Observation | None) -> str | None:
    """返回风险原因；None = 低风险。"""
    if kind in _ALWAYS_RISKY:
        return f"{kind} 属于安装/卸载类高风险动作"
    label = _target_label(kind, arguments, observation)
    if not label:
        return None
    for pat in _PATTERNS:
        if pat.search(label):
            return f"目标「{label[:60]}」命中高风险词 {pat.pattern[:30]}"
    return None
