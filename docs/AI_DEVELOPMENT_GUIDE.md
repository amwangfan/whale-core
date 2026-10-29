# Whale Core · 开发者与 AI 协作指南

> 面向维护者、贡献者与编程 AI。**面向普通使用者的入口是 [README](../README.md)**。本文件是代码导航与工作约定，不替代源码、测试和契约文档。
>
> 更新范围：2026-10-30 的代码审阅快照。仓库可能继续快速变化，实施任何修改前必须以目标分支的最新代码为准。

## 1. 当前定位与边界

Whale Core 是独立的 Python 智能层及初步可运行的管理服务，核心原则是 **事件输入、意图输出；能力由 provider/插件实现，授权由宿主策略决定**。它不是手机、雷鸟眼镜和 VoIP 设备代码的总仓库；基础设施分别见 [whale-io](https://github.com/amwangfan/whale-io)。

- 当前核心使用 Python 3.11+；网页前端使用 React + TypeScript + Vite。
- 管理 API 当前基于 Python 标准库 HTTP server 与 SSE，并非 FastAPI。
- Demo 模式使用 mock 模型和模拟搜索数据；不能描述成实际 AI 服务已经接通。
- 真实模型在 cloud/local 档可配置，但当前默认对话编排并未自动接入意图识别模型。因此「已实现搜索工具」不等于在普通聊天中自然语言必然自动调用搜索。
- QQ/WeQ 相关适配代码、主动行为策略、音频桥契约已经存在，但具体生产部署与设备联调状态应另行核实。
- ASR/TTS、图像/视频生成提供契约、部分 provider 或 mock；对未接实际凭据的服务，不得称为真实联调完成。
- 项目根目录在本次审阅时尚无项目 LICENSE；`licenses/` 中第三方人设许可不是整个 whale-core 的许可。对外发行或复用代码前须由维护者确定许可证。

## 2. 代码地图（从职责而非文件名理解）

| 模块 | 代码入口 | 边界说明 |
| --- | --- | --- |
| 对话与工具编排 | `core/runtime.py`, `core/intent_plan.py`, `core/companion.py` | 会话、意图和工具结果；与事件驱动的 `WhaleAgent` 并存 |
| 模型端点 | `core/deepseek.py`, `plugins/model_provider.py`, `profiles.py` | 供应商接口可换；不能把某个提供商硬编码成授权层 |
| 记忆 | `memory/store.py`, `sqlite_store.py`, `history.py`, `curation.py`, `hybrid.py`, `export.py` | 会话历史、情节/偏好/人格、召回和导出；避免把未经确认的推断升级为事实 |
| 插件运行机制 | `plugins/manifest.py`, `registry.py`, `manager.py`, `gateway.py`, `policy.py` | 六类插件、注册、启停、配置、策略；配置正确不等于执行器已接线 |
| 工具与媒体接口 | `plugins/web_search.py`, `web_fetch.py`, `speech.py`, `media_image.py`, `media_video.py`, `processing.py` | Provider/Tool 具体实现应可替换，长任务和真实媒资独立处理 |
| 行动与云端执行 | `actions/`, `cloud/shell_policy.py`, `shell_executor.py`, `cloud/loop.py` | 模型只提议动作；Shell 策略不能代替 OS 沙箱 |
| 主动行为和消息渠道 | `initiative/`, `life/` | 身份绑定、打扰策略、QQ/WeQ、持久化收件箱、探索和台账 |
| API、CLI、Web | `api/server.py`, `cli/main.py`, `web/src/` | API 默认回环监听、Bearer 鉴权；React 前端不得直接操作核心数据库 |
| Skill | `skills.py` | 工作流组合层；Skill 文本不应具有高于工具策略的权限 |

进一步阅读：[契约](contracts.md) · [目标与工作约定](roadmap-2026-10-30.md) · [进度与差距](gap-analysis-2026-10-30.md) · [贡献指南](../CONTRIBUTING.md) · [桌面打包规划](packaging-desktop.md)。

## 3. 本地开发与验收

```bash
git clone https://github.com/amwangfan/whale-core.git
cd whale-core

python -m pip install -e ".[dev]"
python -m pytest tests -q

# 或使用 uv
# uv sync --extra dev
# uv run pytest -q

whale setup --profile demo
whale doctor
whale run                  # 注意：当前命令固定使用 mock demo REPL

# 网页控制台
pnpm -C web install
pnpm -C web build
whale api                  # http://127.0.0.1:8080
```

API 首次启动生成或从环境变量读取 token；`~/.whale/api_token` 为本地私密文件，不得加入 Git、Issue 或日志。浏览器需要手动填写 token。前端可运行 `pnpm -C web test`，Python 测试为 `pytest`。当前 CI 包含 Python 测试和按 Web 路径触发的前端构建/测试；仍需在真实服务条件下执行单独的端到端验收。

`cloud` 设置时须先配置 `DEEPSEEK_API_KEY`；`local` 使用 OpenAI 兼容的本地模型端点，需要正确设置 `LOCAL_MODEL_BASE_URL` 和 `LOCAL_MODEL_NAME`。无法连接实际服务时按失败处理，不静默降成真实回答。

## 4. 插件、Tool 与 Skill 不要混淆

- **PluginKind：** `input` / `output` / `channel` / `processor` / `model` / `memory`。这是实现/接入类别。
- **Capability：** `llm.chat`、`web.search`、`asr.transcribe` 等可被选择的能力。一个插件可提供多个能力。
- **Tool：** 模型/执行器可以调用的具体函数，需要参数校验、超时、回执和风险策略。
- **Skill：** 跨工具的可复用流程，不应天然拥有新的系统权限。

`PluginManager` 能加载显式 JSON manifest、保存启停状态并连接宿主提供的 factories。**不是安装了 manifest 就自动下载/安装依赖或接通执行器**。仓库中实现的内置 provider 也不代表它们已经被 `~/.whale/manifests/` 自动配置、启用。插件是否可用应检查：声明、配置、工厂接线、健康检查与真实联调。

风险由核心插件策略决定。普通插件不能替代 owner 身份门、ShellPolicy、审批与额度。

## 5. 已确认需要继续处理的问题（2026-10-30 代码审阅）

以下是进一步实现时应优先复查的具体风险，属于**代码层面的发现或限制**，不是对已发生事故的判断：

1. **`whale run` 与当前配置档不一致。** 在 `cli/main.py` 中，`_cmd_run()` 当前直接实例化 `MockChatModel`，没有使用 `assemble_runtime(profile)`。即使 `setup --profile cloud`，调用 `whale run` 仍进入 mock REPL。应在文档中说明并考虑统一运行入口。
2. **自然语言工具路由尚未完成默认接线。** `profiles.py` 构造 `ConversationOrchestrator` 时未设置 `intent_model`；`runtime.py` 在无意图模型时直接走普通聊天。因此注册网页搜索工具不意味着用户在默认聊天中说“帮我搜索”就会调用它。端到端测试里的意图模型注入与实际用户体验需区分。
3. **插件配置的“脱敏”不等于加密存储。** `plugins/manager.py` 将配置字典保存在 MemoryStore 的 kv 中，`config_view()` 仅在返回控制台时遮蔽字段。用户不应通过网页表单填写长期敏感凭据，除非已有单独 Secret Store 或环境变量引用。前端 `Plugins.tsx` 里对已掩码字段采用空编辑值，保存时整体替换配置，可能意外清空旧凭据；需要 PATCH/保留未修改 Secret 的语义和相应测试。
4. **审批页面的持久化回执与实际执行链路需核对。** `api/server.py` 的 `/approvals/{id}/decision` 当前写入 `life:decision:{id}`；应验证该记录是否被实际的 `ProactiveOutbox` 或发送流程读取、执行和确认，避免出现“网页显示批准但动作没有发生”的错觉。
5. **SSE 当前更接近一次事件回放。** `api/server.py` 的 `_events_sse()` 发送近期事件与一次 ping 后结束 handler；如果产品需要实时持续订阅，应增加真正的长连接循环、取消机制及断线恢复，或在前端明确采用定时刷新。
6. **Shell 不是完整沙箱。** `shell_executor.py` 有超时、输出截断、环境过滤和可选降权，但不自带完整的 OS 容器/系统资源隔离；`ShellPolicy` 字符串黑名单不能当作不可绕过的安全边界。
7. **发行许可仍未确定。** 未明确项目级 LICENSE 前，不能向下载者承诺某个开源许可证或自由再分发权限。

改进上述问题时优先增加回归测试，明确区分“代码实现”“mock 通过”“真实链路联通”。不要只在 README 中更改措辞来掩盖缺口。

## 6. 与 AI 协作的约定

用户已经明确：**编程 Token 不计费**。因此不要为了省 Token 省略必要的代码阅读、开源调研、测试、独立审查和反复修正；同样也不要为了消耗 Token 无端堆砌依赖和抽象。

**推荐使用子智能体**，尤其是跨模块任务：架构研究、后端实现、前端实现、安全审查、测试验证、发布文档可以并行分工。主智能体统一接口、文件修改范围、权限边界和集成验收，不直接拼接未经审查的补丁。适合持续推进的大型目标也可以采用 **Goal 模式**；是否实际启用取决于工作环境支持情况和当前任务需要。

建议每轮工作流程：审查当前分支与未提交改动 → 明确目标与验收标准 → 分任务/子智能体研究 → 冻结共享契约 → 实施 → 独立审查 → 运行测试 → 修正 → 报告。

不得：

- 无需核查就假定先前的差距报告仍然准确；
- 未经授权部署生产、切换账号、执行破坏性 Shell 或删除用户数据；
- 将 mock/stub、CI 绿色与真实硬件/服务联调混为一谈；
- 为追求“完全依照旧任务书”而覆盖已经正确的新实现。

完成后应报告：修改了哪些文件、为什么如此选择、实际运行了哪些测试、真实联通到哪一层、还有什么风险。

## 7. 文档维护约定

- `README.md` 和 `README_EN.md`：写给第一次访问项目的人；介绍用途、可体验功能、安装和当前限制。避免把内部类型枚举直接堆到首页。
- 本文：面向开发者和 AI 的代码地图、实现边界、协作规则和审阅发现。
- `docs/contracts.md`：长期维护事件与动作的精确接口。
- `docs/roadmap-2026-10-30.md` 与 `docs/gap-analysis-2026-10-30.md`：分别保留目标与阶段证据；代码变动时及时更新状态。
- `CONTRIBUTING.md`：简明贡献流程与不可突破的底线。

README 中展示的每条安装命令必须与当前实际 CLI 相符；任何“可用”说法都应标注 mock、需要额外凭据、已接线或已联调的边界。
