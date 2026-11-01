# 本地运行时归属（issue #47）

## Canonical 工作树

**`/opt/whale/whale-core`（main 分支）是本机唯一 canonical checkout。**
`whale-api` 与 `qq-bot` 两个 systemd 服务都必须从它启动；开发改动在
`/opt/whale/whale-core-dev` 进行，push 后经 `git fetch origin &&
git reset --hard origin/main` 同步到 canonical，再重启服务生效。
禁止再从 `-dev` clone 或任意目录直接拉起长驻服务进程。

## 2026-11-01 收敛审计（事实记录）

| 项 | 审计发现 | 处置 |
| --- | --- | --- |
| 两 clone HEAD | 均为 `4b77ce6`、工作区干净、同 origin | 无独有提交丢失风险 |
| 主 clone 分支 | 曾停在 `batch/int-20261101` | 已切回 `main`；原分支打 tag `archive/ev02-4b77ce6` 后删除 |
| 主 clone 未跟踪文件 | 15 个 `contracts/v1/examples/*.json`，与 whale-mobile canonical 逐字节一致 | 已归档至 `/opt/whale/archive/2026-11-01-contracts-examples/`，将由 #46 正式入库 |
| 进程归属 | `whale-api`(8180) 与 `qq-bot`(18797) 的 cwd 均在主 clone ✅；另有一个 10:21 手工启动的游离 dev API（cwd=`-dev`，端口 8190，配置目录 `/var/lib/whale-dev`） | 游离进程已终止，8190 已关闭 |
| 未推送提交 | `4b77ce6`（EV-02）仅存在于本地 dev main | 按批次推送纪律，随本轮统一 push 后同步 canonical |

## 工具：`whale runtime`

```
whale runtime status [--json] [--checkout PATH]   # 展示 PID/cwd/cmd/端口/HEAD/健康
whale runtime start <服务名...>                    # 端口被外来进程占用时拒绝
whale runtime stop  <服务名...>                    # 仅停 cwd 在 canonical 内且命令行含登记标记的进程
```

默认登记：`whale-api`（unit whale-api.service，127.0.0.1:8180，
健康检查 `/api/v1/health`）与 `qq-bot`（unit qq-bot.service，
127.0.0.1:18797 TCP 存活）。实现：`src/whale_core/ops/local_runtime.py`，
systemctl/ss/git 与网络探测全部可注入，测试见
`tests/unit/operations/test_local_runtime.py`（20 例，全离线）。

安全语义：

- `stop` 双重校验（cwd 在 canonical checkout 内 **且** cmdline 含登记标记），
  否则拒绝——不会误杀其他 Python/QQ/Flutter 进程。
- 端口冲突按**绑定地址**判定：`tailscale serve` 在 tailnet 地址上代理同端口
  不算冲突；同一地址上的外来监听者才拒绝。
- `status` 在「进程 cwd 不在 canonical」「运行代码 HEAD ≠ 工作树 HEAD」
  「active 但端口无人监听」「健康检查失败」时列出 `[PROBLEM]` 并以退出码 1 报警。

已知限制：`process_head` 读的是进程 cwd 当前的 git HEAD；进程启动后工作树
被 reset 造成的「内存中代码 vs 磁盘代码」漂移无法事后判定——以
`systemctl restart` 后的首次 status 为准。

## 验收证据（2026-11-01，N100 实测）

```
canonical checkout: /opt/whale/whale-core @ 6494053b80d7
[whale-api] active pid=1854757 health=ok
  cwd: /opt/whale/whale-core
  HEAD: 6494053b80d7 = checkout
  port 8180: pids=1854757
[qq-bot] active pid=1930561 health=ok
  cwd: /opt/whale/whale-core
  HEAD: 6494053b80d7 = checkout
  port 18797: pids=1930561
exit=0
```

游离 dev API（PID 1931510，端口 8190）已终止；`ss` 确认 8190 无监听。
本任务不改变生产实例部署形态，只做归属收敛与可审计工具。
