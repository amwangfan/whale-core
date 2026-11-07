"""ADB Transport 单元测试（FakeAdbRunner 脚本化命令映射，不依赖真机）。

覆盖任务要求的七类用例：元素解析与裁剪、fingerprint 稳定性、stale 检测三判据、
坐标越界拒绝、open_app 注入字符拒绝、超时路径、OutputStore 分页；另补
dumpsys 解析、转义、root 门控、argv 注入实证、logcat/app.list/wait/key 等。

真机冒烟用例在文件尾部，pytest.mark.smoke + WHALE_ADB_SMOKE=1 才启用（CI 跳过）。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os

import pytest

from whale_core.devices.transports import adb as adb_mod
from whale_core.devices.transports.adb import (
    AdbResult,
    AdbTransport,
    OutputStore,
    element_fingerprint,
    escape_input_text,
    extract_hierarchy_xml,
    infer_foreground_package,
    parse_adb_devices,
    parse_foreground_state,
    parse_keyboard_visible,
    parse_locked,
    parse_ui_hierarchy,
    screenshot_difference,
)
from whale_core.devices.transports.base import (
    ActionRequest,
    ObserveRequest,
    ReceiptStatus,
)


def run(coro):
    """仓库无 pytest-asyncio 依赖；同步测试体内驱动协程。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# FakeAdbRunner：注入「命令前缀 → 输出/脚本」映射
# ---------------------------------------------------------------------------

class FakeAdbRunner:
    """按 argv 尾部（去掉 adb/-s/serial 前缀）做最长前缀路由。

    handler 形式可脚本化（改路由、sleep 模拟超时）。未命中路由即测试缺陷，
    直接 AssertionError。
    """

    def __init__(self):
        self.calls: list[list[str]] = []
        self._routes: list[tuple[tuple[str, ...], dict]] = []

    def on(self, *prefix: str, stdout: bytes | str = b"", stderr: bytes | str = b"",
           rc: int = 0, timed_out: bool = False, lossy: bool = False,
           handler=None) -> "FakeAdbRunner":
        if isinstance(stdout, str):
            stdout = stdout.encode("utf-8")
        if isinstance(stderr, str):
            stderr = stderr.encode("utf-8")
        # 同前缀后注册覆盖先注册（测试内改路由常见）
        self._routes = [r for r in self._routes if r[0] != tuple(prefix)]
        self._routes.append((tuple(prefix), {
            "stdout": stdout, "stderr": stderr, "rc": rc,
            "timed_out": timed_out, "lossy": lossy, "handler": handler,
        }))
        return self

    async def run(self, argv, *, max_bytes: int, timeout_ms: int) -> AdbResult:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        tail = argv[3:] if len(argv) >= 3 and argv[1] == "-s" else argv[1:]
        best = None
        for prefix, spec in self._routes:
            if len(prefix) <= len(tail) and tuple(tail[:len(prefix)]) == prefix:
                if best is None or len(prefix) > len(best[0]):
                    best = (prefix, spec)
        if best is None:
            raise AssertionError(f"FakeAdbRunner 未路由的 adb 调用: {argv}")
        spec = best[1]
        if spec["handler"] is not None:
            outcome = spec["handler"](tail)
            if asyncio.iscoroutine(outcome):
                outcome = await outcome
            if isinstance(outcome, AdbResult):
                return outcome
            return AdbResult(outcome, spec["stdout"], spec["stderr"],
                             spec["timed_out"], spec["lossy"])
        return AdbResult(spec["rc"], spec["stdout"], spec["stderr"],
                         spec["timed_out"], spec["lossy"])

    def shell_calls(self, *prefix: str) -> list[list[str]]:
        """所有命中给定 shell 前缀的调用（tail 形式）。"""
        out = []
        for argv in self.calls:
            tail = argv[3:] if len(argv) >= 3 and argv[1] == "-s" else argv[1:]
            if tail[:1] == ["shell"] and tail[1:1 + len(prefix)] == list(prefix):
                out.append(tail[1:])
        return out


# ---------------------------------------------------------------------------
# 夹具：PNG / uiautomator XML / dumpsys 文本（后两者取真实设备样本改简化）
# ---------------------------------------------------------------------------

def _png_bytes(width: int = 100, height: int = 200, gray: int = 128) -> bytes:
    from PIL import Image
    img = Image.new("L", (width, height), gray)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


PNG = _png_bytes()

