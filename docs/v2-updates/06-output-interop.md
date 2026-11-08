# 第 06 轮：读取 Mobile 的真实输出格式

Core 现在读取 Agent2 图片输出的 data_base64，同时保留既有 bytes_b64 格式；文本输出保留 UTF-8。合并输出对象与 sensor.result 两层的截断标记，避免输出被截断却向调用方报告完整。

缺失输出、未知编码和损坏的 base64 现在报告 invalid_message，不再悄悄返回空字节。合法空文本仍正常返回。

验证：10 项适配测试通过，覆盖二进制字节保真、两层截断、中文文本、空文本和异常输出。本轮没有修改 Android 代码或协议字段。

后续事项：Mobile NodeRuntimeV2 的输出读取尚未使用 offset/max_bytes，端侧分页仍待单独实现与 Android 验证。本轮只验证输出格式和截断标记。

[返回更新索引](README.md)
