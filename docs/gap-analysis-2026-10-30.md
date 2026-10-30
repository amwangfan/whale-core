# 现状—目标差距表与最小实施切片（2026-10-30）

> 依据 [roadmap-2026-10-30.md](roadmap-2026-10-30.md)（v2，含 §15-18 工作约定）§12/§13/§17 产出：
> 先审查、再给差距、再切片、随后直接进入实现。
> 状态口径（§17.2）：**implemented（有代码）/ tested（有自动化测试）/ integrated（接入真实运行链路）/
> externally_verified（真实服务联调）/ stub/mock / missing**；表中用中文等价标注。
> 评审基线：`main @ b401af7`，`pytest tests/ -q` = **288 passed**（纯离线，参数化展开后），
> src 约 6.5K LOC，零第三方依赖；QQ 链路在 N100 生产真实运行（收→DeepSeek→审批→实发，
> 2026-10-30 04:30 端到端验收）。
> whale-io 侧评审：`docs/architecture.md`、`docs/plugin-architecture.md`（N100 本地副本）；
> `cloud-model-orchestration.md` / `reference-projects.md` 留待 P1 启动时评审。
> 评审方式（§16 记录）：三个并行调查子代理分读 core+memory / plugins+actions+cloud /
> life+perception+initiative+ui+tests（行号级证据），主代理亲读 shell_policy、plugins/manifest、
> registry、gateway、life/initiative 并复核子代理结论一致性后汇总。

## 1. 差距表（按 roadmap §8 P0 顺序）

| # | 路线图表项 | 现状 | 证据 | 差距 / 行动 |
|---|-----------|------|------|-------------|
| 1 | 统一 Runtime / 对话编排 | **PR-2 已落地** | `core/runtime.py` ConversationOrchestrator（记忆指令捷径/意图分流/有界工具执行/结果回流）；`core/router.py` StubModelRouter 保持测试替身定位 | 多轮/记忆/人格复用 WhaleCompanion；事件流 WhaleAgent 并存（roadmap §3.1） |
| 2 | 结构化 IntentPlan | **PR-2 已落地** | `core/intent_plan.py`：校验+模型文本提取（35 例测试） | 「模型提议、核心裁决」：approved 等字段丢弃、risk_hint 仅参考、intent_id 核心生成 |
| 3 | 插件管理器 | **PR-3 已落地** | `plugins/manager.py`（403 行，33 例测试）：清单 JSON 目录加载（显式、非自动发现）、api_version 兼容校验、enable/disable 生命周期 + kv 持久化回放、健康检查五态、config_schema 校验 + 脱敏视图、工厂接线、故障隔离 | 卸载不删记忆（disable 只摘注册）；entrypoint 外部进程启动方式留待首个真实外置插件 |
| 4 | 记忆 V1 | **PR-1 已落地（2026-10-30）** | store/sqlite/continuity/curation 真实实现 | ~~① chat() 不传 history~~ → `memory/history.py` 会话环形缓冲 + `chat(conversation_id=)`；~~② 召回断层~~ → chat 情节作为会话记忆注入（curated 事实仍优先）；~~③ 无导出~~ → `memory/export.py` JSONL 导出/导入；~~④ 双实现三处不一致~~ → parity 对齐 + `test_memory_parity.py` 双后端参数化。新增 `记住：/忘掉：` owner 指令（不调模型、带证据链）。测试：+48 例 |
| 5 | 模型 provider | **PR-4 已落地** | `plugins/model_provider.py`：`register_openai_compatible`（kind=model，provides `llm.chat`）+ `resolve/require_chat_model` 能力解析；`core/deepseek.py`：ModelError 六类分类（config/auth/rate_limited/server/network/bad_response）+ 瞬时错误重试一次 + http 回环/私网放行（公网 http 仍拒，R3）+ `info()` 状态视图（不含 key） | 内置系统提示删除「不声称具有人类情感」一句（与 PR-0 回合入的禁忌 5→4 对齐，roadmap §7.4 一致性）；runner 切换到能力解析留待 PR-9 配置档统一接线 |
| 6 | web.search / web.fetch | **缺失** | `shell_policy.SAFE_EXTRA_TOOLS` 只有工具名（L40-47） | 无任何实现 |
| 7 | CLI（setup/doctor/run/plugins） | **缺失** | — | 仅 examples/ 两个脚本 |
| 8 | 网页控制台 MVP | **PR-7 API 层已落地** | `api/server.py`（stdlib，16 例回环测试）：§5.3 路由轮廓主体已就位（status/health/plugins/memory/chat/approvals/events-SSE）；token 必填+脱敏 | 前端 web/（React+Vite）在 PR-8；WebSocket 未做（SSE 够 MVP） |
| 9 | 配置档 demo/cloud/local | **部分** | runner 有 stub 脑回退（DEEPSEEK_API_KEY 缺失时） | 无 profiles/ 正式配置档 |
| 10 | 基础安全（API 鉴权/Secret 遮蔽/审计） | **部分** | 监听器 Bearer 强制、shell/插件审计脱敏已测试 | API 层不存在；随 PR-7 补齐 |
| 11 | 发布基础 | **缺失（owner 2026-10-30 决定暂缓）** | 根目录**无 LICENSE**；licenses/ 仅第三方卡许可 | roadmap §10.13 发布阻塞项；LICENSE 选择留待正式发布前由 owner 定 |
| 12 | 核心安全不变量（身份门/风险矩阵/频控/审计） | **已测试、已联通** | channels/initiative/plugins/shell_policy 全链路生产在用 | 保持，见 §4 放宽边界 |

