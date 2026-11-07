# 第 06 批：验证工具收尾（本次最后一轮）

状态：联合验证完成，随本页交付；本次迭代到此停止。日期：2026-11-07。

对应提交：[Core f176ab4](https://github.com/dreaminutes/whale-core/commit/f176ab4)、[Mobile 2dd31ac](https://github.com/dreaminutes/whale-mobile/commit/2dd31ac)。

## 更新

- 联合检查新增每阶段超时（默认 180 秒，可用 `--stage-timeout` 调整），超时、启动失败、中断和测试失败均给出明确结果，不继续报告后续阶段通过。
- 指定日志目录后，每次运行创建独立的 `run-*` 子目录，保留旧运行记录，不让 pytest 清理上一轮的临时证据。
- 每阶段保存日志并更新 `summary.json`，记录状态、退出码和耗时；部分失败也有摘要。
- Mobile 校验工具先检查 Schema 自身有效性、必需 Schema 和黄金示例覆盖，缺目录或无效规则返回明确失败。
- 能力生成器同时核对 hello 与 hello_ack 的枚举，防止声明端和接受端能力集合漂移。
- 不改变生产能力、授权范围或协议内容。

## 最终验证

- 五阶段联合入口全部通过：Core 185 项、Mobile 25 项测试通过；协议 20/20、镜像 36 文件一致，四份生成能力表一致。
- 工具故障回归覆盖：失败即停止、阶段超时、启动失败、重复运行不覆盖，以及非法超时参数拒绝。
- 前一批 wheel 在源码目录外安装验证通过，含协议、控制台和鉴权 API。
- 不宣称 Linux 全量、APK 或真机验收完成；Windows 全量发现的 POSIX 权限和符号链接环境问题仍在第04批说明中保留。

本次总共完成当前第04批及后续第05、06批，到此停止，不自动开始第07批。运行中的用户控制台未重启。

[本批 Core 差异](https://github.com/dreaminutes/whale-core/compare/a6d7ecb...f176ab4) · [本批 Mobile 差异](https://github.com/dreaminutes/whale-mobile/compare/6c4b270...2dd31ac) · [返回目录](README.md)
