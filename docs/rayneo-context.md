# 雷鸟眼镜全天转写（ingest / query / control）

眼镜侧 APK 把全天转写原文和官方总结上传到 whale-glasses-api，whale-core 保存原文并由意图模型按窗口总结成主人私人 episode。这些 episode **不会自动进入** `/v1/chat`（不含 `curated` / `chat` 标签）。

## 接口

均需 `Authorization: Bearer <WHALE_GLASSES_TOKEN>`，仅 POST + 单个 `Content-Length`。东京反代只转发，不校验令牌。

| 路径 | 体上限 | 作用 |
|------|--------|------|
| `POST /v1/rayneo/ingest` | 320 KiB，每批 1–50 条 | 上传原文 / 官方总结；按 `dedup_key` 幂等 |
| `POST /v1/rayneo/query` | 64 KiB | 查原文 + 主人/群/受保护原始库 episode |
| `POST /v1/rayneo/control` | 64 KiB | `delete_range` / `purge_all`（真删+墓碑）或 `stop_ingest` / `resume_ingest` |

开关：`WHALE_RAYNEO_INGEST=0` → ingest 503 `ingest_disabled`；`WHALE_RAYNEO_QUERY=0` → query 200 且 `query_disabled=true`。`stop_ingest` 是服务端持久开关，效果相同。

## 查询限域

可读：`owner-working`、所有 `group_space`、`protected_original`（主人确认受保护原始记忆可查）。

不可读：其他成员的 `member_private`、`shareable_fork`、`space_id=''` 未分区记录（fail closed，不误放开）。群空间只读 episode 摘要，不读 `timeline:*` / `chat:history:*`。响应不含 `space_id`、群号、uin、`conversation_id`。

## 原文库

独立文件：默认 `<dirname(WHALE_GLASSES_STORE)>/rayneo_context.sqlite3`，权限 0600，WAL。默认保留 90 天（`WHALE_RAYNEO_RAW_TTL_DAYS`）；到期只清文本（`expired`），不是删除。用户删除走墓碑，阻止 APK 回灌，并删除由这些原文派生的 episode。

## 总结

`WHALE_RAYNEO_SUMMARY=1` 时在 glasses-api 进程内起后台线程。使用独立意图模型实例（`QQ_INTENT_*` / `WHALE_RAYNEO_SUMMARY_MODEL`），不占用共享 `model_calls` 日预算。`WHALE_RAYNEO_SUMMARY_DAILY_MAX` 默认 `0`（不限）。建议先只开 ingest/query 观察，再开总结。近 30 秒内有 `/v1/chat` 会让路。

转写会作为不可信数据放进 `<transcript>` 发给意图模型（第三方）。请在客户端第一次打开开关时向用户说明。

## 备份

`rayneo_context.sqlite3` 不纳入 `memory/backup.py`（schema 不兼容，且避免普通记忆备份捎出转写原文）。运维使用 `RayneoContextStore.backup_to(path)` 或同权限的 SQLite backup / 文件系统快照，与主记忆库分开存放、权限 0600。恢复时同样不要在工具或出站工人仍在跑时覆盖热库。

## 运维

热更需重启 `whale-glasses-api`，`/v1/chat` 会短暂中断，选低峰。密钥只放 N100 `/opt/whale/secrets/` 下的 env，不进仓库、日志或异常文本。