## 2. 可复用资产（禁止重写，直接复用）

- **插件四件套**：`plugins/manifest.py`（六分类+风险级+校验）、`registry.py`（显式注册+kind 唯一归属）、`gateway.py`（fail-closed 单一执行点）、`policy.py`（门控矩阵+交易限额带锁预占）；`processing.py`（ProcessorRouter 摘要截断+DelegatingIntentProcessor 工具循环参考实现）。
- **主动性与账本**：`life/initiative.py` ProactiveOutbox（草稿/裁决/台账/反馈/双配额/unknown 不重试）——生产验证。
- **QQ 生产链**：`life/wq_push.py`（SSE 监听）、`life/inbox.py`（幂等队列+租约恢复）、`life/qq_weq_plugin.py`（channel 插件范例）、`life/weq_send.py`（真实 tRPC 发送）、`life/full_mode_window.py`（按需开窗）。
- **人格**：`ui/persona_cards.py`（内置+导入+切换+全局禁忌硬前缀）——生产在用。
- **API 层技术先例**：wq_push/qq_onebot 的 stdlib `http.server.ThreadingHTTPServer` + token 模式，离线可测。
- **云端闭环骨架**：`cloud/loop.py`（规则选择器+门控裁决真实，派发端 stub）——PR-2 的 Runtime 可与之合流。

## 3. 最小实施切片（PR 序列）

每片一个可验收目标：目的 / 范围 / 测试 / 回滚 = revert。依赖序严格按 roadmap §8「重要依赖顺序」。

| PR | 目标 | 主要新增/改动 | 验收要点 |
|----|------|--------------|----------|
| **PR-0** 文档与合规 | 任务书与差距表入仓；LICENSE 落地 | `docs/roadmap-2026-10-30.md`、本文档、`LICENSE`（等 owner 选定）、README 状态表 | CI 绿；文档状态口径诚实 |
| **PR-1** 记忆 V1 修复 | 补齐 roadmap §7.4 三个已确认缺口 | **已落地（2026-10-30）**：`memory/history.py`（会话环形缓冲 kv 持久化）、`core/companion.py`（history 传递 + chat 情节注入召回）、`memory/store.py`+`sqlite_store.py`（parity 对齐）、`memory/export.py`（JSONL 导出/导入）、`life/qq_workflow.py`（conversation_id 接入 + 记住/忘掉指令） | 多轮历史测试（模型侧断言 history）；召回修复测试；parity 双后端参数化 16 例；导出/导入 12 例；指令路径 8 例。全量 336 绿 |
| **PR-2** IntentPlan + Runtime 骨架 | 结构化意图 + 统一对话编排 | **已落地（2438ae8）**：`core/intent_plan.py` + `core/runtime.py` + `core/memory_commands.py`（指令共享化）+ `examples/runtime_demo.py` | 76 例新增；全量 395 绿；模型自批字段丢弃有专测 |
| **PR-3** PluginManager | 清单文件加载 + 生命周期 | **已落地（998b14c）**：清单 JSON 目录加载、api_version 校验、生命周期+kv 回放、健康检查五态、config 校验+脱敏、工厂接线、故障隔离 | 33 例；全量 453 绿 |
| **PR-4** 模型 provider 化 | DeepSeek 注册为 model 插件（provides `llm.chat`） | **已落地（fce42b3）**：`plugins/model_provider.py` + deepseek ModelError 六分类/重试/回环 http 放行/info() | 18+13 例；全量绿 |
| **PR-5** 搜索工具 | `web.search` + `web.fetch` 官方插件 | **已落地（4381011 + ea8b1cb，47 例）**：web_search（Tavily/Brave/DDG 回退链，DDG 202 反爬诚实降级；Bing 已退役不收）+ web_fetch（SSRF is_global 肯定式+逐跳重校验） | SSRF 负向表（含 CGNAT/云元数据）；key 不进异常 |
| **PR-6** CLI | `whale setup/doctor/run/plugins` + `api` | **已落地（f0c407c + 3adb678）**：`cli/main.py`（30 例）——setup 三档/doctor 诊断/demo REPL/plugins 管理/memory 导出导入/api 入口（token 三源解析） | config.json 只存 env 引用名；mock 带 [mock] 前缀 |
| **PR-7** API 层 | REST + SSE，token 鉴权 | **已落地（1062a0e + 9fc9868 + 5446ea1）**：`api/server.py`（22 例回环测试）——§5.3 路由 + 静态 SPA 托管（防穿越）+ /tasks + 插件配置端点；stop() 死锁修复 | token 必填/常量时间比较/脱敏视图/64KB body 上限 |
| **PR-8** 控制台 MVP | 总览/插件/记忆/审批/对话/事件 | **已落地（44dd57c）**：`web/`（Vite+React18+TS strict，vitest 7 例，CI web-build.yml）；10 项端到端冒烟（真 API + SPA + SSE） | 任务页签与插件配置表单在 PR-13 增强 |
| **PR-9** 配置档 | ~~demo~~/cloud/local | **已落地（07955eb）+ owner 修订（2026-10-30 后续）：demo/mock 档整体移除**（「完全没必要」）——profiles 只余 cloud/local 两档真实装配；插件 register_* 不再有 mock 缺省（缺 provider 诚实 ValueError）；Mock 类移入 `tests/_doubles.py` 仅作测试替身。CLI/README/指南同步 | 全量绿 |

