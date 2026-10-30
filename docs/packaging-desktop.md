# 桌面打包方案（Tauri 2 + Python sidecar）——P2 规划稿

> 状态：**规划，未实施**。roadmap §2.4 的分阶段打包策略中，桌面打包排在最后：
> 核心与 API 稳定、控制台 MVP 可用之后才启动。本文先把方案与验收口径写死，
> 避免到时候临时发明。

## 1. 形态

```text
Tauri 2 壳（Rust 薄壳，跨平台安装包）
   ├── 内嵌 WebView → 加载 whale 控制台前端（web/dist，与 API 同仓库版本化）
   └── sidecar：PyInstaller 打包的 whale-core API 服务可执行文件
         （whale api --host 127.0.0.1 --port <动态空闲端口> --token <一次性会话令牌>）
```

- Tauri 官方支持 sidecar 模式携带 Python 打包产物，最终用户**不需要装 Python**
  （参考：https://v2.tauri.app/zh-cn/develop/sidecar/）。
- sidecar 进程生命周期由壳管理：启动壳 → 拉起 API（随机空闲端口 + 一次性令牌，
  经 stdin/环境变量传入，不写盘）；壳退出 → 强杀 sidecar（进程组）。
- API 仍只绑 127.0.0.1；令牌为随机会话值，桌面壳内闭环，**不对外开放**。

## 2. 数据与配置（打包语义红线）

| 事项 | 约定 |
|------|------|
| 数据目录 | 平台惯例位置（Linux `~/.local/share/whale`，macOS `~/Library/Application Support/whale`，Windows `%APPDATA%/whale`）；内含 memory.sqlite3、config.json、manifests/、exports/ |
| 卸载 | **绝不默认删除用户记忆与数据**（roadmap §2.4.6）；卸载器单独提供「同时删除数据」勾选，默认不勾 |
| 升级 | 配置文件向后兼容；记忆库迁移用 JSONL 导出/导入做恢复兜底（memory/export.py 已有）；升级前自动备份 sqlite 文件 |
| 日志 | 平台日志目录（Linux `~/.local/state/whale/logs`）；日志不落私人对话原文/密钥（API 层既有约定） |
| 端口冲突 | 启动时绑定 0 端口取系统分配；失败重试 3 次后报错退出（不抢固定端口） |
| 密钥 | 桌面档只存 env 引用名；真实 key 由用户在设置页输入后走系统密钥环（Tauri keyring 插件），不落 config.json |

## 3. 构建与发布（规划）

1. `pnpm -C web build` 产出控制台静态文件（CI 已有门禁，见 .github/workflows/web-build.yml）。
2. `pyinstaller --onefile -m whale_core.cli.main api` 产出 sidecar 二进制（可选依赖组 `desktop`）。
3. `tauri build` 产出平台安装包（.AppImage/.dmg/.msi）。
4. CI matrix（linux/mac/windows）+ 安装后冒烟（启动 → /api/v1/health 200 → 退出）。

## 4. 验收口径（届时执行）

- 干净机器安装后可启动；demo 档离线可用；
- 卸载默认保留数据目录；升级后记忆与配置延续；
- 壳崩溃后 sidecar 不残留孤儿进程；
- 安装包内不含任何真实密钥/个人数据（脱敏审计过一遍构建产物）。

## 5. 依赖的前置（未完工阻塞项）

- PR-7 API 层（已落地）；PR-8 控制台 MVP（进行中）；PR-9 配置档（进行中）；
- CLI `whale api` 子命令（随 PR-13 控制台增强一起做：API + 静态托管的正式入口）。
