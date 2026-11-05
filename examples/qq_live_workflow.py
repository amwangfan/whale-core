"""QQ 实况工作流运行器：WeQ SSE 推送 → 配对 → 持久化 inbox → DeepSeek → 待审回复草稿。

W09 的第一条**真实数据**管道（Phase 1 = 收 + AI + 草稿 + 人工审批；Phase 2 =
WeQ Web 真实发送，需显式开启；见 docs/research-weq-deepseek-qq.md §6）。

用法（在 whale-core/ 下）：
    python examples/qq_live_workflow.py run                 # 启动监听（前台，Ctrl+C 退出）
    python examples/qq_live_workflow.py approve <draft_id>  # 人工批准一条草稿（走完整账本）
    python examples/qq_live_workflow.py show                # 查看待审草稿列表
    python examples/qq_live_workflow.py jobs                # 查看入站持久化队列（失败可 requeue）

生产入口（issue #28）：生产请用 ``whale qq run``（``whale_core.cli.qq`` 把参数
原样转发到本文件的 ``main(argv)``，子命令与双开关语义完全一致，并附加单实例
flock）；本文件同时保留为可运行示例，生产 systemd 当前仍直接指向它
（部署切换由后续统一处理）。

审批与发送安全（issue #12）：
- 普通 run 一律**人工批准**；环境变量配齐**不会**隐式开启任何自动发送。
- ``--auto-approve`` 只是 **dry-run 演示**：草稿在影子账本走完整审批流并打印，
  不触网、不动真实账本、不占当日额度。
- 无人值守真实发送需要**双开关**：``--live-auto-send`` 且环境 ``QQ_LIVE_AUTOSEND=1``，
  启动打印醒目警告；仍受渠道白名单 / 额度 / 静默策略约束。
- ``approve`` 子命令（人工发起）在 WEQ_WEB_* 配齐时使用真实发送端；否则 console
  stub（如实记 unknown，不冒充受理，不触网）。

风控策略（issue #13，env 覆盖默认值，run 启动打印生效策略、不打印密钥）：
- 主动行为：DAILY_LIMIT（默认 **10**/天，**0=不限**）+ QUIET_START/QUIET_END（默认 **22–7** 点）；
- 被动回复（仅回应已配对 owner 入站）：PASSIVE_DAILY_LIMIT（默认 200/天，**0=不限**，防跑飞
  保险丝）+ PASSIVE_REPLY_IN_QUIET（默认 1 = 深夜 owner 来件照常回复——对话延续
  不是打扰；**主动行为永不豁免静默**）。

密钥与身份（都不入仓、不打印值）：
    读取 secrets/qq_workflow.env（KEY=VALUE，# 注释；secrets/ 已 gitignore）：
      WEQ_SSE_TOKEN   必填，与 WeQ「设置 → WeQ 服务 → SSE 推送」的 accessToken 一致
      QQ_SELF_UIN     本机登录的 QQ 号（**必须显式配置**），用于过滤自发消息
      OWNER_QQ        你的主账号 QQ 号（配对所有者）；不填 = 观察模式（只报告未配对来件）
      DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL
                      未填 key 时用 stub 脑验证管道（回复带 [stub] 标记）
      WEQ_WEB_TOKEN / WEQ_WEB_URL / OWNER_UID
                      Phase 2 真实发送三件套；WEQ_WEB_ALLOWED_HOSTS 可选主机白名单
      QQ_LIVE_AUTOSEND  与 --live-auto-send 组成无人值守真实发送的双开关

WeQ 侧前置：桌面端处于任意模式（离线即可），账号已打开，SSE 推送目标指向
    http://127.0.0.1:18797/whale 并填同一 token。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Windows 控制台默认 GBK：不可编码字符（emoji 等）降级替换；重定向到日志时行缓冲，
# 保证后台运行也能实时看到事件。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace", line_buffering=True)
        except Exception:
            pass

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins"))

from whale_core.life.channels import OwnerChannelRegistry, OwnerEvent  # noqa: E402
from whale_core.life.initiative import ProactiveOutbox, ProactivePrefs  # noqa: E402
from whale_core.life.qq_workflow import QqInboundWorkflow           # noqa: E402
from weq_channel.wq_push import WeQSsePushListener              # noqa: E402
from whale_core.memory.store import InMemoryStore                   # noqa: E402

DEFAULT_SECRETS = REPO_ROOT / "secrets" / "qq_workflow.env"
DEFAULT_STORE = REPO_ROOT / ".state" / "qq_workflow.sqlite3"
DEFAULT_INBOX = REPO_ROOT / ".state" / "qq_inbox.sqlite3"


def load_secrets(path: Path) -> list[str]:
    """KEY=VALUE 载入环境（不覆盖已有环境变量）；只返回键名列表，绝不打印值。"""
    loaded: list[str] = []
    if not path.exists():
        return loaded
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


class StubBrain:
    """无 DEEPSEEK_API_KEY 时的占位脑：验证管道，回复显式带 [stub] 标记。"""

    def reply(self, text: str, *, persona: str = "", memories=(), history=()) -> str:
        n_mem = len(list(memories))
        return (f"[stub 脑] 已收到「{text[:40]}」（召回记忆 {n_mem} 条）。"
                f"在 secrets 里填 DEEPSEEK_API_KEY 并重启后由 DeepSeek 生成真实回复。")


def build_brain():
    if os.environ.get("DEEPSEEK_API_KEY"):
        from whale_core.core.deepseek import DeepSeekCompanion
        print("[brain] DeepSeek（base_url=%s, model=%s）" % (
            os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")))
        return DeepSeekCompanion()
    print("[brain] [!] stub 脑（DEEPSEEK_API_KEY 未配置；只验证管道，不产生真实回复）")
    return StubBrain()


def build_store(path_str: str):
    if path_str == ":memory:":
        return InMemoryStore()
    from whale_core.memory.sqlite_store import SqliteMemoryStore
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteMemoryStore(path)


def build_inbox(path_str: str):
    """入站持久化队列（issue #14）：``none`` 显式关闭（仅演示，崩溃会丢消息）。"""
    spec = (path_str or "").strip()
    if not spec or spec.lower() == "none":
        print("[inbox] [!] 持久化队列未启用：崩溃将丢失已 ACK 未处理的消息（仅演示用）")
        return None
    from whale_core.life.inbox import SqliteInbox
    return SqliteInbox(spec)


def risk_prefs(store=None) -> ProactivePrefs:
    """生效主动策略：kv（Studio #42 写入）覆盖 env 默认。store=None 时仅 env。"""
    base = _env_risk_prefs()
    if store is None:
        return base
    from whale_core.life.initiative import load_prefs
    return load_prefs(store, base)


# 进程级双开关状态（issue #59）：cmd_run 启动时判定一次并置位。置位后
# 主动私信（direct_message）经 ProactiveOutbox 按授权自动派发——渠道范围、
# 每日额度、静默时段照常约束；电话仍单独授权 + 逐条人审。
_PROCESS_LIVE_AUTO = False


def _env_risk_prefs() -> ProactivePrefs:
    """风控策略（issue #13 统一口径）：被动回复与主动行为是两套预算。

    - 主动：DAILY_LIMIT 默认 10/天（**0=不限**）+ QUIET 22–7；
      双开关（--live-auto-send + QQ_LIVE_AUTOSEND=1）已启用时 auto_send=True，
      主动私信按该授权自动派发（issue #59），不再逐条人审；
    - 被动回复（仅回应已配对 owner 入站）：PASSIVE_DAILY_LIMIT 默认 200/天为
      防跑飞保险丝（**0=不限**），PASSIVE_REPLY_IN_QUIET 默认开——深夜
      owner 来件照常回复（对话延续不是打扰）；豁免**只**作用于被动回复。
    - env 覆盖默认值；生效策略由 cmd_run 启动打印（不打印任何密钥值）。
    发送结果 unknown 不自动重试（账本既有语义），风控信号出现即人工停机。
    """
    from datetime import time as dtime
    return ProactivePrefs(
        enabled=True,
        allowed_channels=frozenset({"direct_message"}),
        auto_send=_PROCESS_LIVE_AUTO,
        daily_limit=int(os.environ.get("DAILY_LIMIT", "10")),
        quiet_start=dtime(int(os.environ.get("QUIET_START", "22"))),
        quiet_end=dtime(int(os.environ.get("QUIET_END", "7"))),
        passive_reply_in_quiet=os.environ.get("PASSIVE_REPLY_IN_QUIET", "1").strip().lower()
        not in ("0", "false", "no", "off"),
        passive_daily_limit=int(os.environ.get("PASSIVE_DAILY_LIMIT", "200")),
    )


def policy_summary(prefs: ProactivePrefs) -> str:
    """启动公示的生效策略（只含策略参数，不含任何密钥）。"""
    approval = ("主动私信按授权自动派发（双开关已启用；渠道/额度/静默仍约束）"
                if prefs.auto_send else
                "人工逐条（--auto-approve 仅 dry-run 演示；无人值守真实发送需 "
                "--live-auto-send + QQ_LIVE_AUTOSEND=1 双开关）")
    active_cap = "不限" if prefs.daily_limit == 0 else f"{prefs.daily_limit}/日"
    if prefs.passive_daily_limit is None:
        passive_cap = "跟随主动"
    elif prefs.passive_daily_limit == 0:
        passive_cap = "不限"
    else:
        passive_cap = f"{prefs.passive_daily_limit}/日"
    return (
        f"[策略] 主动: 上限={active_cap} 静默={prefs.quiet_start.hour:02d}:00–"
        f"{prefs.quiet_end.hour:02d}:00 | "
        f"被动回复: 上限={passive_cap} "
        f"静默豁免={'是' if prefs.passive_reply_in_quiet else '否'} | "
        f"审批: {approval}"
    )


def weq_live_configured() -> bool:
    """Phase 2 真实发送三件套是否配齐（只判断存在性，不打印值）。"""
    return all(
        os.environ.get(name, "").strip()
        for name in ("WEQ_WEB_TOKEN", "WEQ_WEB_URL", "OWNER_UID")
    )


def live_auto_send_enabled(args) -> bool:
    """无人值守真实发送 = CLI 开关 **且** env 开关，缺一即关（fail closed）。"""
    return bool(getattr(args, "live_auto_send", False)) and \
        os.environ.get("QQ_LIVE_AUTOSEND", "").strip() == "1"


class ConsoleSendPort:
    """无真实出站配置时的 stub：如实打印并返回 False（账本记 unknown，不冒充受理）。"""

    def send(self, draft) -> bool:
        print(f"[Phase-2 未接入] 本应发往 QQ（{draft.channel}）：{draft.text[:200]}")
        return False


