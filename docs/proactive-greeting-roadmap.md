# 主动问候（roadmap）

状态：骨架已落在 `whale_core.life.proactive_greeting`；**默认关闭**（`WHALE_PROACTIVE_GREETING` 未设或不为 1）。完整 3min timer / 与 QQ·眼镜出站接线留待下回合。

## 模型分工（owner 2026-11-05）

| 步骤 | 模型端点 | 说明 |
|------|----------|------|
| 每 ~3min 判断要不要主动回 | **AgentScope** `https://platform.agentscope.io/v1` | 该端点下模型目前对其免费（含图/视频） |
| 正常闲聊与问候正文 | **DeepSeek 官方 API** | 更快；不要用 AgentScope 做闲聊回复 |

## 与意图短路的关系

闲聊意图路由已默认 `kind=chat` 跳过 kimi；主动问候是另一条主动通道，不得绕过 shell ACL / PluginGateway。

## 环境变量（草案）

- `WHALE_PROACTIVE_GREETING=1` 才启用
- `WHALE_PROACTIVE_GREETING_INTERVAL_S=180`（默认 180）
- 判断模型：复用 `QQ_INTENT_*` / AgentScope；回复模型：复用 `DEEPSEEK_*`
