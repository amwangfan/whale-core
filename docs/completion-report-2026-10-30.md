# 非 Windows 补全与验收记录

基线：`main@a53c707`。按用户要求先审计与规划，再完成明确范围内的代码和界面工作；[实施计划](completion-plan-2026-10-30.md)保留初始缺口。Python 核心与现有契约继续复用，没有整体改写，也没有恢复生产 mock 档。

## 完成矩阵

| 目标 | 本轮实际实现 | 证据与验证级别 |
| --- | --- | --- |
| 统一运行入口 | CLI/API/桌面共用 profiles；模型实际读取持久设置；通用模型兼容端点 | `profiles.py`, `configuration.py`, `test_completion_integration.py`，已集成并测试 |
| 配置与凭据 | 地址/模型/密钥单代原子提交与读取；公共配置只作投影；密钥保留/替换/撤销；热切换 | 配置故障和读取交错回归；活动任务/工作流锁；旧注册表撤销；真实私密文件权限验证 |
| 插件生命周期 | 官方注册项进入管理器、回放禁用状态、动态撤销模型/工具/向量能力、可视配置与测试 | `plugins/manager.py`, `profiles.py`；跨重启与缓存引用负向回归 |
| 任务闭环 | 运行中持久化、最大步骤/期限、有限worker、取消、失败与unknown、精确审批和恢复 | `core/runtime.py`, `test_runtime_lifecycle.py`；跨插件版本/参数变化重新审批，成功步骤不重放 |
| 多轮与记忆 | 同会话序列化、持久会话恢复、记住/忘掉、浏览/详情/删除/导出、关联会话删除 | memory/core/API与前端测试；活动/未解决任务时拒绝删除关联执行记录 |
| 完整恢复 | SQLite在线一致快照、完整性/结构校验、恢复前保留当前副本、未知执行不重放 | `memory/backup.py`, `test_memory_recovery.py`；CLI `memory backup/restore` |
| 语义召回 | 真实OpenAI兼容embedding provider、有限批次/超时、向量校验、哈希LRU、动态混合召回/失败回退 | `embedding.py`, `semantic.py`, 20项专门回归；未调用付费embedding服务 |
| 搜索与Skill | 动态工具意图、官方搜索阅读Skill、运行时门控的组合执行、审批后继续 | `service/workflows.py`, `workflow_skills/`；合同/异常/恢复测试 |
| 主动探索 | 真实search/fetch接线、来源读取证据、候选草稿、兴趣/反馈/配额/静默/退避、定期worker | `service/worker.py`, `life/initiative.py`；默认不定期探索；跨共享store暂停阻止真实派发端口调用 |
| ASR/TTS与图片 | 标准入口按配置挂载provider和工具；浏览器上传/播放/图片；私密产物 | `capability_setup.py`, speech/image/artifact模块与API/UI；上游合同测试，尚未供应商联调 |
| 视频 | Wan真实HTTP协议provider、异步任务、轮询/取消/超时、产物持久化 | `media_video.py`, API与Media页面；合同测试，尚未实际付费生成 |
| 原生实时语音 | WSS音频provider、有限会话/闲置回收、独立输入输出、人格/短记忆、最终文本转写去重记录 | `realtime_voice.py`, `realtime_bridge.py`, API；真实回环API验证文字模型调用为0、原始音频不落库 |
| 浏览器语音 | 用户点击才申请麦克风，24k单声道PCM16、分块/提交、音频事件播放、打断/退出清理 | `RealtimeVoice.tsx`, `voice.test.tsx`；浏览器API替身测试，未用真实麦克风通话 |
| 受控Shell | 显式personal档+逐次审批、非root、Bubblewrap命名空间、资源/输出上限、超时清理 | `shell_executor.py`, `shell_tool.py`，正/负路径测试；本容器命名空间不可用，已验证fail closed |
| 可视操作台 | 新响应式侧栏/连接页/总览/聊天/媒体/工作流/插件/记忆/任务/审批/事件/设置；真实操作与错误反馈 | 32项前端测试、TypeScript、生产构建；原生浏览器像素检查受本地端口不可达限制 |
| wheel安装 | 包含当前控制台和内置Skill；离开源码目录安装仍可启动 | `build_release.py`, `verify_wheel.py`实际隔离安装验证 |
| 非Windows桌面 | Linux/macOS原生窗口或浏览器入口、生命周期、平台数据/日志目录、菜单入口 | desktop与launcher测试；GUI使用替身，无真实图形显示环境 |
| 免Python发行本体 | PyInstaller目录版/单文件版、内置运行时与控制台/Skill、Linux菜单和macOS.app包装 | Linux两种模式实际构建；空PATH/移除Python变量的独立启动验证；macOS待CI及目标设备 |
| 容器与CI | 前端→wheel→非root镜像、持久卷/健康检查、默认回环映射、Linux/macOS打包与冻结CI | Docker/Compose与CI定义已实现；当前环境没有Docker，实际镜像构建由CI验证 |

