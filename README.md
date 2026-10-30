<div align="center">

# whale-core

*山有木兮木有枝，心悦君兮君不知*

鲸鱼的核心大脑：对话、记忆、工具、语音与主动探索。

[English](README_EN.md) · [使用与部署](docs/deployment.md) · [能力配置](docs/media-and-voice.md) · [开发者指南](docs/AI_DEVELOPMENT_GUIDE.md)

</div>

## 这是什么

whale-core 把鲸鱼的对话、长期记忆、意图判断、工具和权限策略放在同一个 Python 核心里。网页、QQ、手机、电话和眼镜可以共用这套大脑；设备与网络适配在 [whale-io](https://github.com/amwangfan/whale-io)。

本仓库提供核心库、命令行、管理 API、网页控制台，以及 Linux/macOS 桌面入口。没有生产用 mock/demo 档：需要模型或服务的功能使用真实端点，缺配置会明确报错。

## 可以做什么

| 功能 | 当前实现 | 使用条件 |
| --- | --- | --- |
| 对话与记忆 | 多轮历史、人格、SQLite、记住/忘掉、会话恢复、记忆浏览与删除 | 可用的 OpenAI 兼容文字模型 |
| 搜索与阅读 | 搜索、抓取、保留来源链接、组合阅读工作流 | Tavily / Brave 密钥，或显式选择 DDG；免密钥搜索可能被反爬阻挡 |
| 插件与设置 | 清单、真实启停、持久配置、连接测试、即时生效、诊断 | 控制台内置设置；第三方清单还需宿主提供执行器 |
| 工具任务 | 持久轨迹、有限轮数、超时、取消、逐项审批与恢复 | 高风险动作始终通过策略层；未知外部结果不会自动重试 |
| 语音与媒体 | 音频识别、语音合成、图片生成、Wan 异步视频、私有产物 | 按需配置对应端点和密钥；已实现接口与合同测试，真实供应商仍需联调 |
| 实时语音 | 独立音频会话、浏览器麦克风、打断、人格与记忆、文本转写记录 | 兼容 Realtime 协议的 WSS 服务、`voice` 可选依赖；浏览器需允许麦克风 |
| 语义记忆 | 可选真实 embedding 与关键词融合、失败回退、缓存清理 | 配置 OpenAI 兼容 embedding 端点；未配置时不影响关键词记忆 |
| 主动探索 | 兴趣、真实搜索阅读、候选草稿、静默时段、配额、退避、定期 worker、暂停 | 定期探索默认关闭；外发另需已绑定的渠道派发器 |
| 受控终端 | 逐次审批、工作目录限制、输出与资源边界、Linux 系统沙箱 | 非 root、可用的 Bubblewrap + prlimit；不可用时拒绝执行 |
| 备份与恢复 | JSONL 记忆交换、完整 SQLite 备份/恢复 | 恢复前停止服务；完整配置和媒体还需备份数据目录 |

控制台包含总览、对话、工具与媒体、工作流、插件、记忆、任务、审批、事件和设置。页面按需加载，支持移动端；保存配置和批准操作都以服务器实际结果为准。

## 从源码开始

需要 Python 3.11+；构建前端使用 Node 24 和 pnpm 12。

```bash
git clone https://github.com/amwangfan/whale-core.git
cd whale-core
python -m pip install -e ".[dev,voice]"
pnpm -C web install --frozen-lockfile
pnpm -C web build
whale api
```

打开终端显示的本机地址（默认 http://127.0.0.1:8080），粘贴首次生成的访问 token，然后在「设置」填写模型端点、名称和密钥，点击保存、测试连接。token 保存在配置目录的 `api_token`，不会放进网页 URL。

文字模型没有厂商绑定。默认 `local` 指向 `http://127.0.0.1:11434/v1`，请在设置中填写本地实际安装的模型名；`cloud` 默认 DeepSeek，也可改成其他兼容服务。项目不会替你下载或启动模型。

命令行用法：

```bash
whale setup --profile local
whale doctor
whale run
whale plugins list
whale memory backup /private/backups/whale.sqlite3
```

`cloud` 的命令行初始化需要先设置 `DEEPSEEK_API_KEY`；网页首次设置可以直接保存私密凭据。新配置目录默认 `~/.whale`，也可给命令增加 `--config-dir PATH`。

## 安装包、桌面与容器

带控制台的 wheel 可在没有 Node 的机器上运行；Linux/macOS 也有免 Python 可执行构建。构建、菜单启动入口、容器、升级、日志和备份步骤见[部署说明](docs/deployment.md)与[桌面说明](docs/packaging-desktop.md)。Windows 端不在本轮实现范围。

```bash
# 仓库内构建带控制台的 wheel
python scripts/build_release.py

# 桌面：可选原生窗口；缺少窗口依赖时使用系统浏览器
whale desktop
# 或 whale desktop --browser

# Linux 云端/本机服务：默认只向本机映射端口
# docker compose up --build -d
```

原生窗口需要 `desktop` 可选依赖和系统 WebView。免 Python 可执行默认用系统浏览器；macOS 签名、公证与真实图形环境验收不等于自动化测试。

## 数据和安全

网页与外部渠道都通过核心接口，不能直接操作数据库。管理 API 强制 Bearer 鉴权，默认只监听回环地址；公网使用必须有 HTTPS 反向代理。

端点、模型和密钥作为一份私密配置原子提交，避免写入中断造成地址与密钥混代。`config.json` 只是非敏感投影；网页保存后请通过设置页修改配置，密钥不会回显。私密文件权限为 600、数据目录为 700，文件权限保护不是静态加密。

原始语音只在内存中处理，不写入任务日志或备份。最终转写文本可以成为会话记忆；可以删除相关记录。图片、音频和视频产物由受鉴权的接口读取，默认七天过期、总量有上限。

批准只表示授权。任务可能仍待执行、失败或结果未知；取消不会撤销已经发生的外部动作。共享同一数据库的主动派发器遵守全局暂停，明确的被动回复仍可继续。

## 验证与边界

```bash
python -m pytest tests -q
pnpm -C web test
pnpm -C web build
```

本轮已覆盖 API、配置持久化与故障、插件撤销、审批恢复、任务取消、语音隐私、媒体合同、备份恢复、前端交互、wheel 隔离安装和 Linux 免 Python 启动。具体证据见[完成记录](docs/completion-report-2026-10-30.md)。

QQ/WeQ 先前有真实账号链路记录；本轮没有向真实账号发消息，也没有使用付费密钥或眼镜/手机真机。供应商合同测试、CI 和真实服务联调是不同验收层级。Linux 沙箱在当前容器不能使用用户命名空间，已验证其拒绝执行行为；隔离成功的验收需在目标 Linux 主机完成。Docker、macOS 发行和真实窗口通过相应 CI/目标设备继续验证。

项目级 LICENSE 仍按 owner 先前决定暂缓。`licenses/` 只有第三方通知，不代表本项目已经授予某种开源许可；正式对外发行前需确定项目许可。

## 开发文档

- [本轮计划与差距](docs/completion-plan-2026-10-30.md)
- [完成与验收记录](docs/completion-report-2026-10-30.md)
- [管理 API](docs/management-api.md)
- [设备与意图契约](docs/contracts.md)
- [能力与语音配置](docs/media-and-voice.md)
- [Shell 沙箱](docs/shell-sandbox.md)
- [贡献指南](CONTRIBUTING.md)
