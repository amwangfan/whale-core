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
