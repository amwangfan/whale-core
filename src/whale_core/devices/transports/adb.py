"""ADB Transport：Device Gateway 的 ADB 通道实现（contracts/v2 §5/§7/§8）。

主要参考实现为 dsh-mobile-gui-agent（TypeScript），逐点对应关系：
- ``SubprocessAdbRunner``    ← src/adb/adb-client.ts（直接 argv 不过宿主 shell、
  有界 stdout/stderr、单命令 10s 超时、超限即杀进程）
- ``AdbTransport._screencap`` ← src/adb/adb-device.ts screenshot()
  （``exec-out screencap -p`` 二进制直读，16MB 上限）
- ``AdbTransport._dump_hierarchy_once`` ← src/adb/adb-device.ts dumpHierarchyOnce()
  （首选 ``uiautomator dump /dev/tty``，失败回退 ``写 /data/local/tmp 文件 +
  exec-out cat + rm -f``，共 3 次重试、间隔 300ms、4MB 上限）
- ``parse_ui_hierarchy`` / ``extract_hierarchy_xml`` ← src/adb/hierarchy.ts
  （过滤 invisible/零面积/非 meaningful 节点、maxElements 400、序列化 64KB 上限、
  element_id 观察内从 1 递增、fingerprint=sha256(className|text|resourceId|
  contentDescription|bounds|clickable|scrollable|editable) 前 16 hex、
  ui_signature=sha256(JSON(去 element_id 元素表))）
- ``parse_foreground_state`` / ``infer_foreground_package`` /
  ``parse_keyboard_visible`` / ``parse_locked`` ← src/adb/foreground.ts
  （dumpsys 正则 mCurrentFocus|mFocusedApp|mResumedActivity|topResumedActivity +
  resource-id 包名前缀统计兜底）
- ``escape_input_text`` / ``_cmd_input_text`` ← src/adb/adb-device.ts
  escapeInputText()（%→%25、空格→%s、远端 shell 元字符反斜杠转义）
- ``_screenshot_signature`` ← src/adb/screenshot.ts（32x32 灰度比较签名；
  Python 侧用 Pillow 实现，Pillow 为可选依赖，见 pyproject ``adb`` extra）

与 TS 参考的有意差异（PROTOCOL 未覆盖处的决策，见交付报告）：
- 坐标界内校验用 ``wm size``（轻量）而非 TS 的每次点按前重新截图。
- 非 ASCII 文本输入：TS 走 ADB Keyboard 广播辅助 IME；本实现暂拒绝
  （FAILED/invalid_message），不静默退化为乱码。
- ``device.shell`` argv 形式逐元素 ``shlex.quote`` 后走远端 shell
  （实测 adb 只是用空格拼接 argv，远端 sh 会解释元字符，见脚本头部实验）；
  root 执行统一 ``su -c <quoted>``。
"""

from __future__ import annotations

import os
import asyncio
import base64
import dataclasses
import hashlib
import io
import json
import re
import shlex
import time
import uuid

from typing import Awaitable, Callable, Mapping, Protocol, Sequence

from .base import (
    ActionReceipt,
    ActionRequest,
    DeviceTransport,
    Observation,
    ObserveRequest,
    ReceiptStatus,
    TransportHealth,
    UiElement,
    Verification,
    VerificationStatus,
    new_id,
    now_ms,
)

# ---------------------------------------------------------------------------
# 常量（默认值对齐 dsh-mobile-gui-agent src/adb/config.ts）
# ---------------------------------------------------------------------------

DEFAULT_ADB_PATH = os.environ.get("WHALE_ADB", "adb")
DEFAULT_COMMAND_TIMEOUT_MS = 10_000          # config.ts commandTimeoutMs
SCREENSHOT_MAX_BYTES = 16 * 1024 * 1024      # screenshotMaxBytes
HIERARCHY_MAX_BYTES = 4 * 1024 * 1024        # hierarchyMaxBytes
DIAGNOSTIC_MAX_BYTES = 64 * 1024             # diagnosticMaxBytes
MAX_ELEMENTS = 400                           # hierarchyMaxElements
SERIALIZED_MAX_BYTES = 64 * 1024             # hierarchyMaxSerializedBytes
HIERARCHY_ATTEMPTS = 3                       # hierarchyAttempts
# uiautomator dump 在慢设备（nova GSI 实测 >10s）上需要比常规命令更长的超时
HIERARCHY_TIMEOUT_MS = 30_000
# 慢设备 screencap 同样需要一个数量级的超时（nova 实测 18.6s）
SCREENSHOT_TIMEOUT_MS = 30_000
HIERARCHY_RETRY_DELAY_S = 0.3                # hierarchyRetryDelayMs=300
LOGCAT_MAX_BYTES = 1 * 1024 * 1024           # 决策：TS 无 logcat，自定上限
SHELL_MAX_BYTES = 1 * 1024 * 1024            # device.shell 输出硬上限
OUTPUT_TTL_S = 600                           # OutputStore 默认 TTL（10min）
OBSERVATION_CACHE_SIZE = 32                  # 每设备缓存的最近观察数（元素重校验用）
SUMMARY_MAX_CHARS = 2000                     # contracts/v2 §7.3 summary ≤2000

#: 本 transport 实际支持的能力（contracts/v2 §5.1 子集）。
#: device.output.read 属 sensor.query 面（§5.3），不作为动作能力。
ADB_CAPABILITIES: tuple[str, ...] = (
    "device.observe",
    "device.tap",
    "device.long_press",
    "device.swipe",
    "device.input_text",
    "device.key",
    "device.wait",
    "device.app.launch",
    "device.app.list",
    "device.logcat",
    "device.shell",
)

#: device.key 的 keyevent 白名单（adb-device.ts KEY_CODES + back/home）。
KEY_EVENTS: dict[str, str] = {
    "enter": "KEYCODE_ENTER",
    "delete": "KEYCODE_DEL",
    "tab": "KEYCODE_TAB",
    "escape": "KEYCODE_ESCAPE",
    "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
    "power": "KEYCODE_POWER",
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
}

# 包名/组件名校验（adb-device.ts openApp 同款正则，防注入）。
_PACKAGE_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._]+/[A-Za-z0-9.$_]+$")
# logcat 过滤器（tag 或 tag:prio）与 since 时间格式。
_LOGCAT_FILTER_RE = re.compile(r"^[A-Za-z0-9_.\-*]+(?::[VDIWEFAS])?$")
_LOGCAT_SINCE_RE = re.compile(r"^\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$")


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------

