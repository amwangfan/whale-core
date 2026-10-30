<div align="center">

# whale-core

*山有木兮木有枝，心悦君兮君不知*

鲸鱼项目的智能层：对话、记忆、意图、工具和策略。

[English](README_EN.md) · [开发者指南](docs/AI_DEVELOPMENT_GUIDE.md) · [接口契约](docs/contracts.md) · [进度记录](docs/gap-analysis-2026-10-30.md)

</div>

## 这是什么

whale-core 是一个纯 Python 标准库实现的陪伴型 AI 核心（Python 3.11+，Windows 额外装 `tzdata`）。对话、长期记忆、意图判断、工具调用和权限策略都在这一层，QQ、电话、手机、眼镜、网页这些入口共用同一个核心，不用各写一套。

仓库里有三部分：

- 核心库：`src/whale_core/`
- 命令行：`whale setup`、`whale doctor`、`whale run`、`whale plugins`、`whale memory`、`whale api`
- 管理 API（标准库 REST + SSE）和网页控制台：`api/`、`web/`

设备接入、网络桥接和硬件适配在 [whale-io](https://github.com/amwangfan/whale-io)。两边通过事件与意图契约交互，各自独立演进。

核心不绑定厂商：文字模型、语音、搜索、生图都是插件，换 provider 只改装配。插件执行仍要过核心的身份和权限检查。

## 本轮补全与控制台

新增统一插件配置、设置向导、声音与创作工作台、持久化任务与审批执行、每日请求预算、主动生活管理、完整备份恢复、外置 HTTP 插件，以及可与 QQ 共用的控制台。页面适配窄屏，配置和任务结果使用真实后端。

- [当前实现与剩余验收](docs/completion-status.md)：哪些已验证，哪些仍未完成。
- [安装、Docker、升级与恢复](docs/deployment.md)
- [QQ、实时语音、视频和设备桥](docs/service-bridges.md)

**目前仍不能宣称除 Windows 外全部目标已完成。** 实时语音客户端/PCM 中继、万相视频与原生设备适配器已补齐，但真实账号、真机和目标部署验收尚未完成。

## 当前进度

版本 0.1.0，早期开发，模块接口还会变。

截至 2026-10-30：

- 自动测试包括原有回归、真实回环 HTTP 集成和前端组件行为测试；最终结果见当前实现与剩余验收文档
- 没有 mock/demo 档。对话只连真实模型端点，缺凭据就报错；测试替身只放在 `tests/` 里
- 控制台 10 页：总览、对话、声音与创作、插件、主动生活、记忆、任务、审批、事件、设置
- QQ / WeQ 链路（收消息 → 模型 → 人工审批 → 发送）已在真实账号上跑通

各模块现状：

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| 对话与记忆 | 可用 | 多轮历史、SQLite 存储、情节与偏好、召回、JSONL 导入导出、`记住：`/`忘掉：` 指令 |
| 意图与工具 | 可用 | 规则路由；明确的搜索请求调用已配置的搜索工具；可选 LLM 意图规划（`WHALE_INTENT_MODEL_ENABLED=1`）会带着记忆与会话上下文做合理默认（地点缺省=你所在城市）；澄清/待批/拒绝等系统回复由鲸鱼人格措辞 |
| 模型接入 | 需要真实端点 | 任何 OpenAI 兼容端点；`local` 档指本地服务，`cloud` 档默认 DeepSeek |
| 搜索与抓取 | 部分可用 | 需要 `TAVILY_API_KEY` 或 `BRAVE_API_KEY`；DDG 兜底经常被反爬挡住 |
| 插件系统 | 可用 | 清单加载、启停、健康检查、配置脱敏、按风险级免审 |
| Shell 执行 | Linux 隔离入口 | 网页只走 bubblewrap；隔离不可用时拒绝执行，须在目标主机验收 |
| 语音 / 生图 / 生视频 | 契约 + provider | ASR/TTS、图片、异步视频任务的接口和离线测试都有，尚未接真实服务联调 |
| 消息渠道 | QQ / WeQ 已接入 | 另有 OneBot；事件与动作契约覆盖手机、VoIP、眼镜 |
| 主动行为 | 可用，默认保守 | 候选、额度、静默时段、去重、反馈、探索台账 |
| 桌面打包 | 只有方案 | `docs/packaging-desktop.md`，未实施 |

每项的验收证据见[进度记录](docs/gap-analysis-2026-10-30.md)。

## 快速开始

需要 Python 3.11+。要真实对话，还需要一个能访问的 OpenAI 兼容端点（本地 Ollama 之类即可）。

```bash
git clone https://github.com/amwangfan/whale-core.git
cd whale-core
python -m pip install -e ".[dev]"

whale setup --profile local   # 写 ~/.whale/config.json
whale doctor                  # 逐项自检，[ok]/[warn]/[fail]
whale run                     # REPL，/quit 退出
```

`local` 档不需要密钥，默认指向 `http://127.0.0.1:11434/v1`。端点没起来时 `whale run` 会直接报网络错误，不会返回编造的回复。

测试和离线示例：

```bash
python -m pytest tests -q
python examples/runtime_demo.py   # 多轮对话、意图规划、工具闭环，模型与工具都是确定性替身
python examples/life_scenario.py
```

用 uv 也行：`uv sync --extra dev`，之后 `uv run whale ...`。

## 配置

只有两档，写在 `~/.whale/config.json`：

| 档位 | 连什么 | 需要准备 |
| --- | --- | --- |
| `local` | 本地 OpenAI 兼容端点，默认 `http://127.0.0.1:11434/v1` | 端点在跑；可用 `LOCAL_MODEL_BASE_URL`、`LOCAL_MODEL_NAME` 改 |
| `cloud` | DeepSeek（OpenAI 兼容） | 环境变量 `DEEPSEEK_API_KEY` |

`cloud` 档示例：

```bash
export DEEPSEEK_API_KEY=...
whale setup --profile cloud
whale run
```

`config.json` 只存非敏感信息和环境变量名，密钥只从环境读，不写进文件。`setup` 不会覆盖已有配置，要重写加 `--force`。项目不会替你安装模型或启动模型服务。

## 网页控制台

前端需要 Node 24 和 pnpm 12（CI 用这两个版本）。

```bash
pnpm -C web install
pnpm -C web build
whale api
```

然后打开 http://127.0.0.1:8080 。第一次启动会生成访问 token，打印一次后存到 `~/.whale/api_token`（权限 600），填进控制台才能连上。

控制台与运行时共用受鉴权的 API：任务审批后排队执行并留下回执，重启后的未知外部动作不会自动重试；事件页走 SSE 推送。API 默认只监听 `127.0.0.1`，不要无鉴权挂到公网。

## 目录结构

```text
src/whale_core/
  core/        对话编排、意图计划、模型访问、诊断与成本统计
  memory/      SQLite 存储、会话历史、召回融合、导入导出
  plugins/     清单、注册表、管理器、网关，以及模型/搜索/抓取/语音/生图/生视频
  perception/  感知事件采集与理解
  initiative/  主动行为策略、通话规划
  actions/     意图到动作的网关
  cloud/       受控 Shell（策略分档 + 执行器）
  life/        QQ/OneBot/WeQ 链路、收件箱、主动行为账本、探索
  ui/          人格卡、语音、HUD
  api/         管理 REST + SSE + 静态控制台托管
  cli/         whale 命令行
  skills.py    Markdown 驱动的工具组合
  profiles.py  local / cloud 两档装配
web/           控制台（React 18 + Vite + TypeScript）
tests/         pytest 用例，全部离线
docs/          契约、路线图、进度、开发者指南
```

## 已知限制

- 仓库还没有项目级 LICENSE。`licenses/` 里只有第三方人格卡的许可，不覆盖整个项目。
- mock/demo 模式已删除：缺凭据时按 config / network 错误处理，不伪造结果。
- 搜索、语音、生图、生视频有接口和离线测试，但没有真实服务联调。
- 网页 Shell 需要 Linux bubblewrap 和可用的用户命名空间；不满足条件就拒绝运行。旧的宿主执行器不是 OS 沙箱。
- 主动行为和对外发送受额度、静默时段、渠道白名单约束，审批通过不等于已经发出。
- 桌面打包只有方案稿（Tauri sidecar），没有代码。
- 源码前端构建依赖 Node 工具链；带网页的 wheel 使用 `python scripts/build_release.py` 构建，普通源码 pip 安装不自动编译网页。

## 文档

- [开发者与 AI 协作指南](docs/AI_DEVELOPMENT_GUIDE.md)：代码地图、开发约定、已知问题
- [接口契约](docs/contracts.md)：与 whale-io 的边界
- [进度与差距表](docs/gap-analysis-2026-10-30.md)：每项功能的证据与状态
- [路线图](docs/roadmap-2026-10-30.md)
- [桌面打包方案](docs/packaging-desktop.md)（规划稿）
- [贡献指南](CONTRIBUTING.md)
