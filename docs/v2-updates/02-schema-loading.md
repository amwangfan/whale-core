# 第 02 轮：加载时检查嵌套规则

日期：2026-11-08。分支：`refactor/v2-integration`。

校验器此前仅检查 schema 顶层，不支持的嵌套约束可能被静默略过。现在递归检查 properties、items、additionalProperties，拒绝未实现的 type，并拒绝不含消息 schema 的目录。

验证：路径及 schema 加载测试累计 11/11；包含当前协议示例校验。

Core 提交：`053774c`。

[返回五轮索引](README.md)
