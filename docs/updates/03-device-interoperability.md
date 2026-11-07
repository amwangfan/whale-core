# 第 03 批：设备互联整理

状态：已完成本机联合验证，两仓均已推送。日期：2026-11-07。

对应提交：[Core 9afddd4](https://github.com/dreaminutes/whale-core/commit/9afddd4)、[Mobile 637f3e6](https://github.com/dreaminutes/whale-mobile/commit/637f3e6)。

## 本批更新

1. **能力事实源统一**：从 Mobile Schema 生成 Core Python、Mobile Python、Kotlin、Dart 四份能力表，替换分散手工维护的列表，保留原公开入口。
2. **参考网关补齐能力**：认识眼镜、自主体和输出读取 scope；默认配对仍为原 9 项，Dart 默认声明仍为原 15 项，已配对权限不自动变化。
3. **请求绑定设备**：查询、确认和动作等待绑定目标设备；其他设备的回包不能消费原请求。参考网关不记录无对应设备动作的回执。
4. **五阶段联合检查**：新增 `scripts/check_device_interop.py`，顺序检查协议镜像、生成表、示例、Core 设备测试和 Mobile 对端测试；失败即停止，日志可指定目录。
5. **跨平台依赖修复**：Mobile 优先使用当前虚拟环境的 jsonschema，保留离线 fallback，避免 Windows 被 Linux 原生依赖遮蔽。
6. **检查边界与 CI**：两仓同时缺失同类协议文件不能误报一致；手动 CI 增加生成表检查，支持指定 Mobile 仓库、ref 和检查范围。不新增自动触发。

## 本机验证

| 检查 | 结果 |
| --- | --- |
| 协议镜像 | 36 文件一致 |
| 四份生成表 | 与现有 Schema 一致 |
| 协议正例/负例 | 20/20 通过 |
| Core 设备、权限、回执、WebSocket | 125 项通过 |
| Mobile 生成器与参考网关 | 20 项通过 |
| 漂移检查工具回归 | 8 项通过 |
| 联合检查入口 | 五阶段通过 |

Core 全量测试在 Windows 收集 `tests/test_shell_executor.py` 时被现有 `os.geteuid` 调用阻断；完整 Linux 全量测试仍待执行。未执行 APK 构建、Flutter/Kotlin 测试或 Root/语音/眼镜真机联调。Kotlin/Dart 本批仅将原列表切换为生成表别名；生成检查不能替代编译。运行中的控制台未重启。

## 代码与使用说明

- [本批 Core 差异](https://github.com/dreaminutes/whale-core/compare/4e9ac90...9afddd4)
- [本批 Mobile 差异](https://github.com/dreaminutes/whale-mobile/compare/e034ea7...637f3e6)
- [互联开发与联合检查方法](https://github.com/dreaminutes/whale-mobile/blob/refactor/simplify-integration/docs/device-interop-development.md)

这是两仓配套开发批次，不是上游正式发行。原作者仓库及 main 均未更改。

[返回批次目录](README.md)
