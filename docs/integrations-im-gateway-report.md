# IM Gateway 抽象与 QQ dry-run provider — 交付报告

分支 main，未 commit；全部为新增文件，未改动 contracts/、devices/、life/ 任何现有文件。

## 文件清单

| 文件 | 职责 |
| --- | --- |
| `src/whale_core/integrations/__init__.py` | 集成域入口包 |
| `src/whale_core/integrations/im/__init__.py` | 公开导出口（模型/网关/路由/审批桥/投递） |
| `src/whale_core/integrations/im/models.py` | ImMessage / OutboundMessage / ApprovalRequest / ApprovalResolution / Command / DeadLetter；approval_id 用 `devices/transports/base.py` 的 `new_id("aprv")`；`parse_command` |
| `src/whale_core/integrations/im/router.py` | SessionBindingStore 接口 + 内存实现 + SessionRouter（resolve 缺则建 / reset 即 /new） |
| `src/whale_core/integrations/im/gateway.py` | ImGateway（provider 注册、入站三分类路由、命令分发）、ImProvider / TaskControlPort 协议 |
| `src/whale_core/integrations/im/approval_bridge.py` | ApprovalBridge：创建→投递→核销→回调；一次性、懒过期+主动清理、群聊拒绝、JSONL 审计 |
| `src/whale_core/integrations/im/delivery.py` | DeliveryService：send_text/send_attachment/send_approval 统一入口，3 次指数退避，dead-letter 可查询 |
| `src/whale_core/integrations/im/providers/__init__.py` | provider 导出口 |
| `src/whale_core/integrations/im/providers/qq_dryrun.py` | QqDryRunProvider + RecordingOneBotTransport（新建于此，未改 life/qq_onebot.py） |
| `tests/test_im_gateway.py` | 30 个测试 |

## 测试

- 新增：`tests/test_im_gateway.py`，**30 个测试全绿**（0.41s）。
- 回归：`pytest tests`（含 integration）**2202 个全绿**（2190 单测 + 12 集成，190s）。
- 覆盖点：五命令各至少一例（stop/status/steer/model 参数化 + new 单列）；未知命令/缺参提示；私聊绑定稳定与隔离、/new 重建并通知 TaskControlPort；群消息 chat_type=group 可路由；群内命令与群内审批一律拒绝并提示私聊（审批不核销）；审批创建→批准→二次回复「已处理」→显式旧 id 拒绝（新审批不受影响，须新 id 另行批准）→懒过期与 expire_overdue 主动清理（均触发 expired 回调）→JSONL 审计；跨 target 显式引用拒绝；投递重试 3 次指数退避（0.5/1.0s 断言）→死信（attempts=3、error 类型名、可清空）；附件投递；RecordingOneBotTransport JSONL 落盘与失败注入；**socket 守护下全链路零触网**（monkeypatch socket.socket/create_connection/getaddrinfo 为炸点，命令+审批+核销全程跑通且出站全部被记录）；注入队列三种形状（deque/list/Queue）与 drain 语义。

## 复用的现有基建

1. **`life/qq_onebot.parse_onebot_message`**：dry-run provider 入站解析直接复用（私/群判定、自消息过滤、message_id 必填、4000 字截断），与线上一致；`CHANNEL_QQ`、`OneBotTransportError` 一并复用。
2. **`devices/transports/base.py` 的 `new_id`**：approval_id（`aprv_` + ULID，contracts/v2 §3）与会话绑定 id（`conv_`）。
3. **`life/autonomy.ApprovalBook` 的语义参照**（未复用实现）：一次性 + 绑定确定对象 + fail closed。ApprovalBook 是设备动作指纹级 kv 台账（`grant`/`consume` 事务核销），面向 L3 自主动作；IM 审批面向人对自然语言摘要的「批准/拒绝」，匹配键是 target_id+未过期+pending，粒度不同故自带内存台账，语义对齐已写在 approval_bridge 模块 docstring。
4. **`life/temporary_auth` 的群聊纪律参照**（未复用代码）：「群里不 sudo」→ 本实现「群里不核销审批、不执行命令，一律提示私聊」。
5. 测试设施沿用仓库现有 pytest + tmp_path + monkeypatch 风格，未加新依赖。

## 与规划 §7 的偏差

1. **命令面整体收紧到私聊**：§7 只明确「群聊不作审批入口」；本实现把所有斜杠命令（/status 等只读命令含在内）都限制在私聊，群聊仅收通知。理由：与 life/temporary_auth、qq_onebot 默认 accept_group=False 的保守姿态一致；如需放开只读命令，改 `_handle_command` 一处即可。
2. **审批卡片以文本形式投递**：`send_approval` 渲染为结构化文本（摘要/动作/审批号/有效期/回复方式）；真实卡片 UI（按钮）留给各 provider 扩展，OutboundMessage 已预留 attachments/reply_to。
3. **target_id 别名为进程内随机 salt 的 sha256**：进程内稳定、测试可用固定 salt 复现；重启后别名变化（跨重启稳定需持久化别名表，属真实 provider 工作）。conversation_id 用 `conv_` 前缀（contracts §3 未定义该前缀，属 core 内部路由键，非协议 session_id）。
4. **任务执行本体未实现**（按要求）：命令经 TaskControlPort 回调出去，Gateway 只解析与转发。

## 接真实 QQ / Telegram 的扩展点

- **真实 QQ**：实现一个满足 `ImProvider` 协议的 provider——入站侧可直接包 `OneBotHttpListener`/`OneBotInboundBridge`（已鉴权、已配对），出站把 RecordingOneBotTransport 换成 `HttpOneBotTransport` 即可；target_id 别名表改持久化（kv/SQLite），发送失败语义不变（DeliveryService 已统一重试/死信）。QqInboundWorkflow 的 Outbox 草稿链可与本 Gateway 并存：草稿裁决通过后由 Gateway 投递。
- **Telegram**：新建 `providers/telegram.py` 实现 `ImProvider`（channel="telegram"、事件→ImMessage、owns/send），注册到同一 Gateway；路由/审批/投递/命令全部零改动。
- **持久化**：SessionBindingStore 换 kv 实现；ApprovalBridge 的内存台账可同理外置（接口已按锁内操作收敛）。
- **任务本体**：宿主实现 TaskControlPort 五方法对接 core 任务层即可。