### P1/P2 已落地（2026-10-30 同日并行推进）

| PR | 内容 | 状态 |
|----|------|------|
| PR-10 | ASR/TTS 契约 + OpenAI 兼容 provider + Mock（`plugins/speech.py`） | 已落地（2c16256，39 例；真实服务未联调缺 key） |
| PR-11 | Shell 受控执行器（`cloud/shell_executor.py`：超时强杀/输出截断/env 擦洗/cwd 白名单/降权） | 已落地（6e6b1f3，14 例） |
| PR-12 | media.image.generate 契约 + provider + Mock | 已落地（f4d6b01，44 例；未联调） |
| PR-13 | 控制台增强：/tasks + 配置端点（5446ea1）+ 前端任务页签/配置表单（65dae8f）+ /plugins/{id}/test（5bacbe6） | 已落地（vitest 10 例 + 端到端冒烟） |
| PR-14 | 混合召回 HybridCurator（词项+嵌入融合；嵌入为可选 provider） | 已落地（30c1c58，20 例） |
| PR-16 | media.video.generate 异步任务契约 + 生命周期管理器 | 已落地（f20efe5，19 例；未联调） |
| PR-17 | 自主探索管理器（兴趣台账/探索窗口/反馈调权/退避/日记） | 已落地（07ac6b3，27 例） |
| PR-18 | Skill 工作流（Markdown 驱动工具组合） | 已落地（2275807，30 例） |
| PR-19 | 桌面打包方案（Tauri sidecar） | 规划稿已落地（docs/packaging-desktop.md，3e7c847） |

**P0 端到端主线**（roadmap §13.5）：✅ 已由 `tests/test_e2e_p0.py` 离线验收（a90c8f4）——多轮对话 → 记忆召回 → 搜索工具 → 带来源总结 → 任务轨迹审计，全链路断言。

## 4. 安全限制现状评估与放宽边界（owner 已定方向 2026-10-30：「合理放宽，不过于严格」）

背景：当前是**单 owner 个人陪伴**部署。部分保守默认面向「多用户/不可信环境」设计，对 owner 体验构成摩擦。放宽原则：**做成可配置策略 + 单 owner 场景的合理默认值，而不是删除机制**；面向他人安全的底线不动。

### 放宽（做成配置项/放宽默认）

| # | 现状 | 改法 |
|---|------|------|
| R1 | communicate 动作一律逐笔人审；生产靠 `--live-auto-send`+env 双开关旁路 | **已落地**：`PluginActionPolicy(auto_approve_risks=...)` 按风险级免审（communicate 可配 auto；transaction 永不可配——构造即拒；身份门不受影响）。生产 runner 双开关保持不动（已验证路径），新装配用策略参数 |
| R2 | 主动行为 `daily_limit=2`、被动回复静默豁免核心默认 False | **维持核心保守默认，放宽落在部署层**：runner 侧 env 已是主动 10/日 + 被动豁免 True（test_qq_policy_runner 锁死该行为）；核心默认保持保守 + 显式开启，避免库默认在多用户场景误伤 |
| R3 | DeepSeek base_url 强制 https | 允许回环/私网 http（`http://127.0.0.1`、`http://10/172.16/192.168` 段）；公网 http 仍拒绝 |
| R4 | 记忆写入长期事实需显式 consent 参数 | owner 直聊消息视为 consent（陪伴场景「记住我」是默认期待）；curator 证据/来源/可删除语义不动；提供「忘掉」命令路径 |

