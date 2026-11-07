"""Device Transport 抽象基类（contracts/v2 第一轮闭环）。

三种 transport（embedded_wss / adb / ssh）统一实现本接口，由 Device Gateway
按能力和任务类型选择通道。Transport 只提供执行能力，不持有 Agent 人格和
长期记忆。

能力命名、动作语义、回执状态机与 contracts/v2 §5/§7 完全一致，与走不走
WSS 无关。

设计约束：
- 全部方法 async；实现内部可用 asyncio.create_subprocess_exec / 线程池桥接
  同步库，但禁止在事件循环里做无界阻塞。
- 每个动作有 timeout_ms；超时返回 status="timeout"，**绝不**抛超时异常给
  调用方（timeout ≠ 未执行，调用方需要这个终态做决策）。
- 大输出（logcat、shell stdout、UI 树 XML、截图）不落 receipt 本体，写入
  OutputStore 后回 ``output_ref``（``agent:out:<action_id>`` 等），由
  ``read_output`` 分页读取。
"""

from __future__ import annotations

import dataclasses
import enum
import secrets
import time

from typing import Protocol, Sequence, runtime_checkable


# ---------------------------------------------------------------------------
# ID 生成（contracts/v2 §3：前缀 + 26 字符 ULID）
# ---------------------------------------------------------------------------

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """自包含 ULID（48bit ms 时间 + 80bit 随机，Crockford Base32 26 字符）。

    不引入第三方依赖；单调性不要求（同毫秒内随机分量保证唯一性概率足够）。
    """
    value = (int(time.time() * 1000) & ((1 << 48) - 1)) << 80
    value |= secrets.randbits(80)
    chars = []
    for shift in range(125, -1, -5):  # 26 x 5bit = 130bit，最高 2bit 恒 0
        chars.append(_CROCKFORD[(value >> shift) & 0x1F])
    return "".join(chars)


def new_id(prefix: str) -> str:
    """生成 ``<prefix>_<ULID>`` 形式的协议 ID。"""
    return f"{prefix}_{new_ulid()}"


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# 动作与回执（contracts/v2 §7）
# ---------------------------------------------------------------------------

class ReceiptStatus(str, enum.Enum):
    """action.receipt.status 枚举（contracts/v2 §7.2）。"""

    RECEIVED = "received"      # 非终态：在飞
    SUCCEEDED = "succeeded"    # 终态
    FAILED = "failed"          # 终态
    TIMEOUT = "timeout"        # 终态；副作用可能已发生
    CANCELLED = "cancelled"    # 终态
    UNKNOWN = "unknown"        # 终态；不得当失败重放

    @property
    def terminal(self) -> bool:
        return self is not ReceiptStatus.RECEIVED


class VerificationStatus(str, enum.Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclasses.dataclass(frozen=True)
class Verification:
    status: VerificationStatus
    summary: str = ""


@dataclasses.dataclass(frozen=True)
class ActionRequest:
    """一次动作请求（对应 contracts/v2 action.request payload 的运行时表示）。"""

    action_id: str
    capability: str            # §5.1 能力名，如 device.tap / device.shell
    arguments: dict
    idempotency_key: str
    timeout_ms: int = 10_000
    task_id: str | None = None
    run_id: str | None = None
    observation_id: str | None = None
    approval_ref: str | None = None
    correlation_id: str | None = None


@dataclasses.dataclass(frozen=True)
class ActionReceipt:
    """动作回执（对应 contracts/v2 action.receipt payload 的运行时表示）。"""

    action_id: str
    status: ReceiptStatus
    task_id: str | None = None
    summary: str = ""
    output_ref: str | None = None
    verification: Verification | None = None
    error_code: str | None = None
    provider_ref: str | None = None
    started_at: int | None = None
    completed_at: int | None = None


# ---------------------------------------------------------------------------
# 观察（contracts/v2 §8）
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class UiElement:
    """语义 UI 元素。element_id 为观察内局部整数；fingerprint 跨观察稳定。"""

    element_id: int
    class_name: str
    text: str
    resource_id: str
    content_desc: str
    bounds: tuple[int, int, int, int]  # left, top, right, bottom
    clickable: bool = False
    long_clickable: bool = False
    scrollable: bool = False
    editable: bool = False
    enabled: bool = True
    fingerprint: str = ""              # sha256(...)[:16]，见 contracts/v2 §8

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return (left + right) // 2, (top + bottom) // 2


@dataclasses.dataclass(frozen=True)
class ObserveRequest:
    """观察请求。screenshot=False 时实现侧可省略截图（省带宽）。"""

    screenshot: bool = True
    ui_tree: bool = True
    max_elements: int = 400


@dataclasses.dataclass(frozen=True)
class Observation:
    observation_id: str
    device_id: str
    taken_at: int                       # ms
    foreground_package: str
    activity: str | None
    elements: tuple[UiElement, ...]
    ui_signature: str                   # sha256(JSON(去 id 元素表))
    screenshot_ref: str | None          # agent:shot:* / att:* 等不透明引用
    screenshot_signature: str | None    # 32x32 灰度比较签名（屏稳/变化检测）
    screen_width: int
    screen_height: int
    keyboard_shown: bool = False
    locked: bool = False
    truncated: bool = False


# ---------------------------------------------------------------------------
# 能力与健康
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class TransportHealth:
    connected: bool
    detail: str = ""
    checked_at: int = 0


# ---------------------------------------------------------------------------
# Transport 接口
# ---------------------------------------------------------------------------

@runtime_checkable
class DeviceTransport(Protocol):
    """统一设备传输接口（plan §6.5）。实现：embedded_wss / adb / ssh。"""

    kind: str

    async def connect(self, device_id: str) -> None: ...

    async def disconnect(self, device_id: str) -> None: ...

    async def capabilities(self, device_id: str) -> Sequence[str]:
        """该通道实际可提供的能力子集（contracts/v2 §5.1 枚举）。"""
        ...

    async def observe(self, device_id: str, request: ObserveRequest) -> Observation: ...

    async def execute(self, device_id: str, action: ActionRequest) -> ActionReceipt:
        """执行动作并回执终态；超时返回 TIMEOUT 而非抛异常。"""
        ...

    async def read_output(self, device_id: str, output_ref: str, *,
                          offset: int = 0, max_bytes: int = 65536) -> tuple[bytes, bool]:
        """分页读取 output_ref 指向的大输出；返回 (chunk, truncated)。"""
        ...

    async def health(self, device_id: str) -> TransportHealth: ...