class DryRunSendPort:
    """dry-run 发送端（issue #12）：只打印、返回 True（供影子账本）；构造上无网络能力。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def send(self, draft) -> bool:
        self.calls.append(draft.draft_id)
        print(f"[dry-run] 影子发送（不触网、不动真实账本）：{draft.text[:120]}")
        return True


def build_send_port(*, live_ok: bool, group_allow=None):
    """出站通道选择（issue #12）：真实 WeQ Web 发送端**仅**在 live_ok=True 且
    WEQ_WEB_* 配齐时构建。live_ok 语义 = 本次调用是人工显式批准（approve 子命令）
    或已打开双开关的无人值守发送；环境变量配齐本身**不**改变 run 的审批策略。
    """
    if live_ok and weq_live_configured():
        from weq_channel.weq_send import WeQWebSendPort
        url = os.environ.get("WEQ_WEB_URL", "").strip()
        uid = os.environ.get("OWNER_UID", "").strip()
        allowed = tuple(
            h.strip() for h in os.environ.get("WEQ_WEB_ALLOWED_HOSTS", "").split(",") if h.strip()
        ) or None
        # S7 灰度：群白名单（收发同一集合；空 = 不收群也不发群）
        port = WeQWebSendPort(url, os.environ.get("WEQ_WEB_TOKEN", "").strip(),
                              target_uid=uid,
                              expected_account_uin=os.environ.get("QQ_SELF_UIN") or None,
                              allowed_hosts=allowed, allowed_groups=group_allow)
        # Do not log the configured URL (it may contain sensitive query parameters) or UID.
        print("[出站] WeQ Web 真实发送已启用（地址校验通过；目标固定为已配置 owner）")
        # 按需完整模式窗口（full-mode window）：QQ_FULLMODE_ONDEMAND=1 时包装——
        # 开窗=发送时自动，关窗=空闲超时；跨进程协调只靠 state_file。
        if os.environ.get("QQ_FULLMODE_ONDEMAND", "").strip() == "1":
            from weq_channel.full_mode_window import (
                STATE_FILE_DEFAULT, FullModeWindow, WindowedSendPort)
            window = FullModeWindow(
                url, os.environ.get("WEQ_WEB_TOKEN", "").strip(),
                state_file=STATE_FILE_DEFAULT,
                ready_timeout_s=float(os.environ.get("QQ_FULLMODE_READY_TIMEOUT_S", "25")),
                grace_s=float(os.environ.get("QQ_FULLMODE_GRACE_S", "4")))
            port = WindowedSendPort(
                port, window,
                idle_min=float(os.environ.get("QQ_FULLMODE_IDLE_MIN", "10")))
        return port
    print("[出站] [!] console stub（不真实发送；真实端=配齐 WEQ_WEB_* 后走 approve "
          "或 --live-auto-send 双开关）")
    return ConsoleSendPort()


def dispatch_dry_run(store, draft_id: str) -> str:
    """dry-run 派发（issue #12）：影子账本走完整审批流。

    真实账本零写入（attempts/状态/配额都不动），DryRunSendPort 构造上无网络；
    草稿保持 needs_approval，之后仍可人工 approve 真发。
    """
    outbox = ProactiveOutbox(store)
    draft = outbox.get(draft_id)
    if draft is None:
        print(f"[dry-run] 草稿不存在：{draft_id}")
        return "missing"
    shadow = InMemoryStore()
    shadow_outbox = ProactiveOutbox(shadow)
    shadow_outbox.propose(draft)
    verdict = shadow_outbox.dispatch(draft_id, risk_prefs(store), now=datetime.now(timezone.utc),
                                     port=DryRunSendPort(), explicitly_approved=True,
                                     passive_reply=True)
    print(f"[dry-run 账本] {draft_id}: {verdict.status} ({verdict.reason})"
          f"——真实账本未动、额度未占用")
    return verdict.status


def dispatch_approved(store, draft_id: str, port=None) -> str:
    """人工批准派发：完整真实账本（attempt 先记账，unknown 不自动重试）。"""
    outbox = ProactiveOutbox(store)
    verdict = outbox.dispatch(draft_id, risk_prefs(store), now=datetime.now(timezone.utc),
                              port=port or build_send_port(live_ok=True),
                              explicitly_approved=True, passive_reply=True)
    print(f"[账本] {draft_id}: {verdict.status} ({verdict.reason})")
    return verdict.status


def cmd_run(args) -> int:
    if args.auto_approve and args.live_auto_send:
        print("错误：--auto-approve（dry-run）与 --live-auto-send 互斥，请只选择一种模式。")
        return 2
    token = os.environ.get("WEQ_SSE_TOKEN", "")
    if not token:
        print("错误：WEQ_SSE_TOKEN 未配置（secrets 文件或环境变量），监听器 fail closed 拒绝启动。")
        return 2
    self_uin = os.environ.get("QQ_SELF_UIN", "").strip()
    if not self_uin:
        # 公开仓不含任何真实号码：必须显式配置本机登录的 QQ 号。
        # 未配置 = 不启用自发消息过滤（存在自激回路风险），哨兵值不匹配任何真实 UIN。
        print("[配对] [!] QQ_SELF_UIN 未配置：自发消息过滤未启用。"
              "请在 secrets/qq_workflow.env 显式配置本机登录的 QQ 号后重启。")
        self_uin = "__unconfigured__"
    owner_qq = os.environ.get("OWNER_QQ", "").strip()
    bindings = {("qq", owner_qq): "owner"} if owner_qq else {}
    if not owner_qq:
        print("[配对] [!] 观察模式：OWNER_QQ 未配置，所有来件只报告不处理（不会喂 AI）。")
    else:
        print(f"[配对] owner 绑定：(qq, {owner_qq}) → owner")
    store = build_store(args.store)
    # issue #39：持久化行为 Trace（trace.sqlite3 与记忆库同目录）。
    # 纪律：账本写入失败永不阻塞对话链路；不记录消息正文/Prompt/密钥。
    from whale_core.life.trace import TraceLedger
    try:
        _trace_ledger = TraceLedger(
            str(Path(args.store).with_name("trace.sqlite3")))
    except Exception:
        _trace_ledger = None

    def _tbeg(**kw):
        try:
            return _trace_ledger.begin("qq_inbound", **kw) \
                if _trace_ledger else ""
        except Exception:
            return ""

    def _tspan(tid, phase, status, **kw):
        if tid and _trace_ledger:
            try:
                _trace_ledger.span(tid, phase, status, **kw)
            except Exception:
                pass

    def _tfin(tid, status, **kw):
        if tid and _trace_ledger:
            try:
                _trace_ledger.finish(tid, status, **kw)
            except Exception:
                pass

    # ---- 会话自愈（owner 决策 2026-11-01：运行核心不依赖人工/对话介入） ----
    # QQ 账号会话周期性掉线（发送 fail:unknown）时，runner 自行 reopen 账号
    # 并重试一次；仍失败则如实记 failed，等下一波入站再试。冷却 300s 防抖。
    _send_fail_streak = {"n": 0, "last": 0.0}

    def _maybe_recover_channel(tid) -> bool:
        """连续发送失败 ≥2 且过冷却 → 跑 weq reopen 脚本。返回是否尝试恢复。"""
        import subprocess as _sp
        _send_fail_streak["n"] += 1
        now = time.time()
        if _send_fail_streak["n"] < 2 or \
                now - _send_fail_streak["last"] < 300:
            return False
        script = os.environ.get(
            "WHALE_WEQ_REOPEN_SCRIPT", "/opt/whale/scripts/weq-reopen-account.py")
        if not Path(script).is_file():
            return False
        _send_fail_streak["last"] = now
        print(f"  [自愈] 连续发送失败 {_send_fail_streak['n']} 次，"
              f"尝试重连 QQ 账号会话…")
        _tspan(tid, "recovery", "executed", detail="weq reopen")
        try:
            proc = _sp.run(["python3", script], capture_output=True,
                           text=True, timeout=45)
            ok = proc.returncode == 0 and "ok" in (proc.stdout or "")
            print(f"  [自愈] {'成功' if ok else '未生效'}")
            if ok:
                _send_fail_streak["n"] = 0
            return ok
        except Exception as exc:
            print(f"  [自愈] 脚本异常：{type(exc).__name__}")
            return False

    def _send_ok(receipt) -> bool:
        """发送回执统一判定（网关 receipt 字符串或端口 bool）。"""
        if isinstance(receipt, bool):
            return receipt
        return ":fail" not in str(receipt)

    # S1：服务端会话上下文解析器——群里主人不提权、未配对即普通主体；
    # S2：绑定存储接入（会话 → 记忆空间/角色卡，vault 硬拒）。
    from whale_core.life.context_resolver import (
        ConversationContextResolver, pairing_lookup_from_mapping)
    from whale_core.memory.bindings import BindingStore
    from whale_core.memory.consent import MemberConsentStore, MemberMemoryDirectory
    from whale_core.memory.extraction import MemberMemoryExtractor
    from whale_core.memory.spaces import SpaceStore, ensure_owner_working_copy
    from whale_core.life.temporary_auth import TemporaryAuthService
    _spaces = SpaceStore(store)
    _owner_space = ensure_owner_working_copy(store, owner="owner")
    auth_service = TemporaryAuthService(store, owner_id="owner")
    def _owner_convs():
        uid = os.environ.get("OWNER_UID", "").strip()
        out = {"api:api:api", "cli:cli:local"}
        if uid:
            out.add(f"qq:qq_private:private:{uid}")
        return frozenset(out)

    _bindings = BindingStore(store, _spaces, owner_conversations=_owner_convs())
    context_resolver = ConversationContextResolver(
        pairing_lookup=pairing_lookup_from_mapping(bindings),
        bindings=_bindings,
        grants=auth_service,
        owner_id="owner",
        owner_default_space=_owner_space,
        # G2：群友个人记忆（仅成员本人已认证私聊、consent 有效时下发）。
        member_memory=MemberMemoryDirectory(
            _bindings, _spaces, MemberConsentStore(store)))

    def _grant_gate(grant_ids, fingerprint, required_scope=None):
        return any(auth_service.consume_grant_use(
            g, action_fingerprint=fingerprint, required_scope=required_scope)
            for g in grant_ids)
    # issue #12：审批/发送模式判定（fail closed）——先判定双开关再算策略，
    # 让 risk_prefs 拿到进程级 live_auto 状态（issue #59 一致生效链路）。
    global _PROCESS_LIVE_AUTO
    live_auto = live_auto_send_enabled(args)
    _PROCESS_LIVE_AUTO = live_auto
    if args.live_auto_send and not live_auto:
        print("错误：--live-auto-send 还需环境 QQ_LIVE_AUTOSEND=1（双开关防误开，见 --help）。")
        return 2
    prefs = risk_prefs(store)
    print(policy_summary(prefs))
    if live_auto:
        if not weq_live_configured():
            print("错误：无人值守真实发送需配齐 WEQ_WEB_TOKEN/WEQ_WEB_URL/OWNER_UID。")
            return 2
        print("=" * 66)
        print("!! 警告：无人值守真实发送已启用（--live-auto-send + QQ_LIVE_AUTOSEND=1）")
        print("!! 批准范围内的草稿会真实发往 QQ；仍受渠道白名单/额度/静默策略约束。")
        print("=" * 66)
    elif args.auto_approve:
        print("[审批] --auto-approve = dry-run 演示：影子账本走流程，不触网、不占额度；"
              "真实发送用 approve 子命令（人工逐条）。")
    # S7 灰度：群白名单（收发同一集合；空 = 不收群也不发群）
    group_allow = tuple(
        g.strip() for g in os.environ.get("WHALE_QQ_GROUP_ALLOW", "").split(",")
        if g.strip()) or None
    send_port = build_send_port(live_ok=live_auto, group_allow=group_allow)

    # 按需完整模式窗口：启动即关窗定基线（幂等）；closer 只在 runner 主进程起——
    # approve 子进程是独立短命进程，只开窗/续期，关窗由本 closer 跨进程兜底。
    if os.environ.get("QQ_FULLMODE_ONDEMAND", "").strip() == "1":
        from weq_channel.full_mode_window import FullModeWindowError, WindowedSendPort
        if isinstance(send_port, WindowedSendPort):
            try:
                send_port.window.close()
                print("[完整模式] 启动基线：窗口已关闭（attach 离线，收消息不受影响）")
            except FullModeWindowError as exc:
                print(f"[完整模式] 启动基线关窗失败（closer 将兜底重试）：{exc}")
            send_port.start_closer()
            print(f"[完整模式] 按需窗口 idle={send_port.idle_min:g}min")

    inbox = build_inbox(args.inbox)
    # 陌生人/成员被动回复日额度可用 env 覆盖（默认 20；测试期可调大）
    _stranger_limit = int(os.environ.get("WHALE_QQ_STRANGER_DAILY_LIMIT", "20"))
    workflow = QqInboundWorkflow(store, build_brain(), prefs=prefs,
                                 auth_service=auth_service,
                                 context_resolver=context_resolver,
                                 stranger_daily_limit=_stranger_limit)

    def _send_audio_artifacts(audio_ids, *, peer_type: str = "",
                              target: str = "") -> int:
        """把任务产生的音频产物发成 QQ 语音消息；返回实际送达条数。

        2026-11-01 修复：补发场景的语音此前无条件 send_voice（owner 定向），
        群/陌生人请求的语音会落进 owner 私聊——与图片一样按会话定向发送。
        """
        sent = 0
        if not audio_ids or cap.artifacts is None:
            return sent
        for aid in audio_ids[:2]:
            try:
                blob = cap.artifacts.read(aid)
                data = blob[1] if isinstance(blob, tuple) else blob
                if peer_type and target and hasattr(send_port, "send_voice_to"):
                    ok = send_port.send_voice_to(peer_type, target, data,
                                                 filename=f"{aid[:8]}.wav")
                else:
                    ok = send_port.send_voice(data, filename=f"{aid[:8]}.wav")
                print(f"  [语音] {aid[:8]} → {'已发' if ok else '发送失败'}")
                sent += 1 if ok else 0
            except Exception as exc:
                print(f"  [语音] {aid[:8]} 读取失败：{type(exc).__name__}")
        return sent

    def _plan_segments(text, *, is_group, rhythm, anxious=False,
                       familiarity="stranger", conversation_id=""):
        """issue #23：模型规划优先（◆ 分隔/[[沉默]]/[[稍后补充]]），
        无标记回落 split_reply。返回 (bubbles, plan)。
        issue #41：语气/长度目标由确定性信号推导（derive_plan_guidance），
        随计划结构化返回（供 trace/宿主决策；模型不能自报目标）。
        群聊另按时间线 peer 中位推导 soft_ceiling（句界软裁，非硬砍 40 字）。"""
        from whale_core.life.response_planner import (
            derive_plan_guidance, plan_reply)
        guidance = derive_plan_guidance(
            familiarity=familiarity, anxious=anxious, is_group=is_group)
        soft_ceiling = 0
        if is_group and conversation_id:
            try:
                from whale_core.life.group_length import derive_group_length_norm
                soft_ceiling = derive_group_length_norm(
                    timeline.recent(str(conversation_id), limit=40)
                ).soft_ceiling
            except Exception:
                soft_ceiling = 0
        plan = plan_reply(text, is_group=is_group,
                          max_segments=rhythm.effective_max_segments(
                              is_group=is_group),
                          soft_ceiling=soft_ceiling,
                          **guidance)
        if plan.follow_up:
            # 「稍后补充」是意图声明：只有存在服务端可信的未完成任务/事件
            # 时才可能真的续聊（#20）；此处只记录，不产生任何后台行为。
            print(f"  [规划] 模型声明稍后补充：{plan.follow_up[:40]}"
                  f"（无任务关联则不续聊）")
        if plan.planner == "model":
            print(f"  [规划] 模型规划 {len(plan.bubbles)} 个气泡")
        return plan.bubbles, plan

    # issue #17：原请求 message_id → 其回合 epoch；任务补发以此为基线，
    # 不在发送前重读 current_epoch 把中途新消息吞进基线。
    turn_epoch_by_msg: dict[str, int] = {}

    # 两段式补发的顺序门闩（2026-11-02 生产实证：TTS 只花 8s，补发抢在
    # ack 前面发出去——ack 当时还在等开窗）。补发等主流程（ack 发送侧）
    # 收尾后再发；超时放行（宁可乱序也不丢补发）。
    import threading as _threading
    _turns_inflight: set[str] = set()
    _turns_cv = _threading.Condition()

    def _turn_begin(mid: str) -> None:
        if mid:
            with _turns_cv:
                _turns_inflight.add(mid)

    def _turn_end(mid: str) -> None:
        if not mid:
            return
        with _turns_cv:
            _turns_inflight.discard(mid)
            _turns_cv.notify_all()

    def _await_turn(mid: str, timeout: float = 90.0) -> None:
        if not mid:
            return
        deadline = time.monotonic() + timeout
        with _turns_cv:
            while mid in _turns_inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                _turns_cv.wait(remaining)

    def _note_turn_epoch(message_id: str, epoch: int) -> None:
        if not message_id or not epoch:
            return
        turn_epoch_by_msg[message_id] = epoch
        if len(turn_epoch_by_msg) > 500:  # 有界
            for k in list(turn_epoch_by_msg)[:250]:
                turn_epoch_by_msg.pop(k, None)

    def _send_completion(event, reply: str, audio_ids=(), image_ids=()) -> None:
        """两段式补发：草稿+审批+网关发送（与主回复同一条发送链）。"""
        if not live_auto:
            return
        # 顺序门闩：主流程（ack）还在开窗/发送时等它收尾，补发不抢跑
        _await_turn(str(getattr(event, "message_id", "") or ""))
        conv = str(getattr(event, "conversation", "") or "")
        peer_type = "group" if conv.startswith("group:") else "c2c"
        target = conv.split(":", 1)[1] if ":" in conv else ""
        _is_group = conv.startswith("group:")

        def _note_self_media(kind: str, n: int = 1) -> None:
            """媒体补发也回写群时间线（is_self）——模型随后能在时间线里看到
            自己发过图/语音，群友评论「这图…」时接得上话（2026-11-01 实证：
            图片没进时间线，bot 对评论图片的消息答非所问）。"""
            if not (_is_group and n > 0):
                return
            try:
                timeline.append(conv, message_id=(
                    f"selfmedia:{getattr(event, 'message_id', '')}:{kind}"),
                    sender_id=self_uin, text=f"[{kind}]", is_self=True)
            except Exception:
                pass
        # 语音产物先发（单独一条语音消息），按来件会话定向（修复：此前无条件
        # 发 owner 私聊，群/陌生人请求的语音串到了 owner 那边）。
        _note_self_media("语音", _send_audio_artifacts(
            audio_ids, peer_type=peer_type, target=target))
        # 图片产物：真实图片消息发到补发会话（2026-11-01 修复：此前补发只带
        # 文本里的 /api/v1/artifacts/ 本地路径，群/私聊外部根本打不开）。
        if image_ids and cap.artifacts is not None:
            _img_sent = 0
            for aid in image_ids[:2]:
                try:
                    blob = cap.artifacts.read(aid)
                    data = blob[1] if isinstance(blob, tuple) else blob
                    ok = send_port.send_image_to(
                        peer_type, target, data, filename=f"{aid[:8]}.png") \
                        if hasattr(send_port, "send_image_to") else False
                    print(f"  [补发图片] {aid[:8]} → {peer_type}:{target[:12]}… "
                          f"{'已发' if ok else '发送失败'}")
                    _img_sent += 1 if ok else 0
                except Exception as exc:
                    print(f"  [补发图片] {aid[:8]} 读取失败：{type(exc).__name__}")
            _note_self_media("图片", _img_sent)
            # 文本里的媒体引用一律净化成 [图片]（plan 层已做；这里保底）
            import re as _re
            reply = _re.sub(r"!\[[^\]]*\]\([^)\n]+\)", "[图片]", str(reply))
            reply = _re.sub(
                r"https?://[^\s)\]）>\"']*(?:oss[-\w]*\.aliyuncs\.com|dashscope)"
                r"[^\s)\]）>\"']*", "[图片]", reply)
            reply = _re.sub(r"/api/v1/artifacts/[A-Za-z0-9_-]+", "[图片]",
                            reply).strip()
            if not reply:
                return
        draft_id = f"qq-followup:{event.message_id}"
        from whale_core.life.initiative import ProactiveDraft
        from whale_core.life.response_planner import clean_reply_text
        workflow.outbox.propose(ProactiveDraft(
            draft_id=draft_id, text=clean_reply_text(reply),
            reason="两段式慢任务完成后的补发（ack 已发）",
            evidence=(f"qq:{event.conversation}:{event.message_id}",),
            channel="direct_message",
            target_conversation=event.conversation))
        # G3/issue #17：补发也分段 + epoch 校验；基线 = 原请求回合的 epoch
        # （查不到才回落当前值），不把任务执行期间到达的新消息吞进基线。
        from whale_core.life.segments import split_reply
        from whale_core.life.batching import current_epoch, epoch_fresh
        from whale_core.life.rhythm import RhythmStore, rhythm_key_for
        # G4：会话节奏——免打扰开启时压制任务补发（不拦截被动回复）；
        # 分段上限取节奏卡配置（quiet = 单气泡）。
        rhythm = RhythmStore(store).get(
            rhythm_key_for("qq", event.conversation) or "")
        if rhythm.dnd:
            print("  [节奏] 免打扰开启，任务补发跳过")
            return
        ep = turn_epoch_by_msg.get(str(getattr(event, "message_id", "") or "")) \
            or current_epoch(store, event.conversation)
        segments, _plan = _plan_segments(
            reply, is_group=event.conversation.startswith("group:"),
            rhythm=rhythm, conversation_id=event.conversation)
        if _plan.silent:
            print("  [规划] 补发内容被规划为沉默，不发送")
            workflow.outbox.close(draft_id, reason="silent_planned")
            return
        _sent_segs = 0
        for i, seg in enumerate(segments):
            # 2026-11-01 owner 决策：群会话不因后续闲聊截断已开始发送的回复
            if not event.conversation.startswith("group:")                     and not epoch_fresh(store, event.conversation, ep):
                print(f"  [补发] 会话已有新消息，剩余 {len(segments)-i} 段作废")
                break
            seg_id = draft_id if len(segments) == 1 else f"{draft_id}#{i}"
            if len(segments) > 1:
                # 同主回复路径：多段各自入台账，否则网关查不到分段草稿
                workflow.outbox.propose(ProactiveDraft(
                    draft_id=seg_id, text=seg,
                    reason=f"补发多气泡分段 {i+1}/{len(segments)}（主草稿 {draft_id}）",
                    evidence=(f"qq:{event.conversation}:{event.message_id}",),
                    channel="direct_message",
                    target_conversation=event.conversation))
            owner_draft_ids.add(seg_id)
            approved_draft_ids.add(seg_id)
            receipt = plugin_gateway.dispatch(send_message_intent(
                event.conversation, seg, draft_id=seg_id))
            print(f"  [补发] {seg_id} → {receipt}")
            _sent_segs += 1 if _send_ok(receipt) else 0
            if i + 1 < len(segments):
                time.sleep(0.35)
        # 补发文本也回写群时间线（is_self）——与主回复路径同纪律（2026-11-01
        # 实证：群友评论补发的图/内容时，模型在时间线里看不到自己发过什么）。
        if event.conversation.startswith("group:") and _sent_segs:
            try:
                timeline.append(event.conversation,
                                message_id=f"selffc:{event.message_id}",
                                sender_id=self_uin,
                                text="\n".join(segments[:_sent_segs]),
                                is_self=True)
            except Exception:
                pass
        # 2026-11-01 审批积压治理：多段补发主草稿由 #i 分段取代，标 superseded
        if len(segments) > 1:
            workflow.outbox.close(
                draft_id, status="superseded",
                reason=f"由 {len(segments)} 个分段草稿派发（已送达 {_sent_segs} 段）")
    registry = OwnerChannelRegistry(owner_id="owner", bindings=bindings)

    # QQ/WeQ is registered as a channel plugin. The input adapter is consumed here
    # immediately; live unattended sends use PluginGateway + the same workflow Outbox.
    from weq_channel.qq_weq_plugin import (
        QQ_WEQ_PLUGIN_ID, build_qq_weq_gateway, register_qq_weq_plugin, send_message_intent)
    from whale_core.plugins import PluginRegistry
    plugin_registry = PluginRegistry()
    # 2026-11-01 修复：网关口按草稿 target_conversation 路由——群回复发回群里，
    # 不再落进 owner 私聊（修复前 port.send 只认 owner 定向目标）。
    from weq_channel.weq_send import ConversationRoutingPort
    routed_send_port = ConversationRoutingPort(
        send_port, owner_uid=os.environ.get("OWNER_UID", ""))
    adapter, _executor = register_qq_weq_plugin(
        plugin_registry, port=routed_send_port, outbox=workflow.outbox, prefs=prefs)

    # 媒体/语音能力：统一走 configure_optional_capabilities——
    # 支持 WHALE_MEDIA_BASE_URL/WHALE_MEDIA_API_KEY 一个平台全覆盖，也可各能力
    # 专属覆盖（WHALE_IMAGE_*/WHALE_SPEECH_*/WHALE_VIDEO_*）；供应商类型可选
    # dashscope（阿里云百炼 / AgentScope 平台）或 openai。配了端点就注册，
    # 没配完全不出现（无 mock 冒充）。
    from whale_core.core.runtime import ToolRegistry
    from whale_core.plugins.capability_setup import configure_optional_capabilities
    tools = ToolRegistry()
    cap = configure_optional_capabilities(
        plugin_registry, tools, store, env=os.environ,
        artifact_dir=Path(args.store).parent / "artifacts")
    for note in cap.notes:
        print(f"[能力] {note}")
    # Shell ACL：主人 personal 沙箱；QQ 非主人默认拒，sudo 租约可短暂放行
    shell_auto_actions: frozenset[str] = frozenset()
    if os.environ.get("WHALE_SHELL_ENABLED", "").strip() == "1":
        from whale_core.plugins.shell_tool import register_shell, SHELL_AUTO_PAIR
        from whale_core.cloud.shell_policy import ShellPolicy, ShellTier
        ws = Path(args.store).parent / "workspace"
        register_shell(plugin_registry, tools, ws, env=os.environ)
        if ShellPolicy.from_env(os.environ).tier is ShellTier.PERSONAL:
            shell_auto_actions = frozenset({SHELL_AUTO_PAIR})
            print("[能力] 受控终端已挂载（personal：主人沙箱免逐次审批；非主人需 sudo 租约）")
        else:
            print("[能力] 受控终端已挂载（非 personal 档：自由命令仍拒绝）")
    # 图片产物本地字节直发（不经过网络/代理）：端口拿到 artifact 引用时
    # 直接从 ArtifactStore 读字节（穿透 WindowedSendPort 取内层）。
    if cap.artifacts is not None:
        _inner_port = getattr(send_port, "_inner", send_port)
        if hasattr(_inner_port, "send_image"):
            _inner_port.artifact_reader = lambda aid: cap.artifacts.read(aid)

    # issue #57：反应图片/表情素材库（主人显式导入；模型只提语义标签，
    # 选图/去重/频控由素材层完成；无素材或发送失败退回纯文字）。
    from whale_core.life.reaction_media import ReactionMediaStore
    reaction_store = ReactionMediaStore(store, cap.artifacts)

    def _reaction_persona_id(conversation: str) -> str:
        """素材的角色归属口径：会话绑定卡 > 全局活跃卡；取不到 = 全局。"""
        try:
            from whale_core.life.context_resolver import derive_kind
            from whale_core.memory.bindings import BindingStore
            from whale_core.memory.spaces import SpaceStore
            kind = derive_kind("qq", conversation)
            key = f"qq:{kind.value}:{conversation}"
            pid = BindingStore(store, SpaceStore(store)).persona_for(key)
            if pid:
                return pid
        except Exception:
            pass
        return str(store.get("persona:active_card") or "")

    # 意图模型（可选，默认关）：QQ_INTENT_MODEL=<模型 id> 开启；规则优先（显式
    # 搜索/本地生活/shell.safe 本地识别零额外调用）。enable_llm=True 时路由器仍会
    # 对普通闲聊跳过意图 LLM（kind=chat），仅模糊写/媒体等 toolish 才打 kimi；
    # 闲聊回复走 companion/DeepSeek 官模。实测选型见 gap-analysis。
    # 2026-11-01 owner 决策「不走代理」：QQ_INTENT_BASE_URL/QQ_INTENT_API_KEY
    # 可独立于主聊天端点（不同供应商/不同 key 时必备）；缺省回落 DEEPSEEK_*。
    # Shell 审操模型：WHALE_SHELL_MODEL 优先（AgentScope 上的 kimi-k3 等），
    # 未设时回落 QQ_INTENT_MODEL；端点/Key 仍用 QQ_INTENT_*。
    intent_model_id = (
        os.environ.get("WHALE_SHELL_MODEL", "").strip()
        or os.environ.get("QQ_INTENT_MODEL", "").strip())
    if intent_model_id:
        from whale_core.core.deepseek import DeepSeekCompanion  # build_brain 同款局部导入
        from whale_core.core.intent_router import SearchIntentRouter
        from whale_core.core.runtime import ConversationOrchestrator, ToolRegistry
        intent_brain = DeepSeekCompanion(
            model=intent_model_id,
            base_url=os.environ.get("QQ_INTENT_BASE_URL") or None,
            api_key=os.environ.get("QQ_INTENT_API_KEY") or None)
        globals()["_intent_brain_ref"] = intent_brain  # 群定向兜底判定器用
        fallback_search = None
        if (os.environ.get("TAVILY_API_KEY") or os.environ.get("BRAVE_API_KEY")
                or os.environ.get("WHALE_SEARCH_PROVIDER")):
            from search_web.web_fetch import register_web_fetch
            from search_web.web_search import register_web_search, select_provider
            provider = select_provider(os.environ)
            register_web_search(plugin_registry, tools, provider=provider)
            register_web_fetch(plugin_registry, tools)

            def fallback_search(query: str, _provider=provider) -> str:
                results = _provider.search(str(query or ""), limit=5)
                if not results:
                    return "没有找到结果"
                return "\n".join(
                    f"{r.title} — {r.snippet}（{r.url}）" for r in results[:5])

            tool_note = "搜索/抓取已挂载"
        else:
            tool_note = "搜索未挂载（无 TAVILY/BRAVE key，搜索意图会诚实报不可用）"
        if ((os.environ.get("QWEATHER_API_KEY") or "").strip()
                and (os.environ.get("QWEATHER_API_HOST") or "").strip()):
            from qweather.weather import register_qweather
            register_qweather(
                plugin_registry, tools, env=os.environ,
                fallback_search=fallback_search)
            tool_note += "；和风天气已挂载"
        else:
            tool_note += "；和风未挂载（缺 QWEATHER_API_KEY/HOST，天气回落搜索）"
        workflow.orchestrator = ConversationOrchestrator(
            store, workflow.companion,
            intent_model=SearchIntentRouter(model=intent_brain, enable_llm=True, tools=tools),
            tools=tools, context_resolver=context_resolver,
            grant_gate=_grant_gate)
        print(f"[意图] 模型={intent_model_id}（规则优先；闲聊跳过 LLM；模糊写可 LLM；{tool_note}）")

    # 安全审核模型（可选，默认关）：QQ_SAFETY_MODEL=<模型 id> 开启——
    # 回复发送前由该模型复核；有顾虑的草稿强制转人工审批（不自动发）。
    safety_model_id = os.environ.get("QQ_SAFETY_MODEL", "").strip()
    if not safety_model_id and intent_model_id:
        safety_model_id = intent_model_id  # 审核与意图同一插件（owner 决策）
    if safety_model_id.lower() == "off":
        safety_model_id = ""               # 显式关闭
    if safety_model_id:
        from whale_core.core.deepseek import DeepSeekCompanion as _DS
        # 审核模型与意图模型同一端点（QQ_INTENT_* 覆盖生效于两者）
        safety_brain = _DS(model=safety_model_id,
                           base_url=os.environ.get("QQ_INTENT_BASE_URL") or None,
                           api_key=os.environ.get("QQ_INTENT_API_KEY") or None)

        def _review(reply: str) -> str | None:
            prompt = (
                "你是发送前审核员。审核下面这条要发给主人的陪伴机器人回复。"
                "只有两类问题需要拦截：泄露密钥/隐私信息、或明显伤害用户的攻击性内容。"
                "注意：图片/文件链接里的 OSS 签名参数（Expires/OSSAccessKeyId/Signature 等）"
                "是平台生成的临时分享参数，属正常分享链接，不算泄露密钥，不要误拦。"
                "都没问题只回答 OK；有问题用一句话说明顾虑。\n\n待审回复：" + reply[:800])
            try:
                answer = safety_brain.reply(prompt).strip()
            except Exception:
                return None  # 审核服务故障不阻塞正常回复（审核是增强层，非闸门）
            return None if answer.upper().startswith("OK") else answer[:200]

        workflow._safety_reviewer = _review
        print(f"[审核] 安全审核模型={safety_model_id}（发送前复核；顾虑转人工）")
    owner_draft_ids: set[str] = set()
    approved_draft_ids: set[str] = set()
    from weq_channel.qq_weq_plugin import _draft_id_of
    from whale_core.core.runtime import (
        runtime_approval_verifier, runtime_owner_verifier, runtime_shell_grant_verifier)
    from whale_core.plugins import PluginActionPolicy, PluginGateway
    _rt_owner = runtime_owner_verifier(store)
    _rt_approval = runtime_approval_verifier(store)
    _rt_grant = runtime_shell_grant_verifier(store)

    def _combined_owner(intent):
        try:
            if _draft_id_of(intent) in owner_draft_ids:
                return True
        except Exception:
            pass
        return bool(_rt_owner(intent))

    def _combined_approval(intent):
        try:
            if _draft_id_of(intent) in approved_draft_ids:
                return True
        except Exception:
            pass
        return bool(_rt_approval(intent))

    plugin_gateway = PluginGateway(
        plugin_registry,
        PluginActionPolicy(plugin_registry, auto_approve_actions=shell_auto_actions),
        owner_verifier=_combined_owner,
        approval_verifier=_combined_approval,
        grant_verifier=_rt_grant,
    )
    print(f"[插件] {QQ_WEQ_PLUGIN_ID} channel 已注册（send_message=communicate，网关门控+共享账本）")
    if shell_auto_actions:
        print("[插件] shell_sandbox.run_command 已纳入 personal 免审 + sudo grant 路径")
    # 编排器接上真实网关——否则 read_only 工具因 manifest 风险无法核验被拒
    # （unverified-plugin-risk，生产实证：fetch_url 被拒）
    if workflow.orchestrator is not None:
        workflow.orchestrator.gateway = plugin_gateway
    # 两段式慢任务补发（owner 决策 2026-10-31）：ack 先回，结果后台好了再补
    workflow._completion_sender = _send_completion

    # Owner「做 xxx」→ ResultDelivery（私聊结果图等）；群聊不走 do_request 短路。
    # 仅 live_auto 注入真实投递器——人工审批模式下不抢先直发。
    workflow._result_owner_uid = os.environ.get("OWNER_UID", "").strip()
    if live_auto:
        def _result_deliver(items, summary: str = "") -> dict:
            from whale_core.life.result_delivery import (
                ResultDelivery, summarize_receipts)
            owner_uid = os.environ.get("OWNER_UID", "").strip()
            delivery = ResultDelivery(send_port, owner_uid=owner_uid)
            receipts = delivery.deliver(items, summary=summary or "")
            result = summarize_receipts(receipts)
            print(f"  [ResultDelivery] ok={result.get('ok')} "
                  f"channels={result.get('channels')} "
                  f"count={result.get('count')}")
            return result
        workflow._result_deliverer = _result_deliver

    # 2026-11-02 生产实证「视频没消息了」：generate_video 提交即 pending，
    # 外层任务带着「视频还在跑」就交付收口——视频任务无人轮询永远不来。
    # 跟踪器：pending 视频任务 → 后台轮询 → 成了发视频、砸了诚实告知。
    _pending_videos: dict[str, tuple[str, str, float]] = {}

    def _track_video(event, vtask_id: str) -> None:
        conv = str(getattr(event, "conversation", "") or "")
        if conv and cap.video_manager is not None:
            _pending_video[str(vtask_id)] = (
                conv, str(getattr(event, "message_id", "") or ""),
                time.time() + 3600)  # 1h 死线
            print(f"  [视频跟踪] {str(vtask_id)[:8]}… 挂起轮询 ← {conv}")

    workflow._video_pending_hook = _track_video

    def _deliver_video(conv: str, url: str) -> None:
        peer_type = "group" if conv.startswith("group:") else "c2c"
        target = conv.split(":", 1)[1] if ":" in conv else ""
        if not target or cap.artifacts is None:
            return
        try:
            art = cap.artifacts.save_reference(url)
            data = cap.artifacts.read(art.artifact_id)
            blob = data[1] if isinstance(data, tuple) else data
            ok = send_port.send_video_to(peer_type, target, blob,
                                         filename="video.mp4",
                                         width=1280, height=720) \
                if hasattr(send_port, "send_video_to") else False
            if not ok and peer_type == "c2c" and hasattr(send_port, "send_file_to"):
                ok = send_port.send_file_to(peer_type, target, art.path) \
                    if getattr(art, "path", "") else False
            print(f"  [视频补发] → {peer_type}:{target[:12]}… "
                  f"{'已发' if ok else '发送失败'}")
            if ok and peer_type == "group":
                try:
                    timeline.append(conv, message_id=f"selfvideo:{url[-16:]}",
                                    sender_id=self_uin, text="[视频]",
                                    is_self=True)
                except Exception:
                    pass
        except Exception as exc:
            print(f"  [视频补发] 投递失败：{type(exc).__name__}")

    def _video_poller() -> None:
        while True:
            time.sleep(25)
            for tid, (conv, _mid, deadline) in list(_pending_videos.items()):
                try:
                    t = cap.video_manager.poll(tid)
                except Exception:
                    continue
                if t.status == "succeeded" and t.url:
                    _pending_videos.pop(tid, None)
                    _deliver_video(conv, t.url)
                elif t.status in ("failed", "cancelled"):
                    _pending_videos.pop(tid, None)
                    print(f"  [视频跟踪] {tid[:8]}… 生成失败（{t.error or 'provider'}）")
                    try:
                        if conv.startswith("group:"):
                            send_port.send_group_text(conv.split(":", 1)[1],
                                "视频没生成成……供应商那头没跑成，你要的话我换个模型再试")
                        else:
                            send_port.send_c2c(conv.split(":", 1)[1],
                                "视频没生成成……供应商那头没跑成，你要的话我换个模型再试")
                    except Exception:
                        pass
                elif time.time() > deadline:
                    _pending_videos.pop(tid, None)
                    print(f"  [视频跟踪] {tid[:8]}… 超时弃跟")

    import threading as _th
    _th.Thread(target=_video_poller, daemon=True, name="video-poller").start()

    # issue #20：监督任务泵接入既有 Outbox/发送链（不建第二套运行时）。
    # 任务完成草稿经 Outbox 裁决（reactive_completion：深夜不拦），
    # 目标会话从任务授权快照解析（服务端记录，非模型输出）。
    from whale_core.life.supervisor import AgentSupervisor
    from whale_core.life.supervisor_runner import SupervisedTaskRunner
    _supervisor = AgentSupervisor(store, outbox=workflow.outbox)
    supervised_runner = SupervisedTaskRunner(_supervisor,
                                             outbox=workflow.outbox)

    class _FollowupPort:
        """任务续答发送口：按 draft_id → 任务 → authz.conversation 解析目标。"""

        def send(self, draft) -> bool:
            task_id = str(draft.draft_id).removeprefix("task-followup:")
            rec = _supervisor.get(task_id) or {}
            conv = str((rec.get("authz") or {}).get("conversation") or "")
            if not conv:
                print(f"  [任务续答] {draft.draft_id} 无目标会话快照，不发送")
                return False
            receipt = plugin_gateway.dispatch(
                send_message_intent(conv, draft.text, draft_id=draft.draft_id))
            ok = bool(getattr(receipt, "accepted", False))
            print(f"  [任务续答] {draft.draft_id} → {conv} "
                  f"{'已受理' if ok else '被拒'}")
            return ok

    def _pump_followups():
        """每轮入站处理后派发就绪的任务续答（宿主安全策略：QQ 私聊直发
        前已由 send_message_intent 网关裁决；explicitly_approved 不预设）。"""
        try:
            from datetime import datetime as _dt2, timezone as _tz2
            # issue #42：kv 覆盖即时生效（Studio 改主动策略无需重启）
            sent = supervised_runner.dispatch_ready_followups(
                risk_prefs(store), port=_FollowupPort(), now=_dt2.now(_tz2.utc))
            if sent:
                print(f"  [任务续答] 本轮派发 {len(sent)} 条")
        except Exception as exc:
            print(f"  [任务续答] 派发失败 fail-soft: {type(exc).__name__}")
    # 语音产物补发：workflow 的补发回调现在带音频产物 id 列表

    # 启动恢复：上次进程重启掐断的在途任务——未派发的重跑补发、已派发的诚实告知
    def _send_recovery(conversation_id: str, reply: str) -> None:
        if not live_auto or not reply.strip():
            return
        from whale_core.life.initiative import ProactiveDraft
        draft_id = f"qq-recover:{uuid.uuid4().hex[:12]}"
        workflow.outbox.propose(ProactiveDraft(
            draft_id=draft_id, text=reply,
            reason="进程重启打断的在途任务恢复补发",
            evidence=(f"recover:{conversation_id}",), channel="direct_message"))
        owner_draft_ids.add(draft_id)
        approved_draft_ids.add(draft_id)
        receipt = plugin_gateway.dispatch(send_message_intent(conversation_id, reply, draft_id=draft_id))
        print(f"  [恢复补发] {draft_id} → {receipt}")

    def _owner_reply_direct(conversation_id: str, text: str) -> None:
        """主人私聊确定性指令的应答：与恢复补发同款 草稿+审批+网关 链。"""
        draft_id = f"qq-reaction-cmd:{uuid.uuid4().hex[:12]}"
        workflow.outbox.propose(ProactiveDraft(
            draft_id=draft_id, text=text,
            reason="表情素材管理指令的确认回执",
            evidence=(f"qq:{conversation_id}:cmd",), channel="direct_message",
            target_conversation=conversation_id))
        owner_draft_ids.add(draft_id)
        approved_draft_ids.add(draft_id)
        receipt = plugin_gateway.dispatch(send_message_intent(
            conversation_id, text, draft_id=draft_id))
        print(f"  [表情指令] {draft_id} → {receipt}")

    def _handle_reaction_command(event) -> bool:
        """主人私聊的表情素材管理指令（issue #57）。命中并已处理 → True。

        - 存表情：标签1,标签2（须附图；图存私有产物库，字节不进 kv）；
        - 删表情：<media_id>；
        - 表情列表。
        私人聊天图片只在主人显式发指令时入库，绝不自动变成共享素材。
        """
        if not live_auto:
            return False
        text = (event.text or "").strip()
        conv = str(event.conversation)
        if conv.startswith("group:"):
            return False  # 素材管理只在主人私聊（群友无权动素材库）
        if text.startswith(("存表情：", "存为表情：", "存表情:", "存为表情:")):
            raw_tags = text.split("：", 1)[-1] if "：" in text else text.split(":", 1)[-1]
            tags = [t for t in re.split(r"[,，、\s]+", raw_tags) if t]
            images = workflow._attachment_images(event) \
                if getattr(event, "attachments", None) else ()
            if not tags or not images:
                _owner_reply_direct(conv, "存表情要同时附一张图和至少一个标签，"
                                    "比如：存表情：开心（配图）")
                return True
            if cap.artifacts is None:
                _owner_reply_direct(conv, "产物存储没启用，存不了图。")
                return True
            data, mime = images[0]
            # 表情素材长期保留（覆盖产物默认 7 天期，issue #57）
            artifact = cap.artifacts.save_bytes(data, mime,
                                                retention_s=365 * 86400)
            media = reaction_store.add(
                artifact.id, tags,
                persona_id=_reaction_persona_id(conv))
            _owner_reply_direct(
                conv, f"存好了「{', '.join(media.tags)}」（id {media.media_id}）。"
                      "聊天里我应景时会用。")
            return True
        if text.startswith(("删表情：", "删表情:")):
            media_id = text.split("：", 1)[-1] if "：" in text else text.split(":", 1)[-1]
            ok = reaction_store.remove(media_id.strip())
            _owner_reply_direct(conv, "删掉了。" if ok else "没找到这个表情 id。")
            return True
        if text.strip() in ("表情列表", "表情包列表"):
            items = reaction_store.list(
                persona_id=_reaction_persona_id(conv))
            if not items:
                _owner_reply_direct(conv, "还没有表情素材。私聊发图+「存表情：标签」就能存。")
            else:
                lines = [f"- {m.media_id}：{', '.join(m.tags)}"
                         f"{'（全局）' if not m.persona_id else ''}" for m in items]
                _owner_reply_direct(conv, "表情素材：\n" + "\n".join(lines[:20]))
            return True
        return False

    def _maybe_send_reaction(conversation: str, plan) -> None:
        """模型提了反应图意图（[[表情:标签]]）时素材层选图直发（issue #57）。

        无素材/频控中/近期重复/读取或发送失败 → 安静退回纯文字
        （文字气泡已发，不补发不报错）。
        """
        tag = getattr(plan, "reaction_tag", "") or ""
        if not tag or not live_auto:
            return
        try:
            media = reaction_store.pick(
                (tag,), persona_id=_reaction_persona_id(conversation),
                conversation_id=conversation)
            if media is None:
                return
            blob = cap.artifacts.read(media.artifact_id) \
                if cap.artifacts is not None else None
            if not blob:
                return
            artifact, data = blob
            ext = {"image/png": ".png", "image/jpeg": ".jpg",
                   "image/webp": ".webp", "image/gif": ".gif"}.get(
                getattr(artifact, "content_type", ""), ".png")
            peer_type = "group" if conversation.startswith("group:") else "c2c"
            target = conversation.split(":", 1)[1] if ":" in conversation else ""
            ok = send_port.send_image_to(
                peer_type, target, data,
                filename=f"reaction-{media.media_id}{ext}") \
                if hasattr(send_port, "send_image_to") else False
            print(f"  [表情] 「{tag}」→ 素材 {media.media_id} "
                  f"{'已发' if ok else '发送失败'}")
            if ok and peer_type == "group":
                timeline.append(conversation,
                                message_id=f"selfreaction:{uuid.uuid4().hex[:8]}",
                                sender_id=self_uin, text="[图片]", is_self=True)
        except Exception as exc:
            print(f"  [表情] fail-soft：{type(exc).__name__}")

    # 2026-11-03 owner 授权：群里看到的表情包自动收编为自己的
    # （入反应素材库 + QQ 收藏）。私聊图片绝不自动入库（#57 纪律保留）。
    _AUTOSAVE_DAY_CAP = 10            # 全局每日自动收编上限
    _AUTOSAVE_GROUP_MIN_S = 1800      # 每群最小间隔 30min
    _AUTOSAVE_MAX_BYTES = 3 * 1024 * 1024

    def _stickerish(att: dict) -> bool:
        """表情包启发式：summary 带「表情」字样，或小尺寸图/动图。
        手机照片（大图大文件）不命中。"""
        summary = str(att.get("summary") or "")
        if "表情" in summary or "sticker" in summary.lower():
            return True
        w = int(att.get("width") or 0)
        h = int(att.get("height") or 0)
        size = int(att.get("size") or 0)
        small = (0 < max(w, h) <= 600) or (w == 0 and h == 0)
        small_file = 0 < size <= _AUTOSAVE_MAX_BYTES or size == 0
        anim = str(att.get("img_type") or "").lower() in ("gif", "webp")
        return small and small_file and (anim or max(w, h) <= 600)

    def _autosave_gate(conv: str) -> bool:
        """频率闸门：每群间隔 + 全局日上限（kv 记账）。"""
        now = time.time()
        last = store.get(f"reaction:auto:last:{conv}")
        if isinstance(last, (int, float)) and now - float(last) < _AUTOSAVE_GROUP_MIN_S:
            return False
        day = time.strftime("%Y-%m-%d")
        count = int(store.get(f"reaction:auto:day:{day}") or 0)
        return count < _AUTOSAVE_DAY_CAP

    def _autosave_mark(conv: str) -> None:
        store.put(f"reaction:auto:last:{conv}", time.time())
        day = time.strftime("%Y-%m-%d")
        store.put(f"reaction:auto:day:{day}",
                  int(store.get(f"reaction:auto:day:{day}") or 0) + 1)

    def _tag_sticker(data: bytes, mime: str) -> tuple:
        """主模型读图出 1–3 个中文情绪/场景标签；不合适的内容返回 ()。"""
        brain = build_brain()
        out = brain.reply(
            "这是一张群聊表情包。请给 1–3 个简短中文标签（情绪或使用场景，"
            "如 开心/无语/打招呼/庆祝/吐槽），只输出标签、用英文逗号分隔，"
            "不要解释。如果内容不适合公开传播，只输出 NSFW。",
            images=[(data, mime)])
        text = str(out or "").strip()
        if not text or "NSFW" in text.upper():
            return ()
        tags = tuple(t.strip()[:12] for t in re.split(r"[,，、]", text)
                     if t.strip() and "NSFW" not in t.upper())[:3]
        return tags

    def _qq_favorite_add(artifact) -> bool:
        """把素材文件加进小号的 QQ 收藏（best-effort；需完整模式窗口）。"""
        try:
            import glob as _g
            paths = _g.glob(str(cap.artifacts.directory / (artifact.artifact_id + ".*")))
            if not paths:
                return False
            from weq_channel.full_mode_window import FullModeWindow
            url = os.environ.get("WEQ_WEB_URL", "").strip()
            token = os.environ.get("WEQ_WEB_TOKEN", "").strip()
            if not url or not token:
                return False
            window = getattr(send_port, "window", None) or FullModeWindow(
                url, token, state_file="/tmp/whale-fullmode-send.ts")
            window.open()
            import json as _json
            import urllib.request as _u
            payload = {"json": {
                "expectedAccountUin": os.environ.get("QQ_SELF_UIN", ""),
                "action": "add", "path": paths[0]}}
            req = _u.Request(url.rstrip("/") + "/trpc/account.qqActions.favorite",
                             data=_json.dumps(payload).encode(),
                             headers={"Content-Type": "application/json"},
                             method="POST")
            # 发送口已登录持有 cookie；这里独立登录一次拿 cookie
            login = _u.Request(url.rstrip("/") + "/_auth/login",
                               data=_json.dumps({"token": token}).encode(),
                               headers={"Content-Type": "application/json"},
                               method="POST")
            cookie = _u.urlopen(login, timeout=10).headers.get("Set-Cookie", "").split(";")[0]
            req.add_header("Cookie", cookie)
            with _u.urlopen(req, timeout=30) as resp:
                body = _json.load(resp)
            state = ((body.get("result") or {}).get("data") or {}).get("json") or {}
            return str(state.get("state") or "") == "accepted"
        except Exception as exc:
            print(f"  [表情收编] QQ 收藏添加失败 fail-soft：{type(exc).__name__}")
            return False

    def _maybe_autosave_sticker(event, *, self_sent: bool = False) -> None:
        """群里看到的表情包 → 收编为自己的（素材库 + QQ 收藏）。全 fail-soft。"""
        try:
            if self_sent or not live_auto:
                return
            conv = str(getattr(event, "conversation", "") or "")
            if not conv.startswith("group:"):
                return  # 私聊图片绝不自动入库
            atts = [a for a in (getattr(event, "attachments", None) or ())
                    if isinstance(a, dict) and a.get("kind") == "pic" and _stickerish(a)]
            if not atts or not _autosave_gate(conv) or cap.artifacts is None:
                return
            images = workflow._attachment_images(event)
            if not images:
                return
            import hashlib as _hl
            data, mime = images[0]
            sha = _hl.sha256(data).hexdigest()
            if reaction_store.has_sha256(sha):
                return
            tags = _tag_sticker(data, mime)
            if not tags:
                print("  [表情收编] 标签生成失败/不宜传播，跳过")
                return
            artifact = cap.artifacts.save_bytes(data, mime,
                                                retention_s=365 * 86400)
            media = reaction_store.add(artifact.artifact_id, tags,
                                       added_by=f"auto:{conv}")
            _autosave_mark(conv)
            fav = _qq_favorite_add(artifact)
            print(f"  [表情收编] {conv} 新素材 {media.media_id} 标签{list(tags)}"
                  f"{' + QQ收藏' if fav else ''}")
        except Exception as exc:
            print(f"  [表情收编] fail-soft：{type(exc).__name__}")

    if workflow.orchestrator is not None:
        recovered = workflow.orchestrator.recover_interrupted_tasks(
            on_complete=lambda cid, reply, status: _send_recovery(cid, reply))
        if recovered:
            print(f"[恢复] 处理被上次重启打断的任务：{len(recovered)} 个")

    # G3：入站合批——同一人短窗口连发合并为一个处理回合（不同发送者
    # 绝不合并）；epoch 在接受入队时 +1（issue #17），发送前校验作废旧片段（MSG-02）。
    from whale_core.life.batching import (
        MessageBatcher, epoch_fresh)

    def on_owner(event):
        batcher.offer(event)

    def on_unpaired_buffered(incoming):
        batcher.offer(incoming)

    def _flush_batch(batch):
        """合批冲刷：合并同发送者连续消息为一个事件再分发。"""
        last = batch.last_event
        if last is None:
            return
        # issue #17：epoch 已在 offer（消息接受）时 +1 并随批透传
        # （batch.epoch = 冲刷时刻当前值）；发送侧以此为作废基线，
        # AI 处理期间到达的新消息（含尚在合批窗口内的）都使其失效。
        import dataclasses as _dc
        merged_attach = []
        for ev in batch.events:
            merged_attach.extend(getattr(ev, "attachments", ()) or ())
        event = _dc.replace(last, text=batch.text,
                            message_id=batch.message_ids[-1],
                            attachments=tuple(merged_attach))
        if batch.message_ids and len(batch.message_ids) > 1:
            print(f"  [合批] {len(batch.message_ids)} 条合并为一回合 ← {batch.sender_id}")
        # 补发顺序门闩：主流程在途期间，两段式补发等待（见 _await_turn）
        _turn_begin(str(getattr(event, "message_id", "") or ""))
        try:
            if isinstance(last, OwnerEvent):
                _dispatch_owner(event, epoch=batch.epoch)
            else:
                _dispatch_unpaired(event, epoch=batch.epoch)
        finally:
            _turn_end(str(getattr(event, "message_id", "") or ""))
        _pump_followups()

    def _batch_error(key, exc):
        print(f"  [合批] 冲刷回调失败 {key}: {type(exc).__name__}（批次未处理，"
              f"inbox 未 ack 将按策略重投）")

    batcher = MessageBatcher(_flush_batch, store=store, on_error=_batch_error)

    def _dispatch_owner(event, *, epoch: int = 0):
        _note_turn_epoch(str(getattr(event, "message_id", "") or ""), epoch)
        if not adapter.feed(event):
            raise RuntimeError("QQ/WeQ plugin input adapter full")
        plugin_event = next(adapter.poll(), None)
        if plugin_event is None:
            raise RuntimeError("QQ/WeQ plugin input adapter returned no event")
        payload = plugin_event.payload
        event = OwnerEvent(
            owner_id=str(payload["owner_id"]), channel=str(payload["channel"]),
            conversation=str(payload["conversation"]), message_id=str(payload["message_id"]),
            text=str(payload["text"]),
            attachments=tuple(payload.get("attachments") or ()),
            sender_id=str(payload.get("sender_id") or ""),
            sender_uid=str(payload.get("sender_uid") or ""),
            mentions=tuple(payload.get("mentions") or ()),
            reply_to=tuple(payload.get("reply_to") or ()),
        )
        # F-03（红队）：sudo 验证尝试的明文永不进日志（拦截在工作流内，
        # 但宿主打印发生得更早——在这里就脱敏）。
        from whale_core.life.temporary_auth import maybe_intercept_sudo
        shown = "<sudo 验证尝试>" if maybe_intercept_sudo(event.text) is not None \
            else event.text[:60]
        print(f"\n← [owner] conv={event.conversation} msg={event.message_id} "
              f"text={shown!r}")
        # issue #39：Trace 开始（不含正文）
        _tid = _tbeg(conversation_id=event.conversation,
                     principal_id=event.sender_id or "owner",
                     message_id=event.message_id)
        _tspan(_tid, "received", "ok")
        _maybe_autosave_sticker(event)
        # issue #57：主人私聊的表情素材管理指令（存表情/删表情/表情列表）
        # 确定性处理，不进模型（与 记住/忘掉 指令同纪律）。
        if not event.conversation.startswith("group:") \
                and _handle_reaction_command(event):
            _tspan(_tid, "command", "ok", reason_code="reaction_media")
            _tfin(_tid, "replied", summary="表情素材指令已处理")
            return
        # G1/G3.4：群里即使是主人也先裁决「该不该回」——未 @/引用机器人
        # 的群消息只入时间线，不触发回复（主人在群里同样不提权不打扰）。
        if event.conversation.startswith("group:"):
            decision = target_resolver.decide(
                conversation=event.conversation, sender_id=event.sender_id,
                message_id=event.message_id, mentions=event.mentions,
                reply_to=event.reply_to, sender_uid=event.sender_uid)
            if decision.action == "reply":
                _tspan(_tid, "decide", "ok", reason_code=decision.reason_code)
            else:
                # issue #18：quiet/balanced/lively 主动参与裁决（规则先行）。
                from whale_core.life.rhythm import (
                    RhythmStore as _RhythmStore, rhythm_key_for)
                from whale_core.life.reply_target import TurnDecision
                RhythmStore = _RhythmStore
                rhythm0 = RhythmStore(store).get_with_channel_default(
                    rhythm_key_for("qq", event.conversation) or "", "qq")
                join = group_turn_policy.maybe_join(
                    conversation=event.conversation, text=event.text,
                    activity=rhythm0.activity, dnd=rhythm0.dnd,
                    timeline_entries=timeline.recent(event.conversation,
                                                     limit=5))
                if join.action != "join":
                    print(f"  [旁观] 群消息未指向鲸鱼"
                          f"（{decision.reason_code}/{join.reason_code}），"
                          f"仅记录时间线")
                    _tspan(_tid, "decide", "observe",
                           reason_code=join.reason_code or decision.reason_code)
                    _tfin(_tid, "silent")
                    return
                print(f"  [参与] {rhythm0.activity} 档主动接话 ← "
                      f"{event.sender_id}")
                decision = TurnDecision(
                    "reply", f"join:{rhythm0.activity}",
                    target_sender_id=event.sender_id,
                    reply_to_message_id=event.message_id)
                _tspan(_tid, "decide", "ok", reason_code=decision.reason_code)
        else:
            _tspan(_tid, "decide", "ok", reason_code="private")
        try:
            result = workflow.handle_owner_event(event)
        except Exception as exc:  # AI/存储失败不打断监听（inbox 模式下按策略重试）
            print(f"  [工作流失败] {type(exc).__name__}")
            _tspan(_tid, "workflow", "failed", reason_code="model_error",
                   detail=type(exc).__name__)
            _tfin(_tid, "failed")
            raise
        if "skipped" in result:
            print(f"  [闸门] skipped={result['skipped']}")
            _tspan(_tid, "workflow", "skipped", reason_code=result["skipped"])
            _tfin(_tid, "silent")
            return
        print(f"  [AI 草稿] {result['draft_id']} verdict={result['verdict']} ({result['reason']})")
        print(f"  {result['reply'][:300]}")
        # issue #59：auto_send 启用后 inspect 对被动/授权草稿返回 ready（不再是
        # needs_approval）。live-auto 路径对 ready 与 needs_approval 都应继续发送；
        # over_budget / deferred / rejected 等仍静默。
        if result["verdict"] not in ("ready", "needs_approval"):
            _tfin(_tid, "silent",
                  summary=f"verdict={result['verdict']}，未发送")
            return
        if result.get("safety_concern"):
            # 安全审核有顾虑：不自动发送，留人工终审
            print(f"  [安全审核] 顾虑：{result['safety_concern'][:120]} → 不自动发送，待人工 approve")
            _tspan(_tid, "workflow", "skipped", reason_code="safety_review")
            _tfin(_tid, "silent")
            return
        if result.get("delivered_via") == "result_delivery":
            # 结果已由 ResultDelivery 直投（文字摘要+图）；此处只回一条短确认，
            # 避免再把整段摘要当普通气泡重发（图已发过）。
            print(f"  [做事] ResultDelivery 已投递 do={((result.get('do_request') or {}).get('kind'))}")
            if live_auto and result.get("reply"):
                # 短确认仍走网关，便于会话里看见「好了」；失败不阻断（图可能已到）。
                draft_id = result["draft_id"]
                owner_draft_ids.add(draft_id)
                approved_draft_ids.add(draft_id)
                receipt = plugin_gateway.dispatch(send_message_intent(
                    event.conversation, result["reply"], draft_id=draft_id))
                print(f"  [做事确认] {draft_id} → {receipt}")
                _tfin(_tid, "delivered", summary="result_delivery")
            else:
                _tfin(_tid, "delivered", summary="result_delivery-pending-approve")
            return
        if live_auto:
            # 语音产物先发（同步路径的 TTS 任务产物——「语音跟我说」这类），
            # 按来件会话定向（群请求的语音发回群里，不落 owner 私聊）。
            from whale_core.life.qq_workflow import _extract_audio_artifacts
            _conv = str(getattr(event, "conversation", "") or "")
            _send_audio_artifacts(
                _extract_audio_artifacts(result.get("receipts")),
                peer_type="group" if _conv.startswith("group:") else "c2c",
                target=_conv.split(":", 1)[1] if ":" in _conv else "")
            # G3：语义分段多气泡；每段发送前校验 epoch——新消息已到达则
            # 剩余片段作废（MSG-02：旧回复不越过新消息）。
            from whale_core.life.segments import split_reply
            from whale_core.life.batching import current_epoch
            from whale_core.life.rhythm import RhythmStore, rhythm_key_for
            # G4：分段上限取会话节奏卡配置（quiet = 单气泡）。
            rhythm = RhythmStore(store).get_with_channel_default(
                rhythm_key_for("qq", event.conversation) or "", "qq")
            ep = current_epoch(store, event.conversation)
            segments, _plan = _plan_segments(
                result["reply"],
                is_group=event.conversation.startswith("group:"),
                rhythm=rhythm, conversation_id=event.conversation)
            if _plan.silent:
                print("  [规划] 本轮规划为沉默，不发送")
                # 2026-11-01 审批积压治理：沉默轮的草稿终态关闭，不占审批列表
                workflow.outbox.close(result["draft_id"], reason="silent_planned")
                _tspan(_tid, "plan", "skipped", reason_code="silent_planned")
                _tfin(_tid, "silent")
                return
            _sent_segs = 0
            for i, seg in enumerate(segments):
                # 2026-11-01 owner 决策：群里别人接着说 ≠ 截断机器人已开始
                # 的回复（群节奏快，截断表现为「部分完成」残句）；私聊保留。
                if not event.conversation.startswith("group:")                         and not epoch_fresh(store, event.conversation, ep):
                    print(f"  [分段] 会话已有新消息，剩余 {len(segments)-i} 段作废")
                    _tspan(_tid, "send", "skipped", reason_code="epoch_stale",
                           detail=f"剩余 {len(segments)-i} 段作废")
                    break
                seg_id = result["draft_id"] if len(segments) == 1 \
                    else f"{result['draft_id']}#{i}"
                if len(segments) > 1:
                    # 多气泡分段各自需要台账条目：网关 outbox 模式按 draft_id
                    # 查台账，带 #i 后缀的分段 id 未 propose 会 fail:missing
                    # （2026-11-01 生产实证：多段回复全段丢发）。
                    from whale_core.life.initiative import ProactiveDraft as _PD
                    workflow.outbox.propose(_PD(
                        draft_id=seg_id, text=seg,
                        reason=f"多气泡分段 {i+1}/{len(segments)}"
                               f"（主草稿 {result['draft_id']}）",
                        evidence=(f"qq:{event.conversation}:{event.message_id}",),
                        channel="direct_message",
                        target_conversation=event.conversation))
                owner_draft_ids.add(seg_id)
                approved_draft_ids.add(seg_id)
                receipt = plugin_gateway.dispatch(send_message_intent(
                    event.conversation, seg, draft_id=seg_id))
                # 会话自愈：失败后自动 reopen 账号并重试本段一次
                if not _send_ok(receipt) and _maybe_recover_channel(_tid):
                    receipt = plugin_gateway.dispatch(send_message_intent(
                        event.conversation, seg, draft_id=seg_id))
                print(f"  [插件网关] 段{i+1}/{len(segments)} {receipt}")
                # issue #39：回执即真相——gateway 回执含 fail 时如实记 failed
                _ok_send = _send_ok(receipt)
                if _ok_send:
                    _send_fail_streak["n"] = 0
                _tspan(_tid, "send", "executed" if _ok_send else "failed",
                       reason_code="" if _ok_send else "send_failed",
                       detail=f"段{i+1}/{len(segments)}")
                _sent_segs += 1 if _ok_send else 0
                if i + 1 < len(segments):
                    time.sleep(0.35)
            # issue #39：收尾——全段送达 replied；部分送达 partial
            _tfin(_tid, "replied" if _sent_segs == len(segments) else "partial",
                  summary=f"{_sent_segs}/{len(segments)} 段已发送")
            if _sent_segs:
                _maybe_send_reaction(event.conversation, _plan)
            # 2026-11-01 审批积压治理：多气泡主草稿已被 #i 分段取代（分段台账
            # 各自 accepted），主草稿标 superseded 终态，不占审批列表。
            if len(segments) > 1:
                workflow.outbox.close(
                    result["draft_id"], status="superseded",
                    reason=f"由 {len(segments)} 个分段草稿派发（已送达 "
                           f"{_sent_segs} 段）")
            # G1 补全（2026-11-01 生产实证：模型看不到自己回过什么，重复接旧话茬）：
            # 网关路径的群回复也回写时间线（is_self），记录**实际发出的净文本**
            # （分段内容，非带规划标记的原始回复）。
            if event.conversation.startswith("group:") and _sent_segs:
                timeline.append(event.conversation,
                                message_id=f"self:{event.message_id}",
                                sender_id=self_uin,
                                text="\n".join(segments[:_sent_segs]),
                                is_self=True)
        elif args.auto_approve:
            # --auto-approve remains isolated dry-run: no real Outbox or plugin executor.
            dispatch_dry_run(store, result["draft_id"])
            print(f"  → 真实发送记录：python examples/qq_live_workflow.py approve {result['draft_id']}")
        else:
            print(f"  → 批准发送记录：python examples/qq_live_workflow.py approve {result['draft_id']}")

    # S7：陌生人私聊入口。WHALE_QQ_STRANGER_CHAT=1 开受限对话；sudo 验证
    # 尝试无论开关都受理（口令验证本身就是门）。群聊陌生人永不进入。
    stranger_chat = os.environ.get("WHALE_QQ_STRANGER_CHAT", "").strip() == "1"

    def on_unpaired(incoming):
        batcher.offer(incoming)

    def _dispatch_unpaired(incoming, *, epoch: int = 0):
        is_private = str(incoming.conversation).startswith("private:")
        # S7 灰度：白名单群的成员消息也进入受限对话（匿名 ctx + 群上限，
        # resolver 强制；sudo 在群里永不验证）。
        if not is_private and not str(incoming.conversation).startswith("group:"):
            return  # 频道等其他形态：不处理
        # issue #39：Trace 开始（不含正文）
        _tid = _tbeg(conversation_id=str(incoming.conversation),
                     principal_id=str(incoming.account),
                     message_id=str(incoming.message_id))
        _tspan(_tid, "received", "ok")
        _maybe_autosave_sticker(incoming)
        if not is_private:
            # G1：群消息先裁决「该不该回」——未 @/未引用机器人时再过
            # issue #18 的主动参与策略（quiet/balanced/lively）；
            # 仍不命中 = 旁观，只留时间线，不产生回复（防刷屏）。
            decision = target_resolver.decide(
                conversation=incoming.conversation, sender_id=incoming.account,
                message_id=incoming.message_id, mentions=incoming.mentions,
                reply_to=incoming.reply_to, sender_uid=incoming.sender_uid)
            if decision.action != "reply":
                from whale_core.life.rhythm import (
                    RhythmStore as _RS2, rhythm_key_for as _rkf2)
                _rhythm2 = _RS2(store).get_with_channel_default(
                    _rkf2("qq", incoming.conversation) or "", "qq")
                join = group_turn_policy.maybe_join(
                    conversation=incoming.conversation, text=incoming.text,
                    activity=_rhythm2.activity, dnd=_rhythm2.dnd,
                    timeline_entries=timeline.recent(incoming.conversation,
                                                     limit=12))
                if join.action != "join":
                    _tspan(_tid, "decide", "observe",
                           reason_code=join.reason_code or decision.reason_code)
                    _tfin(_tid, "silent")
                    return
                print(f"  [参与] {_rhythm2.activity} 档主动接话 ← "
                      f"{incoming.account}（{join.reason_code}）")
                _tspan(_tid, "decide", "ok", reason_code=join.reason_code)
                if join.reason_code == "repeat_chain" and join.text:
                    # 复读跟风（2026-11-02 owner 决策）：原文复刻直发不过模型；
                    # 仍过安全审核（复读的是群友原文，防链本身带毒）。
                    rep = join.text
                    if workflow._safety_reviewer is not None:
                        concern = workflow._safety_reviewer(rep)
                        if concern:
                            print(f"  [安全审核] 复读文本有顾虑：{concern[:80]} → 不跟")
                            _tspan(_tid, "workflow", "skipped",
                                   reason_code="safety_review")
                            _tfin(_tid, "silent")
                            return
                    group_code = incoming.conversation.split(":", 1)[1]
                    ok = send_port.send_group_text(group_code, rep) \
                        if hasattr(send_port, "send_group_text") else False
                    if not ok and _maybe_recover_channel(_tid):
                        ok = send_port.send_group_text(group_code, rep) \
                            if hasattr(send_port, "send_group_text") else False
                    print(f"  [复读] 跟风 +1 ← {group_code} "
                          f"{'已发送' if ok else '发送失败'}")
                    _tspan(_tid, "send", "executed" if ok else "failed",
                           reason_code="repeat_chain")
                    _tfin(_tid, "replied" if ok else "failed")
                    if ok:
                        timeline.append(incoming.conversation,
                                        message_id=f"self:{incoming.message_id}",
                                        sender_id=self_uin, text=rep,
                                        is_self=True)
                    return
        from whale_core.life.temporary_auth import maybe_intercept_sudo
        if maybe_intercept_sudo(incoming.text) is None and not stranger_chat:
            print(f"[未配对] account={incoming.account} conv={incoming.conversation} "
                  f"msg={incoming.message_id}（在 secrets 设 OWNER_QQ 并重启即可配对；"
                  "或发送 sudo <一次性口令> 申请临时授权）")
            _tspan(_tid, "decide", "skipped", reason_code="unpaired_no_consent")
            _tfin(_tid, "silent")
            return
        try:
            result = workflow.handle_stranger_event(incoming)
        except Exception as exc:
            print(f"  [陌生人处理失败] {type(exc).__name__}")
            _tspan(_tid, "workflow", "failed", reason_code="model_error",
                   detail=type(exc).__name__)
            _tfin(_tid, "failed")
            return
        reply = (result.get("reply") or "").strip()
        if not reply:
            print(f"  [陌生人] skipped={result.get('skipped', 'empty_reply')}")
            _tspan(_tid, "workflow", "skipped",
                   reason_code=str(result.get("skipped", "empty_reply")))
            _tfin(_tid, "silent")
            return
        if result.get("auth_grant"):
            print(f"  [临时授权] 已签发租约 {result['auth_grant']} ← {incoming.account}")
        if not live_auto:
            print(f"  [陌生人回复-未发送] {reply[:120]}")
            _tspan(_tid, "send", "planned",
                   detail="live_auto 未开启：仅规划不真实发送")
            _tfin(_tid, "silent", summary="dry-run 未发送")
            return
        if workflow._safety_reviewer is not None:
            concern = workflow._safety_reviewer(reply)
            if concern:
                print(f"  [安全审核] 陌生人回复有顾虑：{concern[:120]} → 不发送")
                _tspan(_tid, "workflow", "skipped", reason_code="safety_review")
                _tfin(_tid, "silent")
                return
        # 陌生人回复：owner 定向的网关/Outbox 发送口不适用（draft 未入
        # Outbox 账本 + port 锁定 owner 目标）。陌生人/群回复走端口的
        # 回复语义直发方法（目标 = 来件对端/白名单群，已在上方过安全审核）。
        # 2026-11-01 修复：直发也走外层 WindowedSendPort（穿窗），不再解包
        # _inner 绕过完整模式窗口——否则关窗态下陌生人/群回复必发送失败。
        port_inner = send_port
        # G3：分段 + epoch 校验（新消息到达则剩余段作废）
        from whale_core.life.segments import split_reply
        from whale_core.life.batching import current_epoch, epoch_fresh
        from whale_core.life.rhythm import RhythmStore, rhythm_key_for
        ep = epoch or current_epoch(store, incoming.conversation)
        is_group = incoming.conversation.startswith("group:")
        # G4：分段上限取会话节奏卡配置（quiet = 单气泡）。
        rhythm = RhythmStore(store).get_with_channel_default(
            rhythm_key_for("qq", incoming.conversation) or "", "qq")
        segments, _plan = _plan_segments(
            reply, is_group=is_group, rhythm=rhythm,
            conversation_id=incoming.conversation)
        if _plan.silent:
            print("  [规划] 本轮规划为沉默，不发送")
            if result.get("draft_id"):
                workflow.outbox.close(result["draft_id"], reason="silent_planned")
            _tspan(_tid, "plan", "skipped", reason_code="silent_planned")
            _tfin(_tid, "silent")
            return
        _sent_segs = 0
        _send_failed = False
        for i, seg in enumerate(segments):
            if not is_group and not epoch_fresh(store, incoming.conversation, ep):
                print(f"  [分段] 会话已有新消息，剩余 {len(segments)-i} 段作废")
                _tspan(_tid, "send", "skipped", reason_code="epoch_stale",
                       detail=f"剩余 {len(segments)-i} 段作废")
                break
            if is_group:
                group_code = incoming.conversation.split(":", 1)[1]
                ok = port_inner.send_group_text(group_code, seg) \
                    if hasattr(port_inner, "send_group_text") else False
                if not ok and _maybe_recover_channel(_tid):
                    ok = port_inner.send_group_text(group_code, seg) \
                        if hasattr(port_inner, "send_group_text") else False
                print(f"  [群回复] 段{i+1}/{len(segments)} {'已发送' if ok else '发送失败'} ← {group_code}")
            else:
                peer_uid = incoming.conversation.split(":", 1)[1]
                ok = port_inner.send_c2c(peer_uid, seg) \
                    if hasattr(port_inner, "send_c2c") else False
                if not ok and _maybe_recover_channel(_tid):
                    ok = port_inner.send_c2c(peer_uid, seg) \
                        if hasattr(port_inner, "send_c2c") else False
                print(f"  [陌生人回复] 段{i+1}/{len(segments)} {'已发送' if ok else '发送失败'} ← {peer_uid[:12]}…")
            if ok:
                _send_fail_streak["n"] = 0
            _tspan(_tid, "send", "executed" if ok else "failed",
                   reason_code="" if ok else "send_failed",
                   detail=f"段{i+1}/{len(segments)}")
            _send_failed = _send_failed or not ok
            _sent_segs += 1 if ok else 0
            if i + 1 < len(segments):
                time.sleep(0.35)
        _tfin(_tid,
              "failed" if _send_failed and _sent_segs == 0
              else ("replied" if _sent_segs == len(segments) else "partial"),
              summary=f"{_sent_segs}/{len(segments)} 段已发送")
        if _sent_segs:
            _maybe_send_reaction(incoming.conversation, _plan)
        # 2026-11-01 审批积压治理：直发路径不过网关/台账，回复草稿的
        # 「draft」状态会永远悬在审批列表——发送成功后补终态。
        if _sent_segs and result.get("draft_id"):
            workflow.outbox.close(
                result["draft_id"], status="accepted",
                reason=f"直发路径已送达 {_sent_segs}/{len(segments)} 段")
        if is_group and _sent_segs:
            # G1：自己的发言也进时间线（is_self），模型上下文才完整。
            # 记实际发出的净文本（分段拼接），不记带 ◆ 规划标记的原始回复——
            # 否则标记泄漏进时间线，反过来教会模型继续内联用 ◆。
            timeline.append(incoming.conversation,
                            message_id=f"self:{incoming.message_id}",
                            sender_id=self_uin,
                            text="\n".join(segments[:_sent_segs]),
                            is_self=True)

    def on_mass(meta):
        print(f"[mass] {meta['chatType']}:{meta['convId']} seq {meta['fromSeq']}→{meta['toSeq']} "
              f"count={meta['count']}（合并事件，不进消息管道）")

    # G1：群时间线 + 回复目标裁决（self uid 用于识别 @/引用机器人）
    from whale_core.memory.timeline import GroupTimelineStore
    from whale_core.life.reply_target import ReplyTargetResolver
    timeline = GroupTimelineStore(store)
    # 2026-11-01 owner 决策：时间线标签用群名片/昵称，不再是「群友<hash>」
    # 代号（生产实证：机器人称呼群友只会念出一串 ID，"像乱码"）。
    # 昵称仅作展示标签；解析失败自动回退代号，不影响主链路。
    try:
        from weq_channel.name_directory import WeqNameDirectory
        timeline.name_resolver = WeqNameDirectory(store, send_port).resolver()
        print("[昵称] 群成员昵称目录已装配（uin→群名片/昵称，24h 缓存）")
    except Exception as exc:
        print(f"[昵称] 昵称目录装配失败，回退代号标签：{type(exc).__name__}")
    workflow.companion.timeline = timeline
    self_uid = os.environ.get("QQ_SELF_UID", "").strip()
    # issue #15：时间线渲染标注「@我」需要自身 uid
    workflow.companion.self_uid = self_uid
    # issue #23：QQ 会话启用回复规划标记（◆ 多气泡 / [[沉默]] / [[稍后补充]]）
    from whale_core.life.response_planner import PLANNING_HINT
    workflow.companion.planning_hint = PLANNING_HINT
    # 2026-11-01 生产实证：对话模型不知道自身能力会虚假否认（「画图我做不到」）。
    # 按实际挂载的工具补一句能力自述（宿主事实层，非角色卡内容）。
    _cap_names = []
    if tools.for_capability("web.search"):
        _cap_names.append("搜索")
    if tools.for_capability("media.image.generate"):
        _cap_names.append("画图")
    if tools.for_capability("media.video.generate"):
        _cap_names.append("生成视频")
    if tools.for_capability("tts.synthesize"):
        _cap_names.append("发语音")
    if _cap_names:
        workflow.companion.planning_hint += (
            "\n你当前具备这些能力：" + "、".join(_cap_names) +
            "——对方提出这类需求时会有任务流程接住处理，答应下来即可，"
            "不要自称做不到或不会。")
    # 读图能力自述（2026-11-01 生产实证：模型一边真读图一边说「读不了」）——
    # 诚实口径：当条消息附带的图能直接看到；没被 @/没触发回复的旧图看不到。
    workflow.companion.planning_hint += (
        "\n对方当条消息里附带的图片你能直接看到内容并讨论；"
        "别再说自己读不了图——之前看图猜内容都是真看到了，不是瞎猜。")
    # 2026-11-05 owner 明确选 B：群聊人格偏温柔体贴（非 Grok 互损）。
    # 原则与安全仍由系统后台把关；称呼用群名片/昵称。
    workflow.companion.planning_hint += (
        "\n群里都是熟人朋友：语气偏温柔体贴，少攻击性；可以轻轻接梗、关心人，"
        "但不抬杠、不阴阳、不互损怼人，也不端着说教。"
        "提到或称呼群友时直接用他们的群名片/昵称（时间线里 […|名字] 就是），"
        "不要念 ID 或代号。"
        "私聊可以稍亲近一点，但仍偏温柔。"
        "别用客服腔，也别用破折号（——）串成长句那种作文腔；像平时打字一样短句。")
    # issue #57：反应图能力说明只在素材库非空时出现（每轮动态求值；
    # 模型只提语义标签，选图/频控由素材层兜底，发不出就纯文字）。
    _base_planning_hint = workflow.companion.planning_hint

    def _dynamic_planning_hint():
        hint = _base_planning_hint
        try:
            items = reaction_store.list()
            if items:
                # 带上素材库的真实标签——模型写不存在的标签=选不到图
                tag_pool = sorted({t for m in items for t in m.tags})[:12]
                hint += ("\n想在文字之外配一张应景的表情图时，可以在回复结尾写 "
                         "[[表情:标签]]（只能从现有素材标签里挑："
                         + "/".join(tag_pool) +
                         "）；有匹配素材才会真的发出去，别每轮都用。")
        except Exception:
            pass
        return hint

    workflow.companion.planning_hint = _dynamic_planning_hint
    if not self_uid:
        try:
            import glob as _glob, json as _json
            for f in _glob.glob(
                    os.path.expanduser("~/.config/weq/config/accounts/*.json")):
                cfg = _json.load(open(f))
                if str(cfg.get("uin")) == self_uin and cfg.get("uid"):
                    self_uid = str(cfg["uid"])
                    break
        except Exception:
            pass
    target_resolver = ReplyTargetResolver(self_uin=self_uin, self_uid=self_uid)

    # issue #18：群聊主动参与策略（quiet/balanced/lively）。
    # 时间静默判定复用 Outbox 的同一口径：静默时段压制非定向自主插话，
    # 不拦定向被动回复（issue #18/#22 补充）。
    from whale_core.life.group_turn import GroupTurnPolicy

    def _quiet_now() -> bool:
        from whale_core.life.initiative import ProactiveOutbox
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        return ProactiveOutbox._is_quiet(
            _dt.now(ZoneInfo(prefs.tz)).time(), prefs)

    # 从显式选择的角色卡/宿主配置取得称呼，不在核心硬编码角色名。
    from whale_core.ui.persona_cards import active_card
    selected_card = active_card(store)
    bot_aliases = tuple(dict.fromkeys(
        n.strip() for n in (
            (selected_card.name if selected_card else ""),
            *os.environ.get("WHALE_BOT_NAMES", "").split(","),
        ) if n.strip()
    ))

    # 2026-11-02 owner 决策：规则判不出的「是不是在找鲸鱼」由意图模型兜底
    # （实证：「说出来各位的名字哦」无「你」无问号被静默压制，其实是对着
    # 鲸鱼刚发的语音说的）。仅在机器人近窗发过言时调用，调用有界。
    def _llm_directed(text, entries) -> bool:
        brain = globals().get("_intent_brain_ref")
        if brain is None:
            return False
        lines = []
        for e in list(entries or ())[-6:]:
            if not isinstance(e, dict):
                continue
            who = "Enxi" if e.get("is_self") else "群友"
            body = " ".join(str(e.get("text") or "").split())[:80]
            if body:
                lines.append(f"{who}：{body}")
        name = bot_aliases[0] if bot_aliases else "Enxi"
        prompt = (
            f"判断一条群聊消息是不是在对群里名叫「{name}」的机器人说话"
            "（叫她回应、让她做事、接着她的话头说事都算；群友之间互相聊、"
            "单纯陈述不算）。\n"
            + ("群聊最近消息：\n" + "\n".join(lines) + "\n" if lines else "")
            + f"最新一条：{str(text)[:200]}\n只回答一个字：是 或 否。")
        try:
            ans = brain.reply(prompt)
        except Exception:
            return False
        return str(ans or "").strip().startswith("是")

    # 2026-11-02 owner 决策：复读链命中后由意图模型判断「该不该跟」——
    # 应景/好玩的链跟，无意义/广告/ky 的链不跟。
    def _chain_judge(chain_text, entries) -> bool:
        brain = globals().get("_intent_brain_ref")
        if brain is None:
            return True
        lines = []
        for e in list(entries or ())[-8:]:
            if not isinstance(e, dict):
                continue
            who = "Enxi" if e.get("is_self") else "群友"
            body = " ".join(str(e.get("text") or "").split())[:60]
            if body:
                lines.append(f"{who}：{body}")
        name = bot_aliases[0] if bot_aliases else "Enxi"
        prompt = (
            f"群聊里正在复读刷屏这句话：「{str(chain_text)[:60]}」。\n"
            + ("群聊最近消息：\n" + "\n".join(lines) + "\n" if lines else "")
            + f"判断：群里名叫「{name}」的机器人跟着复读一句，是否应景、"
              "好玩、合时宜？（无意义灌水、广告、ky 泼冷水、与当前话题无关的"
              " → 否；应景好玩的 → 是）\n只回答一个字：是 或 否。")
        try:
            ans = brain.reply(prompt)
        except Exception:
            return False
        return str(ans or "").strip().startswith("是")

    try:
        _topic_prob = float(os.environ.get("WHALE_GROUP_TOPIC_JOIN_PROB", "0.18"))
    except ValueError:
        _topic_prob = 0.18
    group_turn_policy = GroupTurnPolicy(
        store, bot_names=bot_aliases, quiet_now=_quiet_now,
        directed_classifier=_llm_directed, chain_judge=_chain_judge,
        topic_join_prob=_topic_prob,
    )

    _member_extractor = MemberMemoryExtractor(
        store, timeline, MemberConsentStore(store), _bindings, _spaces)

    def _on_member_provisioned(principal_id: str, reason: str) -> None:
        """issue #16：成员建档/恢复后的历史回填（有界/幂等/增量）。

        - auto（首次私聊建档）：全量回填当前各群时间线（有界窗口）；
        - optin（「恢复记录我」）：先钉水位再回填——退出期间的消息不补采。
        重复 auto 触发是 no-op（水位 done 即跳过）。
        """
        try:
            if reason == "optin":
                from whale_core.memory.extraction import note_optin_watermark
                note_optin_watermark(store, principal_id)
            stats = _member_extractor.backfill_for_principal(
                principal_id, force=(reason == "optin"))
            if stats.written or stats.scanned:
                print(f"  [成员回填] {principal_id[-6:]}… 扫描 {stats.scanned} "
                      f"写入 {stats.written}（{reason}）")
        except Exception as exc:
            print(f"  [成员回填] 失败 fail-soft: {type(exc).__name__}")

    workflow._on_member_provisioned = _on_member_provisioned

    listener = WeQSsePushListener(
        registry, expected_token=token, self_uin=self_uin,
        on_owner_event=on_owner, on_unpaired=on_unpaired, on_mass=on_mass,
        accept_group=args.accept_group,
        group_allow=frozenset(group_allow) if group_allow else None,
        timeline=timeline,
        # G2：群消息入时间线后自动提炼已授权成员的记忆（未授权 0 写入）
        member_extractor=_member_extractor,
        port=args.port, inbox=inbox)
    try:
        listener.start()
    except OSError as exc:
        print(f"错误：监听 {args.port} 失败：{exc}")
        return 2
    if inbox is not None:
        counts = inbox.counts()
        print(f"[inbox] 持久化队列：pending={counts['pending']} failed={counts['failed']} "
              f"succeeded={counts['succeeded']}；启动收回过期租约={listener.recovered}")
    print(f"[监听] http://{listener.host}:{listener.port}/（等 WeQ SSE 推送；Ctrl+C 退出）")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()
        print(f"\n[停止] 过载丢弃={listener.dropped} 条；租约收回={listener.recovered} 条")
        if inbox is not None:
            inbox.close()
    return 0


def cmd_approve(args) -> int:
    store = build_store(args.store)
    dispatch_approved(store, args.draft_id)
    return 0


def cmd_show(args) -> int:
    store = build_store(args.store)
    outbox = ProactiveOutbox(store)
    ids = [k.split("life:draft:", 1)[1] for k in store.keys() if k.startswith("life:draft:")]
    if not ids:
        print("（没有草稿）")
        return 0
    for draft_id in sorted(ids):
        draft = outbox.get(draft_id)
        ledger = outbox.ledger(draft_id)
        print(f"- {draft_id} [{ledger.get('status', '?')}] {draft.text[:80]!r}" if draft
              else f"- {draft_id} [损坏]")
    return 0


def cmd_jobs(args) -> int:
    """入站持久化队列观测（issue #14）：计数 + 失败清单（不含正文）+ 人工 requeue。"""
    from whale_core.life.inbox import SqliteInbox
    inbox = SqliteInbox(args.inbox)
    counts = inbox.counts()
    print(f"[inbox] {args.inbox}: " + " ".join(f"{k}={v}" for k, v in counts.items()))
    failed = inbox.failed_ids()
    for mid, attempts, err in failed:
        print(f"  - failed msg={mid} attempts={attempts} error={err}")
    if args.requeue_failed:
        n = inbox.requeue_all_failed()
        print(f"[inbox] 已重新入队 {n} 条 failed 任务（下次 run 续处理）")
    inbox.close()
    return 0