### 保持不动（底线，不参与放宽）

身份门与渠道白名单（保护的是 owner 自己的账号风控）；transaction 逐笔人审+限额；Shell 33 条硬拒清单（全是灾难级：rm 根目录/fork bomb/关防火墙/抹审计/git push --force 等，无误伤日常）；密钥不进日志/manifest；注入防护（卡片/记忆=上下文非指令）；发送结果 unknown 不自动重试；插件不做自动发现（PR-3 是「显式清单加载」不是自动扫描）；GLOBAL_TABOOS 4 条。

## 5. 风险与注意事项

- **stdlib API vs FastAPI**：选 stdlib 保离线测试 purity，代价是手写路由/SSE；若后续要 WebSocket 再评估（SSE 对控制台推送已够）。
- **免 key 搜索**（如 DDG HTML 端点）稳定性一般：provider 必须可插拔，失败有明确错误。
- **记忆自动召回打通后**，召回质量靠词项匹配（bigram）——embedding 混合召回是 P1 可选项，不在 P0。
- **web/ 前端**引入首个 Node 工具链：构建产物不入库，CI 构建即可。
- 每次 PR 同步更新本文档状态列；诚实标注，不把 stub 说成已联通。

## 5.6 意图模型选型实证（2026-10-30）

生产实测（AgentScope 平台，同一意图提示词 × 6 条代表输入）：

| 模型 | JSON 解析成功率 | 平均延迟 | 备注 |
|------|----------------|----------|------|
| qwen3.8-flash | 6/6 | **7.1s** | 边缘语义略糙（图片请求误挂 web.search） |
| kimi-k3 | 6/6 | 12.8s | 边缘语义更准，但 ~2× 慢 |

**决策：qwen3.8-flash**。理由：意图层职责是路由+抽取（陪伴表达由主模型负责），
flash 档快 2×、成本低；kimi-k3 的质量优势对路由任务不构成差价理由。
若未来引入多步工具规划再评估 kimi-k3/qwen3.8-max。

接线：`QQ_INTENT_MODEL=qwen3.8-flash` 开启（runner 组装 SearchIntentRouter
规则优先 + ConversationOrchestrator；搜索工具按 TAVILY/BRAVE key 可用性挂载）。

## 5.5 独立安全审查与加固（2026-10-30，1b3271f）

roadmap §16.2.6 要求的独立安全审查（与实现者分离的子代理，逐行读码+实证）已完成。
结论：**1 高 8 中全部修复**（web_fetch TOCTOU/rebinding 连接层 peer 复核、NAT64 显式拒绝、
API 客户端跳转认证头泄漏、shell 解码逃逸/降权附加组、配置嵌套脱敏、回执 ":ok:" 子串伪造、
augmented 注入面强分隔），低危批量加固一批（总时限/字面 IP 判定/multipart 白名单/响应上限/
异常原文不回显）。回归测试 +7 例。接受并记录的已知限制（回环单 owner 部署可接受）：
API 无连接读超时（Slowloris）、approvals 决策可覆写、ToolSpec 风险级自声明不交叉校验——
三者均为宿主信任域内事项，已在相应模块 docstring 标注。

## 6. 并行分工约定（roadmap §16 落地）

| 角色 | 承担 | 实例 |
|------|------|------|
| 主代理（本会话） | 架构决策、接口冻结、集成、全量测试、对外报告；core/ 与 life/ 等共享文件的直接改动 | 本差距表；各 PR 的契约定义与合入 |
| 调查/研究子代理 | 只读调研，出证据报告 | 本次三路线现状精读 |
| 实现子代理 | 边界清晰、文件不重叠的模块实现（契约由主代理先冻结） | PR-1 起按模块切分 |
| 独立审查子代理 | 安全敏感 PR（审批/密钥/SSRF/删除语义）的独立评审，与实现者分离 | PR-1 记忆删除语义、PR-5 SSRF、PR-7 鉴权 |

冲突规避：同一文件不并行；共享契约（IntentPlan、Capability schema、API 路由）主代理先定稿再分包。
Token 约定（§15）：以工程质量为先，充分迭代「分析→实现→测试→审查→修正」，不为省 token 省略测试与审查。
