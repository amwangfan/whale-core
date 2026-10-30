# Linux Shell 执行

Shell 默认使用 Bubblewrap 隔离，不会自动降级为宿主机执行。推荐将 Whale 服务部署在普通专用用户下，并安装 `bubblewrap` 与 `util-linux`（提供 `prlimit`）。Windows Shell 不在当前实现范围。

```python
from whale_core.cloud.shell_executor import ShellExecutor
from whale_core.cloud.shell_policy import ShellPolicy

executor = ShellExecutor(
    ShellPolicy(tier="personal"),
    allowed_cwds=("/srv/whale/workspace",),
    timeout_s=30,
)
result = executor.run("ls -la", is_owner=True, cwd="/srv/whale/workspace")
```

`is_owner` 必须来自已鉴权的宿主上下文，不能使用模型生成的布尔值。Shell 策略中的命令拒绝清单是防误操作护栏，实际隔离由内核命名空间提供。

默认隔离包含：独立用户、PID、网络等命名空间；移除所有 capabilities；只读系统运行库；独立 `/proc`、设备目录与临时目录；只把本次显式授权工作目录挂到 `/workspace`。不挂载宿主的用户目录、`/etc`、密钥目录或 Docker socket。网络默认禁用。未指定工作目录时，每次创建权限为 700 的临时工作目录，结束后清理。

默认资源限制：CPU 30 秒（随超时调整）、地址空间 512 MiB、单文件 16 MiB、64 个文件描述符、32 个进程。输出采用增量读取，总输出超过 1 MiB 即终止进程组，内存仅保存待显示的输出前缀。超时、输出超限与任务退出都会清理同组后台进程。

以 root 运行时必须显式配置 `drop_to_user` 为非 root 用户；宿主没有降权权限时返回 `credential transition unavailable`。容器可能禁止创建用户命名空间；此时执行失败并提示检查 Bubblewrap/namespace 支持，不会把相同命令重新放到宿主执行。应先在目标 Linux 主机运行无副作用的 `echo` 验收。

仅对完全受信任的宿主命令，维护者可以显式选择 `sandbox="disabled"`。这不是安全沙箱；它仍保留策略、资源、超时与输出限制，但命令可以访问服务账户有权限的宿主文件与网络。控制台不得把它作为默认回退选项。

返回状态包括 `success`、`failed`、`timeout`、`output_limit`、`denied`、`error`。`success` 仅表示命令退出码为零。调用方应审计策略与结果元数据，避免将命令输出中的个人信息或密钥写入公开日志。

## 在标准运行入口启用

同时设置 `WHALE_SHELL_ENABLED=1` 和 `WHALE_SHELL_TIER=personal` 才允许 owner 提议自由命令；默认 managed 不执行自由命令。可在控制台设置这些明确开关，也可由部署环境注入。每个命令仍需批准，实际运行还要求非 root 的 Linux 与可用 bwrap/prlimit。工作目录仅为配置目录下的 workspace；不会把密钥、数据库或主目录挂进沙箱。macOS 上该 Linux 执行器诚实报告不可用，可连接独立 Linux 执行宿主，不能静默退到宿主 shell。
