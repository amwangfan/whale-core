# 管理 API v1

所有 `/api/v1/` 请求需要 `Authorization: Bearer <token>`。成功为 `{"result":...}`，错误为 `{"error":{"code","message"}}`。同源浏览器访问；CLI 无 Origin 请求可用。普通 JSON body 上限 64KiB，ASR 为 36MiB（含 base64），实时音频分块接口为 2MiB。连接和并发都有边界，产物只在鉴权后提供。

## 主要端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health`, `/status`, `/diagnostics` | 活性、运行状态、配置/插件/探索 worker 与调用统计 |
| GET/POST | `/settings` | 查看脱敏配置、验证后原子保存并即时切换；活动任务/工作流阻止切换 |
| POST | `/providers/test` | 一次真实短模型请求，返回 ok/latency_ms/error_kind |
| GET | `/plugins`, `/plugins/{id}` | 清单、启停状态、脱敏配置 |
| POST | `/plugins/{id}/enable`, `/disable`, `/config`, `/test` | 启停、配置、连接检查；没有执行器不等于真实能力已接通 |
| POST | `/chat` | `{text,conversation_id}` → reply/status/receipts/task_id |
| GET/DELETE | `/conversations/{id}` | 最近多轮历史、删除关联会话与明确关联派生记录；活动任务时409 |
| GET | `/memory/search?q=...`, `/memory/{key}` | 搜索/详情；空搜索返回最近最多100条 |
| DELETE | `/memory/{key}` | 删除真实MemoryRecord，不能访问或删除任意内部KV |
| POST | `/memory/export` | JSONL下载，不等于完整数据库备份 |
| GET | `/tasks`, `/tasks/{id}` | 可公开任务状态与工具回执，不返回私有执行journal |
| POST | `/tasks/{id}/cancel` | 协作取消；in-flight结果可能为unknown |
| POST | `/tasks/{id}/decision` | `{decision:approve|reject}`；不执行动作 |
| POST | `/tasks/{id}/resume` | 审批后继续，已完成步骤不重放 |
| GET | `/approvals` | `kind=task` 或 `kind=draft` 的待处理授权 |
| POST | `/approvals/{draft_id}/decision` | 渠道草稿授权；派发器负责后续真实发送 |
| POST | `/capabilities/{capability}` | `{params:{...}}` → output/status/reply/receipts/task_id，仍经过运行时和策略 |
| GET | `/artifacts/{32hex_id}` | 私密图片、音频、视频；不支持任意服务器路径 |
| GET | `/video/tasks` | 异步视频任务 |
| POST | `/video/tasks/{id}/poll`, `/cancel` | 查询/尝试取消真实供应商任务，取消不保证供应商支持 |
| POST | `/voice/sessions` | `{instructions?}` 创建独立语音会话，注入短人格和记忆，不调用文字模型 |
| POST | `/voice/sessions/{id}/{append,commit,receive,cancel,close}` | PCM音频分块、提交、事件接收、打断和释放 |
| GET | `/workflows/skills` | 已打包/宿主显式提供的工作流列表 |
| POST | `/workflows/skills/{name}/run` | `{input_text}` 启动工具组合；名字不能是任意路径 |
| POST | `/workflows/{id}/resume` | 前一步任务批准并执行后继续工作流 |
| GET/POST | `/workflows/settings` | paused/daily_limit/quiet_start/quiet_end/timezone/schedule_enabled/interval_s |
| POST | `/workflows/explore` | `{topics:[...]}`，真实搜索/阅读后形成草稿，不直接外发 |
| GET | `/events` | 已鉴权SSE与心跳；最近事件环不是持久审计库 |

## 设置语义

`GET /settings` 包含 profile、model（base_url/model/api_key_configured）、intent_model_enabled、当前可用 capabilities、services 与 notes。services 的密钥字段只返回是否配置。

`POST /settings` 支持 profile、model、intent_model_enabled 和 services（明确白名单的环境变量名）。密钥省略或空字符串保留；`null` 清除并覆盖环境注入。模型档位切换不沿用另一档保存的凭据。端点不得含 username/password、查询串或片段。

私密 `secrets/settings.json` 是配置和凭据的单代权威快照。读取必须同时取同一代地址与密钥；`config.json` 只作非敏感投影，不能作为旧代回退。部署环境仍可提供环境变量默认值。恢复完整配置需备份私密目录，而不只是数据库。

## 任务和执行状态

`answered`、`failed`、`denied`、`pending_approval`、`cancelled`、`unknown` 有不同含义。超时或执行中取消时，不可宣称外部动作已撤销；未知结果不允许自动恢复重放。

审批绑定任务、当前步骤、插件版本和动作参数摘要，不能由模型输出 `approved=true` 自行授权。重新配置或禁用插件会撤销旧组合根中的能力引用。原始音频参数不写入执行journal。

## 语音与媒体

具体 provider 配置、音频格式与调用示例见 [media-and-voice.md](media-and-voice.md)。网页音频、图片和视频展示使用 Bearer fetch 后创建 Blob URL，不把 token 放到 URL 中。语音最终转写通过有界队列写入文本记忆；原始音频不持久化。

Realtime 是服务兼容协议，不承诺任何供应商的不同协议都能直接互换。输入转写是否可用取决于服务是否发送对应 final transcript 事件。