## 实际测试

最终本地验证：

- `python -m pytest tests -q`：**923 passed**（基线为807 passed / 1 failed）。
- `pnpm -C web test`：**32 passed**。
- `pnpm -C web build`：TypeScript检查和Vite生产构建通过。
- Python源码/脚本编译、`git diff --check`通过。
- 带控制台wheel构建及checkout外隔离安装：核心组合根、插件、内置Skill、静态资源和鉴权API通过。
- Linux frozen目录版与单文件版：无外部Python/Node条件下版本、帮助、随机本机端口、控制台JS、401、Skill资源与退出关闭通过。
- 独立安全复核覆盖配置写入中断/同代读取、热切换、审批、原始音频隐私、非法请求无付费调用、语音人格/记忆/去重和全局暂停。

测试使用可控本地HTTP端点、协议替身和浏览器API替身，不把这些称为真实第三方服务联通。新增CI运行结果与本地结果分开记录，不能用“已添加CI”替代“CI已通过”。

## 关键决定

1. 延续标准库API与现有Python核心，避免框架迁移引入无关回归。
2. 不用单纯状态开关模拟启停；模型、工具和向量检索每次都检查当前注册表。
3. 不把取消/超时当作撤销外部效果；unknown不自动重试，运行中的旧任务不能被配置切换误当崩溃恢复。
4. 权威配置包含地址和密钥，单次原子替换；非敏感投影失败也不能重新组合旧地址和新密钥。
5. 语音实时路径不等待文字LLM；初始化只装载短人格与记忆，原始音频只在内存。
6. 默认关闭定期探索；探索形成草稿，发送继续由已绑定的渠道派发器和核心策略负责。
7. Linux Shell隔离不可用时明确报错，不回退到宿主执行。桌面其他平台可以使用核心/控制台；此Linux Shell后端并不冒充macOS沙箱。

## 真实联调和发行边界

下面是尚需目标环境才能完成的验收，不能宣称“所有服务和设备均正常”：

- 未提供并使用真实模型/搜索/语音/图片/视频/embedding凭据，本轮没有付费调用。
- 手机、雷鸟眼镜、VoIP与QQ/WeQ真机/真实账号未在本轮重新联调；设备适配继续由whale-io承担。先前真实QQ链路记录不等于新版本已重新验收。
- 当前Linux容器不允许所需用户命名空间，未完成Bubblewrap成功隔离的目标机正向验收。
- Docker不可在当前环境执行；macOS原生窗口、免Python包及签名/公证需要相应环境。已提供自动构建和验收路径，但本地不能替代这些结果。
- 云端浏览器不能访问本地开发端口，未完成真实页面截图和麦克风实听；前端行为测试、类型与构建已通过。
- 项目级LICENSE按owner先前决定暂缓；第三方通知已随包保留，正式对外发行仍需确定项目许可。
- Windows排除在本轮范围。多owner/多宿主机同步是路线图中“确有需要再做”的条件性扩展，本轮维持单owner canonical SQLite边界。

没有执行生产部署、真实消息发送、账户权限变更或破坏用户数据。回滚源码可revert本PR；数据升级/恢复前应保留整个配置目录和SQLite备份。

## 首次真实使用验收顺序

1. 启动API/桌面，填token；设置实际文字模型并测试、多轮聊天、记住/回忆、重启验证。
2. 配置搜索后跑内置搜索阅读Skill，核对来源和任务回执。
3. 按需要配置ASR/TTS/图片/视频/实时音频/embedding；每种服务先进行一次最小真实请求，核对供应商实际协议与模型名。
4. 在目标Linux非root主机安装bwrap/prlimit，明确启用personal档，审批一次无害命令并完成文件/网络隔离负向验收。
5. 将whale-io设备/渠道适配器接同一个核心与数据库，核对绑定、被动回复、主动暂停、审批与发送回执。
6. 先备份并恢复演练，再启用所需定期探索与正式部署。
