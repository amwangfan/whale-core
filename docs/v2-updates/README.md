# V2 互联整理：逐轮更新

最近更新：2026-11-09。保留现有功能，减少重复实现，修复两端连接。

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
| 11 | [文件分页与完整输出](11-output-paging.md) | Mobile 文件读取/运行时 + Core 解码拼接 + 协议镜像 |
| 12 | [观察与采集完整链路](12-observation-pipeline.md) | Mobile 采集选项 + Core 坐标/元数据/签名解析 |
| 13 | [持久状态与只读对账](13-action-reconciliation.md) | Core 离线重复请求、原通道对账、缓存并发刷新 |
| 14 | [会话等待生命周期](14-session-wait-lifecycle.md) | Core 查询/动作/确认共同等待、断线收束、异步通知 |
| 15 | [REST/协议帧/运行时边界](15-rest-wire-and-runtime.md) | Core 参数与编码 + Mobile 门面转发、重绑定与格式兼容 |
| 16 | [全局运行锁与原子状态](16-global-runtime-and-state.md) | 跨平台运行锁、内存/SQLite事务、回执与清理竞争 |
| 17 | [全局生命周期与验证](17-global-lifecycle-and-validation.md) | 备份/QQ/录制重绑定、全量回归与可复现JVM入口 |
| 18 | [严格解析与参数一致性](18-strict-schema-and-options.md) | 参考schema对照、整数/JSON边界、录制选项纯策略 |
| 19 | [重连与会话归属](19-connection-ownership.md) | 握手确认顺序、等待项归属、旧WebSocket回调隔离 |
| 20 | [心跳扫描与客户端关闭](20-heartbeat-and-shutdown.md) | 快照复查、心跳恢复原子化、统一任务提交与幂等销毁 |

每轮说明包含实际改动与验证证据。源码提交与本索引均在 `refactor/v2-integration`，没有直接修改 snow 的主分支。

第 01–05 轮验证：Core 相关回归 **129/129**，新版网页类型检查与构建、wheel 构建及源码目录外安装冒烟通过。

第 06–10 轮最终验证：Core 相关回归 **165/165**，Mobile 协议校验 **72/72**、工具测试 **4/4**，两仓 **68 个契约文件一致**。WebSocket 测试有 47 条现有连接 API 弃用警告，不影响结果。本轮没有运行完整 Python 全量测试或 GitHub CI。

当前源码另行构建 wheel 并在源码目录外安装，V2 契约加载、网页和带认证的 API 冒烟通过。前端未改，复用第 03 轮的 V2 网页构建。

开工已拉取两个上游，main 无新增提交。第 06–10 轮发现的问题位于 Core 的适配与连接边界；Mobile 生产代码未改，更新了本索引的入口和联合验证说明。

第 11–15 轮按完整链路扩大改动：最终 Core **249/249**，Mobile 纯 JVM **18/18**，协议 **72/72**、工具 **4/4**，契约镜像 **68 个文件一致**。跨语言 203 页、202048 字节逐字节一致；当前源码 wheel 与源码外安装冒烟通过。本批修改了两端生产实现，完整 Android 构建与真机仍待验收；没有声称 CI 或完整 Python 全量测试通过。

第 16–17 轮全局验证（2026-11-09）：完整 Core Python 套件 **2627 passed / 18 skipped**，网页 **96/96**、类型检查及构建通过，Mobile 纯 JVM **108/108**、协议 **72/72**、Python工具 **4 passed / 1 skipped**；两仓 **68 个契约文件一致**。新构建 wheel 的源码外契约/网页/API 冒烟通过。18 项 Core 跳过主要为平台、符号链接权限或可选运行环境，Mobile Python 的媒体测试因没有 javac 跳过；并非把它们算作通过。

验证边界：本机已检查 Python V2 链路、跨仓协议与 wheel；没有 Android 真机、Flutter/Android 构建或 Linux/macOS 冻结包的验收结果。本次没有重启用户正在使用的服务，也没有修改其令牌、模型配置或数据。

第18–19轮最终全量（2026-11-09）：Core **2659 passed / 18 skipped / 69 warnings**，Mobile纯JVM **120/120**（15个测试类），协议 **72/72**，Python工具 **4 passed / 1 skipped**，双仓 **68个契约文件一致**。网页仍使用本批第17轮验证的96项、类型检查和构建结果；18–19轮没有前端变更。最终源码wheel构建、源码外安装及V2契约/网页/带认证API冒烟通过。Core跳过项保留平台/依赖原因，Mobile媒体宿主因缺javac跳过。

第20轮最终验证：完整Core Python **2665 passed /18 skipped /69 warnings**（231.77秒），Mobile纯JVM **125/125 /16个测试类**，协议 **72/72**，Python工具 **4 passed /1 skipped**，两仓 **68个契约文件一致**。前端未改，沿用第17轮网页96项、类型检查与构建结果；wheel在本轮源码上重新构建及源码外冒烟。Android组件、Robolectric、完整APK与真机未验证，纯JVM不代表这些项目通过。

运行记录集中保存在本机工作区 `logs/12-V2分支初始化` 至 `logs/33-V2第20轮最终优化`；不将原始运行记录和私人配置提交到源码仓库。按用户最后追加要求完成第20轮，更新现有两个PR后停止。
