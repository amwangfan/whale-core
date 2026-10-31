# 语音与媒体：配置、调用和验收

本页说明云端/本地服务的接线与调用方式。功能使用真实 provider，生产运行不会回退为模拟回答。下面的 API 必须带控制台管理 token；示例中的地址、模型名和密钥都应换成自己的配置。

## 配置服务

配置保存在控制台“设置”页，或通过下列环境变量传入。填了端点才会注册对应插件；配置端点不代表已完成真实调用。

### 统一平台配置（推荐，2026-10-30 起）

一套端点 + 密钥覆盖全部媒体能力（三级回退：专属 > 共享 > 回复模型）：

| 层级 | 变量 | 说明 |
| --- | --- | --- |
| 专属覆盖 | `WHALE_IMAGE_*` / `WHALE_SPEECH_*` / `WHALE_VIDEO_*` | 单能力单独换供应商时 |
| 共享媒体平台 | `WHALE_MEDIA_BASE_URL` / `WHALE_MEDIA_API_KEY` / `WHALE_MEDIA_PROVIDER` | 一个平台全覆盖（dashscope 兼容协议：阿里云百炼 / AgentScope 平台同路径） |
| 回复模型兜底 | `DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY` | 都不配时吃回复模型同款 |

供应商类型（`WHALE_*_PROVIDER` 或共享 `WHALE_MEDIA_PROVIDER`）：`dashscope`（DashScope 兼容协议，默认）或 `openai`（OpenAI Images/Audio 兼容）。

DashScope 兼容协议的实测模型（2026-10-30，AgentScope 平台）：生图 `qwen-image-3.0`（同步+异步轮询两形态都处理，尺寸见手册宽高比表，如 `1328*1328`）；视频 `happyhorse-1.1-t2v`（resolution/ratio 参数形态，时长 3–15 秒）；TTS `qwen-audio-3.0-tts-flash/-plus`；ASR `fun-asr-flash-2026-06-15`（默认走百炼主站——AgentScope 平台侧 ASR 当前不可用）。

### 逐能力专属配置

| 能力 | 端点 | 密钥 | 模型及其他参数 |
| --- | --- | --- | --- |
| 图片生成 | `WHALE_IMAGE_BASE_URL`，例如 `https://api.openai.com/v1` | `WHALE_IMAGE_API_KEY` | `WHALE_IMAGE_MODEL`，dashscope 默认 `qwen-image-3.0`、openai 默认 `dall-e-3` |
| 识别与合成 | `WHALE_SPEECH_BASE_URL`，OpenAI Audio 兼容地址 | `WHALE_SPEECH_API_KEY` | dashscope 默认 `fun-asr-flash-2026-06-15` / `qwen-audio-3.0-tts-flash`；openai 默认 `whisper-1`/`tts-1` |
| 视频生成 | `WHALE_VIDEO_BASE_URL`，例如 `https://dashscope.aliyuncs.com/api/v1` | `WHALE_VIDEO_API_KEY`，缺省读取 `DASHSCOPE_API_KEY` | `WHALE_VIDEO_MODEL`，默认 `happyhorse-1.1-t2v` |
| 语义记忆嵌入 | `WHALE_EMBEDDING_BASE_URL`，OpenAI Embeddings 兼容地址 | `WHALE_EMBEDDING_API_KEY` | `WHALE_EMBEDDING_MODEL=text-embedding-3-small` |
| 原生实时语音 | `WHALE_REALTIME_URL`，兼容 Realtime 事件协议的 `wss://…/realtime` 地址 | `WHALE_REALTIME_API_KEY` | `WHALE_REALTIME_MODEL` 必填；`WHALE_REALTIME_VOICE` 缺省 `alloy` |
| whale-io PCM 语音桥 | `WHALE_PCM_BRIDGE_HOST`（填了才注册，默认端口 `WHALE_PCM_BRIDGE_PORT=18799`） | `WHALE_PCM_BRIDGE_TOKEN`（现有音频桥的桥 token） | `WHALE_PCM_BRIDGE_TLS=1`（公网桥必须 TLS）；`WHALE_PCM_BRIDGE_PUBLIC_WS_URL`（HTTPS 控制台的 WSS 反代地址） |

