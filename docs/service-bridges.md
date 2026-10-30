# 服务桥与设备接入

本文件区分现有 whale-io/万相的原生适配器与可选自定义桥协议。原生适配器无需另搭通用桥；自定义桥需要实现对应协议。配置好 URL 不等于已经通过账号或真机验收，远程服务应使用 HTTPS。

## 万相视频（无需另建桥）

`media_video` 默认 `protocol=dashscope`，使用万相文生视频 2.1–2.6 异步协议；默认模型 `wan2.6-t2v`，5 秒、720p。填写与模型同地域的 API Key 和 `/api/v1` 基址即可调用。服务仍需用户自行开通并承担实际费用。建议使用业务空间专属域名；默认北京旧域名仍可在设置中替换。

原生路径：提交 `/services/aigc/video-generation/video-synthesis`，携带 `X-DashScope-Async: enable`；每 15 秒查询 `/tasks/{id}`；尝试 `/tasks/{id}/cancel`。平台只允许取消排队中的任务，已经运行时返回 unknown 并保留外部 ID，不假称停费。重启后可在任务页人工“核对远程状态”，只查询原任务，不重新生成。

参考：[文生视频 API](https://help.aliyun.com/zh/model-studio/legacy-wan-text-to-video-api-reference)、[异步任务管理](https://docs.modelstudio.console.alibabacloud.com/zh/model-studio/manage-asynchronous-tasks)。当前仅做了真实回环协议验收，未使用付费真实密钥调用。

## 自定义视频桥

选择 `protocol=bridge` 时使用下述协议。

配置插件 `media_video`：`base_url`、`api_key`、`model`、`timeout`。路径相对 base_url：

| 请求 | JSON 请求体 | JSON 响应 |
| --- | --- | --- |
| `POST /jobs` | `prompt, model, duration_s, size` | `{"task_id":"供应商任务 ID"}` |
| `GET /jobs/{id}` | 无 | `{"status":"pending或running或succeeded或failed或cancelled","url":"产物地址","error":"短错误分类"}` |
| `POST /jobs/{id}/cancel` | `{}` | `{"cancelled":true}` |

提交不盲目重试；宿主每 2 秒轮询，10 分钟后停止等待，未知结果须核对供应商。原始供应商任务 ID 会保存在任务记录的 `external_task_id`。收到成功后，宿主下载产物进入受鉴权的本地产物目录。产物只允许受支持的媒体类型，最多 128 MiB，默认保留 7 天。公网 URL 使用 DNS 和实际连接对端检查，拒绝私网和重定向；桥可直接返回 `data:video/mp4;base64,...`。不能将需要另带供应商 Authorization 的下载链接直接当公开 URL 返回。

桥要根据选定供应商处理其 SDK、异步状态和取消语义。本仓库提供 HTTP adapter 与真实回环 wire 测试，**没有附带一个已部署的供应商视频桥**。

## 浏览器实时语音与现有 PCM 桥

先安装 `python -m pip install -e '.[voice]'`。在插件 `voice_bridge` 填写现有 whale-io 音频桥的 `host`、`port`（默认 18799）、`bridge_token`，然后启用。创作页选择实时语音并点击开始，浏览器请求麦克风权限。

链路为：浏览器 AudioWorklet 采集/播放 PCM16 16 kHz → WebSocket 中继 → 现有音频桥 TCP 帧。核心不在中间等待文字模型，不保存原始麦克风音频。桥端原有 Omni/级联选择继续有效，回声、VAD 与打断能力受桥模式/设备影响。

中继在第一次创建会话时监听 API 端口 + 1（8080 对应 8081）。会话通过受鉴权的 `POST /api/v1/voice/session` 创建，一次性 60 秒票据由 WebSocket subprotocol 发送，不放 URL；永久桥 token 只留服务端。最多 4 个会话/票据，每次最长 10 分钟。默认 loopback/plain PCM，公网 PCM 必须 TLS；公网网页应配置 WSS 反代和插件 `public_ws_url`。Compose 已映射本机 8081。

现有 whale-io 帧是 `uint32 BE 长度 + uint8 类型 + payload`：类型 0 为 PCM，类型 1 为控制文本；连接时发送 `AUTH <token>` 并验证 `HELLO`，与 `infra/call-bridge/audio-bridge/` 一致。真实 TCP+WebSocket 回环测试验证了音频往返与票据防重放；尚未使用真实手机/Omni API Key 联调。

人格与转写：同一可信主机上让旧音频桥的 `WHALE_MEMORY_DB` 指向核心 canonical 库，桥便可在建会话时读取角色卡并异步写回最终转写；不要经公网同步 SQLite 文件。其他桥可使用 `POST /api/v1/transcripts` 写最终转写。

高级 `voice_http` 插件仍提供 `POST /sessions` 会话协商（请求 session_id/instructions/model，返回 session_id/url），用于其他外置实时服务；它不取代原生 PCM 中继。

## 手机、眼镜与消息投递

`channel_bridge` 默认 `protocol=whale-io`，无需另建通用服务：

- HUD：填写完整 `dashboard_url` 和 writer token，使用现有卡片 `{title,body,layout_hint}` 协议。
- 来电：填写 `ring_url` 和 ring token，调用现有 `/v1/ring`；`target` 是固定设备 ID。需要在主动生活设置允许 call 且显式允许电话，每次仍审批。
- QQ：填写 WeQ URL/token、固定 `owner_uid` 和登录账号校验，复用现有 WeQ 发送器；也可用下文 QQ 共用宿主模式。

在创作页“设备投递”填写 channel/text，生成待审任务；接收方只能来自插件配置。全局暂停、静默、预算、渠道允许列表仍然有效。原生适配器对未配置的渠道明确失败，不冒充送达。`phone_notice` 需另接通用桥，不能把写 dashboard 当作手机通知完成。

高级 `protocol=http` 使用以下自定义消息桥契约：

插件 `channel_bridge` 配置固定 `target` 和连接凭据。`POST /messages` 请求：

```json
{"channel":"hud","target":"显式固定接收方","text":"经批准的文案","idempotency_key":"草稿 ID"}
```

响应 `{"accepted":true}` 仅表示渠道受理，不代表送达或已读。超时/拒绝记录为 unknown，不自动重发。发送前仍经过 `ProactiveOutbox` 的暂停、渠道白名单、静默时段、额度及单次审批。需要桥按 idempotency_key 幂等处理。

设备入站：先在控制台设置明确的 channel/account 绑定，再 `POST /api/v1/channels/inbound`，传 `channel, account, conversation, message_id, text`；携带 Bearer token。同一渠道消息重复上传返回同一任务。未配对身份为 403。

目前 API token 是单 owner 管理凭据，不是多用户设备隔离方案。只交给可信的桥；多用户/低信任设备应另做 scoped token 和租户隔离。

## QQ 与控制台共用实际运行时

保留既有 QQ 运行器的所有配对、WeQ、dry-run 与双开关语义。增加显式共用模式：

```bash
python examples/qq_live_workflow.py run \
  --core-config-dir ~/.whale --console-port 8080 \
  --store ~/.whale/memory.sqlite3
```

先按 QQ 运行器已有说明配置 `WEQ_SSE_TOKEN`、`OWNER_QQ`、`QQ_SELF_UIN` 等；真实发送仍需 WeQ 配置。这个模式的控制台与 QQ 使用同一个 bundle/store/插件注册表：网页禁用 `qq_weq` 后，不处理新 owner 入站；网页批准 QQ 回复后，真实发送走既有 WeQ 端口及 outbox 台账。没有真实出站配置时报告失败，不用 dry-run 冒充发送。

不要同时另起 `whale api` 接管同一目录。原来的独立运行方式继续可用，但它不自动获得新控制台的插件管理能力；要统一管理，应使用上面的共用模式。QQ 原有配对仍取自运行器环境配置，网页“设备配对”对应的是新接入 API。

## 外置 HTTP 插件

在配置目录 `manifests/` 放显式清单，示例：

```json
{
  "plugin_id":"my_notify",
  "name":"我的通知适配器",
  "version":"1.0.0",
  "kind":"output",
  "transport":"http",
  "actions":["notify"],
  "action_risk":{"notify":"communicate"},
  "config_schema":{"properties":{
    "base_url":{"type":"string"},
    "api_key":{"type":"string","format":"secret"},
    "timeout":{"type":"number"}
  }}
}
```

重启加载清单，网页配置实际地址/密钥后启用。桥实现 `POST /execute`，接收 `plugin_id, action, target, args`，返回 `{"ok":true,"receipt":"短回执"}`。控制台创作页选择“外置插件”，填写例如：

```json
{"plugin_id":"my_notify","action":"notify","target":"owner","args":{"text":"你好"}}
```

请求进入持久化审批；批准后由 `PluginGateway` 根据当前 manifest 再裁决。未声明动作、禁用插件、没有额度配置的交易动作均不能靠审批字段绕过。清单不会导入第三方 Python 代码；不支持的 transport 会被拒绝。

## 图像提供者默认值

新配置默认使用 `gpt-image-2` 和标准尺寸 1024×1024 / 1536×1024 / 1024×1536；旧兼容尺寸仍保留给自建端点。用户已经保存的自定义型号不会被覆盖。DALL·E 3 已不适合作为 OpenAI 新配置默认值，迁移时可在插件设置中改型号。模型可用性和费用仍以账号与区域为准。核对依据：[官方图像 API](https://developers.openai.com/api/reference/resources/images/methods/generate)、[弃用说明](https://developers.openai.com/api/docs/deprecations)。
