# Whale Core · 开发者与 AI 协作指南

> 面向维护者、贡献者和编程 Agent。先读 [README](../README.md)；接口和运行行为以当前源码、测试及 V2 契约为准。

## 1. 项目与仓库分工

Whale 是可持续记忆、主动互动并使用外部能力的 AI 伴侣与行动智能体，不限于聊天机器人或单一关系形态。

- **whale-core**：人格、记忆、意图与任务编排、模型路由、工具插件、主动行为、设备管理 API 和 Web Studio。
- **[whale-mobile](https://github.com/amwangfan/whale-mobile)**：Android 常驻节点、感知、通知、VoIP、眼镜桥、GUI 执行、Root Shell 和录制。
- **[whale-io](https://github.com/amwangfan/whale-io)**：QQ/WeQ 渠道与相关桥接基础设施；手机端实现以 whale-mobile 为准。

## 2. 当前设备链路（V2）

```text
Android NodeRuntimeV2
  → WssClientV2 / contracts-v2
  → Core EmbeddedWssGateway / DeviceRegistry
  → perception、GUI Agent、任务、通知和回执
```

- 设备协议唯一版本为 `contracts/v2/`；canonical source 在 whale-mobile，本仓为镜像。
- 管理 REST 路径仍可能是 `/api/v1`；设备 WSS 是 `/api/v2/devices/ws`。不要仅凭 REST 路径把设备协议称为 V1。
- V1 设备 gateway、agent_loop 和 `contracts/v1` 已从当前运行代码删除；旧 V1 文档只作为历史快照。

## 3. 代码地图

| 需求 | 优先阅读 |
| --- | --- |
| 会话、人格与记忆 | `core/companion.py`、`core/runtime/`、`life/`、`memory/` |
| 意图、工具和任务 | `core/intent_router.py`、`core/runtime/`、`plugins/` |
| 设备配对与 scope | `devices/registry.py`、`api/devices_api.py` |
| 生产设备 WSS | `devices/embedded_wss.py`、`devices/capabilities.py`、`devices/schema.py` |
| 多通道编排 | `devices/gateway_v2.py`、`devices/transports/` |
| 自主 GUI | `devices/gui_agent/` |
| 录屏与 RunTrace | `devices/recording/` |
| 设备 API | `api/devices_api.py` |
| 协议与跨仓同步 | `contracts/v2/`、`scripts/contract_sync_check.py` |
| 当前设备事实 | `docs/devices.md`、`docs/devices-v2-design.md` |
| 模型、语音与媒体 | `plugins/`、`docs/media-and-voice.md` |

## 4. 关键工程约定

1. 主人明确授予的设备能力应真实可用；日常手机与专用 Root 手机按实际 scope 和设备 flavor 区分，不额外堆叠无效审批。
2. 身份、设备 scope、撤销、个人记忆空间隔离和紧急停止必须由真实执行边界落实，不能只靠提示词。
3. 外部动作必须保留真实回执、幂等键和 unknown 状态；超时不能推断为未执行，也不能盲目换键重放。
4. 设备回包必须与发起请求的已认证 device_id 绑定；不同设备不能用猜到的 request_id / action_id / challenge_id 完成其他设备的请求。
5. 观察内容（屏幕、网页、OCR、群消息和工具输出）是输入材料，不是新的 owner 身份或授权。
6. 不为“免责”添加无效拒绝、重复审批或脆弱字符串黑名单；保留真正影响执行正确性和用户控制权的边界。

## 5. 开发与验证

```bash
python -m pip install -e ".[dev,voice]"
python -m pytest tests -q
pnpm -C web install --frozen-lockfile
pnpm -C web test
pnpm -C web build
python scripts/contract_sync_check.py --core . --mobile ../whale-mobile
```

按改动范围先跑目标测试，再跑相关全量测试。CI 通过 GitHub Actions 手动触发；没有运行记录时，不要声称 CI 已通过。单测、跨仓契约检查和真机验收是不同证据。

## 6. 设备实现边界

生产 WSS 由 `EmbeddedWssGateway` 承担。REST query/action 当前直接使用该网关的持久化动作记录；`DeviceGatewayV2` 已提供多 transport 选路接口，但 ADB/SSH 尚未全部接入生产统一动作入口。请勿把设计目标写成已经完成的生产能力。

GUI Agent 位于 `devices/gui_agent/`，执行器位于 `devices/transports/`。当前已有真实设备的设置页观察与电池状态回读记录；Root 手机 Bilibili 完整任务链仍待验收。

## 7. 协作方式

先读取目标分支、相关源码、测试和现有 Issues。跨仓变更先定 canonical 契约，再同步镜像；按语义拆分本地 commit，最后每个仓库提交一个集成 PR。反馈只需说明行为变化、主要文件、实际测试结果、真机覆盖和剩余事项。