文字模型的密钥与上述媒体服务密钥相互独立。公共 HTTP 端点被拒绝；可信回环/私网的图片、ASR、TTS、视频兼容服务可使用 HTTP。实时语音要求 WSS。端点不能携带 URL 用户名、密码或 fragment。

实时语音需要安装可选依赖：

```bash
python -m pip install -e '.[voice]'
```

DashScope Wan 的模型、密钥和端点必须属于同一区域；也可填写自己的 workspace 专属域名。内置视频 provider 使用 Wan 2.6 兼容协议，不会自动把其他厂商的视频 API 当成同一协议。生图模型的尺寸、图片数量支持范围也由供应商决定，供应商拒绝的参数会明确报错。

## 调用图片、ASR、TTS

统一入口：`POST /api/v1/capabilities/{capability}`，请求体为 `{"params":{…}}`。仅执行已启用插件提供的只读能力 handler，不执行任意函数或 Shell。

```bash
# WHALE_API_TOKEN 使用实际控制台 token
curl http://127.0.0.1:8080/api/v1/capabilities/media.image.generate \
  -H "Authorization: Bearer $WHALE_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"params":{"prompt":"夕阳下跃出海面的鲸鱼","size":"1024x1024","n":1}}'
```

响应 `result` 内包含 `capability` 与 `output`。`output` 是工具的字符串输出；媒体工具输出为 JSON 字符串，需要再解码一次。

| 能力 | `params` | 成功输出 |
| --- | --- | --- |
| `media.image.generate` | `prompt`，可选 `size`、`n` | `task_id`、`status`、`images` 产物链接 |
| `asr.transcribe` | `audio_base64`，可选 `lang`（默认 `zh`）、`format`（默认 `wav`，也支持 `webm/ogg/mp3/m4a/flac/mp4`） | 转写文字 |
| `tts.synthesize` | `text`，可选 `voice`、`rate` | 产物 `artifact_id`、`content_type`、`size_bytes`、`url` 等 |
| `media.video.generate` | `prompt`，可选 `duration_s`、`size` | 已提交的异步 `VideoTask`，初始通常为 `pending` |

ASR 输入应是 WAV 等供应商支持的完整音频文件的标准 base64，不能附加 `data:` 头。音频最大 25 MiB；管理 API 的能力请求总大小限制为 36 MiB，覆盖 base64 开销。TTS 文本为 1–4000 字符；语速范围 0.25–4.0。默认 TTS 请求 MP3，产物保存为 `audio/mpeg`。

图片尺寸为 `1024x1024`、`1792x1024` 或 `1024x1792`；请求数量限制为 1–4。实际模型可能有更严格约束。请求会产生供应商用量，不要对超时的生成请求盲目重试。

## 视频任务

视频提交立即返回任务记录，不阻塞等待几分钟。Wan 2.6 支持时长 `5`、`10`、`15` 秒；默认 5 秒。尺寸包括 `1280x720`、`720x1280`、`1024x1024`。

| API | 行为 |
| --- | --- |
| `GET /api/v1/video/tasks` | 查看已提交的视频任务 |
| `POST /api/v1/video/tasks/{task_id}/poll` | 查询状态，返回 `result` 任务记录 |
| `POST /api/v1/video/tasks/{task_id}/cancel` | 请求取消，返回最新任务记录；供应商只允许取消排队中的任务 |

状态为 `pending`、`running`、`succeeded`、`failed`、`cancelled`。同一个 manager 至少间隔 15 秒向供应商查一次；重复读取不会重复提交任务。任务保存在同一 SQLite 库，重启可恢复查询。供应商暂时出错时保留原状态与错误分类；超过 30 分钟的本地等待会标记超时，错误中明确提示远端可能仍在运行。

成功视频会下载为私有产物，防止供应商临时链接过期。下载失败会保留非终态与 `artifact_save_failed`，可再次查询保存，不重新付费生成。取消失败不会假装任务已经取消。

## 产物

`GET /api/v1/artifacts/{artifact_id}` 需要 Bearer 认证，不能把它当作公共匿名外链。浏览器应使用鉴权 fetch 获取 Blob 后展示/下载；不要把 token 拼进 URL。