XML_OK = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout"
        package="com.x" content-desc="" checkable="false" checked="false"
        clickable="false" enabled="true" focusable="false" focused="false"
        scrollable="false" long-clickable="false" password="false"
        selected="false" visible-to-user="true" bounds="[0,0][100,200]">
    <node text="OK" resource-id="com.x:id/ok" class="android.widget.Button"
          content-desc="确定" clickable="true" enabled="true" focusable="true"
          scrollable="false" long-clickable="false" selected="false"
          visible-to-user="true" bounds="[10,10][50,30]" />
    <node text="幽灵" class="android.widget.TextView" resource-id=""
          content-desc="" clickable="false" enabled="true" focusable="false"
          scrollable="false" long-clickable="false" selected="false"
          visible-to-user="false" bounds="[0,0][100,20]" />
    <node text="零面积" class="android.widget.TextView" resource-id=""
          content-desc="" clickable="false" enabled="true" focusable="false"
          scrollable="false" long-clickable="false" selected="false"
          visible-to-user="true" bounds="[5,5][5,20]" />
    <node text="" class="android.widget.EditText" resource-id="com.x:id/input"
          content-desc="" clickable="false" enabled="true" focusable="true"
          scrollable="false" long-clickable="false" selected="false"
          visible-to-user="true" bounds="[0,40][100,70]" />
    <node text="越界裁剪" class="android.widget.TextView" resource-id=""
          content-desc="" clickable="false" enabled="true" focusable="false"
          scrollable="false" long-clickable="false" selected="false"
          visible-to-user="true" bounds="[-10,-10][150,300]" />
    <node text="" class="android.view.View" resource-id="" content-desc=""
          clickable="false" enabled="true" focusable="false" scrollable="false"
          long-clickable="false" selected="false" visible-to-user="true"
          bounds="[0,80][100,100]" />
  </node>
</hierarchy>
"""

#: 两个稳定键（class+rid+desc+bounds）相同但 text 不同的节点 → 多匹配 stale 用
XML_DUP = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node text="甲" resource-id="com.x:id/dup" class="android.widget.TextView"
        content-desc="" clickable="true" enabled="true" focusable="false"
        scrollable="false" long-clickable="false" selected="false"
        visible-to-user="true" bounds="[10,10][60,40]" />
  <node text="乙" resource-id="com.x:id/dup" class="android.widget.TextView"
        content-desc="" clickable="true" enabled="true" focusable="false"
        scrollable="false" long-clickable="false" selected="false"
        visible-to-user="true" bounds="[10,10][60,40]" />
</hierarchy>
"""

#: XML_OK 去掉 OK 按钮（元素在新鲜观察中消失 → element_not_found 用）
XML_GONE = XML_OK.replace('text="OK"', 'text=" renamed"').replace(
    'resource-id="com.x:id/ok"', 'resource-id="com.x:id/ok2"')

DUMP_WINDOW = """
  mCurrentFocus=Window{4b434a5 u0 StatusBar}
  mFocusedApp=AppWindowToken{a11ab2b token=Token{9a3a77a ActivityRecord{7dc919c u0 com.ss.launcher2/.MainActivity t111}}}
"""
DUMP_ACTIVITY = """
      topResumedActivity=ActivityRecord{caaf89e u0 com.onyx/.reader.main.ui.MainActivity} t1491}
"""
DUMP_IME = "mShowRequested=false mInputShown=false\n  mIsInputViewShown=false mStatusIcon=0\n"
DUMP_POLICY = "      isStatusBarKeyguard=false\n      mShowingLockscreen=false\n      showing=false\n"


def _wired(fake: FakeAdbRunner, *, xml: str = XML_OK,
           rooted: bool = False) -> AdbTransport:
    """标准联通路由：connect 探测 + observe 全套 + 常用输入。"""
    fake.on("get-state", stdout="device\n")
    fake.on("shell", "getprop", "ro.product.model", stdout="FakePhone\n")
    fake.on("shell", "getprop", "ro.build.version.release", stdout="13\n")
    fake.on("shell", "su", "-c", "id",
            stdout="uid=0(root) gid=0(root)\n" if rooted else "",
            rc=0 if rooted else 1)
    fake.on("exec-out", "screencap", "-p", stdout=PNG)
    fake.on("shell", "wm", "size", stdout="Physical size: 100x200\n")
    fake.on("shell", "uiautomator", "dump", "/dev/tty",
            stdout=f"UI hierchary dumped to: /dev/tty\n{xml}")
    fake.on("shell", "dumpsys", "window", "windows", stdout=DUMP_WINDOW)
    fake.on("shell", "dumpsys", "activity", "activities", stdout=DUMP_ACTIVITY)
    fake.on("shell", "dumpsys", "input_method", stdout=DUMP_IME)
    fake.on("shell", "dumpsys", "window", "policy", stdout=DUMP_POLICY)
    # 观察唤醒链路的电源查询（默认 Awake，个案可覆盖）
    fake.on("shell", "dumpsys", "power", stdout="mWakefulness=Awake\n")
    fake.on("shell", "input", "tap")
    fake.on("shell", "input", "swipe")
    fake.on("shell", "input", "text")
    fake.on("shell", "input", "keyevent")
    fake.on("shell", "cmd", "package", "resolve-activity", "--brief",
            stdout="priority=0 preferredOrder=0 match=0x108000\n"
                   "com.android.settings/.Settings\n")
    fake.on("shell", "am", "start", "-n", stdout="Starting: Intent { cmp=x }\n")
    fake.on("shell", "pm", "list", "packages",
            stdout="package:com.b\npackage:com.a\n")
    fake.on("shell", "logcat", "-d")
    return AdbTransport({"dev1": "serial1"}, runner=fake, adb_path="adb-fake")


