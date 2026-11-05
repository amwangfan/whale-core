# 和风天气（QWeather）环境变量

眼镜 / AskAi 天气类意图优先走 `weather.now`（插件 `plugins/qweather`），
失败或未配置时回落 `web.search`（Tavily 等）。

## 必填（只放 secrets，勿提交真实值）

| 变量 | 说明 |
|------|------|
| `QWEATHER_API_KEY` | 控制台 API KEY；请求头 `X-QW-Api-Key` |
| `QWEATHER_API_HOST` | 账号独立 API Host，形如 `<your-host>.re.qweatherapi.com`（不要带路径） |

二者缺一则**不注册**天气工具，意图自动走网页搜索。

## 位置（向后兼容，可读多个名字）

| 变量 | 说明 |
|------|------|
| `WHALE_DEFAULT_CITY` | 推荐：无口述城市且无缓存坐标时的默认地名 |
| `WHALE_HOME_CITY` | 同义回落 |
| `WHALE_LOCATION` | 同义回落 |

运行时还会读 sqlite `glasses:last_location`。口述城市永远优先于默认位置。

## 相关搜索回落

| 变量 | 说明 |
|------|------|
| `WHALE_SEARCH_PROVIDER` | 如 `tavily` |
| `TAVILY_API_KEY` / `BRAVE_API_KEY` | 搜索 provider key |

认证方式：API KEY（非 JWT）。响应可能 gzip，插件内已处理。进程内缓存默认 10 分钟。