产物默认保存七天，单项最多 100 MiB，总额度 512 MiB；启动和后续写入时清理过期文件。文件名由系统生成，不采用模型给出的文件路径。支持 PNG/JPEG/WebP/GIF、MP3/WAV/OGG/WebM 音频和 MP4；拒绝 HTML、SVG 等主动内容。远端产物下载不携带供应商密钥，逐跳执行 SSRF 与大小检查。文件权限为 600，读取时检查完整性；音频不写入普通日志或长期记忆。

## 可选语义记忆召回

填写 `WHALE_EMBEDDING_BASE_URL`、`WHALE_EMBEDDING_API_KEY` 与 `WHALE_EMBEDDING_MODEL` 后，系统注册 `embedding.embed`，普通文字对话会把已确认的记忆摘要与当前查询发送给配置的嵌入服务，结合关键词分数召回。未配置时保留关键词检索，不会启动散列占位模型。

`DynamicHybridCurator` 保留 `MemoryCurator` 的事实写入、证据、授权和删除接口，只增强召回。插件禁用后下一次召回立即回到关键词路径；嵌入网络失败、响应不合法、超出本轮预算时也会退回本地召回。稳定事实的空查询不调用嵌入服务，实时电话链路不等待语义检索。

嵌入请求默认每批 32 条、本轮最多 512 条，每条不超过 8000 字符，总字符预算 512 Ki；所有批次共享 8 秒连接预算。响应必须返回同样数量的向量，按 `index` 恢复原始顺序，向量维数必须一致且包含有限数值；非法响应不会进入缓存。缓存只保留至多 512 个 SHA-256 文本摘要键与向量，不持久化原文；删除记忆后不再把其作为候选，经过 curator 删除还会清理对应缓存。

启用云端嵌入意味着记忆摘要会发送给所配置的服务；需要全部本地处理时选择本地 Embeddings 兼容端点，或禁用该插件。首次验收可记住一条有明确来源的事实，再用不同措辞询问，检查召回依据是否包含“嵌入相似”；之后禁用插件、删除事实，验证不会继续使用已删除内容。

## 原生实时语音

实时通道直接交换 PCM 音频与供应商 Realtime 事件，不等待文字陪伴模型。`whale-io` 的 Omni 桥仍负责电话/手机/眼镜硬件接入；本仓库提供独立会话 provider 与 HTTP 会话桥，便于浏览器和其他客户端接入。

| API | 请求体 | 用途 |
| --- | --- | --- |
| `POST /api/v1/voice/sessions` | 由宿主生成当前人格 instructions | 打开会话，返回随机 `session_id` |
| `POST /api/v1/voice/sessions/{id}/append` | `audio_base64` | 追加 PCM16 音频块，单块最大 1 MiB |
| `POST /api/v1/voice/sessions/{id}/commit` | `{}` | 提交音频并请求模型回复 |
| `POST /api/v1/voice/sessions/{id}/receive` | 可选 `timeout`，0.1–15 秒 | 获取下一条原生事件；暂无事件时返回心跳 |
| `POST /api/v1/voice/sessions/{id}/cancel` | `{}` | 打断当前回复并清空尚未提交的音频 |
| `POST /api/v1/voice/sessions/{id}/close` | `{}` | 关闭连接；重复关闭幂等 |

所有会话接口均需管理 token。至多同时打开四个会话，闲置 120 秒自动关闭，服务退出时关闭所有连接。浏览器退出通话时应显式关闭，不能只关闭 UI。禁止同一个 session 同时开启多个 receive 请求。

原生事件使用所配端点的兼容协议。当前客户端发送 `session.update`、`input_audio_buffer.append/commit/clear`、`response.create/cancel`，消费供应商的 `response.audio.delta`、最终转写和结束事件。输入输出 PCM 采样率必须与端点约定匹配，客户端须按实际供应商协议处理重采样；Omni 电话桥已有对应音频处理。接 OpenAI 新版 GA 协议时应确认其 session/audio 参数结构是否兼容，而不能只替换 URL 后默认通过。

## 浏览器低延迟通话（whale-io PCM 中继，移植自 PR#4）

