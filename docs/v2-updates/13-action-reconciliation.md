# 第 13 轮：持久状态、离线重复请求与缓存对账

重复 action 请求、并发占键失败和显式 action/status 共用一套持久状态投影。已有动作不再要求设备当前在线；设备状态/scope 检查继续执行，新动作仍必须在线。过期推定 unknown 标记来源，返回嵌套数据的独立快照，不让调用方修改存储。

EmbeddedWssTransport 增加只读 action_status，DeviceGatewayV2 增加 reconcile_action：通过原通道读取在途、timeout/unknown 的当前状态，重复 execute 命中缓存时走对账，不再永久卡在 received。它不会改选其他通道或重新下发动作。并发查询合并，取消单个等待者不取消共享对账；缓存淘汰同步清理通道引用。

完成的 succeeded/failed/cancelled 保持缓存；迟到的真实结果可更新本地等待超时，原始持久记录继续约束终态语义。同步 action_status 保留原来的缓存快照 API，生产 REST 持久状态入口保持不变。没有声称进程内编排器可以替代完整的持久恢复系统。

验证：81 项设备/状态/路由及真实 WebSocket 回归通过，覆盖离线重复请求、嵌套快照、迟到回执、8 个并发查询、等待取消和缓存容量。

[返回更新索引](README.md)
