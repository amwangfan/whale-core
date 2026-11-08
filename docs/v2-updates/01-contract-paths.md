# 第 01 轮：统一 V2 契约路径

日期：2026-11-08。分支：`refactor/v2-integration`。

Core 的校验器与能力枚举此前各自查找协议目录，环境变量指向 contracts 或 v2 时行为不同，缓存还可能保留先前目录的数据。现在共用 contract_paths.messages_dir，并按实际目录缓存枚举。

验证：新增路径、目录切换及错误路径回归，5/5 通过。

Core 提交：`26dd88d`。

[返回五轮索引](README.md)
