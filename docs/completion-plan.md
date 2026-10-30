# 非 Windows 功能补全与验收计划

基线：`main@a53c707`。范围：whale-core 的 Python/网页/服务器能力；Windows 与桌面安装包不在本次范围。保留既有真实模型原则，不恢复 demo。真实账号发送、付费供应商调用及生产部署需有相应凭据与环境，离线或回环验收不能冒充真实联调。

## 审计结果与实施顺序

| 目标 | 基线证据 / 缺口 | 实施与验收 |
| --- | --- | --- |
| 配置真正生效 | profiles 未读取 config.model；manager 不知道 register_* 内置插件 | 统一装配、持久化内置清单/配置、动态 provider 与工具解析；重启、禁用与热更新回归 |
| 首次使用与诊断 | Web 无设置向导，CLI/网页装配不同 | Schema 表单、连接测试、可复制诊断；新目录安装冒烟 |
| 工具与任务闭环 | max_tool_rounds 未使用；部分成功会覆盖失败；无取消/持久化审批续跑 | 有界执行、准确终态、持久化任务队列、审批绑定参数、恢复时 unknown 不重试 |
| 语音/图片 | provider 存在但 API/控制台未接通，产物未管理 | 上传识别、合成试听、图片工作台、受鉴权产物与过期清理 |
| 视频 | 只有协议，无真实 HTTP provider | 可配置异步 HTTP adapter、提交/轮询/取消/超时；回环服务验收 |
| 实时语音与渠道 | whale-io 承担设备/音频桥 | 复用其协议，经可配置桥接入口；明确设备联调边界 |
| Shell | 无 OS 隔离，preexec 异常逃逸，communicate 无内存上限 | 非 root 容器隔离适配器、资源/网络/目录限制、输出上限、失败关闭；能力缺失时明确不可用 |
| 记忆/人格 | 有记录导出，无完整备份/恢复控制台、会话浏览缺失 | 导入、会话历史、人格配置；SQLite 在线备份、完整性检查及恢复流程 |
| 自主生活/工作流 | 库级模块存在但统一宿主未装配 | 管理入口、持久化暂停/策略、探索工作流；外发仍过既有 outbox |
| 部署与发行 | 无 Docker、构建网页不随 wheel 分发；LICENSE owner 已暂缓 | Docker 多阶段构建、Compose、环境示例、持久化与恢复文档；不擅自选择许可证 |
| 界面 | 功能页原型，缺统一反馈、手机适配、连接验证 | 侧栏/总览/设置/工作台、可访问表单、加载/错误/空状态；构建+浏览器验收 |

## 文件边界

- `plugins/manager.py`, `plugins/builtin.py`, `profiles.py`, `cli/main.py`：配置、内置能力、统一装配。
- `core/runtime.py`, `core/jobs.py`：有界工具执行、任务/审批/取消/恢复。
- `api/server.py`：鉴权、严格参数校验、设置/诊断/会话/媒体/任务 API。
- `plugins/media_video.py`, `plugins/bridge.py`, `cloud/container_executor.py`：真实适配器与隔离执行。
- `web/src/**`：统一布局与操作界面，严格使用上述 API。
- `Dockerfile`, `compose.yaml`, `scripts/**`, `docs/**`, tests：部署、恢复、契约和验收证据。

## 检查矩阵

运行现有全量 pytest；新增配置/禁用/审批篡改/重启/超时/取消/鉴权/Secret 回归；使用真实 HTTP 回环服务验证 wire protocol；运行 TS 类型检查、Vite build、Vitest；按 README 从空配置启动并通过浏览器检查主要页面。真实供应商、容器 daemon 和设备不可用时记录未验证项，不能把计划表全部打勾。

基础回归：807 passed / 1 failed（受限宿主 root 不能 setgroups，ShellExecutor 未捕获 preexec 错误）。