def _action(capability: str, arguments: dict | None = None, **kw) -> ActionRequest:
    return ActionRequest(
        action_id=kw.pop("action_id", "act_TEST123"),
        capability=capability,
        arguments=arguments or {},
        idempotency_key="idem-test-1",
        timeout_ms=kw.pop("timeout_ms", 10_000),
        **kw,
    )


# ---------------------------------------------------------------------------
# 1. 元素解析与裁剪（hierarchy.ts 移植语义）
# ---------------------------------------------------------------------------

class TestHierarchyParsing:
    def test_filters_and_ids(self):
        elements, signature, truncated = parse_ui_hierarchy(
            XML_OK, width=100, height=200)
        assert not truncated
        # 保留：OK 按钮、EditText、越界裁剪文本；丢弃：不可见/零面积/非 meaningful
        assert len(elements) == 3
        assert [e.element_id for e in elements] == [1, 2, 3]  # 观察内从 1 递增
        ok = elements[0]
        assert ok.class_name == "android.widget.Button"
        assert ok.text == "OK" and ok.resource_id == "com.x:id/ok"
        assert ok.content_desc == "确定"
        assert ok.clickable and ok.bounds == (10, 10, 50, 30)
        assert ok.center == (30, 20)
        edit = elements[1]
        assert edit.editable  # 类名 EditText 推导（editable 属性缺省也算）
        clipped = elements[2]
        assert clipped.bounds == (0, 0, 100, 200)  # 越界坐标被裁到屏幕内
        assert len(signature) == 64

    def test_truncation_by_count(self):
        elements, _, truncated = parse_ui_hierarchy(
            XML_OK, width=100, height=200, max_elements=2)
        assert truncated and len(elements) == 2

    def test_truncation_by_bytes(self):
        elements, _, truncated = parse_ui_hierarchy(
            XML_OK, width=100, height=200, max_serialized_bytes=200)
        assert truncated and len(elements) < 3

    def test_malformed_xml(self):
        with pytest.raises(adb_mod.AdbError) as ei:
            parse_ui_hierarchy("not xml at all", width=100, height=200)
        assert ei.value.code == "internal"

    def test_extract_from_noisy_stdout(self):
        noisy = "UI hierchary dumped to: /dev/tty\n" + XML_OK + "\ntrailing"
        assert extract_hierarchy_xml(noisy).startswith("<?xml")
        with pytest.raises(adb_mod.AdbError):
            extract_hierarchy_xml("UI hierchary dumped to: /dev/tty\n")


class TestFingerprint:
    def test_matches_spec_formula(self):
        # contracts/v2 §8：sha256(className|text|resourceId|contentDescription|
        # bounds|clickable|scrollable|editable) 前 16 hex
        fp = element_fingerprint("android.widget.Button", "OK", "com.x:id/ok",
                                 "确定", (10, 10, 50, 30), True, False, False)
        expect = hashlib.sha256(
            "android.widget.Button|OK|com.x:id/ok|确定|10,10,50,30|true|false|false"
            .encode("utf-8")).hexdigest()[:16]
        assert fp == expect and len(fp) == 16

    def test_stable_across_observations(self):
        e1, sig1, _ = parse_ui_hierarchy(XML_OK, width=100, height=200)
        e2, sig2, _ = parse_ui_hierarchy(XML_OK, width=100, height=200)
        assert [e.fingerprint for e in e1] == [e.fingerprint for e in e2]
        assert sig1 == sig2  # ui_signature 与 element_id 分配无关、跨观察稳定

    def test_text_changes_fingerprint(self):
        changed = XML_OK.replace('text="OK"', 'text="OK!"')
        e1, _, _ = parse_ui_hierarchy(XML_OK, width=100, height=200)
        e2, _, _ = parse_ui_hierarchy(changed, width=100, height=200)
        assert e1[0].fingerprint != e2[0].fingerprint


# ---------------------------------------------------------------------------
# 2. dumpsys 解析与转义（foreground.ts / adb-device.ts 移植）
# ---------------------------------------------------------------------------

