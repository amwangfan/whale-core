# Linux / macOS 桌面入口

现在已有可运行的桌面入口，不再只有打包方案：`whale desktop` 或 `whale-desktop`。它启动同一套本地 API 和已构建的控制台，优先使用 pywebview 原生窗口；缺少窗口或系统 WebView 依赖时明确提示并打开系统浏览器。Windows 不在本轮范围。

现在提供两种形态：开发/原生窗口入口使用 Python；PyInstaller 冻结包携带 Python 运行时、控制台与工作流资源，使用者无需另外安装 Python 或 Node。冻结包默认打开系统浏览器，支持 Linux 目录/单文件包和 macOS `.app` 包装。包未签名，仅用于维护者内部试用，项目级许可仍待确定。

## 独立可执行包：使用者无需安装 Python

在目标操作系统和架构上构建，不能用 Linux 产物冒充 macOS 产物：

```bash
python3 -m venv .freeze-venv
. .freeze-venv/bin/activate
python -m pip install 'pyinstaller>=6,<7' 'websockets>=14,<17'
pnpm -C web install --frozen-lockfile --ignore-scripts
python scripts/freeze_desktop.py
python scripts/verify_frozen.py dist/frozen/whale-core/whale-core
```

构建工具安装在独立虚拟环境；运行机不需要这些工具。默认生成：

- `dist/frozen/whale-core/`：包含可执行文件和 `_internal` 运行时，分发时整个目录必须保留。
- Linux 的 `launch-whale-core.sh`：启动完整浏览器界面；`install-launcher.sh` 添加用户应用菜单入口，不调用 Python。
- macOS 的 `Whale Core.app`：内含运行时，打开 Terminal 启动；可整体移动，包装脚本按自身路径定位二进制。
- `build-info.json`：操作系统、架构、libc 与所带 Python 版本。

可选的单文件构建：

```bash
python scripts/freeze_desktop.py --onefile --skip-web-build
python scripts/verify_frozen.py dist/frozen-onefile/whale-core
```

单文件版启动时会把运行时展开到临时目录，目标机器必须允许在该目录执行；目录版对限制临时目录执行的机器更合适。Linux 产物还受构建机 glibc 基线约束，目标需匹配架构及兼容系统库；对更旧系统应在对应系统重新构建。

独立二进制保留完整 CLI：`whale-core --version`、`whale-core --help`、`whale-core doctor`、`whale-core api` 等。无参数时启动浏览器桌面入口。`--serve --config-dir PATH` 是回环集成验证入口，不打开浏览器，默认随机端口，鉴权仍从环境变量或私密 token 文件读取。

CI 为 Linux/macOS 原生构建冻结包，验证 `--help`、`--version`、控制台资源、鉴权 API、工作流资源和停止后端口关闭，再将保留可执行权限的压缩包保存为内部验收产物；不自动发布 GitHub Release。

## 安装和启动

从源码开发：

```bash
python -m pip install -e ".[desktop]"
pnpm -C web install --frozen-lockfile --ignore-scripts
pnpm -C web build
whale desktop
```

从已构建 wheel 安装：

```bash
python -m pip install "/path/to/whale_core-0.1.0-py3-none-any.whl[desktop]"
whale-desktop
```

pywebview 官方说明 Linux 需要选择 Qt 或 GTK 后端；可在当前 Python 环境安装 `python -m pip install 'pywebview[qt]>=5,<7'`。macOS 使用系统 WebKit 和 PyObjC 绑定。不同发行版仍可能需要图形系统库；具体依赖以 [pywebview 安装文档](https://pywebview.flowrl.com/guide/installation) 为准。

不需要原生窗口时：

```bash
whale desktop --browser
# 两个入口等价，独立入口也支持以下参数：
whale-desktop --browser --config-dir /path/to/desktop-data
```

API 只监听 `127.0.0.1`，使用系统分配的空闲端口，避免固定端口冲突。每次启动生成新的随机 token，在启动终端显示；复制到窗口中的连接框即可。token 不放在 URL、JavaScript 源码或普通日志中，也不复用网页版 API 的长期 token。关闭原生窗口后会停止 API 并关闭数据库；浏览器模式需保持终端运行，按 Ctrl+C 停止，关闭一个浏览器标签不代表停止服务。

## 应用菜单入口

使用安装了 whale-core 的 Python 执行：

```bash
python scripts/install_desktop_launcher.py
```

- Linux：创建 `~/.local/share/applications/whale-core.desktop`，可以从应用菜单启动。入口使用终端，确保能看到本次 token。
- macOS：创建 `~/Applications/Whale Core.app`，它打开 Terminal 启动当前 Python 环境下的桌面服务。

这个脚本创建的是应用入口，Python 环境仍需保留；不是独立二进制安装包。虚拟环境移动或更换后，重新运行脚本。脚本不会安装依赖或删除数据。

## 数据、日志与升级

| 系统 | 默认桌面数据 | 生命周期日志 |
| --- | --- | --- |
| Linux | `$XDG_DATA_HOME/whale-core`，未设置时 `~/.local/share/whale-core` | `$XDG_STATE_HOME/whale-core/logs/desktop.log`，未设置时 `~/.local/state/whale-core/logs/desktop.log` |
| macOS | `~/Library/Application Support/Whale Core` | `~/Library/Logs/Whale Core/desktop.log` |

桌面默认使用独立目录，不直接共享 CLI 的 `~/.whale`，避免意外混用实例。也可用 `--config-dir` 显式选择已有目录；同一数据目录只应由一个运行实例拥有。日志只包含启动、关闭和错误类别，不记录 token、聊天原文或音频；文件按大小轮换，目录仅用户可访问。

升级前关闭桌面并备份完整数据目录，备份必须包含数据库、配置、私密凭据和产物；操作见 [部署与恢复](deployment.md)。然后在原 Python 环境安装新 wheel，重新打开桌面。数据库命令备份适合保存记忆/任务，但不能替代整个目录的备份。

卸载 `python -m pip uninstall whale-core` 或删除应用菜单入口不会删除用户数据。删除 Linux `.desktop` 或 macOS `.app` 仅移除启动入口；若要删除数据，必须由用户另行明确操作。

## 已验证和未验证

自动测试使用假窗口/浏览器验证：关闭窗口停止 API 和数据库、缺少 GUI 后回退浏览器、Ctrl+C 清理、每次 token 不同、token 不进入 URL/日志、Windows 明确拒绝、无静态资源给出操作提示。另有真实回环 HTTP 集成测试，验证随机端口、控制台/鉴权接口及退出后端口关闭。

当前执行环境无桌面显示服务，未在真实 Linux GUI 或 macOS 真机上点击验收。pywebview 的接口与主线程要求依据 [官方 API 文档](https://pywebview.flowrl.com/api/)。Linux 目录版和单文件版已在本环境实际构建，通过清空 PATH、移除 Python 环境变量后的独立进程验证；控制台与工作流资源均从可执行包内部读取。macOS 构建和命令级验证由对应 CI 完成，真实 GUI 点验另记。签名发行、原生系统密钥环和自动升级并非本轮内部可用性验收的已完成能力。