class AdbError(Exception):
    """ADB 层错误。``code`` 取 contracts/v2 §14 错误码枚举。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AdbTimeoutError(AdbError):
    """单条 adb 命令超时（映射回执 status=timeout）。"""

    def __init__(self, message: str):
        super().__init__("timeout", message)


# ---------------------------------------------------------------------------
# Runner：直接 argv 子进程（不过宿主 shell），有界输出 + 超时杀进程
# （对应 adb-client.ts 的 AdbClient.text/binary + readBounded）
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class AdbResult:
    """一次 adb 进程执行的有界结果。"""

    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False   # 超 timeout_ms 被杀
    lossy: bool = False       # stdout 超 max_bytes 被杀（输出截断）


class AdbRunner(Protocol):
    """adb 进程执行抽象。测试注入 FakeAdbRunner，生产用 SubprocessAdbRunner。"""

    async def run(self, argv: Sequence[str], *, max_bytes: int,
                  timeout_ms: int) -> AdbResult: ...


class SubprocessAdbRunner:
    """真实 adb 子进程执行器（asyncio.create_subprocess_exec，无宿主 shell）。"""

    STDERR_MAX_BYTES = DIAGNOSTIC_MAX_BYTES

    async def run(self, argv: Sequence[str], *, max_bytes: int,
                  timeout_ms: int) -> AdbResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AdbError("unavailable", f"adb 可执行文件不存在: {argv[0]}") from exc
        except OSError as exc:
            raise AdbError("unavailable", f"adb 启动失败: {exc}") from exc

        out = bytearray()
        err = bytearray()
        lossy = False

        async def _read_capped(stream: asyncio.StreamReader, buf: bytearray,
                               limit: int) -> bool:
            """读到 EOF 或超 limit 即止（超限返回 True，由外层杀进程）。"""
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    return False
                if len(buf) + len(chunk) > limit:
                    keep = limit - len(buf)
                    if keep > 0:
                        buf.extend(chunk[:keep])
                    return True
                buf.extend(chunk)

        async def _collect() -> None:
            nonlocal lossy
            over_out, _ = await asyncio.gather(
                _read_capped(proc.stdout, out, max_bytes),
                _read_capped(proc.stderr, err, self.STDERR_MAX_BYTES),
            )
            lossy = over_out
            if lossy:
                # 输出无界：杀进程，防止管道写满悬挂（adb-client.ts overflow 同款处理）
                proc.kill()
            await proc.wait()

        timed_out = False
        try:
            await asyncio.wait_for(_collect(), timeout=timeout_ms / 1000)
        except (asyncio.TimeoutError, TimeoutError):
            timed_out = True
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, TimeoutError):  # pragma: no cover - 防御
                pass
        except asyncio.CancelledError:
            # 外层动作超时取消时，绝不留孤儿 adb 进程
            proc.kill()
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001 - 取消路径尽力收尾
                pass
            raise
        return AdbResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=bytes(out),
            stderr=bytes(err),
            timed_out=timed_out,
            lossy=lossy,
        )


# ---------------------------------------------------------------------------
# OutputStore：内存 dict + TTL，output_ref = agent:out:<action_id> 等
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class OutputEntry:
    data: bytes
    content_type: str
    created_at: float  # monotonic 秒


class OutputStore:
    """进程内大输出暂存（截图/日志/UI 树 JSON），TTL 惰性清理。"""

    def __init__(self, *, ttl_s: float = OUTPUT_TTL_S, max_entries: int = 512,
                 clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl_s
        self._max_entries = max_entries
        self._clock = clock
        self._entries: dict[str, OutputEntry] = {}

    def put(self, ref: str, data: bytes | str,
            content_type: str = "application/octet-stream") -> str:
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._sweep()
        if len(self._entries) >= self._max_entries:
            # 满了先淘汰最旧（插入序即年龄序）
            self._entries.pop(next(iter(self._entries)))
        self._entries[ref] = OutputEntry(data, content_type, self._clock())
        return ref

    def read(self, ref: str, *, offset: int = 0,
             max_bytes: int = 65536) -> tuple[bytes, bool]:
        """分页读取；返回 (chunk, truncated)，truncated=其后还有字节。"""
        self._sweep()
        entry = self._entries.get(ref)
        if entry is None or offset < 0:
            return b"", False
        chunk = entry.data[offset:offset + max_bytes]
        return chunk, offset + len(chunk) < len(entry.data)

    def size_of(self, ref: str) -> int | None:
        self._sweep()
        entry = self._entries.get(ref)
        return None if entry is None else len(entry.data)

    def _sweep(self) -> None:
        now = self._clock()
        expired = [k for k, v in self._entries.items()
                   if now - v.created_at > self._ttl]
        for k in expired:
            del self._entries[k]


# ---------------------------------------------------------------------------
# uiautomator XML 解析（hierarchy.ts 的 Python 移植，解析器换 stdlib ET）
# ---------------------------------------------------------------------------

_BOUNDS_RE = re.compile(r"^\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]$")


def extract_hierarchy_xml(output: str) -> str:
    """从 uiautomator 嘈杂 stdout 中截取 XML 文档（hierarchy.ts 同名函数）。"""
    decl = output.find("<?xml")
    hier = output.find("<hierarchy")
    start = decl if decl >= 0 else hier
    if start < 0:
        raise AdbError("internal", "uiautomator 输出中未找到 XML")
    end = output.rfind("</hierarchy>")
    if end < start:
        raise AdbError("internal", "uiautomator hierarchy 不完整")
    return output[start:end + len("</hierarchy>")]


def _bool_attr(value: str | None) -> bool:
    return value == "true"


def _non_empty(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip()


def _parse_bounds(value: str, width: int, height: int) -> tuple[int, int, int, int] | None:
    """解析并裁剪 ``[l,t][r,b]``；零面积/出界即丢弃（hierarchy.ts parseBounds）。"""
    m = _BOUNDS_RE.match(value)
    if m is None:
        return None
    raw_left, raw_top, raw_right, raw_bottom = (int(g) for g in m.groups())
    left = max(0, min(width, raw_left))
    top = max(0, min(height, raw_top))
    right = max(0, min(width, raw_right))
    bottom = max(0, min(height, raw_bottom))
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def element_fingerprint(class_name: str, text: str, resource_id: str,
                        content_desc: str, bounds: tuple[int, int, int, int],
                        clickable: bool, scrollable: bool, editable: bool) -> str:
    """contracts/v2 §8 fingerprint：sha256 前 16 hex（与 hierarchy.ts 逐字节一致）。"""
    semantic = "|".join([
        class_name, text, resource_id, content_desc,
        ",".join(str(v) for v in bounds),
        "true" if clickable else "false",
        "true" if scrollable else "false",
        "true" if editable else "false",
    ])
    return hashlib.sha256(semantic.encode("utf-8")).hexdigest()[:16]


def _element_semantic(e: UiElement) -> list:
    """去 element_id 的元素语义表项（ui_signature 输入；TS 为对象去 id 后的 JSON）。"""
    return [
        e.class_name, e.text, e.resource_id, e.content_desc,
        list(e.bounds), e.clickable, e.long_clickable, e.scrollable,
        e.editable, e.enabled, e.fingerprint,
    ]


def _serialize_element(e: UiElement) -> str:
    return json.dumps(_element_semantic(e), ensure_ascii=False,
                      separators=(",", ":"))


def parse_ui_hierarchy(xml_text: str, *, width: int, height: int,
                       max_elements: int = MAX_ELEMENTS,
                       max_serialized_bytes: int = SERIALIZED_MAX_BYTES
                       ) -> tuple[tuple[UiElement, ...], str, bool]:
    """解析+裁剪一棵 uiautomator 树。

    返回 (elements, ui_signature, truncated)。过滤规则与 hierarchy.ts 相同：
    invisible-to-user / 零面积 / 非 meaningful 节点不保留（但其子节点仍会被
    访问——父节点被过滤不传染子树，只有触及条数/字节上限才停止整趟遍历）。
    """
    import xml.etree.ElementTree as ET  # stdlib，局部导入保持模块头轻量

    try:
        document = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise AdbError("internal", f"uiautomator 返回了畸形 XML: {exc}") from exc
    if document.tag != "hierarchy":
        raise AdbError("internal", "uiautomator 输出缺少 hierarchy 根")

    retained: list[UiElement] = []
    used_bytes = 0
    truncated = False

    def visit(node: "ET.Element") -> None:
        nonlocal used_bytes, truncated
        if truncated:
            return
        element = _node_to_element(node, len(retained) + 1, width, height)
        if element is not None:
            line_bytes = len(_serialize_element(element).encode("utf-8")) + 1
            if (len(retained) >= max_elements
                    or used_bytes + line_bytes > max_serialized_bytes):
                truncated = True
                return
            retained.append(element)
            used_bytes += line_bytes
        for child in node:
            if child.tag == "node":
                visit(child)

    for child in document:
        if child.tag == "node":
            visit(child)

    semantic = [_element_semantic(e) for e in retained]
    signature = hashlib.sha256(
        json.dumps(semantic, ensure_ascii=False, separators=(",", ":"))
        .encode("utf-8")).hexdigest()
    return tuple(retained), signature, truncated


def _node_to_element(node: "object", element_id: int, width: int,
                     height: int) -> UiElement | None:
    """单个原始 node → 保留的语义元素（hierarchy.ts toElement）。"""
    get = node.get  # type: ignore[attr-defined]
    if get("visible-to-user") == "false":
        return None
    bounds = _parse_bounds(get("bounds") or "", width, height)
    if bounds is None:
        return None
    class_name = get("class") or ""
    text = _non_empty(get("text"))
    resource_id = _non_empty(get("resource-id"))
    content_desc = _non_empty(get("content-desc"))
    clickable = _bool_attr(get("clickable"))
    long_clickable = _bool_attr(get("long-clickable"))
    scrollable = _bool_attr(get("scrollable"))
    editable = class_name.endswith("EditText") or _bool_attr(get("editable"))
    focusable = _bool_attr(get("focusable"))
    focused = _bool_attr(get("focused"))
    enabled = get("enabled") != "false"
    selected = _bool_attr(get("selected"))
    checkable = _bool_attr(get("checkable")) or bool(
        re.search(r"(?:Switch|CheckBox|RadioButton)$", class_name))
    meaningful = (clickable or long_clickable or scrollable or editable
                  or focusable or focused or selected or checkable
                  or bool(text) or bool(resource_id) or bool(content_desc))
    if not meaningful:
        return None
    fingerprint = element_fingerprint(
        class_name, text, resource_id, content_desc, bounds,
        clickable, scrollable, editable)
    return UiElement(
        element_id=element_id,
        class_name=class_name,
        text=text,
        resource_id=resource_id,
        content_desc=content_desc,
        bounds=bounds,
        clickable=clickable,
        long_clickable=long_clickable,
        scrollable=scrollable,
        editable=editable,
        enabled=enabled,
        fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# dumpsys 解析（foreground.ts 的 Python 移植）
# ---------------------------------------------------------------------------

_FOREGROUND_RE = re.compile(
    r"(?:mCurrentFocus|mFocusedApp|mFocusedWindow|mResumedActivity|topResumedActivity)"
    r"[^\n]*?\s([A-Za-z0-9._]+)/([A-Za-z0-9.$_]+)")
_FOREGROUND_FALLBACK_RE = re.compile(
    r"ActivityRecord\{[^\n]*?\s([A-Za-z0-9._]+)/([A-Za-z0-9.$_]+)")
_RES_ID_PKG_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+):id/")


def parse_foreground_state(output: str) -> tuple[str, str | None]:
    """从 dumpsys window/activity 文本解析前台 (package, activity)。"""
    m = _FOREGROUND_RE.search(output) or _FOREGROUND_FALLBACK_RE.search(output)
    if m is None:
        return "", None
    package, activity = m.group(1), m.group(2)
    return package, f"{package}/{activity}"


def infer_foreground_package(elements: Sequence[UiElement]) -> str:
    """resource-id 包名前缀统计兜底；证据不足或并列时返回 ""。"""
    counts: dict[str, int] = {}
    for e in elements:
        m = _RES_ID_PKG_RE.match(e.resource_id)
        if m:
            pkg = m.group(1)
            counts[pkg] = counts.get(pkg, 0) + 1
    if not counts:
        return ""
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return ""
    return ranked[0][0]


def parse_keyboard_visible(output: str) -> bool:
    if re.search(r"mInputShown=true|mIsInputViewShown=true", output):
        return True
    if re.search(r"mInputShown=false|mIsInputViewShown=false", output):
        return False
    m = re.search(r"mImeWindowVis=0x([0-9a-fA-F]+)", output)
    if m:
        return (int(m.group(1), 16) & 0x2) != 0
    return False


def parse_wakefulness(output: str) -> str:
    """dumpsys power 的 mWakefulness（Awake/Dozing/Asleep/...）。"""
    m = re.search(r"mWakefulness=(\w+)", output)
    return m.group(1) if m else ""


def parse_locked(output: str) -> bool:
    """锁屏判定：以 keyguard「正在显示」为准。

    真机实测（nova GSI）：``isStatusBarKeyguard=true`` 在解锁后依然成立
    （它只是状态栏窗口属性），不能作为锁屏信号。主信号是
    KeyguardServiceDelegate 的 ``showing=`` / ``mShowingLockscreen=``；
    都没有时才回退 isStatusBarKeyguard（保守视为未锁，让 UI 树说话）。
    """
    m = re.search(r"(?:mShowingLockscreen|mKeyguardShowing|showing)"
                  r"=(true|false)", output)
    if m:
        return m.group(1) == "true"
    # 无 showing 信号的固件：回退旧启发式，但 isStatusBarKeyguard 单独不足以判锁
    if re.search(r"mShowingLockscreen=true", output):
        return True
    return False


_WM_SIZE_RE = re.compile(r"(\d+)x(\d+)")


def parse_wm_size(output: str) -> tuple[int, int] | None:
    """解析 ``wm size`` 输出；优先 Physical size，兼容 Override size。"""
    physical = re.search(r"Physical size: (\d+)x(\d+)", output)
    if physical:
        return int(physical.group(1)), int(physical.group(2))
    matches = _WM_SIZE_RE.findall(output)
    if matches:
        w, h = matches[-1]
        return int(w), int(h)
    return None


def escape_input_text(text: str) -> str:
    """Android ``input text`` 参数转义（adb-device.ts escapeInputText 同款）。

    顺序敏感：先 %→%25，再空格→%s，最后反斜杠转义远端 shell 元字符
    （adb shell 会把 argv 用空格拼接后交给设备端 sh 解释，实测确认）。
    """
    text = text.replace("%", "%25").replace(" ", "%s")
    return re.sub(r"([\\&<>;()|'\"`$])", r"\\\1", text)


def screenshot_difference(before_b64: str, after_b64: str) -> float:
    """两个 32x32 灰度签名的归一化平均像素差（screenshot.ts 同名函数）。"""
    left = base64.b64decode(before_b64)
    right = base64.b64decode(after_b64)
    if not left or len(left) != len(right):
        return 1.0
    total = sum(abs(a - b) for a, b in zip(left, right))
    return total / (len(left) * 255)


def _decode_screenshot(png: bytes) -> tuple[int, int, str]:
    """校验 PNG 并产出 (width, height, 32x32 灰度签名 base64)。

    对应 screenshot.ts decodeScreenshot；Pillow 懒加载（可选依赖）。
    """
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AdbError("internal", "screencap 输出不是合法 PNG（魔数不符）")
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 环境兜底
        raise AdbError(
            "internal",
            "截图解码需要 Pillow（pip install pillow，见 pyproject adb extra）") from exc
    try:
        with Image.open(io.BytesIO(png)) as img:
            img.load()
            width, height = img.size
            thumb = img.convert("L").resize((32, 32), Image.Resampling.BILINEAR)
            signature = base64.b64encode(thumb.tobytes()).decode("ascii")
    except AdbError:
        raise
    except Exception as exc:
        raise AdbError("internal", f"screencap 返回的 PNG 无法解码: {exc}") from exc
    return width, height, signature


def parse_adb_devices(output: str) -> dict[str, str]:
    """解析 ``adb devices`` 输出 → {serial: state}（device/unauthorized/offline…）。"""
    devices: dict[str, str] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices[parts[0]] = parts[1]
    return devices


# ---------------------------------------------------------------------------
# AdbTransport
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class _DeviceState:
    serial: str
    model: str = ""
    android_release: str = ""
    rooted: bool = False


class AdbTransport(DeviceTransport):
    """ADB 设备传输。

    - device_id ↔ adb serial 映射由构造参数 ``device_serials`` 注入，
      transport 内不硬编码任何设备。
    - 同一 device 的所有动作经 per-device ``asyncio.Lock`` 串行
      （ADB 通道怕乱序；观察与动作互斥）。
    - 所有超时一律回 ``ReceiptStatus.TIMEOUT``，绝不向调用方抛超时异常。
    """

    kind = "adb"

    def __init__(self, device_serials: Mapping[str, str], *,
                 runner: AdbRunner | None = None,
                 adb_path: str = DEFAULT_ADB_PATH,
                 output_store: OutputStore | None = None,
                 command_timeout_ms: int = DEFAULT_COMMAND_TIMEOUT_MS,
                 wake_on_observe: bool = True):
        self._serials = dict(device_serials)
        self._adb_path = adb_path
        self._runner: AdbRunner = runner if runner is not None else SubprocessAdbRunner()
        self._outputs = output_store if output_store is not None else OutputStore()
        self._command_timeout_ms = command_timeout_ms
        # 灭屏唤醒：screencap 在灭屏时只回黑帧（nova 实测），观察前按需唤醒
        self._wake_on_observe = wake_on_observe
        self._devices: dict[str, _DeviceState] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._observations: dict[str, dict[str, Observation]] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def connect(self, device_id: str) -> None:
        serial = self._serial_for(device_id)
        async with self._lock_for(device_id):
            state = await self._adb_state(serial)
            if state != "device" and ":" in serial:
                # 网络 adb：未连接时尝试 adb connect（幂等）
                await self._run([self._adb_path, "connect", serial],
                                max_bytes=DIAGNOSTIC_MAX_BYTES, timeout_ms=10_000,
                                check=False)
                state = await self._adb_state(serial)
            if state != "device":
                raise AdbError("unavailable",
                               f"adb 设备 {serial} 不在线（状态 {state or 'unknown'}）")
            info = _DeviceState(serial=serial)
            # 以下全部 best-effort：拿不到不阻塞连接
            info.model = (await self._shell_quiet(
                serial, ["getprop", "ro.product.model"])).strip()
            info.android_release = (await self._shell_quiet(
                serial, ["getprop", "ro.build.version.release"])).strip()
            info.rooted = await self._probe_root(serial)
            self._devices[device_id] = info

    async def disconnect(self, device_id: str) -> None:
        # adb 无长连可关；丢弃每设备运行时缓存（rooted/型号/观察缓存）。
        self._devices.pop(device_id, None)
        self._observations.pop(device_id, None)

    async def capabilities(self, device_id: str) -> Sequence[str]:
        self._serial_for(device_id)
        return ADB_CAPABILITIES

    async def health(self, device_id: str) -> TransportHealth:
        checked = now_ms()
        serial = self._serials.get(device_id)
        if serial is None:
            return TransportHealth(False, f"未知 device_id: {device_id}", checked)
        try:
            state = await self._adb_state(serial, timeout_ms=5_000)
        except AdbError as exc:
            return TransportHealth(False, exc.message, checked)
        if state == "device":
            info = self._devices.get(device_id)
            detail = f"serial={serial}"
            if info and info.model:
                detail += f" model={info.model} android={info.android_release}"
                detail += f" rooted={info.rooted}"
            return TransportHealth(True, detail, checked)
        return TransportHealth(False, f"adb 状态 {state or 'unknown'}", checked)

    # ------------------------------------------------------------------
    # 观察（contracts/v2 §8）
    # ------------------------------------------------------------------

    async def observe(self, device_id: str, request: ObserveRequest) -> Observation:
        self._serial_for(device_id)
        await self._ensure_connected(device_id)
        async with self._lock_for(device_id):
            return await self._observe_inner(device_id, request)

    async def _observe_inner(self, device_id: str,
                             request: ObserveRequest) -> Observation:
        """observe 本体（调用方须已持 per-device 锁）。"""
        serial = self._serial_for(device_id)

        async def _screenshot() -> tuple[bytes, int, int, str]:
            # 慢设备实测 nova GSI 软件渲染 screencap 可达 18s+，须独立超时
            png = await self._exec_out(serial, ["screencap", "-p"],
                                       max_bytes=SCREENSHOT_MAX_BYTES,
                                       timeout_ms=SCREENSHOT_TIMEOUT_MS)
            w, h, sig = _decode_screenshot(png)
            return png, w, h, sig

        async def _size() -> tuple[int, int]:
            text = await self._shell(serial, ["wm", "size"])
            parsed = parse_wm_size(text)
            if parsed is None:
                raise AdbError("internal", f"wm size 输出无法解析: {text[:100]!r}")
            return parsed

        async def _diagnostic(args: list[str]) -> str:
            try:
                return await self._shell(serial, args)
            except AdbError:
                return ""  # 诊断类输出 best-effort（adb-device.ts diagnostic()）

        # 灭屏时 screencap 只产黑帧（nova 实测 mean=0）；先按需唤醒。
        # 锁屏已被设备侧禁用的前提下，唤醒即可用，不引入 keyguard。
        if self._wake_on_observe and request.screenshot:
            power_text = await _diagnostic(["dumpsys", "power"])
            if parse_wakefulness(power_text) not in ("", "Awake"):
                await self._shell_quiet(
                    serial, ["input", "keyevent", "KEYCODE_WAKEUP"])
                await asyncio.sleep(1.0)  # 等屏幕就绪

        if request.screenshot:
            shot_coro: Awaitable = _screenshot()
        else:
            shot_coro = _size()
        if request.ui_tree:
            tree_coro: Awaitable = self._dump_hierarchy(serial)
        else:
            tree_coro = _const_coro("")
        # 截图/尺寸、UI 树、四类 dumpsys 全部并行（adb-device.ts getState 的
        # Promise.all 对应；同一设备并发 adb 会话由 adbd 侧隔离）
        (shot_result, xml_text, window_text, activity_text, ime_text,
         policy_text) = await asyncio.gather(
            shot_coro, tree_coro,
            _diagnostic(["dumpsys", "window", "windows"]),
            _diagnostic(["dumpsys", "activity", "activities"]),
            _diagnostic(["dumpsys", "input_method"]),
            _diagnostic(["dumpsys", "window", "policy"]),
        )

        png: bytes | None = None
        screenshot_sig: str | None = None
        if request.screenshot:
            png, width, height, screenshot_sig = shot_result
        else:
            width, height = shot_result

        if request.ui_tree:
            elements, ui_signature, truncated = parse_ui_hierarchy(
                xml_text, width=width, height=height,
                max_elements=request.max_elements)
        else:
            elements, ui_signature, truncated = (), _empty_ui_signature(), False

        package, activity = parse_foreground_state(
            f"{window_text}\n{activity_text}")
        if not package:
            package = infer_foreground_package(elements)
        keyboard = parse_keyboard_visible(f"{ime_text}\n{window_text}")
        locked = parse_locked(policy_text)

        observation_id = new_id("obs")
        screenshot_ref: str | None = None
        if png is not None:
            screenshot_ref = self._outputs.put(
                f"agent:shot:{observation_id}", png, "image/png")
        observation = Observation(
            observation_id=observation_id,
            device_id=device_id,
            taken_at=now_ms(),
            foreground_package=package,
            activity=activity,
            elements=elements,
            ui_signature=ui_signature,
            screenshot_ref=screenshot_ref,
            screenshot_signature=screenshot_sig,
            screen_width=width,
            screen_height=height,
            keyboard_shown=keyboard,
            locked=locked,
            truncated=truncated,
        )
        self._cache_observation(device_id, observation)
        return observation

    # ------------------------------------------------------------------
    # 动作执行（contracts/v2 §7）
    # ------------------------------------------------------------------

    async def execute(self, device_id: str, action: ActionRequest) -> ActionReceipt:
        started = now_ms()
        try:
            self._serial_for(device_id)
            # 懒连接：gateway 生命周期会显式 connect；直调 execute 时兜底。
            # 失败转成 FAILED 回执而不是抛出（execute 不抛约定）。
            await self._ensure_connected(device_id)
        except AdbError as exc:
            return self._receipt(device_id, action, ReceiptStatus.FAILED, started,
                                 error_code=exc.code, summary=exc.message)
        async with self._lock_for(device_id):
            try:
                summary, output_ref = await asyncio.wait_for(
                    self._dispatch(device_id, action),
                    timeout=max(action.timeout_ms, 1) / 1000)
                return self._receipt(device_id, action, ReceiptStatus.SUCCEEDED,
                                     started, summary=summary, output_ref=output_ref)
            except (asyncio.TimeoutError, TimeoutError):
                return self._receipt(device_id, action, ReceiptStatus.TIMEOUT,
                                     started,
                                     error_code="timeout",
                                     summary=f"动作超过 timeout_ms={action.timeout_ms}")
            except AdbTimeoutError as exc:
                return self._receipt(device_id, action, ReceiptStatus.TIMEOUT,
                                     started, error_code="timeout",
                                     summary=exc.message)
            except AdbError as exc:
                return self._receipt(device_id, action, ReceiptStatus.FAILED,
                                     started, error_code=exc.code,
                                     summary=exc.message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 回执兜底，绝不外抛
                return self._receipt(device_id, action, ReceiptStatus.FAILED,
                                     started, error_code="internal",
                                     summary=f"未预期异常: {exc!r}"[:500])

    async def _dispatch(self, device_id: str,
                        action: ActionRequest) -> tuple[str, str | None]:
        """返回 (summary, output_ref)。异常统一为 AdbError/AdbTimeoutError。"""
        cap = action.capability
        args = action.arguments or {}
        serial = self._serial_for(device_id)
        if cap not in ADB_CAPABILITIES:
            raise AdbError("capability_unknown",
                           f"adb transport 不支持能力 {cap!r}；"
                           f"支持: {', '.join(ADB_CAPABILITIES)}")
        if cap == "device.observe":
            return await self._act_observe(device_id, action)
        if cap == "device.tap":
            x, y, label = await self._resolve_point(device_id, args, action)
            await self._shell(serial, ["input", "tap", str(x), str(y)])
            return f"已点击 {label} ({x},{y})", None
        if cap == "device.long_press":
            x, y, label = await self._resolve_point(device_id, args, action)
            duration = self._int_arg(args, "duration_ms", 800, 1, 60_000)
            # long_press = 同点 swipe（adb-device.ts longPress）
            await self._shell(serial, ["input", "swipe", str(x), str(y),
                                       str(x), str(y), str(duration)])
            return f"已长按 {label} ({x},{y}) {duration}ms", None
        if cap == "device.swipe":
            x1 = self._int_arg(args, "x1", None, None, None)
            y1 = self._int_arg(args, "y1", None, None, None)
            x2 = self._int_arg(args, "x2", None, None, None)
            y2 = self._int_arg(args, "y2", None, None, None)
            duration = self._int_arg(args, "duration_ms", 300, 1, 60_000)
            w, h = await self._screen_size(serial)
            self._check_point(x1, y1, w, h)
            self._check_point(x2, y2, w, h)
            await self._shell(serial, ["input", "swipe", str(x1), str(y1),
                                       str(x2), str(y2), str(duration)])
            return f"已滑动 ({x1},{y1})→({x2},{y2}) {duration}ms", None
        if cap == "device.input_text":
            return await self._act_input_text(serial, args), None
        if cap == "device.key":
            key = str(args.get("key", ""))
            keyevent = KEY_EVENTS.get(key)
            if keyevent is None:
                raise AdbError("invalid_message",
                               f"未知按键 {key!r}；白名单: {', '.join(sorted(KEY_EVENTS))}")
            await self._shell(serial, ["input", "keyevent", keyevent])
            return f"已按键 {key} ({keyevent})", None
        if cap == "device.wait":
            ms = self._int_arg(args, "ms", None, 0, 60_000)
            await asyncio.sleep(ms / 1000)
            return f"已等待 {ms}ms", None
        if cap == "device.app.launch":
            return await self._act_app_launch(serial, args), None
        if cap == "device.app.list":
            return await self._act_app_list(serial, action, args)
        if cap == "device.logcat":
            return await self._act_logcat(serial, action, args)
        if cap == "device.shell":
            return await self._act_shell(device_id, serial, action, args)
        raise AdbError("capability_unknown", f"未实现的能力 {cap!r}")  # pragma: no cover

    # ------------------------------------------------------------------
    # 各能力实现
    # ------------------------------------------------------------------

    async def _act_observe(self, device_id: str,
                           action: ActionRequest) -> tuple[str, str | None]:
        obs = await self._observe_inner(device_id, ObserveRequest())
        doc = {
            "observation_id": obs.observation_id,
            "device_id": obs.device_id,
            "taken_at": obs.taken_at,
            "foreground_package": obs.foreground_package,
            "activity": obs.activity,
            "elements": [dataclasses.asdict(e) for e in obs.elements],
            "ui_signature": obs.ui_signature,
            "screenshot_ref": obs.screenshot_ref,
            "screenshot_signature": obs.screenshot_signature,
            "screen_width": obs.screen_width,
            "screen_height": obs.screen_height,
            "keyboard_shown": obs.keyboard_shown,
            "locked": obs.locked,
            "truncated": obs.truncated,
        }
        output_ref = self._outputs.put(
            f"agent:out:{action.action_id}",
            json.dumps(doc, ensure_ascii=False),
            "application/json")
        summary = (
            f"前台 {obs.foreground_package or '?'}"
            f"{('/' + obs.activity.split('/', 1)[1]) if obs.activity else ''}"
            f"，元素 {len(obs.elements)}{'（已截断）' if obs.truncated else ''}"
            f"，屏幕 {obs.screen_width}x{obs.screen_height}"
            f"，键盘={'显示' if obs.keyboard_shown else '隐藏'}"
            f"，锁屏={'是' if obs.locked else '否'}"
            f"，截图 {obs.screenshot_ref or '无'}"
            f"，ui_signature {obs.ui_signature[:12]}…"
        )
        return summary[:SUMMARY_MAX_CHARS], output_ref

    async def _act_input_text(self, serial: str, args: dict) -> str:
        text = args.get("text")
        if not isinstance(text, str):
            raise AdbError("invalid_message", "device.input_text 需要字符串参数 text")
        if text == "":
            return "空文本，无操作"
        if re.search(r"[^\x20-\x7e]", text):
            # TS 走 ADB Keyboard 广播辅助 IME；本实现暂未内置该辅助，明确拒绝
            raise AdbError("invalid_message",
                           "非 ASCII 文本需要 Unicode IME 辅助（当前未实现），已拒绝")
        await self._shell(serial, ["input", "text", escape_input_text(text)])
        return f"已输入 {len(text)} 字符"

    async def _act_app_launch(self, serial: str, args: dict) -> str:
        package = str(args.get("package", "")).strip()
        if not _PACKAGE_RE.match(package):
            raise AdbError("invalid_message",
                           f"非法 Android 包名 {package!r}（防注入校验拒绝）")
        # adb-device.ts openApp：先 resolve-activity 再 am start -n
        resolution = await self._shell(
            serial, ["cmd", "package", "resolve-activity", "--brief", package])
        component = None
        for line in resolution.splitlines():
            line = line.strip()
            if "/" in line and _COMPONENT_RE.match(line):
                component = line
                break
        if component is None:
            raise AdbError("internal",
                           f"包 {package!r} 无可启动 Activity"
                           f"（resolve-activity 输出: {resolution.strip()[:200]!r}）")
        await self._shell(serial, ["am", "start", "-n", component])
        return f"已启动 {component}"

    async def _act_app_list(self, serial: str, action: ActionRequest,
                            args: dict) -> tuple[str, str | None]:
        argv = ["pm", "list", "packages"]
        if args.get("third_party_only"):
            argv.append("-3")
        text = await self._shell(serial, argv, max_bytes=SHELL_MAX_BYTES)
        packages = sorted(
            line[len("package:"):] for line in text.splitlines()
            if line.startswith("package:"))
        output_ref = self._outputs.put(
            f"agent:out:{action.action_id}",
            json.dumps({"packages": packages, "count": len(packages)},
                       ensure_ascii=False),
            "application/json")
        return f"共 {len(packages)} 个包", output_ref

    async def _act_logcat(self, serial: str, action: ActionRequest,
                          args: dict) -> tuple[str, str | None]:
        argv = ["logcat", "-d", "-v", "threadtime"]
        max_lines = args.get("max_lines")
        since = args.get("since")
        if max_lines is not None:
            n = self._int_arg(args, "max_lines", None, 1, 100_000)
            argv += ["-t", str(n)]
        elif since is not None:
            if not isinstance(since, str) or not _LOGCAT_SINCE_RE.match(since):
                raise AdbError("invalid_message",
                               "logcat since 须为 'MM-DD HH:MM:SS.mmm' 格式")
            # 时间含空格：嵌入引号防止 adb 空格拼接后被远端 sh 拆开（实测确认）
            argv += ["-T", f'"{since}"']
        tag_filter = args.get("filter")
        if tag_filter is not None:
            if not isinstance(tag_filter, str) or not _LOGCAT_FILTER_RE.match(tag_filter):
                raise AdbError("invalid_message",
                               f"非法 logcat 过滤器 {tag_filter!r}"
                               "（仅支持单 tag 或 tag:prio）")
            argv += ["-s", tag_filter]
        max_bytes = self._int_arg(args, "max_bytes", 256 * 1024,
                                  1024, LOGCAT_MAX_BYTES)
        text = await self._shell(serial, argv, max_bytes=max_bytes)
        output_ref = self._outputs.put(
            f"agent:out:{action.action_id}", text, "text/plain; charset=utf-8")
        return f"logcat {len(text.encode('utf-8'))} 字节", output_ref

    async def _act_shell(self, device_id: str, serial: str,
                         action: ActionRequest,
                         args: dict) -> tuple[str, str | None]:
        argv = args.get("argv")
        command = args.get("command")
        allow_root = bool(args.get("allow_root", False))
        max_bytes = self._int_arg(args, "max_bytes", 256 * 1024,
                                  1024, SHELL_MAX_BYTES)
        if (argv is None) == (command is None):
            raise AdbError("invalid_message",
                           "device.shell 需要且仅需要 argv（字符串数组）或 command（字符串）之一")
        if argv is not None:
            if (not isinstance(argv, list) or not argv
                    or not all(isinstance(a, str) and a for a in argv)):
                raise AdbError("invalid_message", "argv 须为非空字符串数组")
            # argv 元素逐元素 shlex.quote：adb 只用空格拼接，不 quote 会被
            # 远端 sh 拆词/解释元字符（实测确认，见模块 docstring）
            inner = " ".join(shlex.quote(a) for a in argv)
        else:
            if not isinstance(command, str) or not command.strip():
                raise AdbError("invalid_message", "command 须为非空字符串")
            inner = command
        if allow_root:
            state = self._devices.get(device_id)
            if state is None or not state.rooted:
                raise AdbError("permission_missing",
                               "设备未确认 rooted（connect 时 su -c id 探测失败），"
                               "拒绝 allow_root 执行")
            # root 执行统一 su -c；inner 整体 quote 防远端 sh 拆词
            shell_argv = ["su", "-c", shlex.quote(inner)]
        else:
            # 单元素整体交给远端 sh 解析（argv 形式已在 inner 内逐元素 quote）
            shell_argv = [inner]
        text = await self._shell(serial, shell_argv, max_bytes=max_bytes)
        output_ref = self._outputs.put(
            f"agent:out:{action.action_id}", text, "text/plain; charset=utf-8")
        head = text.strip().splitlines()
        summary = head[0][:500] if head else "(无输出)"
        return f"{summary}（输出 {len(text.encode('utf-8'))} 字节）", output_ref

    # ------------------------------------------------------------------
    # 元素定位与重校验（contracts/v2 §8）
    # ------------------------------------------------------------------

    async def _resolve_point(self, device_id: str, args: dict,
                             action: ActionRequest) -> tuple[int, int, str]:
        """解析 tap/long_press 目标点。返回 (x, y, 描述)。"""
        serial = self._serial_for(device_id)
        if "element_id" in args:
            element_id = args["element_id"]
            if not isinstance(element_id, int) or isinstance(element_id, bool):
                raise AdbError("invalid_message", "element_id 须为整数")
            observation_id = args.get("observation_id") or action.observation_id
            if not observation_id:
                raise AdbError("invalid_message",
                               "引用 element_id 时 observation_id 必填（§7.1）")
            cached = self._observations.get(device_id, {}).get(observation_id)
            if cached is None:
                raise AdbError("stale_observation",
                               f"观察 {observation_id} 不在缓存（已过期或被逐出）")
            target = next((e for e in cached.elements
                           if e.element_id == element_id), None)
            if target is None:
                raise AdbError("stale_observation",
                               f"元素 {element_id} 不属于观察 {observation_id}")
            # §8：fresh 观察按稳定键（className+resourceId+contentDescription+
            # bounds，不含 text）重校验恰好一个匹配
            fresh = await self._observe_inner(
                device_id, ObserveRequest(screenshot=False, ui_tree=True))
            key = _stable_key(target)
            matches = [e for e in fresh.elements if _stable_key(e) == key]
            if len(matches) == 0:
                raise AdbError("element_not_found",
                               f"元素 {element_id} 在新鲜观察中已消失"
                               f"（{target.class_name} {target.resource_id}）")
            if len(matches) > 1:
                raise AdbError("stale_observation",
                               f"元素 {element_id} 在新鲜观察中有 {len(matches)} 个匹配，"
                               "无法唯一重校验")
            x, y = matches[0].center
            self._check_point(x, y, fresh.screen_width, fresh.screen_height)
            return x, y, (f"元素#{element_id}"
                          f"[{matches[0].text or matches[0].content_desc or matches[0].class_name}]")
        x = self._int_arg(args, "x", None, None, None)
        y = self._int_arg(args, "y", None, None, None)
        w, h = await self._screen_size(serial)
        self._check_point(x, y, w, h)
        return x, y, "坐标"

    @staticmethod
    def _check_point(x: int, y: int, width: int, height: int) -> None:
        """坐标界内校验（adb-device.ts assertCoordinates）。"""
        if not (0 <= x < width and 0 <= y < height):
            raise AdbError("invalid_message",
                           f"坐标 ({x},{y}) 越界（屏幕 {width}x{height}）")

    @staticmethod
    def _int_arg(args: dict, name: str, default: int | None,
                 minimum: int | None, maximum: int | None) -> int:
        value = args.get(name, default)
        if not isinstance(value, int) or isinstance(value, bool):
            raise AdbError("invalid_message", f"参数 {name} 须为整数，得到 {value!r}")
        if minimum is not None and value < minimum:
            raise AdbError("invalid_message", f"参数 {name}={value} 小于下限 {minimum}")
        if maximum is not None and value > maximum:
            raise AdbError("invalid_message", f"参数 {name}={value} 超过上限 {maximum}")
        return value

    # ------------------------------------------------------------------
    # read_output
    # ------------------------------------------------------------------

    async def read_output(self, device_id: str, output_ref: str, *,
                          offset: int = 0, max_bytes: int = 65536) -> tuple[bytes, bool]:
        # output store 为 transport 级（ref 自带 action/observation 命名空间），
        # device_id 仅作接口一致性要求，不参与寻址。
        self._serial_for(device_id)
        return self._outputs.read(output_ref, offset=offset, max_bytes=max_bytes)

    # ------------------------------------------------------------------
    # 内部：命令封装
    # ------------------------------------------------------------------

    def _serial_for(self, device_id: str) -> str:
        serial = self._serials.get(device_id)
        if serial is None:
            raise AdbError("unavailable",
                           f"未知 device_id {device_id!r}（serial 映射未注入）")
        return serial

    def _lock_for(self, device_id: str) -> asyncio.Lock:
        lock = self._locks.get(device_id)
        if lock is None:
            lock = self._locks[device_id] = asyncio.Lock()
        return lock

    async def _ensure_connected(self, device_id: str) -> None:
        if device_id not in self._devices:
            await self.connect(device_id)

    def _base_argv(self, serial: str) -> list[str]:
        return [self._adb_path, "-s", serial]

    async def _run(self, argv: Sequence[str], *, max_bytes: int,
                   timeout_ms: int | None = None, check: bool = True) -> AdbResult:
        """统一命令封装：有界输出、超时→AdbTimeoutError、非零退出→AdbError。"""
        budget = timeout_ms if timeout_ms is not None else self._command_timeout_ms
        result = await self._runner.run(argv, max_bytes=max_bytes,
                                        timeout_ms=budget)
        if result.timed_out:
            raise AdbTimeoutError(f"adb 命令超时（{budget}ms）: {_fmt_argv(argv)}")
        if result.lossy:
            raise AdbError("internal",
                           f"adb 输出超过上限 {max_bytes} 字节: {_fmt_argv(argv)}")
        if check and result.returncode != 0:
            raise self._classify_failure(result)
        return result

    @staticmethod
    def _classify_failure(result: AdbResult) -> AdbError:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        stdout = result.stdout.decode("utf-8", "replace").strip()
        message = stderr or stdout or f"adb 退出码 {result.returncode}"
        low = message.lower()
        if any(token in low for token in (
                "device offline", "device not found", "no devices",
                "unauthorized", "cannot connect", "failed to connect",
                "connection refused", "connection reset")):
            code = "unavailable"   # 设备掉线/未授权：与 internal 区分（§14）
        else:
            code = "internal"
        return AdbError(code, message[:500])

    async def _shell(self, serial: str, args: Sequence[str], *,
                     max_bytes: int = DIAGNOSTIC_MAX_BYTES,
                     timeout_ms: int | None = None) -> str:
        """``adb -s <serial> shell <args>`` → stdout 文本。

        注意：adb 会用空格把 args 拼接后交给设备端 sh 解释（实测确认），
        因此所有内部调用的参数必须是程序生成且已校验/转义的。
        """
        result = await self._run([*self._base_argv(serial), "shell", *args],
                                 max_bytes=max_bytes, timeout_ms=timeout_ms)
        return result.stdout.decode("utf-8", "replace")

    async def _shell_quiet(self, serial: str, args: Sequence[str]) -> str:
        try:
            return await self._shell(serial, args)
        except AdbError:
            return ""

    async def _exec_out(self, serial: str, args: Sequence[str], *,
                        max_bytes: int,
                        timeout_ms: int | None = None) -> bytes:
        """``adb -s <serial> exec-out <args>`` → 原始字节（二进制安全）。"""
        result = await self._run([*self._base_argv(serial), "exec-out", *args],
                                 max_bytes=max_bytes, timeout_ms=timeout_ms)
        return result.stdout

    async def _adb_state(self, serial: str, timeout_ms: int = 5_000) -> str:
        result = await self._run(
            [*self._base_argv(serial), "get-state"],
            max_bytes=DIAGNOSTIC_MAX_BYTES, timeout_ms=timeout_ms, check=False)
        if result.returncode != 0:
            return ""
        return result.stdout.decode("utf-8", "replace").strip()

    async def _probe_root(self, serial: str) -> bool:
        """su -c id 探测 root（best-effort，3s 超时；Magisk 拒绝/无 su 都算未 root）。"""
        try:
            result = await self._run(
                [*self._base_argv(serial), "shell", "su", "-c", "id"],
                max_bytes=DIAGNOSTIC_MAX_BYTES, timeout_ms=3_000, check=False)
        except AdbTimeoutError:
            return False
        return result.returncode == 0 and "uid=0" in result.stdout.decode(
            "utf-8", "replace")

    async def _screen_size(self, serial: str) -> tuple[int, int]:
        """当前屏幕尺寸。决策：每次现取 wm size（轻量），
        而非 TS 的点按前重新截图。"""
        text = await self._shell(serial, ["wm", "size"])
        parsed = parse_wm_size(text)
        if parsed is None:
            raise AdbError("internal", f"wm size 输出无法解析: {text[:100]!r}")
        return parsed

    async def _dump_hierarchy(self, serial: str) -> str:
        """uiautomator dump，3 次重试、间隔 300ms（adb-device.ts dumpHierarchy）。"""
        cause: AdbError | None = None
        for attempt in range(1, HIERARCHY_ATTEMPTS + 1):
            try:
                return await self._dump_hierarchy_once(serial)
            except AdbTimeoutError:
                raise  # 超时不重试，直接冒泡为 TIMEOUT 回执
            except AdbError as exc:
                cause = exc
                if attempt < HIERARCHY_ATTEMPTS:
                    await asyncio.sleep(HIERARCHY_RETRY_DELAY_S)
        raise AdbError("internal",
                       f"UIAutomator {HIERARCHY_ATTEMPTS} 次尝试均未返回 XML: "
                       f"{cause.message if cause else 'unknown'}")

    async def _dump_hierarchy_once(self, serial: str) -> str:
        """首选 /dev/tty 直出；失败回退写文件 + exec-out cat + rm -f。

        （adb-device.ts dumpHierarchyOnce；本仓库两台真机实测 /dev/tty 只回
        banner 不回 XML，回退路径是实际生效路径。）
        """
        try:
            direct = await self._shell(
                serial, ["uiautomator", "dump", "/dev/tty"],
                max_bytes=HIERARCHY_MAX_BYTES,
                timeout_ms=HIERARCHY_TIMEOUT_MS)
            return extract_hierarchy_xml(direct)
        except AdbError:
            pass  # 设备不支持 /dev/tty 直出 → 文件回退
        remote_path = f"/data/local/tmp/whale-adb-{uuid.uuid4().hex[:12]}.xml"
        try:
            await self._shell(serial, ["uiautomator", "dump", remote_path],
                              timeout_ms=HIERARCHY_TIMEOUT_MS)
            content = await self._exec_out(serial, ["cat", remote_path],
                                           max_bytes=HIERARCHY_MAX_BYTES)
            return extract_hierarchy_xml(content.decode("utf-8", "replace"))
        finally:
            try:
                await self._shell(serial, ["rm", "-f", remote_path])
            except AdbError:
                pass  # 临时文件清理 best-effort（内容是公开无障碍 XML）

    # ------------------------------------------------------------------
    # 内部：观察缓存与回执构造
    # ------------------------------------------------------------------

    def _cache_observation(self, device_id: str, observation: Observation) -> None:
        cache = self._observations.setdefault(device_id, {})
        cache[observation.observation_id] = observation
        while len(cache) > OBSERVATION_CACHE_SIZE:
            cache.pop(next(iter(cache)))  # FIFO 逐出最旧

    def last_observation(self, device_id: str) -> Observation | None:
        cache = self._observations.get(device_id)
        if not cache:
            return None
        return next(reversed(cache.values()))

    def _receipt(self, device_id: str, action: ActionRequest,
                 status: ReceiptStatus, started: int, *,
                 summary: str = "", output_ref: str | None = None,
                 error_code: str | None = None) -> ActionReceipt:
        return ActionReceipt(
            action_id=action.action_id,
            status=status,
            task_id=action.task_id,
            summary=summary[:SUMMARY_MAX_CHARS],
            output_ref=output_ref,
            # §7.3：坐标类动作端侧未做结果验证，一律 SKIPPED，禁止伪报 passed
            verification=Verification(VerificationStatus.SKIPPED, "端侧未做结果验证"),
            error_code=error_code,
            provider_ref=f"adb:{self._serials.get(device_id, '?')}",
            started_at=started,
            completed_at=now_ms(),
        )

    # ------------------------------------------------------------------
    # 设备发现
    # ------------------------------------------------------------------

    @classmethod
    async def discover(cls, *, adb_path: str = DEFAULT_ADB_PATH,
                       runner: AdbRunner | None = None,
                       timeout_ms: int = 10_000) -> dict[str, str]:
        """``adb devices`` → {serial: state}。只做发现，不做 device_id 映射。"""
        exe: AdbRunner = runner if runner is not None else SubprocessAdbRunner()
        result = await exe.run([adb_path, "devices"],
                               max_bytes=DIAGNOSTIC_MAX_BYTES,
                               timeout_ms=timeout_ms)
        if result.returncode != 0:
            raise AdbError("unavailable",
                           result.stderr.decode("utf-8", "replace").strip()
                           or "adb devices 失败")
        return parse_adb_devices(result.stdout.decode("utf-8", "replace"))


def _stable_key(e: UiElement) -> tuple:
    """§8 元素重校验稳定键：className+resourceId+contentDescription+bounds（无 text）。"""
    return (e.class_name, e.resource_id, e.content_desc, e.bounds)


def _empty_ui_signature() -> str:
    return hashlib.sha256(b"[]").hexdigest()


def _fmt_argv(argv: Sequence[str]) -> str:
    text = " ".join(str(a) for a in argv[:8])
    return text[:120] + ("…" if len(text) > 120 else "")


def _const_coro(value):
    """把常量包装成协程（供 asyncio.gather 与真实协程混用）。"""
    async def _inner():
        return value
    return _inner()