class TestDumpsysParsing:
    def test_foreground_from_focused_app(self):
        pkg, act = parse_foreground_state(DUMP_WINDOW)
        assert pkg == "com.ss.launcher2" and act == "com.ss.launcher2/.MainActivity"

    def test_foreground_from_top_resumed(self):
        pkg, act = parse_foreground_state(DUMP_ACTIVITY)
        assert pkg == "com.onyx" and act == "com.onyx/.reader.main.ui.MainActivity"

    def test_foreground_fallback_by_resource_id(self):
        elements, _, _ = parse_ui_hierarchy(XML_OK, width=100, height=200)
        assert infer_foreground_package(elements) == "com.x"
        assert infer_foreground_package(()) == ""
        pkg, _ = parse_foreground_state("nothing useful")
        assert pkg == ""

    def test_keyboard_and_locked(self):
        assert parse_keyboard_visible(DUMP_IME) is False
        assert parse_keyboard_visible("mInputShown=true") is True
        assert parse_keyboard_visible("mImeWindowVis=0x2") is True
        assert parse_locked(DUMP_POLICY) is False
        # nova GSI 实测：isStatusBarKeyguard=true 解锁后依然成立（状态栏窗口属性），
        # 不足以判锁；锁屏以 keyguard showing= 为准
        assert parse_locked("isStatusBarKeyguard=true") is False
        assert parse_locked("showing=true") is True
        assert parse_locked("showing=false") is False
        assert parse_locked("mShowingLockscreen=true") is True
        assert parse_locked("showingAndNotOccluded=true") is False  # 无 "showing=" 子串
        assert parse_locked(
            "      showing=true\n      showingAndNotOccluded=true\n") is True

    def test_escape_input_text(self):
        # adb-device.ts escapeInputText：%→%25、空格→%s、shell 元字符反斜杠
        assert escape_input_text("a b%&") == r"a%sb%25\&"
        assert escape_input_text("x;y") == r"x\;y"
        assert escape_input_text("plain") == "plain"

    def test_screenshot_difference(self):
        import base64
        a = base64.b64encode(bytes([0] * 1024)).decode()
        b = base64.b64encode(bytes([0] * 1024)).decode()
        c = base64.b64encode(bytes([255] * 1024)).decode()
        assert screenshot_difference(a, b) == 0.0
        assert screenshot_difference(a, c) == 1.0
        assert screenshot_difference(a, "") == 1.0

    def test_parse_adb_devices(self):
        text = ("List of devices attached\n"
                "100.92.91.85:5555\tdevice\n"
                "emulator-5554\toffline\n\n")
        assert parse_adb_devices(text) == {
            "100.92.91.85:5555": "device", "emulator-5554": "offline"}


# ---------------------------------------------------------------------------
# 3. observe / execute 主流程
# ---------------------------------------------------------------------------

class TestObserve:
    def test_observe_assembles_observation(self):
        fake = FakeAdbRunner()
        t = _wired(fake)

        async def main():
            await t.connect("dev1")
            return await t.observe("dev1", ObserveRequest())

        obs = run(main())
        assert obs.observation_id.startswith("obs_")
        assert obs.device_id == "dev1"
        assert obs.foreground_package == "com.ss.launcher2"
        assert obs.activity == "com.ss.launcher2/.MainActivity"
        assert len(obs.elements) == 3
        assert (obs.screen_width, obs.screen_height) == (100, 200)
        assert obs.keyboard_shown is False and obs.locked is False
        assert obs.screenshot_ref and obs.screenshot_ref.startswith("agent:shot:")
        # 截图落 OutputStore，可读回原始 PNG
        data, truncated = run(t.read_output("dev1", obs.screenshot_ref))
        assert data == PNG and truncated is False
        assert obs.screenshot_signature  # 32x32 灰度签名

    def test_observe_without_screenshot_uses_wm_size(self):
        fake = FakeAdbRunner()
        t = _wired(fake)

        async def main():
            await t.connect("dev1")
            return await t.observe("dev1", ObserveRequest(screenshot=False))

        obs = run(main())
        assert obs.screenshot_ref is None and obs.screenshot_signature is None
        assert (obs.screen_width, obs.screen_height) == (100, 200)
        assert not fake.shell_calls("screencap")

    def test_execute_observe_returns_output_ref(self):
        fake = FakeAdbRunner()
        t = _wired(fake)

        async def main():
            await t.connect("dev1")
            return await t.execute("dev1", _action("device.observe"))

        receipt = run(main())
        assert receipt.status is ReceiptStatus.SUCCEEDED
        assert receipt.output_ref == "agent:out:act_TEST123"
        assert "com.ss.launcher2" in receipt.summary
        raw, truncated = run(t.read_output("dev1", receipt.output_ref))
        doc = json.loads(raw.decode("utf-8"))
        assert doc["foreground_package"] == "com.ss.launcher2"
        assert len(doc["elements"]) == 3
        assert receipt.verification.status.value == "skipped"  # 不伪报 passed
        assert receipt.provider_ref == "adb:serial1"

    def test_uiautomator_file_fallback(self):
        """首选 /dev/tty 失败 → 写文件 + exec-out cat + rm -f（3 次重试链）。"""
        fake = FakeAdbRunner()
        t = _wired(fake)
        fake._routes = [r for r in fake._routes
                        if r[0][:3] != ("shell", "uiautomator", "dump")]
        fake.on("shell", "uiautomator", "dump", "/dev/tty",
                stdout="ERROR: null output", rc=1)
        fake.on("shell", "uiautomator", "dump",
                stdout="UI hierchary dumped to: /data/local/tmp/x.xml\n")
        fake.on("exec-out", "cat", stdout=XML_OK)
        fake.on("shell", "rm", "-f")

        async def main():
            await t.connect("dev1")
            return await t.observe("dev1", ObserveRequest(screenshot=False))

        obs = run(main())
        assert len(obs.elements) == 3
        dumps = fake.shell_calls("uiautomator", "dump")
        assert dumps[0][-1] == "/dev/tty"
        assert dumps[1][-1].startswith("/data/local/tmp/whale-adb-")
        rms = fake.shell_calls("rm", "-f")
        assert rms and rms[0][-1] == dumps[1][-1]  # 临时文件被清理


