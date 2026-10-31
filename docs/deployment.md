# 安装、运行与恢复（Linux / macOS）

本指南适用于个人单 owner 部署。Windows 桌面端不在本轮实现范围。项目级许可证仍待维护者选定；现有第三方人格卡许可不覆盖整个项目。本轮生成的 wheel 和容器用于维护者内部验收，不代表已获授权对外发行。

## 选择安装方式

- **本地开发**：Python 3.11+、Node 24、pnpm 12，按源码安装，适合修改代码。
- **独立 wheel**：先在构建机编译控制台，再把 wheel 安装到运行机。运行机只需 Python，不需要 Node。
- **独立可执行包**：Linux/macOS 本机构建，携带 Python、控制台与工作流，运行机无需 Python 或 Node，见 [桌面打包](packaging-desktop.md)。
- **Docker Compose**：Linux 服务器推荐入口；镜像内含控制台，默认非 root、只读根文件系统、命名卷保存数据，只把管理端口映射到宿主回环地址。

真实对话需要可访问的 OpenAI 兼容模型。没有模型、密钥或外部服务时会明确报错；不会自动返回模拟内容。外部服务计费由所用 provider 决定。

## 源码安装

```bash
git clone https://github.com/amwangfan/whale-core.git
cd whale-core
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
pnpm -C web install --frozen-lockfile --ignore-scripts
pnpm -C web build
whale api
```

打开终端显示的地址，输入首次生成的访问 token。token 存在 `~/.whale/api_token`，只在创建时打印一次。控制台设置页用于配置模型端点并测试连接。命令行使用者也可 `whale setup --profile local` 后运行 `whale run`。配置、数据库和密钥都应位于个人私密目录，不要放进仓库。

macOS / Linux 使用系统时区库。Shell 的系统沙箱实现针对 Linux bubblewrap；macOS 不满足此运行条件时，Shell 默认明确不可用，其余管理、对话、记忆、搜索与云服务能力仍可使用。不要为了启用 Shell 将不可信命令切换到无隔离执行。

## 桌面窗口

Linux / macOS 可以运行 `whale desktop` 或 `whale-desktop`，原生窗口依赖可选的 `desktop` extra；缺少图形依赖时回退系统浏览器。详细安装、应用菜单入口、独立数据目录、退出和升级语义见 [桌面入口](packaging-desktop.md)。

## 构建并验证独立安装包

在有 Node 与 pnpm 的构建机执行：

```bash
python -m pip install hatchling
pnpm -C web install --frozen-lockfile --ignore-scripts
python scripts/build_release.py --no-build-isolation
python scripts/verify_wheel.py dist/*.whl
```

构建脚本在临时目录组合 Python 源码与当前 `web/dist`，不把编译产物写入源码目录。验证脚本把 wheel 安装到 checkout 以外的临时目录，启动真实回环 HTTP 服务，检查包内控制台首页和带鉴权的健康接口，避免仅在源码目录能工作的假象。

将得到的 wheel 传到运行机后，在独立虚拟环境中：

```bash
python -m pip install /path/to/whale_core-0.1.0-py3-none-any.whl
whale api
```

运行机无需 Node。需要实时语音时，安装 `python -m pip install "/path/to/whale_core-0.1.0-py3-none-any.whl[voice]"`，由可选依赖提供 WebSocket 客户端。Docker 镜像已经包含该依赖。升级前备份完整配置目录，然后 `python -m pip install --upgrade /path/to/new.whl`，停止旧进程再重启。不要同时启动两个进程占用同一监听端口；数据目录应由一个运行实例拥有。

## Docker Compose

需要支持 `env_file.required` 的 Docker Compose 2.24+：

```bash
cp .env.example .env
# 按需编辑 .env；也可启动后在控制台配置模型。
docker compose up --build -d
docker compose logs whale
```

访问 `http://127.0.0.1:8080`，token 在首次启动日志中显示。如果显式配置 `WHALE_API_TOKEN`，则使用该值。默认端口可在 `.env` 中设置 `WHALE_PORT`。

```bash
docker compose ps
docker compose exec whale python /opt/whale/healthcheck.py
docker compose restart whale
docker compose down
```

`down` 保留命名卷；不要加 `-v`，除非明确要删除配置、记忆、凭据和产物。健康检查仅表示 API 进程能响应，模型、语音或设备是否可用须在控制台分别检查。

容器内的 `127.0.0.1` 指容器自己，不能直接访问宿主 Ollama。将模型端点设置为实际可访问的宿主/LAN 地址，按网络环境配置并限制该模型服务的访问来源。不要把远程模型地址误配成管理 API 地址。

镜像安装 bubblewrap，但默认容器安全策略或宿主内核可能禁止 user namespace。此时 Shell 应返回沙箱不可用；不要使用 `--privileged`、挂载 Docker socket 或移除整个隔离策略来“修复”。只在专门设计并验证过的执行宿主上启用 Shell。

远程使用时保留回环端口映射，通过 HTTPS 反向代理或 SSH 隧道连接。管理 API 的 Bearer token 必须保留；不要将其放进 URL 或截图。使用反向代理时为 SSE 保留长连接并关闭代理缓冲。

## 完整备份与恢复

“导出记忆 JSONL”适合交换长期记忆，不能代替完整备份。完整备份需包含数据库、会话、任务、配置、token、私密凭据和媒体产物。

数据库层可用命令备份与恢复；恢复前必须停止使用该数据目录的 API、CLI 和 worker：

```bash
whale memory backup /path/to/memory-backup.sqlite3
# 确认运行实例已停止，再执行恢复。
whale memory restore /path/to/memory-backup.sqlite3
```

数据库备份不包含文件系统中的 token、私密凭据和媒体产物。要获得可恢复整个部署的副本，对源码/wheel部署应先停止进程，再复制整个配置目录：

```bash
# 确认 whale 进程已停止。
umask 077
tar -czf whale-backup.tar.gz -C "$HOME" .whale
```

备份中含私密信息，应只保存在受控位置；异地保存前使用自己信任的加密方案。恢复时停止目标实例，将备份解压到预期配置目录，保留属主与权限，再启动。不要将备份覆盖到仍运行的数据库文件上。

Compose 使用完整数据卷备份，下面的 tar 通过标准输出/输入传输，不依赖猜测卷的实际名称：

```bash
umask 077
docker compose stop whale
docker compose run --rm --no-deps -T --entrypoint tar whale -czf - -C /data/whale . > whale-backup.tar.gz
docker compose start whale
```

恢复前先保存当前状态。以下命令是覆盖恢复，会替换同名文件，必须由操作者确认目标部署正确；建议恢复到新的空卷后验收：

```bash
docker compose stop whale
docker compose run --rm --no-deps -T --entrypoint tar whale -xzf - -C /data/whale < whale-backup.tar.gz
docker compose start whale
```

恢复后检查 `docker compose ps`、鉴权健康检查、控制台会话/记忆/任务、一个新对话和一次导出。恢复不会自动重发状态不明的外部动作；应在任务详情中核对回执。

## 运行限制与验收口径

- 离线契约测试、真实 HTTP 回环测试、真实第三方服务调用、手机/眼镜真机验证是不同层级，应分别记录。
- 环境变量密钥适合部署注入；控制台保存的私密配置仍需要文件权限保护，不等同于加密密钥库。
- 新版本可能调整早期接口。升级前备份，回滚应同时确认数据格式兼容；不要只替换代码就假定数据库可以降级。
- 本指南不会创建外部账户、启用付费服务、发送 QQ 消息或部署到生产环境。
