# Dreaminutes Fork 更新说明

本页记录 `refactor/simplify-integration` 分支的开发进度，便于原作者和协作者审阅。修改只推送到 Dreaminutes 的 Fork，尚未合入 amwangfan 原仓库。

**按批次查看：[第 01 批](docs/updates/01-device-actions-and-windows.md) · [第 02 批](docs/updates/02-receipts-and-data-paths.md) · [第 03 批](docs/updates/03-device-interoperability.md) · [总目录](docs/updates/README.md)。**

## 当前状态

第 04 批已完成协议校验、安装包协议资源和 QQ 跨平台单实例锁修复，详情见 [批次目录](docs/updates/README.md)。后续将再迭代两批后停止。

2026-11-07：前三批实现和本机验证已完成，详见编号更新页。当前网页控制台是本地服务，不是公网演示站点。下面的测试结果不代表 Android 真机、语音或眼镜联调完成。

## 第二批：回执校验解耦与 Windows 数据路径

提交：`8f994f1`、`df94dd6`。

- 将设备回执校验归入 `devices/actions.py`，设备失败分类不再导入生活层自主裁决模块。
- 保留 `life.autonomy.validate_action_receipt` 入口，旧代码仍能捕获原有 `AutonomyError`，错误码和消息保持一致。
- 修复诊断数据库路径在 Windows 上按 `/` 拆分的问题，数据库现在跟随记忆库目录创建，避免落到源码目录。
- 同步更新 [Core/Mobile 互联说明](docs/core-mobile-integration.md)。

验证：157 项设备、诊断 API 等相关测试通过，36 个跨仓协议文件一致。没有移动或删除现有数据库；已运行服务需重启后使用新路径逻辑。

## 第一批：设备动作接口收拢与 Windows 启动

提交：`3a56f8e`、`e5a507c`。

- 新增稳定设备动作接口，集中 `DeviceActionPort`、`DeviceUnavailable` 和回执状态集合，减少三处重复定义。
- 网关、模拟设备和生活逻辑共用同一份状态，保留旧导入路径与对象身份兼容。
- Windows 进程锁改用原生字节锁，保留同一数据目录单实例保护；Linux/macOS 继续使用 flock。
- 增加互联文档，列明配对、连接、感知、动作及大输出查询的实际入口。

验证：201 项相关测试通过；Mobile 协议示例与负例 20/20 通过，36 个跨仓协议文件一致。Core 网页在 Windows 本地成功启动。

## 第三批：设备互联整理

协议生成四份能力表，补齐参考网关词表，绑定请求目标设备，提供五阶段联合检查和手动 CI。Core 125 项、Mobile 20 项测试通过，协议示例 20/20，镜像 36 文件一致。Linux 全量测试与 APK/真机验收待执行，详见 [第 03 批](docs/updates/03-device-interoperability.md)。

## 如何查看与反馈

- [全部代码差异](https://github.com/dreaminutes/whale-core/compare/main...refactor/simplify-integration)
- [开发分支](https://github.com/dreaminutes/whale-core/tree/refactor/simplify-integration)
- [Mobile Fork](https://github.com/dreaminutes/whale-mobile)

目前未创建上游 PR；原作者的 main 未修改。完整命令日志与运行数据留在本地，不上传密钥、记忆、对话内容或数据库。
