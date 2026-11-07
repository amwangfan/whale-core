# 第 01 批：动作接口与 Windows 启动

状态：已完成、已推送。日期：2026-11-07。

## 改动

- 将设备动作接口、不可用异常与回执状态集中到 `devices/actions.py`。
- 真实网关、模拟设备与生活逻辑共用定义，旧导入路径继续兼容。
- Windows 使用原生字节锁，保留同目录单实例保护；Linux/macOS 继续 flock。
- 新增 Core/Mobile 实际互联入口说明。

## 验证

201 项相关测试通过，跨仓协议 36 个文件一致；Mobile 协议 20/20 通过。本地网页成功启动。未做 Android 真机、语音、眼镜联调。

## 对应提交

- [3a56f8e — Windows 进程锁](https://github.com/dreaminutes/whale-core/commit/3a56f8e)
- [e5a507c — 设备动作接口收拢](https://github.com/dreaminutes/whale-core/commit/e5a507c)
- [仅查看本批差异](https://github.com/dreaminutes/whale-core/compare/7ef61f3...e5a507c)

[返回批次目录](README.md)
