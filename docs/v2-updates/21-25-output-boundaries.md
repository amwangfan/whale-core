# 第21–25轮：完整输出读取的边界

2026-11-12。Core从最新上游31ad9e9建立refactor/v2-output-hardening；此前PR81已包含在上游历史。Mobile保留本地已验证的第20轮基线，新建同名后续分支；手机上游同步遭遇网络重置，未声称已同步成功。

|轮次|实际修改|
|---|---|
|21|Core验证offset/limit/响应对象，旧格式单页也遵守请求的字节预算|
|22|拒绝残缺分页元数据及字符串/数字伪装的截断标记，避免静默误判完整输出|
|23|收集器独立检查提供者返回的OutputPage、每页预算、偏移、总长、最终页完整性与元数据稳定性|
|24|Mobile分页参数复用精确整数解析，拒绝高精度小数及显式null，保留合法整数和默认值|
|25|Mobile直接Request构造与旧输出适配共用范围约束，旧来源截断标记须为布尔值|

专项验证：Core分页/旧格式/观察相关51项通过；Mobile纯JVM127项通过，协议72/72。这些结果不代表Android组件、APK或真机已验证。全量及远端交付的实际结果记录于本地logs/34-V2第21至25轮。网络失败不算推送或PR成功。

继续保留既有功能和未协商分页的旧调用格式；没有更改用户服务地址、模型、密钥或重启真实服务。

最终全量：3063 passed /18 skipped /14 failed /69 warnings，327.54秒。失败位于test_glasses_media、test_rayneo_context_store与test_shell_partial_output，不能将本次全量称为通过。新wheel构建与源码外契约/网页/认证API冒烟通过。GitHub fetch/push/API多次连接重置，未完成远端PR交付；本地各轮提交及离线patch保留。
