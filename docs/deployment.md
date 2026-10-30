# Linux / macOS 安装、升级与恢复

Python 核心及网页支持 Linux/macOS（实时网页语音需 voice 可选依赖）。Windows 与桌面安装包不在本次补全范围。完整运行需要实际模型端点；不会自动安装 Ollama 模型或创建付费账号。

## 源码安装

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
corepack enable
pnpm -C web install --frozen-lockfile --ignore-scripts
pnpm -C web run build
whale setup --profile local
whale api
```

在网页填入首次启动输出的 token，再进入设置填写模型地址、名称与凭据。配置保存后作用于后续调用；禁用插件不取消已经发出的远程请求。`whale run`、`whale plugins ...` 与 API 使用同一配置目录；在下一次调用或状态查询时应用 CLI 保存的配置变化。新增 manifest 文件需重启宿主加载。

本地模型默认地址 `http://127.0.0.1:11434/v1`，默认模型名 `llama3.2`；必须与自己实际安装的模型一致。云端可选 DeepSeek，也可在插件表单替换为任何兼容端点。HTTPS URL 不允许内嵌凭据、查询参数和片段，凭据请求不跟随重定向。

## Docker Compose

```bash
docker compose up --build -d
docker compose logs whale
```

镜像多阶段构建网页并随 Python 包安装，使用非 root 用户、只读根文件系统、CPU/内存/进程限制，宿主 API/音频中继端口默认仅绑定 `127.0.0.1:8080`、`127.0.0.1:8081`。数据在 `whale-data` volume 内的 `/data`。token 首次生成后持久化；也可复制 `.env.example` 为 `.env` 设置 `WHALE_API_TOKEN` 等。

容器中的 `127.0.0.1` 是容器本身，不是宿主 Ollama。按自己的网络拓扑配置可达端点；远程部署前加 TLS 反代。不要把模型密钥写进镜像或 Git。

当前环境没有 Docker daemon，**此 Compose 尚未做实际容器启动验收**；多阶段路径、wheel 中的网页和本机 API 静态托管已分别验证。并非已部署到用户服务器。

## Shell 隔离

网页命令任务永远使用 Linux `bubblewrap`，不会静默回退到宿主 `/bin/sh`：只读系统挂载、无宿主 home/config、无网络、新用户/PID/挂载命名空间、无 capabilities、私有临时目录与工作目录；另有 CPU/地址空间/文件大小/进程数/文件描述符限制，以及有界输出、超时和取消强杀。

默认 `managed`/`safe` 不允许自由命令。要运行自定义命令，在控制台显式选择 `personal`，仍需单次审批。核心必须使用专用非 root 账号，且有 `bwrap` 和可用的非特权用户命名空间；macOS 无此隔离器时命令能力不可用。Docker 默认安全配置也可能阻止嵌套命名空间，**不得为了跑 Shell 去给应用 privileged 或挂 docker.sock**。在合适的 Linux 主机部署隔离工作进程后再启用。

已有 `ShellExecutor` 仍供可信宿主兼容使用，它有策略、降权、超时及输出限制，但不是 OS 沙箱；网页不使用这条兼容路径。

## 任务、取消与恢复

- 一个配置目录只允许一个 API/QQ 控制台宿主接管任务；第二个宿主启动失败。
- 队列最多 20 个非终态任务、2 个并行 worker。审批绑定已保存请求，不能在批准时替换参数。
- `queued/running/cancelling` 在重启后变成 `unknown`，不自动重试外部动作。根据供应商任务 ID/渠道台账核对后再人工决定。
- 取消 HTTP 任务是协作式：等待当前有时限请求返回后停止；Shell 杀进程组，视频尝试远程 cancel。外发和远程生成结果不明确时保留 unknown。
- 每日默认最多 200 次模型请求、50 个媒体任务，可在网页修改；按上海自然日计数，失败也占用。字符数统计不等于实际 token/费用。

## 备份、升级与恢复

```bash
whale backup /private/whale-snapshot.sqlite3
# 停止 API、QQ、音频桥等所有数据库写入进程后：
whale restore /private/whale-snapshot.sqlite3 --offline
whale doctor
```

网页也可下载完整 SQLite 一致快照。备份包含 kv（会话/任务/账本/设置）和记忆 records；密钥在 `secrets/`、token 在 `api_token`，媒体在 `artifacts/`，需要另外私密备份。只拷贝活动数据库主文件可能遗漏 WAL，请使用上述在线备份。

恢复前做完整性和表结构检查；原库会另存 `*.before-restore-*` 快照，然后原子替换主库。`--offline` 是明确的操作前提，目录锁只能拦住本项目宿主，不能替你停止其他旧程序。升级前先备份，再停止服务、更新源码/镜像、安装新包、重新构建网页并启动；卸载不删除配置目录或 volume。

新功能只新增 kv 命名空间，沿用原有 records/kv 表；敏感配置继续从普通 SQLite 分离到私有文件。回滚代码前保留快照，新版任务记录不应由旧版自动重放。

## 带控制台的 wheel

```bash
python -m pip install hatchling
python scripts/build_release.py
python -m pip install dist/whale_core-*.whl
```

脚本构建网页、复制到包内、打 wheel 并检查静态资产。普通源码 `pip install .` 不会自动运行 Node 构建；要分发可直接打开控制台的 wheel，应使用此脚本。

项目级 LICENSE 按 owner 先前决定继续暂缓，尚不能当作正式开源发行完成；第三方人格卡许可仍在 `licenses/`。
