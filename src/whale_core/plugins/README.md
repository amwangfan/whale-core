# whale_core.plugins —— 目录职责与分层（issue #31）

本目录混放了两类代码：**插件框架契约**与**内置 provider 实现**。为兼容优先
（import 面太广，物理移动风险大），不调整文件位置，只用分层标注 + 架构测试
（``tests/test_arch_plugins.py``）锁定边界。

## 三层职责定义

| 层 | 位置 | 职责 | 发行单元 |
| --- | --- | --- | --- |
| **framework（插件框架契约）** | 本目录（下表） | 插件与核心之间的稳定契约面：清单、注册、加载、生命周期、门控、执行网关、事件/意图契约 | 随 `whale-core` wheel（`src/whale_core`） |
| **builtin providers（内置 provider 实现）** | 本目录（下表） | 媒体 / 语音 / shell / realtime / 模型供给等能力的官方实现——注册为普通插件走同一框架，但**不是框架契约**，可按需替换 | 随 `whale-core` wheel（`src/whale_core`） |
| **bundled official plugins（捆绑官方插件）** | 仓库根 `plugins/`（`weq_channel`、`search_web`） | 从核心拆出的官方插件，独立发行单元；经 `plugins/loader.py` 显式加载，**核心不 import 其业务代码** | 随同一 wheel 打包（`pyproject.toml` `[tool.hatch.build.targets.wheel] packages` 显式列入），见根 `plugins/README.md` |

## 本目录模块分类

### framework —— 插件框架契约（9）

| 模块 | 职责 |
| --- | --- |
| `manifest.py` | 插件清单：能力声明 + 风险分级 + 注册时校验 |
| `registry.py` | 插件注册表：显式注册、能力查询、执行器解析（不做自动发现） |
| `loader.py` | 外部目录插件加载器：plugin.json 校验 + api_version 兼容检查 |
| `manager.py` | PluginManager：清单加载 + 生命周期 + 健康检查 + 启停持久化 |
| `gateway.py` | PluginGateway：`plugin_action` 的单一执行点（策略内嵌） |
| `policy.py` | PluginActionPolicy：按风险分级的唯一门控点 |
| `events.py` | PluginEvent：插件输入事件契约（PerceptionEvent 子类，固定 external） |
| `intents.py` | PluginAction：插件输出动作意图契约 |
| `processing.py` | processor 类插件框架：ProcessorRouter + DelegatingIntentProcessor |

### builtin providers —— 内置 provider 实现（13）

| 模块 | 能力 |
| --- | --- |
| `media_image.py` | `media.image.generate` 契约 + OpenAI 兼容生图 |
| `media_video.py` | `media.video.generate` 异步任务契约 + DashScope Wan |
| `dashscope_media.py` | DashScope 兼容协议媒体 provider（生图/ASR/TTS） |
| `speech.py` | ASR/TTS 契约 + OpenAI 兼容 provider |
| `pcm_voice.py` | whale-io PCM wire 实时音频适配 |
| `realtime_voice.py` | 原生 Realtime WebSocket 会话 |
| `realtime_bridge.py` | 经认证宿主的 realtime 会话桥 |
| `embedding.py` | OpenAI 兼容 embedding provider |
| `model_provider.py` | `llm.chat` 模型供给官方装配 |
| `shell_tool.py` | 官方 shell 工具（走 `cloud.shell_policy` 分档） |
| `whale_io.py` | whale-io 原生渠道适配骨架 |
| `bridge.py` | 自定义服务桥 HTTP 客户端（渠道桥 + 外置执行器） |
| `capability_setup.py` | 内置能力的宿主装配点（显式端点才注册 provider） |

### 其他 —— 内置 provider 支撑设施（2，非框架契约）

| 模块 | 职责 |
| --- | --- |
| `provider_http.py` | 内置 provider 共用的有界 HTTP 传输（禁重定向、限大小） |
| `artifacts.py` | 私有有界媒体存储（owner 鉴权前提） |

## 分层规则（由 `tests/test_arch_plugins.py` 锁定）

1. framework 模块不得 import 任何 builtin provider / 支撑模块
   （当前全部满足，测试逐对断言）。
2. `whale_core.plugins.loader` 不得 import `whale_core.life`
   （当前无此依赖，测试锁定）。
3. 根 `plugins/` 下 `search_web` / `weq_channel` 的 `__init__` 必须可导入
   （懒加载设计，导入即安全）。
4. 本目录新增模块必须在 README 本表中归类，否则架构测试失败。

## 已知例外（不阻断分层断言，记录在案）

- **`provider_http.py` → `search_web`**（第 8 行
  `from search_web.web_fetch import _NoRedirectHandler`）：内置 provider 的
  共享传输层复用了捆绑插件 search_web 的禁重定向 handler。方向是
  「框架包内支撑设施 → bundled official plugin」，属于历史代码共享；
  因两者随同一 wheel 发行（`pyproject.toml` packages 同时列入
  `src/whale_core` 与 `plugins/search_web`），运行期可用。framework 契约
  模块无此依赖，故不违反上表规则 1。后续若 search_web 拆出独立发行，
  需先把 `_NoRedirectHandler` 下沉到框架包或复制实现。
- **`whale_io.py` → `whale_core.life.initiative`**（函数体内延迟导入
  `ProactiveDraft`）：whale_io 是 builtin provider（宿主适配骨架），
  不属于 framework，不违反规则 2（规则只锁 `loader`）。
