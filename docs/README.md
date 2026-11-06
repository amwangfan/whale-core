# Whale Core 文档入口

> 本页是 `docs/` 的索引：上半部分是**当前有效**的架构 / 开发 / 部署 / 安全约定文档，
> 下半部分是**历史阶段文档**（归档快照，不追着现状改）。
> 面向普通使用者的入口是仓库根 [README.md](../README.md)（[English](../README_EN.md)）。

## 当前有效文档

### 架构与开发约定

| 文档 | 内容 |
| --- | --- |
| [AI_DEVELOPMENT_GUIDE.md](AI_DEVELOPMENT_GUIDE.md) | 开发者与编程 AI 协作指南：三仓分工、代码导航、运行链路、实际权限与验收方式 |
| [contracts.md](contracts.md) | 智能层与基础设施（whale-io）的边界契约：事件 / 意图 / 动作 |
| [vision-map.md](vision-map.md) | 原设想 → whale-core 类型与模块落点对照（权威覆盖表在 whale-io） |
| [devices.md](devices.md) | 设备面（whale-mobile 接入，D1–D4 范围说明）；协议 canonical 事实源是 **whale-mobile** 的 `contracts/v1/`（本仓同目录为镜像，漂移校验见 `scripts/contract_sync_check.py`） |

### 部署、发行与运维

| 文档 | 内容 |
| --- | --- |
| [deployment.md](deployment.md) | 安装、运行与恢复（Linux / macOS，单 owner 部署） |
| [local-runtime.md](local-runtime.md) | 本地运行时归属：canonical checkout 与 `whale runtime` start/status/stop |
| [packaging-desktop.md](packaging-desktop.md) | 桌面入口与免 Python 冻结包 |
| [g4-operations.md](g4-operations.md) | 运维可靠性与开发者自检：`whale doctor`、能力健康视图、provider 指标、插件信任级别 |

### 接口与配置

| 文档 | 内容 |
| --- | --- |
| [management-api.md](management-api.md) | 管理 API v1：端点、鉴权、限额 |
| [media-and-voice.md](media-and-voice.md) | 语音与媒体能力的配置、调用与验收边界 |
| [shell-sandbox.md](shell-sandbox.md) | Linux Shell 受控执行与 Bubblewrap 沙箱 |

### Whale Studio（多人版，设计 / 实施文档，2026-10-31）

| 文档 | 内容 |
| --- | --- |
| [studio/whale-studio-prd-2026-10-31.md](studio/whale-studio-prd-2026-10-31.md) | 产品需求与安全架构（PRD / RFC） |
| [studio/architecture.md](studio/architecture.md) | 架构设计（S0 基线审计后的实施版） |
| [studio/threat-model.md](studio/threat-model.md) | 威胁模型（S0） |
| [studio/migration-plan.md](studio/migration-plan.md) | 数据分类与迁移计划（S0） |
| [studio/whale-studio-goal-chain-2026-10-31.md](studio/whale-studio-goal-chain-2026-10-31.md) | Goal 目标链路与多智能体实施计划 |

### 阶段记录（仍在 docs/ 根部的审阅 / 验收记录）

| [proactive-greeting-roadmap.md](proactive-greeting-roadmap.md) | 主动问候：默认关、开则 dry-run、3min 循环与模型分工 |
| [eng-priority-20261105.md](eng-priority-20261105.md) | 2026-11-05 工程精简优先级与砍项 |


| 文档 | 内容 |
| --- | --- |
| [g3-scenarios.md](g3-scenarios.md) | G3 三条全天助手场景验收记录（模拟器层，2026-10-31） |
| [pr3-pr4-reconciliation.md](pr3-pr4-reconciliation.md) | PR #3 / PR #4 并行实现整合审阅（2026-10-30） |

运行截图在 [screenshots/](screenshots/)。

## 当前工程约定

以现有代码和 [AI 开发指南](AI_DEVELOPMENT_GUIDE.md) 为准，不把历史风险清单自动变成新功能的限制。

1. 优先实现好用的 AI 伴侣：人格、记忆、设备交互和主动行为按真实体验迭代；避免无意义的免责和重复防御性语句。
2. 设备授权按角色区分：日常手机需要相应的操作授权，专用 Root 手机允许持续授权自主执行。服务器 Shell 沙箱和手机 Root 是不同执行通道。
3. 以真实回执表达状态：有代码、单测通过、真实联调、已经送达各有不同；暂存和结果未知不得冒充完成。
4. 跨仓先对齐契约，改动应有针对性测试；个人凭据与私密数据不进入提交和日志。

## 历史阶段文档（archive）

归档快照记录各阶段的目标、证据与验收边界，**不追着现状改**；当前状态以代码、测试和
[AI_DEVELOPMENT_GUIDE.md](AI_DEVELOPMENT_GUIDE.md) 为准。

### [archive/2026-10-30/](archive/2026-10-30/)

2026-10-30 阶段：从现状审计到非 Windows 可用性补全（基线 `main@a53c707` → 932 passed）。

| 文档 | 内容 |
| --- | --- |
| [roadmap-2026-10-30.md](archive/2026-10-30/roadmap-2026-10-30.md) | 总体技术方案与实施任务书（v2，含 §15–18 工作约定） |
| [gap-analysis-2026-10-30.md](archive/2026-10-30/gap-analysis-2026-10-30.md) | 现状—目标差距表与最小实施切片（PR-0…PR-19）、安全放宽边界 R1–R4 |
| [completion-plan-2026-10-30.md](archive/2026-10-30/completion-plan-2026-10-30.md) | 非 Windows 可用性补全计划（六阶段） |
| [completion-report-2026-10-30.md](archive/2026-10-30/completion-report-2026-10-30.md) | 补全完成矩阵、实测结果与真实联调边界 |
| [security-review-2026-10-30.md](archive/2026-10-30/security-review-2026-10-30.md) | 独立安全复核：直接修复、集成复核与部署验收边界 |

归档时仅修复了因移动失效的相对链接（截图路径、仓内路径引用），未改写原文内容。

## 维护约定

- 新增、移动或归档文档时同步更新本页，并在 [AI_DEVELOPMENT_GUIDE.md](AI_DEVELOPMENT_GUIDE.md) §9 登记维护责任。
- 阶段文档归档到 `docs/archive/<日期>/`，用 `git mv` 保留历史；归档原文不改写。
- 测试组织（issue #29 起）：新测试按领域与层级放入 `tests/unit/`、`tests/integration/`、
  `tests/security/`、`tests/contract/`，命名 `test_<领域>_<主题>.py`，不再用里程碑名（P0/G2/G4…）；
  组织约定与旧名→新名映射见 [tests/README.md](../tests/README.md)。