class TestConnectHealth:
    def test_connect_probes_and_health(self):
        fake = FakeAdbRunner()
        t = _wired(fake, rooted=True)
        run(t.connect("dev1"))
        health = run(t.health("dev1"))
        assert health.connected and "model=FakePhone" in health.detail
        assert "rooted=True" in health.detail
        assert run(t.capabilities("dev1")) == adb_mod.ADB_CAPABILITIES
        # 未知设备
        bad = run(t.health("nope"))
        assert not bad.connected

    def test_connect_offline_device(self):
        fake = FakeAdbRunner()
        fake.on("get-state", stdout="", stderr="error: device offline", rc=1)
        fake.on("shell", "getprop")
        fake.on("shell", "su", "-c", "id", rc=1)
        t = AdbTransport({"dev1": "serial1"}, runner=fake)
        with pytest.raises(adb_mod.AdbError) as ei:
            run(t.connect("dev1"))
        assert ei.value.code == "unavailable"


# ---------------------------------------------------------------------------
# 4. 输入动作：坐标校验 / stale 三判据 / 注入拒绝 / 超时
# ---------------------------------------------------------------------------

class TestTap:
    def test_tap_by_coordinates(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        receipt = run(t.execute("dev1", _action("device.tap", {"x": 30, "y": 20})))
        assert receipt.status is ReceiptStatus.SUCCEEDED
        assert fake.shell_calls("input", "tap")[-1][2:] == ["30", "20"]

    @pytest.mark.parametrize("x,y", [(-1, 0), (0, -5), (100, 20), (30, 200)])
    def test_tap_out_of_bounds_rejected(self, x, y):
        fake = FakeAdbRunner()
        t = _wired(fake)
        receipt = run(t.execute("dev1", _action("device.tap", {"x": x, "y": y})))
        assert receipt.status is ReceiptStatus.FAILED
        assert receipt.error_code == "invalid_message"
        assert not fake.shell_calls("input", "tap")  # 拒绝在发命令之前

    def test_tap_non_int_rejected(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        receipt = run(t.execute(
            "dev1", _action("device.tap", {"x": 1.5, "y": 2})))
        assert receipt.status is ReceiptStatus.FAILED
        assert receipt.error_code == "invalid_message"

    def _observe_then(self, t, capability="device.tap", extra=None, xml=None):
        async def main():
            obs = await t.observe("dev1", ObserveRequest())
            args = {"element_id": 1, "observation_id": obs.observation_id}
            if extra:
                args.update(extra)
            return await t.execute("dev1", _action(capability, args))
        return run(main())

    def test_tap_by_element_success(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        run(t.connect("dev1"))
        receipt = self._observe_then(t)
        assert receipt.status is ReceiptStatus.SUCCEEDED
        # OK 按钮 bounds [10,10][50,30] → 中心 (30,20)
        assert fake.shell_calls("input", "tap")[-1][2:] == ["30", "20"]

    def test_stale_unknown_observation(self):
        """判据一：observation_id 不在缓存 → stale_observation。"""
        fake = FakeAdbRunner()
        t = _wired(fake)
        run(t.connect("dev1"))
        receipt = run(t.execute("dev1", _action(
            "device.tap", {"element_id": 1, "observation_id": "obs_GHOST"})))
        assert receipt.status is ReceiptStatus.FAILED
        assert receipt.error_code == "stale_observation"

    def test_stale_element_not_in_observation(self):
        """判据二：element_id 不属于该观察 → stale_observation。"""
        fake = FakeAdbRunner()
        t = _wired(fake)
        run(t.connect("dev1"))
        receipt = self._observe_then(t, extra={"element_id": 999})
        assert receipt.status is ReceiptStatus.FAILED
        assert receipt.error_code == "stale_observation"

    def test_stale_zero_match_now(self):
        """判据三a：新鲜观察中元素消失 → element_not_found（§14 元素丢失）。"""
        fake = FakeAdbRunner()
        t = _wired(fake)
        state = {"xml": XML_OK}

        def swap(tail):
            nonlocal_xml = state["xml"]
            return AdbResult(0, f"dumped\n{nonlocal_xml}".encode(), b"")

        fake._routes = [r for r in fake._routes
                        if r[0][:3] != ("shell", "uiautomator", "dump")]
        fake.on("shell", "uiautomator", "dump", handler=swap)
        run(t.connect("dev1"))

        async def main():
            obs = await t.observe("dev1", ObserveRequest())
            state["xml"] = XML_GONE  # 观察后界面变化：OK 按钮消失
            return await t.execute("dev1", _action(
                "device.tap", {"element_id": 1,
                               "observation_id": obs.observation_id}))

        receipt = run(main())
        assert receipt.status is ReceiptStatus.FAILED
        assert receipt.error_code == "element_not_found"
        assert not fake.shell_calls("input", "tap")

    def test_stale_multiple_matches(self):
        """判据三b：新鲜观察中稳定键多匹配（text 不同不参与稳定键）→ stale。"""
        fake = FakeAdbRunner()
        t = _wired(fake)
        state = {"xml": XML_OK}
        fake._routes = [r for r in fake._routes
                        if r[0][:3] != ("shell", "uiautomator", "dump")]
        fake.on("shell", "uiautomator", "dump",
                handler=lambda tail: AdbResult(
                    0, f"dumped\n{state['xml']}".encode(), b""))
        run(t.connect("dev1"))

        async def main():
            obs = await t.observe("dev1", ObserveRequest())
            state["xml"] = XML_DUP.replace(
                'resource-id="com.x:id/dup"', 'resource-id="com.x:id/ok"').replace(
                'class="android.widget.TextView"', 'class="android.widget.Button"').replace(
                'content-desc=""', 'content-desc="确定"').replace(
                'bounds="[10,10][60,40]"', 'bounds="[10,10][50,30]"')
            return await t.execute("dev1", _action(
                "device.tap", {"element_id": 1,
                               "observation_id": obs.observation_id}))

        receipt = run(main())
        assert receipt.status is ReceiptStatus.FAILED
        assert receipt.error_code == "stale_observation"
        assert not fake.shell_calls("input", "tap")

    def test_element_requires_observation_id(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        run(t.connect("dev1"))
        receipt = run(t.execute("dev1", _action("device.tap", {"element_id": 1})))
        assert receipt.status is ReceiptStatus.FAILED
        assert receipt.error_code == "invalid_message"  # §7.1：必填缺失

    def test_long_press_default_800ms(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        receipt = run(t.execute(
            "dev1", _action("device.long_press", {"x": 10, "y": 10})))
        assert receipt.status is ReceiptStatus.SUCCEEDED
        # long_press = 同点 swipe 800ms（adb-device.ts longPress）
        assert fake.shell_calls("input", "swipe")[-1][2:] == [
            "10", "10", "10", "10", "800"]

    def test_swipe(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        receipt = run(t.execute("dev1", _action(
            "device.swipe", {"x1": 1, "y1": 2, "x2": 30, "y2": 40,
                             "duration_ms": 500})))
        assert receipt.status is ReceiptStatus.SUCCEEDED
        assert fake.shell_calls("input", "swipe")[-1][2:] == [
            "1", "2", "30", "40", "500"]


class TestInputTextKeyWait:
    def test_input_text_escaped(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        receipt = run(t.execute(
            "dev1", _action("device.input_text", {"text": "a b%&"})))
        assert receipt.status is ReceiptStatus.SUCCEEDED
        assert fake.shell_calls("input", "text")[-1][2:] == [r"a%sb%25\&"]

    def test_input_text_non_ascii_rejected(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        receipt = run(t.execute(
            "dev1", _action("device.input_text", {"text": "中文"})))
        assert receipt.status is ReceiptStatus.FAILED
        assert receipt.error_code == "invalid_message"
        assert not fake.shell_calls("input", "text")

    def test_key_whitelist(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        ok = run(t.execute("dev1", _action("device.key", {"key": "back"})))
        assert ok.status is ReceiptStatus.SUCCEEDED
        assert fake.shell_calls("input", "keyevent")[-1][2:] == ["KEYCODE_BACK"]
        bad = run(t.execute("dev1", _action("device.key", {"key": "rm -rf /"})))
        assert bad.status is ReceiptStatus.FAILED
        assert bad.error_code == "invalid_message"

    def test_wait(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        receipt = run(t.execute("dev1", _action("device.wait", {"ms": 20})))
        assert receipt.status is ReceiptStatus.SUCCEEDED
        bad = run(t.execute("dev1", _action("device.wait", {"ms": -5})))
        assert bad.status is ReceiptStatus.FAILED


class TestAppLaunch:
    def test_launch_happy(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        receipt = run(t.execute(
            "dev1", _action("device.app.launch",
                            {"package": "com.android.settings"})))
        assert receipt.status is ReceiptStatus.SUCCEEDED
        assert "com.android.settings/.Settings" in receipt.summary
        assert fake.shell_calls("am", "start")[-1][2:] == [
            "-n", "com.android.settings/.Settings"]

    @pytest.mark.parametrize("package", [
        "com.x; rm -rf /",          # 分号注入
        "com.x && reboot",          # 与注入
        "com.x`id`",                # 反引号
        "com.x/$(id)",              # 路径/替换
        "com.x y",                  # 空格
        "noDot",                    # 无点
        "",                         # 空
    ])
    def test_launch_injection_rejected(self, package):
        fake = FakeAdbRunner()
        t = _wired(fake)
        receipt = run(t.execute(
            "dev1", _action("device.app.launch", {"package": package})))
        assert receipt.status is ReceiptStatus.FAILED
        assert receipt.error_code == "invalid_message"
        # 拒绝发生在任何 resolve/start 之前
        assert not fake.shell_calls("cmd", "package", "resolve-activity")
        assert not fake.shell_calls("am", "start")

    def test_launch_no_activity(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        fake._routes = [r for r in fake._routes
                        if r[0][:2] != ("shell", "cmd")]
        fake.on("shell", "cmd", "package", "resolve-activity", "--brief",
                stdout="No activity found\n")
        receipt = run(t.execute(
            "dev1", _action("device.app.launch", {"package": "com.ghost.app"})))
        assert receipt.status is ReceiptStatus.FAILED
        assert receipt.error_code == "internal"


class TestTimeoutAndFailureMapping:
    def test_action_timeout_returns_timeout_receipt(self):
        """execute 级 wait_for：慢命令 → TIMEOUT 终态，绝不抛出。"""
        fake = FakeAdbRunner()
        t = _wired(fake)

        async def slow(tail):
            await asyncio.sleep(2)
            return 0

        fake._routes = [r for r in fake._routes if r[0][:2] != ("shell", "input")]
        fake.on("shell", "input", "tap", handler=slow)
        receipt = run(t.execute(
            "dev1", _action("device.tap", {"x": 1, "y": 1}, timeout_ms=100)))
        assert receipt.status is ReceiptStatus.TIMEOUT
        assert receipt.error_code == "timeout"
        assert receipt.status.terminal

    def test_command_timeout_maps_to_timeout(self):
        """runner 层 timed_out 标记 → TIMEOUT 回执。"""
        fake = FakeAdbRunner()
        t = _wired(fake)
        fake._routes = [r for r in fake._routes if r[0][:2] != ("shell", "input")]
        fake.on("shell", "input", "tap", timed_out=True)
        receipt = run(t.execute("dev1", _action("device.tap", {"x": 1, "y": 1})))
        assert receipt.status is ReceiptStatus.TIMEOUT
        assert receipt.error_code == "timeout"

    def test_device_offline_maps_unavailable(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        fake._routes = [r for r in fake._routes if r[0][:2] != ("shell", "input")]
        fake.on("shell", "input", "tap",
                stderr="error: device offline", rc=1)
        receipt = run(t.execute("dev1", _action("device.tap", {"x": 1, "y": 1})))
        assert receipt.status is ReceiptStatus.FAILED
        assert receipt.error_code == "unavailable"

    def test_generic_failure_maps_internal(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        fake._routes = [r for r in fake._routes if r[0][:2] != ("shell", "input")]
        fake.on("shell", "input", "tap", stderr="java.lang.Exception", rc=1)
        receipt = run(t.execute("dev1", _action("device.tap", {"x": 1, "y": 1})))
        assert receipt.status is ReceiptStatus.FAILED
        assert receipt.error_code == "internal"

    def test_capability_unknown(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        receipt = run(t.execute("dev1", _action("device.rocket", {})))
        assert receipt.status is ReceiptStatus.FAILED
        assert receipt.error_code == "capability_unknown"

    def test_unknown_device(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        receipt = run(t.execute("ghost", _action("device.wait", {"ms": 1})))
        assert receipt.status is ReceiptStatus.FAILED
        assert receipt.error_code == "unavailable"

    def test_lossy_output_maps_internal(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        fake._routes = [r for r in fake._routes if r[0][:2] != ("shell", "input")]
        fake.on("shell", "input", "tap", lossy=True)
        receipt = run(t.execute("dev1", _action("device.tap", {"x": 1, "y": 1})))
        assert receipt.status is ReceiptStatus.FAILED
        assert receipt.error_code == "internal"


# ---------------------------------------------------------------------------
# 5. shell / logcat / app.list
# ---------------------------------------------------------------------------

class TestShellLogcatApps:
    def test_shell_argv_quoted(self):
        """argv 形式逐元素 quote：adb 空格拼接 + 远端 sh 解释（实证行为）。"""
        fake = FakeAdbRunner()
        t = _wired(fake)
        fake.on("shell", "echo 'a b'", stdout="a b\n")
        receipt = run(t.execute(
            "dev1", _action("device.shell", {"argv": ["echo", "a b"]})))
        assert receipt.status is ReceiptStatus.SUCCEEDED
        tail = fake.calls[-1]
        assert tail[3:] == ["shell", "echo 'a b'"]  # 单元素、已 quote
        raw, _ = run(t.read_output("dev1", receipt.output_ref))
        assert raw == b"a b\n"

    def test_shell_command_form(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        fake.on("shell", "echo hi | wc -c", stdout="3\n")
        receipt = run(t.execute(
            "dev1", _action("device.shell", {"command": "echo hi | wc -c"})))
        assert receipt.status is ReceiptStatus.SUCCEEDED
        assert fake.calls[-1][-1] == "echo hi | wc -c"  # 原样走远端 sh

    def test_shell_root_gating(self):
        fake = FakeAdbRunner()
        t = _wired(fake, rooted=False)
        run(t.connect("dev1"))
        receipt = run(t.execute("dev1", _action(
            "device.shell", {"command": "id", "allow_root": True})))
        assert receipt.status is ReceiptStatus.FAILED
        assert receipt.error_code == "permission_missing"
        # 除 connect 时的 root 探测（su -c id）外，不得有任何 su 调用
        assert fake.shell_calls("su") == [["su", "-c", "id"]]

    def test_shell_root_allowed_when_rooted(self):
        fake = FakeAdbRunner()
        t = _wired(fake, rooted=True)
        fake.on("shell", "su", "-c", "'id -u'", stdout="0\n")
        run(t.connect("dev1"))
        receipt = run(t.execute("dev1", _action(
            "device.shell", {"command": "id -u", "allow_root": True})))
        assert receipt.status is ReceiptStatus.SUCCEEDED
        # 命令整体 quote 后经 su -c 下发（含空格，quote 生效）
        assert ["su", "-c", "'id -u'"] in fake.shell_calls("su", "-c")

    def test_shell_requires_exactly_one_form(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        bad = run(t.execute("dev1", _action("device.shell", {})))
        assert bad.status is ReceiptStatus.FAILED
        assert bad.error_code == "invalid_message"

    def test_logcat_routes(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        fake.on("shell", "logcat", "-d", stdout="11-07 X Y Z\n" * 3)
        receipt = run(t.execute("dev1", _action(
            "device.logcat", {"filter": "WhaleVoip:E", "max_lines": 50})))
        assert receipt.status is ReceiptStatus.SUCCEEDED
        argv = fake.shell_calls("logcat")[-1]
        assert "-t" in argv and "50" in argv and "-s" in argv
        assert "WhaleVoip:E" in argv
        raw, _ = run(t.read_output("dev1", receipt.output_ref))
        assert raw.startswith(b"11-07")

    def test_logcat_since_quoted(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        fake.on("shell", "logcat", "-d", stdout="x\n")
        receipt = run(t.execute("dev1", _action(
            "device.logcat", {"since": "11-07 04:50:00.000"})))
        assert receipt.status is ReceiptStatus.SUCCEEDED
        argv = fake.shell_calls("logcat")[-1]
        # since 含空格：必须带嵌入引号整体传入（adb 空格拼接实证，见模块 docstring）
        assert [a for a in argv if a.startswith('"')] == ['"11-07 04:50:00.000"']
        assert "11-07 04:50:00.000" not in argv  # 裸值会被远端 sh 拆成两个词
        bad = run(t.execute("dev1", _action(
            "device.logcat", {"since": "11-07; reboot"})))
        assert bad.status is ReceiptStatus.FAILED

    def test_logcat_bad_filter(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        receipt = run(t.execute("dev1", _action(
            "device.logcat", {"filter": "a; rm -rf /"})))
        assert receipt.status is ReceiptStatus.FAILED
        assert receipt.error_code == "invalid_message"

    def test_app_list(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        receipt = run(t.execute("dev1", _action("device.app.list", {})))
        assert receipt.status is ReceiptStatus.SUCCEEDED
        raw, _ = run(t.read_output("dev1", receipt.output_ref))
        doc = json.loads(raw.decode())
        assert doc["packages"] == ["com.a", "com.b"] and doc["count"] == 2


# ---------------------------------------------------------------------------
# 6. OutputStore 分页与 TTL
# ---------------------------------------------------------------------------

class TestOutputStore:
    def test_pagination(self):
        store = OutputStore(ttl_s=600)
        ref = store.put("agent:out:act_1", b"x" * 100)
        c1, t1 = store.read(ref, offset=0, max_bytes=30)
        c2, t2 = store.read(ref, offset=30, max_bytes=30)
        c3, t3 = store.read(ref, offset=60, max_bytes=30)
        c4, t4 = store.read(ref, offset=90, max_bytes=30)
        assert (len(c1), t1) == (30, True)
        assert (len(c2), t2) == (30, True)
        assert (len(c3), t3) == (30, True)
        assert (c4, t4) == (b"x" * 10, False)
        # offset 越界 → 空且不再截断
        assert store.read(ref, offset=200, max_bytes=10) == (b"", False)

    def test_unknown_ref(self):
        store = OutputStore()
        assert store.read("agent:out:ghost", max_bytes=10) == (b"", False)

    def test_ttl_expiry(self):
        now = [1000.0]
        store = OutputStore(ttl_s=600, clock=lambda: now[0])
        ref = store.put("agent:out:act_2", b"data")
        assert store.read(ref)[0] == b"data"
        now[0] += 601  # 超过 TTL
        assert store.read(ref) == (b"", False)

    def test_transport_read_output_pagination(self):
        fake = FakeAdbRunner()
        t = _wired(fake)
        receipt = run(t.execute("dev1", _action("device.app.list", {})))
        assert receipt.output_ref
        c1, t1 = run(t.read_output("dev1", receipt.output_ref,
                                   offset=0, max_bytes=5))
        assert len(c1) == 5 and t1 is True


# ---------------------------------------------------------------------------
# 7. 真机冒烟（CI 跳过：WHALE_ADB_SMOKE=1 才运行；主入口是 scripts/adb_smoke.py）
# ---------------------------------------------------------------------------

SMOKE_DEVICES = {
    "leaf5": "100.92.91.85:5555",
    "nova": "100.102.110.11:5555",
}
ADB_PATH = "/opt/android-sdk/platform-tools/adb"


@pytest.mark.smoke
@pytest.mark.skipif(os.environ.get("WHALE_ADB_SMOKE") != "1",
                    reason="真机冒烟：设置 WHALE_ADB_SMOKE=1 启用")
def test_smoke_real_devices():
    from scripts.adb_smoke import smoke_device  # noqa: PLC0415

    async def main():
        t = AdbTransport(SMOKE_DEVICES, adb_path=ADB_PATH)
        reports = []
        for device_id in SMOKE_DEVICES:
            reports.append(await smoke_device(t, device_id))
        return reports

    reports = run(main())
    for report in reports:
        assert report["ok"], f"{report['device_id']} 冒烟失败: {report}"
