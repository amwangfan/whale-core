# 原设想 → 契约落点（vision-map）

权威对照表：whale-io 主仓 [docs/vision-coverage.md](https://github.com/amwangfan/whale-io/blob/main/docs/vision-coverage.md)。
本文只列 **whale-core 类型 / 模块** 落点。

| 原设想 | 优先级 | whale-core 落点 |
|--------|--------|-----------------|
| 云端主动推送到眼镜 | — | `actions.ShowCard` → dashboard Gateway |
| AI 主动打电话 | P1 | `PlaceCall` + `ProactiveCallPlanner` + `ProactiveCallPolicy`（stub 可跑） |
| 感知手机当前使用状态 | — | `perception.PhoneForeground`；忙态供 `DisturbancePolicy` |
| 全天环境语音感知 | P1 | `AmbientSpeechSegment` → `SituationBrief.ambient_note` + `extract_ambient_events` + `AmbientListen` |
| 长期记忆与人格连续性 | P0 | `memory.MemoryKind` / `MemoryRecord` / `PersonaContinuity` / `ContinuityContext` |
| 课程表、位置、天气推理 | P0 | `ContextCourse` / `ContextLocation` / `ContextWeather` + `ContextReasoner`→`SituationBrief` |
| 主动决策：提醒还是不打扰 | P0 | `DisturbancePolicy`（保留 `QuietHoursPolicy`） |
| 对手机执行操作 | P1 | `PhoneAction` + `PhoneControlGateway` / `CompositeActionGateway`（不写 ADB） |
| 鲸鱼形象、像素画、惊喜 HUD | P2 | `ui.WhaleLook`（+ 既有 Persona / Hud） |
| 智能导航与实时路线提醒 | P2 | `NavRouteProgress` + `NavigateHint` |
| 多设备会话连续性 | P2 | `core.SessionState`；`WhaleAgent.get_session()` |

## 安全提醒

- 环境语音：**元数据可入事件**；原始音频字节**不入库**。  
- 位置：粗标签；**勿存精确坐标要求**。  
- 禁止真实 IP / Token / 密钥写入本仓。域名示例与 `127.0.0.1:18799` 可保留。
