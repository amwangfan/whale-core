# V2 互联整理：逐轮更新

日期：2026-11-08。保留现有功能，减少重复实现，修复两端连接。

本分支从 snow 最新 V2 上游重新建立，沿用现有 Fork，旧 `refactor/simplify-integration` 分支保留。Core 基线 `bbefca8`；Mobile 基线 `646c4ec`。旧 PR #75/#48 已关闭，本轮没有重新打开。

| 轮次 | 内容 | 改动位置 |
| --- | --- | --- |
| 01 | [统一 V2 契约路径](01-contract-paths.md) | Core；Mobile 保持现有 V2 实现 |
| 02 | [加载时检查嵌套规则](02-schema-loading.md) | Core；Mobile 保持现有 V2 实现 |
| 03 | [安装包携带 V2 协议](03-release-packaging.md) | Core；Mobile 保持现有 V2 实现 |
| 04 | [发送前预检与状态保护](04-outbound-validation.md) | Core；Mobile 保持现有 V2 实现 |
| 05 | [完整跨仓检查](05-contract-sync.md) | Core + Mobile 同步检查工具 |
| 06 | [读取 Mobile 真实输出格式](06-output-interop.md) | Core 图片/文本输出适配 |
| 07 | [在途动作状态](07-inflight-receipts.md) | Core 幂等记录适配 |
| 08 | [统一观察与执行检查](08-route-authorization.md) | Core 多通道编排器 |
| 09 | [确认等待项保护](09-confirmation-waiters.md) | Core 设备确认下发 |
| 10 | [会话清理](10-session-cleanup.md) | Core 连接生命周期 |

每轮说明包含实际改动与验证证据。源码提交与本索引均在 `refactor/v2-integration`，没有直接修改 snow 的主分支。

第 01–05 轮验证：Core 相关回归 **129/129**，新版网页类型检查与构建、wheel 构建及源码目录外安装冒烟通过。

第 06–10 轮最终验证：Core 相关回归 **165/165**，Mobile 协议校验 **72/72**、工具测试 **4/4**，两仓 **68 个契约文件一致**。WebSocket 测试有 47 条现有连接 API 弃用警告，不影响结果。本轮没有运行完整 Python 全量测试或 GitHub CI。

当前源码另行构建 wheel 并在源码目录外安装，V2 契约加载、网页和带认证的 API 冒烟通过。前端未改，复用第 03 轮的 V2 网页构建。

开工已拉取两个上游，main 无新增提交。第 06–10 轮发现的问题位于 Core 的适配与连接边界；Mobile 生产代码未改，更新了本索引的入口和联合验证说明。

验证边界：本机已检查 Python V2 链路、跨仓协议与 wheel；没有 Android 真机、Flutter/Android 构建或 Linux/macOS 冻结包的验收结果。本次没有重启用户正在使用的服务，也没有修改其令牌、模型配置或数据。

运行记录集中保存在本机工作区 `logs/12-V2分支初始化` 至 `logs/22-V2第10轮`；不将原始运行记录和私人配置提交到源码仓库。第 06–10 轮已完成，按要求停止。
