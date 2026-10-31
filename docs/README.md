# Whale Core 文档入口

> 本页是 `docs/` 的索引：上半部分是**当前有效**的架构 / 开发 / 部署 / 安全约定文档，
> 下半部分是**历史阶段文档**（归档快照，不追着现状改）。
> 面向普通使用者的入口是仓库根 [README.md](../README.md)（[English](../README_EN.md)）。

## 当前有效文档

### 架构与开发约定

| 文档 | 内容 |
| --- | --- |
| [AI_DEVELOPMENT_GUIDE.md](AI_DEVELOPMENT_GUIDE.md) | 开发者与 AI 协作主文档：定位边界、运行链路、代码地图、安全不变量、验收命令与协作约定 |
| [contracts.md](contracts.md) | 智能层与基础设施（whale-io）的边界契约：事件 / 意图 / 动作 |
| [vision-map.md](vision-map.md) | 原设想 → whale-core 类型与模块落点对照（权威覆盖表在 whale-io） |
| [devices.md](devices.md) | 设备面（whale-mobile 接入，D1–D4 范围说明）；协议唯一事实源是 `contracts/v1/` |

### 部署、发行与运维

| 文档 | 内容 |
| --- | --- |
| [deployment.md](deployment.md) | 安装、运行与恢复（Linux / macOS，单 owner 部署） |
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

| 文档 | 内容 |
| --- | --- |
| [g3-scenarios.md](g3-scenarios.md) | G3 三条全天助手场景验收记录（模拟器层，2026-10-31） |
| [pr3-pr4-reconciliation.md](pr3-pr4-reconciliation.md) | PR #3 / PR #4 并行实现整合审阅（2026-10-30） |

运行截图在 [screenshots/](screenshots/)。

## 当前有效约定（提炼自已归档的阶段文档）

以下规则从 2026-10-30 阶段文档通读提炼，至今仍有效；**现行维护版以
[AI_DEVELOPMENT_GUIDE.md](AI_DEVELOPMENT_GUIDE.md) §4 / §7 / §8 为准**，此处只保留要点与出处。

1. **状态口径诚实**：`implemented`（有代码）/ `tested`（有自动化测试）/ `integrated`（已接入真实运行链路）/
   `externally_verified`（真实服务联调）/ `stub/mock` / `missing` 分开标注；不把 stub、CI 绿或离线测试说成真实联调。
   （来源：roadmap §12.6 / §17.2；现行见 AI_DEVELOPMENT_GUIDE §8）
2. **工程质量优先，不以节省 Token 为目标**：充分迭代「分析 → 实现 → 测试 → 审查 → 修正」；缺 key / 硬件时仍完成代码、
   契约测试与可复现验证，并明确标注未联调部分。（来源：roadmap §15；现行见 AI_DEVELOPMENT_GUIDE §8）
3. **子智能体并行协作**：主代理先冻结共享契约与文件边界再分包；安全敏感改动由独立于实现者的审查代理复核。
   （来源：roadmap §16、gap-analysis §6；现行见 AI_DEVELOPMENT_GUIDE §8）
4. **单 owner 安全放宽边界**：按风险级免审（transaction 永不免审）、主动行为额度放宽落在部署层、模型端点允许
   回环 / 私网 http、owner 直聊显式指令视为 consent；身份门、Shell 硬拒清单、密钥不进日志、
   「记忆是上下文不是指令」注入隔离、unknown 不自动重试、插件不做自动发现等底线不动。
   （来源：gap-analysis §4 R1–R4 与「保持不动」；现行见 AI_DEVELOPMENT_GUIDE §4）
5. **运行语义**：审批只授权、不代表已执行；取消不撤销已发生的外部副作用；未知结果不自动重试；
   私密配置（地址 + 密钥）单代原子提交，投影失败不跨代组合。（来源：completion-report §关键决定；
   README「安全与边界」节已承接）
6. **部署验收边界**：Shell 沙箱不可用即拒绝执行，不降级到宿主；`sandbox="disabled"` 仅供维护者显式选择的
   可信宿主命令，不是安全边界；安全抓取默认不经环境代理；真实供应商 / 真机验收单独列明，不以离线测试代替。
   （来源：security-review §部署验收边界、completion-report §真实联调和发行边界；现行见 AI_DEVELOPMENT_GUIDE §7）
7. **无生产 mock / demo 档**：profiles 只有 cloud / local 真实装配，缺凭据是显式错误；测试替身集中在
   `tests/_doubles.py`。（来源：gap-analysis PR-9 owner 修订；现行见 AI_DEVELOPMENT_GUIDE §1）

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