这是与上面 chunk/poll 会话**并存的第二种传输**：浏览器 AudioWorklet 采集/播放 PCM16 16 kHz，经 WebSocket 中继直达现有 whale-io 音频桥 TCP 帧，不经过文字模型，延迟显著低于 base64 轮询。控制台「媒体与语音」页的「低延迟通话」入口使用它；「实时语音」入口仍走上面的 `/voice/sessions`，两者互不影响。

> **诚实声明：协议按 PR#4 契约实现（`uint32 BE 长度 + uint8 类型 + payload`，类型 0=PCM、1=控制文本，连接后先发 `AUTH <token>`、验证 `HELLO` 问候），离线回环测试验证了帧往返、AUTH、票据防重放与回环/TLS 约束；尚未与真实 whale-io 桥联调。** 真实桥联调通过前不要对owner 宣称浏览器原生电话能力已可用。

配置（全部环境变量，填 `WHALE_PCM_BRIDGE_HOST` 才注册 `voice_bridge` 插件，能力名 `voice.pcm_bridge`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WHALE_PCM_BRIDGE_HOST` / `WHALE_PCM_BRIDGE_PORT` | — / `18799` | 现有音频桥 TCP 地址；公网地址必须同时 `WHALE_PCM_BRIDGE_TLS=1` |
| `WHALE_PCM_BRIDGE_TOKEN` | 空 | 桥永久凭据，只留在服务端，不进 URL/日志/会话响应 |
| `WHALE_PCM_BRIDGE_TLS` | 关 | 置 `1` 用系统默认 TLS 上下文包裹桥连接 |
| `WHALE_PCM_BRIDGE_PUBLIC_WS_URL` | 空 | HTTPS 控制台下的 WSS 反代地址（`wss://`，禁止内嵌凭据/query/fragment；远程地址用 `ws://` 会被拒绝） |
| `WHALE_PCM_BRIDGE_TIMEOUT` | `15` | 桥连接/读写超时（0–120 秒） |

| API | 请求体 | 用途 |
| --- | --- | --- |
| `POST /api/v1/voice/session` | `{}` | 签发一次性票据，返回 `{protocol, url, ticket, session_id, sample_rate}` |

票据设计：`secrets.token_urlsafe(32)` 随机值，**一次性**（WebSocket 握手出示即消费）、60 秒过期、只驻留内存不落盘；浏览器经 WebSocket subprotocol（`["whale", ticket]`）出示，绝不拼进 URL；票据与桥 token 都不进日志（日志只记 `session_id`）。票据+进行中会话合计最多 4 个，超过返回 429；每通会话最长 10 分钟。中继 WebSocket 在首次签票时懒监听于 API 端口 + 1（8080 → 8081，占用时退到临时端口，URL 里总是真实端口）；`websockets` 未安装时端点 503 并提示 `whale-core[voice]`。插件在会话期间被禁用或重装配时，活动会话立即关闭。

## 验收与限制

自动化测试覆盖参数边界、HTTP 错误分类、凭据不泄漏、禁止认证跳转、响应上限、SSRF、产物完整性、插件禁用、视频异步状态/超时，以及实时音频事件和会话清理。测试使用传输替身，不代表本次已经消耗真实供应商额度或完成电话/眼镜真机联调。

部署者完成以下步骤后再将该服务标为“真实可用”：

1. 配置实际区域、端点、模型和密钥，测试一次连接。
2. ASR 上传一段可辨音频，核对文字；TTS 合成并播放一句话。
3. 生图后从鉴权产物接口下载、显示并核对；视频提交一次后轮询到完成并播放。
4. 实时语音打开会话、发送有效采样率 PCM、收到并播放音频；测试取消、断网和退出清理。
5. 禁用对应插件，确认能力调用被拒绝；重启后确认启停配置与任务记录仍然正确。

协议参考（2026-10-30 核对）：[Wan 兼容文生视频 API](https://help.aliyun.com/en/model-studio/legacy-wan-text-to-video-api-reference)、[异步任务查询与取消](https://help.aliyun.com/en/model-studio/manage-asynchronous-tasks)、[whale-io 音频桥](https://github.com/amwangfan/whale-io/tree/main/infra/call-bridge/audio-bridge)。[OpenAI Embeddings 协议](https://platform.openai.com/docs/api-reference/embeddings/create)。供应商能力和型号可能变化，以账号实际可用服务为准。