def add_shared_options(parser: argparse.ArgumentParser, *, store: bool = False,
                       inbox: bool = False) -> None:
    """Allow deployment paths on either side of a subcommand."""
    parser.add_argument("--secrets", default=argparse.SUPPRESS)
    if store:
        parser.add_argument("--store", default=argparse.SUPPRESS)
    if inbox:
        parser.add_argument("--inbox", default=argparse.SUPPRESS)


def main(argv: list[str] | None = None, *, prog: str | None = None) -> int:
    """运行器入口：解析参数并分派，返回退出码（不自行 sys.exit）。

    ``argv`` 缺省读 ``sys.argv``；``prog`` 供 CLI 入口（whale qq）传入，
    让帮助/错误信息的 prog 名与调用面一致（issue #28）。
    """
    parser = argparse.ArgumentParser(prog=prog, description=__doc__.splitlines()[0])
    parser.add_argument("--secrets", default=str(DEFAULT_SECRETS))
    parser.add_argument("--store", default=str(DEFAULT_STORE),
                        help="记忆/账本存储（sqlite 路径或 :memory:）")
    parser.add_argument("--inbox", default=str(DEFAULT_INBOX),
                        help="入站持久化队列 sqlite 路径（:memory: 或 none 关闭；"
                             "关闭后崩溃会丢已 ACK 消息，仅演示用）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="启动 SSE 监听 + AI 工作流")
    add_shared_options(p_run, store=True, inbox=True)
    p_run.add_argument("--port", type=int, default=int(os.environ.get("WQ_PUSH_PORT", "18797")))
    p_run.add_argument("--accept-group", action="store_true",
                       help="同时接收群消息（默认只收私聊；群内容不可信）")
    p_run.add_argument("--auto-approve", action="store_true",
                       help="dry-run 演示：草稿在影子账本走完整审批流并打印；不触网、"
                            "不动真实账本、不占额度（issue #12）")
    p_run.add_argument("--live-auto-send", action="store_true",
                       help="无人值守真实发送（需同时设 QQ_LIVE_AUTOSEND=1 双开关；"
                            "启动打印警告；仍受白名单/额度/静默约束）")
    p_run.set_defaults(fn=cmd_run)
    p_ap = sub.add_parser("approve", help="人工批准一条草稿（WEQ_WEB_* 配齐则真实发送）")
    add_shared_options(p_ap, store=True)
    p_ap.add_argument("draft_id")
    p_ap.set_defaults(fn=cmd_approve)
    p_ls = sub.add_parser("show", help="列出草稿与账本状态")
    add_shared_options(p_ls, store=True)
    p_ls.set_defaults(fn=cmd_show)
    p_jobs = sub.add_parser("jobs", help="入站持久化队列：计数/失败清单/requeue")
    add_shared_options(p_jobs, inbox=True)
    p_jobs.add_argument("--requeue-failed", action="store_true",
                        help="把所有 failed 任务重新入队（下次 run 续处理）")
    p_jobs.set_defaults(fn=cmd_jobs)
    args = parser.parse_args(argv)
    loaded = load_secrets(Path(args.secrets))
    if loaded:
        print(f"[secrets] 已载入键：{', '.join(loaded)}（值不打印）")
    return args.fn(args)


if __name__ == "__main__":
    # QQ messages and SQLite WAL/SHM files contain private conversation data.
    if os.name == "posix":
        os.umask(0o077)
    raise SystemExit(main())
