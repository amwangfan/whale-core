# plugins/ —— bundled official plugins（捆绑官方插件）

本目录存放**从核心拆出的官方插件**，是与核心并列的独立发行单元，
不是插件框架本身。

## 与 src/whale_core/plugins/ 的区别

| | 本目录（`plugins/`） | `src/whale_core/plugins/` |
| --- | --- | --- |
| 角色 | bundled official plugins：具体业务能力 | 插件框架契约 + 内置 provider 实现 |
| 内容 | `weq_channel`（QQ/WeQ 渠道）、`search_web`（网页搜索/抓取） | manifest / registry / loader / manager / gateway / policy / events 等框架 + 媒体/语音等内置 provider |
| 加载方式 | 宿主经 `whale_core.plugins.loader` 按目录**显式加载**（plugin.json 校验 + api_version 检查），或按包名直接导入 | 随核心包常驻，宿主组装代码显式 `register()` |
| 依赖方向 | 单向依赖框架契约（manifest/registry）；**核心不 import 本目录业务代码** | 框架模块不得 import 本目录（见下「已知例外」） |

## 打包发行（事实核对：pyproject.toml）

`pyproject.toml` 的 `[tool.hatch.build.targets.wheel]`：

```toml
packages = ["src/whale_core", "plugins/search_web", "plugins/weq_channel"]
```

即两个插件包**随同一 wheel 打包发行**（frozen / Docker 亦同口径，
见 `src/whale_core/plugins/loader.py` 模块 docstring 的发行策略说明）。
测试环境经 `[tool.pytest.ini_options] pythonpath = ["src", "plugins", "."]`
使本目录包可按名导入。

## 插件包约定

- 每个插件一个子目录，含 `plugin.json`（plugin_id / api_version / kind /
  actions / action_risk 等）与 `__init__.py`（暴露 `register(registry, ...)`）；
- `__init__.py` 必须**懒加载**（不在导入时拉起依赖），保证
  `import search_web` / `import weq_channel` 永远安全——由
  `tests/test_arch_plugins.py` 锁定；
- 凭据与宿主依赖（发送端口、账本、策略、env key）一律由宿主注入，
  插件包不自带凭据；
- 与仓内纪律一致：**不做自动发现**，加载永远显式。

## 已知例外

`src/whale_core/plugins/provider_http.py`（内置 provider 共享传输层）
复用了本目录 `search_web/web_fetch.py` 的 `_NoRedirectHandler`
（`from search_web.web_fetch import _NoRedirectHandler`）——方向是
「框架包内支撑设施 → 本目录插件」，属历史代码共享；因两者随同一 wheel
发行，运行期可用。详见 `src/whale_core/plugins/README.md` 的「已知例外」。
