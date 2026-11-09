# 第 08 轮：统一多通道观察与执行检查

DeviceGatewayV2 的 observe 此前直接选路，没有检查 registry 中的设备状态和 scope。现在与 execute 共用 _authorize，并在异步选路结束、调用通道前复查，处理选路期间的吊销或 scope 收回。

已授权观察、既有选路优先级和幂等行为保持不变；未注入 registry 的开发模式仍可使用。

验证：22 项测试通过，包括观察拒绝、有效观察、选路期间吊销/权限变化和已有路由/幂等回归。此编排器仍不代表所有生产 REST 入口已接入 ADB/SSH。

[返回更新索引](README.md)
