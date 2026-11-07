# 第 02 批：回执校验与数据目录

状态：已完成、已推送。日期：2026-11-07。

## 改动

- 回执校验归到设备层，失败分类不再直接依赖生活层自主裁决。
- 旧校验函数继续抛出原 `AutonomyError`，保持错误码和消息兼容。
- 修复 Windows 诊断数据库路径，随记忆库目录创建，避免散落源码目录。

## 验证

157 项相关测试通过，跨仓协议 36 个文件一致。没有移动或删除旧数据库；运行中的服务未重启，新路径在重启后生效。未做真机联调。

## 对应提交

- [8f994f1 — 回执校验解耦](https://github.com/dreaminutes/whale-core/commit/8f994f1)
- [df94dd6 — 诊断数据库路径](https://github.com/dreaminutes/whale-core/commit/df94dd6)
- [仅查看本批差异](https://github.com/dreaminutes/whale-core/compare/e5a507c...df94dd6)

[返回批次目录](README.md)
