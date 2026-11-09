# 第 17 轮：全局生命周期与跨平台验证

Core 的 QQ 与主运行器共用跨平台文件锁；QQ 在运行器返回、异常或加载失败后释放锁，Windows 不再缺锁静默启动。Linux 锁路径保持原值，Windows 使用系统临时目录；没有修改当前运行服务的地址或配置。

修复 SQLite 备份连接生命周期：事务上下文不等于关闭连接，现显式关闭临时快照和恢复源，避免 Windows 文件替换失败。补充备份/恢复后立即移动与删除快照的测试。待确认事实支持注入 UTC 时钟，过期测试不再依赖当前日期；内存记录事务也隔离嵌套值，回滚与 SQLite 一致。

Mobile 录制模块按 dispatcher 实例注册，新客户端可重新安装 handler；RecordingService 重新绑定时复用原控制器，保留正在录制/暂停的状态。扩展纯 JVM 验证入口，固定列出实际生产源码与原有测试，统一输出编译日志、JUnit 结果和状态报告。

全局测试处理明确的平台差异：POSIX 权限位仅在支持的平台断言，其他秘密不外泄与功能检查继续执行；Windows 缺符号链接特权时只跳过对应符号链接用例；Linux launcher 与真实 POSIX shell 执行不伪装成 Windows 测试。修正旧会话夹具、硬编码部署契约路径与 fake git 路径规范化。

最终验证：完整 Python 套件 **2627 passed / 18 skipped**，网页全部 **96 项**及类型检查/构建通过，Mobile 纯 JVM **108 项**、协议 **72/72**、工具 **4 passed / 1 skipped**；两仓68个契约文件一致，wheel源码外安装冒烟通过。Core 跳过项保留明确平台/依赖原因；Mobile 媒体宿主测试因没有 javac 跳过。未执行 GitHub CI、完整 Android/Robolectric/APK 构建或真机验收，纯逻辑测试不代替 Android 组件验证。

[返回更新索引](README.md)
