# Devices V2 架构设计（第一轮闭环）

> 2026-11-07 · 依据 whale-mobile-core-development-plan-2026-11-07.md 第 14 节。
> 协议事实源：whale-mobile `contracts/v2/PROTOCOL.md`（本仓 contracts 目录为镜像）。
> v1 现状底稿：docs/devices-v1-factsheet.md。

## 1. 目标与边界

第一轮只交付一个真实可演示闭环：

```
IM(QQ 模拟) 发起任务 → Core 理解 → Device Gateway 选通道 → GUI Agent
observe→decide→act→verify（真机）→ 结果回 IM（记录不发送）→ scrcpy 录屏
+ RunTrace JSONL 导出
```

边界：不做 ScreenMemo 整合（Phase 3）、不做聊天 UI（Phase 4）、不做双 APK
（Phase 2）、ntfy 推迟（用户决策）。embedded_wss v2 的端侧配套属于 mobile
大改版，第一轮 Core 侧只为它预留 transport 位。

## 2. 模块切分

```
src/whale_core/devices/
├── registry.py            # 复用 v1：配对/吊销/scope（kv 存储）
├── gateway.py             # v1 WSS 网关保留（生产 v1 设备继续用）
├── gateway_v2.py          # 新：多 transport 编排器（本设计核心）
├── dispatcher.py          # 新：transport 无关的动作派发（幂等/回执/对账/清扫）
├── capabilities.py        # 新：contracts/v2 §5.1 单一能力枚举 + 子集校验
├── agent_loop.py          # v1 循环保留
├── agent_failures.py      # 复用
├── gui_agent/             # 新：observe→decide→act→verify（另立设计）
├── transports/
│   ├── base.py            # DeviceTransport Protocol + 运行时类型（已定稿）
│   ├── adb.py             # ADB 通道（GUI 自动化主力）
│   ├── ssh.py             # SSH 通道（shell/文件/长任务）
│   └── embedded_wss.py    # 占位：mobile v2 客户端就绪后接
└── recording/
    ├── run_recorder.py    # 录屏生命周期（scrcpy 优先）
    └── trace_align.py     # 视频↔JSONL 时间戳对齐

src/whale_core/integrations/im/
├── gateway.py / router.py / approval_bridge.py / delivery.py / models.py
└── providers/qq_dryrun.py  # QQ 模拟（RecordingOneBotTransport，零网络发送）
```

## 3. 关键设计决定

| # | 决定 | 理由 |
| --- | --- | --- |
| V2-D1 | Transport 只负责执行，不持有设备状态真相 | registry 仍是设备/scope 真相；transport 健康另报（计划 §6.2：一种通道断开 ≠ 设备离线） |
| V2-D2 | ActionDispatcher 从 v1 gateway.py 抽出，transport 无关 | v1 最有价值部分（幂等预约/回执状态机/retention sweep），ADB/SSH/WSS 共用 |
| V2-D3 | ADB 为主力 GUI 通道 | 第一轮不需要 mobile 端任何改动即可闭环；embedded WSS 待 mobile v2 |
| V2-D4 | SSH 仅密钥认证 + host key pinning | 计划 §6.4；禁 accept-any |
| V2-D5 | 同一设备可同时注册多 transport | gateway_v2 按能力选路：observe/GUI → adb；shell/文件 → ssh；长在线原生能力 → embedded |
| V2-D6 | 回执状态机全协议一致 | ADB/SSH 本地执行也产出 contracts/v2 §7.2 五终态回执；超时绝不自动重放（沿用 v1 reconcile 纪律） |
| V2-D7 | observation_id/element_id 绑定 + 稳定键重校验在 transport 内 | 端侧/core 侧实现位置不同但语义一致（contracts/v2 §8） |
| V2-D8 | IM 审批一次性、限时、仅私聊 | 计划 §6.9/§13；群聊只通知不审批 |
| V2-D9 | QQ provider 零网络发送 | 用户决策：模拟模式；RecordingOneBotTransport 记录全部出站供断言 |
| V2-D10 | run_id 贯穿 task/trace/录像 | 计划 §9.3：视频与步骤按时间戳对齐的前提 |
| V2-D11 | 通知走 Whale 原生系统：WSS 首选送达 + Android NotificationManager 展示；ntfy/Bark/PushDeer 仅为可选外部出口 | 原生通知修正方案 2026-11-07；contracts/v2 §13 通知子协议（原方案 §8.3 写 v1，适配决策：落入 v2，v1 已冻结） |

## 4. gateway_v2 选路规则

```
execute(device_id, action):
    caps_needed = CAPABILITY_OF[action.capability]
    candidates = transports_where(device online AND capability declared)
    优先级：embedded_wss > adb > ssh（GUI 类）
            ssh > embedded_wss > adb（shell/文件类）
    选中后 execute；FAILED/unavailable 时可降级下一通道一次，
    降级事实写入 receipt.provider_ref（如 "degraded:adb→ssh"）
```

- observe 只走能提供 UI 树的通道（adb 完整 / ssh 降级 / embedded 完整）。
- 通道级 FAILED 不更新 registry 设备状态；连续失败由 transport health 反映，
  设备离线判定仍走 registry/心跳（v1 语义不变）。

## 5. Dispatcher 语义（从 v1 gateway.py 提炼）

- kv 原子占幂等键，重复 → 回已记录状态（409 语义在 REST 层）。
- 终态 first-wins；在飞重复键重发 received。
- 超时保留 pending，`action_status(device_id, idempotency_key)` 只读对账，绝不重发。
- retention：终态 7d、pending 超 24h 标 unknown、单趟删除封顶 500（沿用 v1 参数）。

## 6. 与 v1 的共存

- v1 WSS 端点 `/api/v1/devices/ws` 与 v1 gateway 不动（生产设备继续使用）。
- v2 新增 `/api/v2/devices/ws`（embedded_wss transport 就位前可 404）。
- 设备记录新增可选字段 `transports: [{kind, address, state}]`（kv 值 JSON 扩字段，
  向后兼容）。
- contracts/v2 镜像进本仓 contracts/v2/，`contract_sync_check.py` 扩展为同时
  比对 v1+v2。

## 7. 第一轮验收锚点

1. contracts/v2 校验器全绿（v1+v2 双目录，负例增强）。
2. adb transport 真机冒烟：leaf5 + nova observe/launch/settings 往返。
3. ssh transport 真机冒烟：shell/文件/超时/取消。
4. GUI agent FakeTransport 单测全绿 + 真机一段任务（leaf5：打开设置→搜索→返回）。
5. QQ dryrun：模拟入站任务消息 → agent 执行 → 结果出现在 RecordingOneBotTransport 记录中；审批流模拟批准/拒绝/过期。
6. scrcpy 录屏与 JSONL 时间戳对齐导出 MP4。
