# 更新批次目录

这是 Dreaminutes Fork 的开发批次，不是上游正式发行版本。每批一页，按编号查看。

| 批次 | 状态 | 内容 |
| --- | --- | --- |
| [01 — 动作接口与 Windows 启动](01-device-actions-and-windows.md) | 已推送 | 动作接口、回执状态统一；Windows 进程锁 |
| [02 — 回执校验与数据目录](02-receipts-and-data-paths.md) | 已推送 | 回执校验解耦；诊断数据库路径修复 |
| [03 — 设备互联整理](03-device-interoperability.md) | 两仓已推送 | 生成能力词表、请求归属、五阶段联合检查与手动 CI |
| [04 — 协议、安装包与锁](04-schema-packaging-and-locks.md) | 本机验证完成 | 嵌套 Schema 校验、包内协议、QQ 跨平台单实例锁 |

另见 [汇总说明](../../FORK_UPDATES.md) 与 [全部代码差异](https://github.com/dreaminutes/whale-core/compare/main...refactor/simplify-integration)。
