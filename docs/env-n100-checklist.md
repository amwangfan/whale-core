# N100 `qq_workflow.env` 变量清单（脱敏）

仅变量名与说明；真实值只放 `/opt/whale/secrets/qq_workflow.env`。

## 模型 / 意图
| 变量 | 说明 |
|------|------|
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | 主聊天模型 |
| `QQ_INTENT_API_KEY` / `QQ_INTENT_BASE_URL` / `QQ_INTENT_MODEL` | 意图/审操（可与主模型不同供应商） |
| `QQ_SAFETY_MODEL` | 回复安全复核；可与意图同模型 |
| `WHALE_SHELL_MODEL` | Shell 审操模型名（端点沿用 QQ_INTENT_*） |

## 搜索 / 天气
| 变量 | 说明 |
|------|------|
| `WHALE_SEARCH_PROVIDER` | 如 `tavily` |
| `TAVILY_API_KEY` | Tavily |
| `QWEATHER_API_KEY` | 和风 API KEY（头 `X-QW-Api-Key`） |
| `QWEATHER_API_HOST` | 账号 Host，形如 `<your-host>.re.qweatherapi.com` |

## 位置
| 变量 | 说明 |
|------|------|
| `WHALE_DEFAULT_CITY` | 无口述且无缓存时默认地名（代码仍兼容 `WHALE_HOME_CITY` / `WHALE_LOCATION`） |

## 眼镜
| 变量 | 说明 |
|------|------|
| `WHALE_GLASSES_TOKEN` | `/v1/chat` Bearer |
| `WHALE_GLASSES_STORE` | 眼镜会话 sqlite 路径 |

## 媒体
| 变量 | 说明 |
|------|------|
| `WHALE_MEDIA_API_KEY` / `WHALE_MEDIA_BASE_URL` / `WHALE_MEDIA_PROVIDER` | 图/语音/视频共享平台 |
| `WHALE_MEDIA_OPEN_TEST` | `1` 时群/陌生人也可媒体 scope |
| `WHALE_VIDEO_MODEL` / `WHALE_VIDEO_MODELS` / `WHALE_TTS_MODELS` | 模型清单 |
| `WHALE_MINIMAX_API_KEY` / `WHALE_MINIMAX_TTS_MODEL` | MiniMax 可选 |

## QQ / WeQ / Shell
见 `docs/qweather-env.md` 与 `.env.example`；通道类含 `OWNER_*`、`QQ_*`、`WEQ_*`、`WHALE_QQ_*`、`WHALE_SHELL_*`。


## QQ 群接话 / 门控（脱敏）
| 变量 | 说明 |
|------|------|
| `WHALE_QQ_GROUP_ALLOW` | 逗号分隔群号白名单；空=不收任何群（即使 `--accept-group`） |
| `WHALE_BOT_NAMES` | 可选；逗号分隔点名别名。缺省用当前角色卡名（如 Enxi） |
| `WHALE_GROUP_TOPIC_JOIN_PROB` | 话题滚动可回概率 0..1，默认 `0.18`；`0` 关闭 |
| CLI `--accept-group` | systemd `qq-bot` 已开；关群聊则去掉此旗标并重启 |

门控 reason（日志/`life:turnlast:{group}`）：`at_bot` / `reply_to_bot` / `join` / `topic_roll` / `repeat_chain` / `not_directed` / `media_only` / `already_covered` / `cooldown` / `budget_exceeded` / `quiet_*` / `dnd`。

群记忆：`conversation=group:{id}` 命名空间；须 Studio/`bind:mem` 绑定 **group_space 且 can_write** 才写入。无写绑定 **绝不** 回落到主人私密库。群闲聊原文不作隐私审查过滤；与私聊记忆隔离即可。

协议端：N100 现网为 **WeQ SSE**（`weq-web` + `qq-bot`），不是另起 NapCat。

## 安卓自主体（core#58，脱敏）
| 变量 | 说明 |
|------|------|
| `WHALE_DEVICE_AGENT_ENABLED` | `1` 启用 core 编排循环接线；默认关 |
| `WHALE_DEVICE_AGENT_MAX_STEPS` | 单任务最大步数，默认 `8` |
| `WHALE_DEVICE_AGENT_TIMEOUT_S` | 单步 action/query 超时秒，默认 `15` |
| `DEEPSEEK_*` | 决策模型（`DeepSeekDecisionPort`）；勿把 key 写入仓库 |
| `QQ_INTENT_*` | 可选：意图/审操仍走 AgentScope 端点 |

设备 scope：`device.agent.control` / `observe` / `shell` / `output.read`（后者独立，旧配对不自动扩大）。停止：端侧 estop，或 core `revoke` / `POST .../scopes` 收窄。
