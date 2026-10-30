"""QQ 实况工作流运行器：WeQ SSE 推送 → 配对 → 持久化 inbox → DeepSeek → 待审回复草稿。

W09 的第一条**真实数据**管道（Phase 1 = 收 + AI + 草稿 + 人工审批；Phase 2 =
WeQ Web 真实发送，需显式开启；见 docs/research-weq-deepseek-qq.md §6）。

用法（在 whale-core/ 下）：
    python examples/qq_live_workflow.py run                 # 启动监听（前台，Ctrl+C 退出）
    python examples/qq_live_workflow.py approve <draft_id>  # 人工批准一条草稿（走完整账本）
    python examples/qq_live_workflow.py show                # 查看待审草稿列表
    python examples/qq_live_workflow.py jobs                # 查看入站持久化队列（失败可 requeue）

审批与发送安全（issue #12）：
- 普通 run 一律**人工批准**；环境变量配齐**不会**隐式开启任何自动发送。
- ``--auto-approve`` 只是 **dry-run 演示**：草稿在影子账本走完整审批流并打印，
  不触网、不动真实账本、不占当日额度。
- 无人值守真实发送需要**双开关**：``--live-auto-send`` 且环境 ``QQ_LIVE_AUTOSEND=1``，
  启动打印醒目警告；仍受渠道白名单 / 额度 / 静默策略约束。
- ``approve`` 子命令（人工发起）在 WEQ_WEB_* 配齐时使用真实发送端；否则 console
  stub（如实记 unknown，不冒充受理，不触网）。

风控策略（issue #13，env 覆盖默认值，run 启动打印生效策略、不打印密钥）：
- 主动行为：DAILY_LIMIT（默认 **10**/天）+ QUIET_START/QUIET_END（默认 **22–7** 点）；
- 被动回复（仅回应已配对 owner 入站）：PASSIVE_DAILY_LIMIT（默认 200/天，防跑飞
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
import sys
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

from whale_core.life.channels import OwnerChannelRegistry, OwnerEvent  # noqa: E402
from whale_core.life.initiative import ProactiveOutbox, ProactivePrefs  # noqa: E402
from whale_core.life.qq_workflow import QqInboundWorkflow           # noqa: E402
from whale_core.life.wq_push import WeQSsePushListener              # noqa: E402
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


def risk_prefs() -> ProactivePrefs:
    """风控策略（issue #13 统一口径）：被动回复与主动行为是两套预算。

    - 主动：DAILY_LIMIT 默认 10/天 + QUIET 22–7（与 docs/research §6.5 一致）；
    - 被动回复（仅回应已配对 owner 入站）：PASSIVE_DAILY_LIMIT 默认 200/天为
      防跑飞保险丝（自激循环/风暴），PASSIVE_REPLY_IN_QUIET 默认开——深夜
      owner 来件照常回复（对话延续不是打扰）；豁免**只**作用于被动回复。
    - env 覆盖默认值；生效策略由 cmd_run 启动打印（不打印任何密钥值）。
    发送结果 unknown 不自动重试（账本既有语义），风控信号出现即人工停机。
    """
    from datetime import time as dtime
    return ProactivePrefs(
        enabled=True,
        allowed_channels=frozenset({"direct_message"}),
        daily_limit=int(os.environ.get("DAILY_LIMIT", "10")),
        quiet_start=dtime(int(os.environ.get("QUIET_START", "22"))),
        quiet_end=dtime(int(os.environ.get("QUIET_END", "7"))),
        passive_reply_in_quiet=os.environ.get("PASSIVE_REPLY_IN_QUIET", "1").strip().lower()
        not in ("0", "false", "no", "off"),
        passive_daily_limit=int(os.environ.get("PASSIVE_DAILY_LIMIT", "200")),
    )


def policy_summary(prefs: ProactivePrefs) -> str:
    """启动公示的生效策略（只含策略参数，不含任何密钥）。"""
    return (
        f"[策略] 主动: 上限={prefs.daily_limit}/日 静默={prefs.quiet_start.hour:02d}:00–"
        f"{prefs.quiet_end.hour:02d}:00 | "
        f"被动回复: 上限={prefs.passive_daily_limit}/日 "
        f"静默豁免={'是' if prefs.passive_reply_in_quiet else '否'} | "
        f"审批: 人工逐条（--auto-approve 仅 dry-run 演示；无人值守真实发送需 "
        f"--live-auto-send + QQ_LIVE_AUTOSEND=1 双开关）"
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


def build_send_port(*, live_ok: bool):
    """出站通道选择（issue #12）：真实 WeQ Web 发送端**仅**在 live_ok=True 且
    WEQ_WEB_* 配齐时构建。live_ok 语义 = 本次调用是人工显式批准（approve 子命令）
    或已打开双开关的无人值守发送；环境变量配齐本身**不**改变 run 的审批策略。
    """
    if live_ok and weq_live_configured():
        from whale_core.life.weq_send import WeQWebSendPort
        url = os.environ.get("WEQ_WEB_URL", "").strip()
        uid = os.environ.get("OWNER_UID", "").strip()
        allowed = tuple(
            h.strip() for h in os.environ.get("WEQ_WEB_ALLOWED_HOSTS", "").split(",") if h.strip()
        ) or None
        port = WeQWebSendPort(url, os.environ.get("WEQ_WEB_TOKEN", "").strip(),
                              target_uid=uid,
                              expected_account_uin=os.environ.get("QQ_SELF_UIN") or None,
                              allowed_hosts=allowed)
        # Do not log the configured URL (it may contain sensitive query parameters) or UID.
        print("[出站] WeQ Web 真实发送已启用（地址校验通过；目标固定为已配置 owner）")
        # 按需完整模式窗口（full-mode window）：QQ_FULLMODE_ONDEMAND=1 时包装——
        # 开窗=发送时自动，关窗=空闲超时；跨进程协调只靠 state_file。
        if os.environ.get("QQ_FULLMODE_ONDEMAND", "").strip() == "1":
            from whale_core.life.full_mode_window import (
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
    verdict = shadow_outbox.dispatch(draft_id, risk_prefs(), now=datetime.now(timezone.utc),
                                     port=DryRunSendPort(), explicitly_approved=True,
                                     passive_reply=True)
    print(f"[dry-run 账本] {draft_id}: {verdict.status} ({verdict.reason})"
          f"——真实账本未动、额度未占用")
    return verdict.status


def dispatch_approved(store, draft_id: str, port=None) -> str:
    """人工批准派发：完整真实账本（attempt 先记账，unknown 不自动重试）。"""
    outbox = ProactiveOutbox(store)
    verdict = outbox.dispatch(draft_id, risk_prefs(), now=datetime.now(timezone.utc),
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
    prefs = risk_prefs()
    print(policy_summary(prefs))

    # issue #12：审批/发送模式判定（fail closed）
    live_auto = live_auto_send_enabled(args)
    if args.live_auto_send and not live_auto:
        print("错误：--live-auto-send 还需环境 QQ_LIVE_AUTOSEND=1（双开关防误开，见 --help）。")
        return 2
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
    send_port = build_send_port(live_ok=live_auto)

    # 按需完整模式窗口：启动即关窗定基线（幂等）；closer 只在 runner 主进程起——
    # approve 子进程是独立短命进程，只开窗/续期，关窗由本 closer 跨进程兜底。
    if os.environ.get("QQ_FULLMODE_ONDEMAND", "").strip() == "1":
        from whale_core.life.full_mode_window import FullModeWindowError, WindowedSendPort
        if isinstance(send_port, WindowedSendPort):
            try:
                send_port.window.close()
                print("[完整模式] 启动基线：窗口已关闭（attach 离线，收消息不受影响）")
            except FullModeWindowError as exc:
                print(f"[完整模式] 启动基线关窗失败（closer 将兜底重试）：{exc}")
            send_port.start_closer()
            print(f"[完整模式] 按需窗口 idle={send_port.idle_min:g}min")

    inbox = build_inbox(args.inbox)
    workflow = QqInboundWorkflow(store, build_brain(), prefs=prefs)
    registry = OwnerChannelRegistry(owner_id="owner", bindings=bindings)

    # QQ/WeQ is registered as a channel plugin. The input adapter is consumed here
    # immediately; live unattended sends use PluginGateway + the same workflow Outbox.
    from whale_core.life.qq_weq_plugin import (
        QQ_WEQ_PLUGIN_ID, build_qq_weq_gateway, register_qq_weq_plugin, send_message_intent)
    from whale_core.plugins import PluginRegistry
    plugin_registry = PluginRegistry()
    adapter, _executor = register_qq_weq_plugin(
        plugin_registry, port=send_port, outbox=workflow.outbox, prefs=prefs)

    # 意图模型（可选，默认关）：QQ_INTENT_MODEL=<模型 id> 开启——同 AgentScope
    # 平台的 OpenAI 兼容端点复用 DEEPSEEK_* 凭据；规则优先（显式搜索指令本地
    # 识别零额外调用），LLM 规划用于其余消息。实测选型记录见 gap-analysis。
    intent_model_id = os.environ.get("QQ_INTENT_MODEL", "").strip()
    if intent_model_id:
        from whale_core.core.intent_router import SearchIntentRouter
        from whale_core.core.runtime import ConversationOrchestrator, ToolRegistry
        intent_brain = DeepSeekCompanion(model=intent_model_id)  # 同平台不同模型
        tools = ToolRegistry()
        if (os.environ.get("TAVILY_API_KEY") or os.environ.get("BRAVE_API_KEY")):
            from whale_core.plugins.web_fetch import register_web_fetch
            from whale_core.plugins.web_search import register_web_search, select_provider
            register_web_search(plugin_registry, tools, provider=select_provider(os.environ))
            register_web_fetch(plugin_registry, tools)
            tool_note = "搜索/抓取已挂载"
        else:
            tool_note = "搜索未挂载（无 TAVILY/BRAVE key，搜索意图会诚实报不可用）"
        workflow.orchestrator = ConversationOrchestrator(
            store, workflow.companion,
            intent_model=SearchIntentRouter(model=intent_brain, enable_llm=True),
            tools=tools)
        print(f"[意图] 模型={intent_model_id}（规则优先+LLM 规划；{tool_note}）")
    owner_draft_ids: set[str] = set()
    approved_draft_ids: set[str] = set()
    plugin_gateway = build_qq_weq_gateway(
        plugin_registry, owner_draft_ids=owner_draft_ids,
        approved_draft_ids=approved_draft_ids)
    print(f"[插件] {QQ_WEQ_PLUGIN_ID} channel 已注册（send_message=communicate，网关门控+共享账本）")

    def on_owner(event):
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
        )
        print(f"\n← [owner] conv={event.conversation} msg={event.message_id} "
              f"text={event.text[:60]!r}")
        try:
            result = workflow.handle_owner_event(event)
        except Exception as exc:  # AI/存储失败不打断监听（inbox 模式下按策略重试）
            print(f"  [工作流失败] {type(exc).__name__}")
            raise
        if "skipped" in result:
            print(f"  [闸门] skipped={result['skipped']}")
            return
        print(f"  [AI 草稿] {result['draft_id']} verdict={result['verdict']} ({result['reason']})")
        print(f"  {result['reply'][:300]}")
        if result["verdict"] != "needs_approval":
            return
        if live_auto:
            # The dual-switch unattended mode is the only auto path that can reach
            # the real port; the host authorizes this owner-derived draft explicitly.
            owner_draft_ids.add(result["draft_id"])
            approved_draft_ids.add(result["draft_id"])
            receipt = plugin_gateway.dispatch(send_message_intent(
                event.conversation, result["reply"], draft_id=result["draft_id"]))
            print(f"  [插件网关] {receipt}")
        elif args.auto_approve:
            # --auto-approve remains isolated dry-run: no real Outbox or plugin executor.
            dispatch_dry_run(store, result["draft_id"])
            print(f"  → 真实发送记录：python examples/qq_live_workflow.py approve {result['draft_id']}")
        else:
            print(f"  → 批准发送记录：python examples/qq_live_workflow.py approve {result['draft_id']}")

    def on_unpaired(incoming):
        print(f"[未配对] account={incoming.account} conv={incoming.conversation} "
              f"msg={incoming.message_id}（在 secrets 设 OWNER_QQ={incoming.account} 并重启即可配对）")

    def on_mass(meta):
        print(f"[mass] {meta['chatType']}:{meta['convId']} seq {meta['fromSeq']}→{meta['toSeq']} "
              f"count={meta['count']}（合并事件，不进消息管道）")

    listener = WeQSsePushListener(
        registry, expected_token=token, self_uin=self_uin,
        on_owner_event=on_owner, on_unpaired=on_unpaired, on_mass=on_mass,
        accept_group=args.accept_group, port=args.port, inbox=inbox)
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
        import time
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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
    args = parser.parse_args()
    loaded = load_secrets(Path(args.secrets))
    if loaded:
        print(f"[secrets] 已载入键：{', '.join(loaded)}（值不打印）")
    return args.fn(args)


if __name__ == "__main__":
    # QQ messages and SQLite WAL/SHM files contain private conversation data.
    if os.name == "posix":
        os.umask(0o077)
    raise SystemExit(main())
