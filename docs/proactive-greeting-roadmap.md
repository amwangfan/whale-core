# 主动问候（roadmap）

状态（2026-11-05）：`whale_core.life.proactive_greeting` 已支持可开关 3min 后台循环；
**默认关闭**。`WHALE_PROACTIVE_GREETING=1` 时默认 **dry-run**（只记日志不外发）；
显式 `WHALE_PROACTIVE_GREETING_DRY_RUN=0` 才经 QQ 出站发送。眼镜服务
`whale_core.api.glasses` 启动时自动挂载（若开启）。

## 模型分工（owner 2026-11-05）

| 步骤 | 模型端点 | 说明 |
|------|----------|------|
| 每 ~3min 判断要不要主动回 | **AgentScope** `https://platform.agentscope.io/v1` | 该端点下模型目前对其免费（含图/视频） |
| 正常闲聊与问候正文 | **DeepSeek 官方 API** | 更快；不要用 AgentScope 做闲聊回复 |

## 与意图短路的关系

闲聊意图路由已默认 `kind=chat` 跳过 kimi；主动问候是另一条主动通道，不得绕过 shell ACL / PluginGateway。

## 环境变量

- `WHALE_PROACTIVE_GREETING=1` 才启用循环
- `WHALE_PROACTIVE_GREETING_DRY_RUN` 缺省/`1` = 只记草稿；`0` = 真发
- `WHALE_PROACTIVE_GREETING_INTERVAL_S=180`（默认 180，最小 30）
- 判断模型：复用 `QQ_INTENT_*` / AgentScope；回复模型：复用 `DEEPSEEK_*`
