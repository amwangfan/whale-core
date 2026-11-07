# Whale Core · 开发者与 AI 协作指南

> 面向维护者、贡献者和编程 Agent。第一次了解项目请先读 [README](../README.md)；接口与运行细节以代码、测试及对应契约为准。
>
> 更新：2026-11-06。本文是持续维护的代码导航，不再固定旧 commit 的测试通过数；历史阶段记录位于 [archive/](archive/2026-10-30/)。

## 1. 定位与边界

鲸鱼是可持续记忆、主动互动并使用外部能力的 AI 伴侣 / 自主体，不限于电子女友或聊天机器人。人格负责表达与偏好，意图和编排负责做事，插件和边缘设备提供行动能力。

- **whale-core**：Python 智能层、会话/记忆、意图和工具编排、主动行为、设备管理、管理 API 与网页控制台。
- **[whale-mobile](https://github.com/amwangfan/whale-mobile)**：Android 常驻节点、感知、通知、VoIP、眼镜桥、UI 操作和专用 Root 手机执行端。
- **[whale-io](https://github.com/amwangfan/whale-io)**：QQ/WeQ 与桥接相关集成和运维记录；手机功能的开发事实源是 mobile，不回填历史副本。

目前主要服务单 owner；核心 Python 3.11+，管理端是 ThreadingHTTPServer + SSE，网页是 React/TypeScript/Vite。模型采用兼容端点及角色路由；需要外部服务的功能须配置真实连接。不要凭目录名字或旧阶段文档判断功能已上线。

## 2. 运行链路

```text
QQ / 网页 / 手机 / 眼镜 / 电话
  → 渠道入口与设备网关（life/、api/、devices/）
  → ConversationOrchestrator + 意图路由
  → 人格、记忆、工具 / PluginGateway / 模型 provider
  → 任务轨迹、动作回执、记忆写回与结果投递
  → 渠道反馈、设备通知或网页展示
```

重点区分：

- **对话**：`core/companion.py`、`life/expression.py`、`life/response_planner.py`、`life/group_length.py`。群/私聊表达可不同，别为了“像真人”堆叠重复提示词。
- **做事**：`core/runtime/` 的意图、工具调度、任务持久化与恢复；`life/do_request.py` 当前只对简单结果图等明确请求做快捷闭环，其他任务仍走既有编排。
- **投递**：`life/result_delivery.py` 处理 QQ 图文和离线暂存；只有实际送达才应在用户提示中称为 delivered；当前离线 staging 仍可能返回 ok，需结合 stub 详情辨别，不能当作手机图库送达。
- **设备**：`devices/gateway.py` 下发动作并对账；`devices/agent_loop.py` 已实现 observe→decide→control/shell 及模拟测试，`DeepSeekDecisionPort` 目前尚未接入常驻生产入口。
- **设备动作接口**：`devices/actions.py` 集中维护动作端口、不可用异常和回执状态；跨仓入口与边界见 [Core/Mobile 互联说明](core-mobile-integration.md)。
- **主动互动**：`life/proactive_greeting.py` 和 `initiative/` 的启停、时机、额度与回执；不要把判断模型和回复模型混成同一条固定链。

## 3. 代码地图

| 需求 | 优先阅读 |
| --- | --- |
| 配置、CLI、管理与网页 | `src/whale_core/configuration.py`、`api/server.py`、`cli/`、`web/src/` |
| 会话、工具任务与恢复 | `core/runtime/`、`core/intent_router.py`、`core/companion.py` |
| 人格、聊天风格、关系 | `life/expression.py`、`life/response_planner.py`、`life/group_length.py`、`ui/persona.py` |
| 记忆、空间、写回 | `memory/`、`life/` 中的绑定与事件处理 |
| 插件及模型供给 | `plugins/`、`plugins/search_web/`、`plugins/qweather/`、`plugins/weq_channel/` |
| 设备管理 / Root 自主体 | `devices/registry.py`、`devices/gateway.py`、`devices/agent_loop.py`、`api/devices_api.py` |
| QQ、主动问候、结果图与投递 | `examples/qq_live_workflow.py`、`life/qq_workflow.py`、`life/proactive_greeting.py`、`life/do_request.py`、`life/result_delivery.py` |
| 眼镜、语音、媒体 | `api/glasses.py`、`api/voice.py`、`plugins/` 中的 Realtime / ASR / TTS / 媒体模块 |
| 端云协议 | `contracts/v1/`、`docs/devices.md`；canonical 以 mobile 的同版本契约为准 |
| 部署与实测记录 | `docs/deployment.md`、`docs/media-and-voice.md`、`docs/eng-priority-20261105.md`、`tests/` |

## 4. 工程约定：只保留真正必要的边界

1. **尊重明确授予的能力**：专供鲸鱼操作的 Root 手机可以持续授权 Shell、观察和 UI 动作；日常主手机按自己的设备 scope 和确认设置处理操作，普通通知等常规能力不人为添加多余审批。不要把服务器 Bubblewrap Shell 的限制错误套到 Android Root 自主体。
2. **实现优先于声明**：不为“免责”添加无效拒绝、过度的硬编码禁忌或多层审批；确实生效的身份、scope、密钥保护、任务幂等与停止机制按现有契约保留。
3. **记录真实结果**：`accepted`、`executed`、`unknown` 含义不同；服务器 `staging` 也不等于已送达。超时不代表未执行；对可能产生副作用的动作，不得盲目换幂等键重发。
4. **来源和命令分开**：群消息、网页、屏幕 OCR、记忆和工具输出可供模型理解，不因此获得 owner 身份或授权。具体动作仍经明确注册的执行入口。
5. **凭据和个人数据留在指定存储**：不要把真实 token、密钥、私有语音/截图正文写进仓库、普通日志或 PR。记忆的删除、备份与导出保持真实语义。
6. **简化而不破坏**：优先移除重复检查、过时分层和无意义限制；确实影响执行正确性和用户控制权的限制用少量可测试机制表达。

## 5. 本地开发与验收

```bash
python -m pip install -e ".[dev,voice]"
python -m pytest tests -q
pnpm -C web install --frozen-lockfile
pnpm -C web test
pnpm -C web build
python scripts/contract_sync_check.py --core . --mobile ../whale-mobile
```

按改动范围选测，再运行相应全量/契约检查。Core CI 入口在 `.github/workflows/`；不要把离线单测当作真机、QQ 真账号或外部供应商联调。执行发行检查参考 `docs/deployment.md`。

## 6. 常见改动的落点

- 人格不自然、机械问候：先看 `life/expression.py`、`response_planner.py`、`group_turn.py` 与相关测试；优先修节奏、上下文和动作选择，少堆约束词。
- 模型路由、工具与任务：`core/intent_router.py` → `core/runtime/` → 插件注册与 Gateway，保持调用方/执行方契约一致。
- 专用 Root 手机自主操作：Core `devices/agent_loop.py` + `gateway.py`，Mobile `modules/agent/`；先跑模拟与协议检查，再测设备真实回执。
- QQ 或结果发不出去：`plugins/weq_channel/weq_send.py`、`life/result_delivery.py`、`examples/qq_live_workflow.py`；保留真实 sequence / 送达状态。
- 记忆和主动互动：`memory/`、`life/proactive_greeting.py`、`initiative/`；避免不同会话和设备空间串写。
- 新插件：沿 `plugins/manifest.py` → `registry.py` → `manager.py` → `gateway.py` 现有路径，不为单一功能复制一套系统。

## 7. 当前仍需联调的部分

- Android 自主体编排代码与模拟用例已存在，但未找到生产运行入口对 `DeviceAgentLoop` / `DeepSeekDecisionPort` 的实际实例化；Root 手机完整 Bilibili 任务链仍要实测。
- ResultDelivery 已支持 QQ 私聊图文，离线手机图库仍是 staging / ADB 探测，不是完整 mobile 图库 API。
- 各模型/天气/语音/媒体供应商与眼镜、手机的可用状态，分别查看测试记录和实际部署配置；不要把写在文档中的示例开关当作已启用。
- 真机设备、外部账号、私密环境与部署过程不由离线测试自动覆盖。

## 8. 与编程 AI 协作

- 先读取目标分支、相关源码、测试和现有 Issues；能直接修就修，不为显而易见的任务反复确认。
- 用户重视创造与体验；按具体问题推进，减少冗长的免责、拒绝和法律报告式文本。不要以节省编程 Token 为由跳过必要的验证。
- 跨仓更改先对齐协议，再分别改 Core / Mobile；小步本地提交、最后按仓库提交一个便于审阅的 PR。
- 测试、联调和部署是不同动作；明确告知实际执行了什么。涉及正式环境操作时按用户指令推进，不擅自改生产账号与数据。
- 交付时说明行为变化、主要文件、测试结果，以及尚未完成的真实接线；避免无价值的重复风险清单。

## 9. 文档维护

- `README.md` / `README_EN.md`：对外用途、能力、安装、真实现状，双语同步。
- 本指南：代码地图、设计分工与 AI 协作；功能和目录改变时更新，不固化某次测试数。
- `docs/README.md`：文档索引；`docs/devices.md`、`docs/contracts.md`、`docs/media-and-voice.md` 与 `docs/deployment.md` 分别维护协议、能力与操作细节。
- `docs/archive/` 保存历史，不把过去阶段的限制自动继承为新版本要求。
