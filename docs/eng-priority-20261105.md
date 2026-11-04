# 工程精简优先级（2026-11-05）

概念方向来自 owner；本表是工程取舍，可随现实改。

## 本回合已做 / 正在做

1. **可观测**：`TurnResult` + `/v1/chat` 回报 `intentMs` / `toolMs` / `replyMs` / `latencyMs`
2. **眼镜编排复用**：companion/orchestrator/quick brain 进程内缓存（带锁）
3. **主动问候**：stub → 可开关 3min 循环，默认 off；开则默认 dry-run
4. **搜索规则**：百度/谷歌一下等 shortcut；clarify 提示克制
5. **nginx**：upstream keepalive 补 `Connection ""`，去掉重复坏 location（东京）

## 砍掉或降级（本阶段不划算）

| 项 | 决定 |
|----|------|
| #19 音频桥 | 跳过（owner 明示） |
| MoonCheck | 保持停 |
| 迁国内为延迟第一优先 | 否 |
| #60 边缘拓扑大图 | 降级：先把现有眼镜/QQ 打磨稳 |
| #58 安卓自主 Shell 全开 | 降级：契约已有，真机自主另开回合 |
| #54/#55 角色兴趣深度 | 降级：先可观测与主动问候干跑 |
| Studio 多人版 | 不进单 owner 生产路径 |
| DeepSeek urllib 连接池大改 | 降级：nginx keepalive + orch 复用先吃肉 |

## 本回合补齐（四轨）

- #53：`--preset companion|glasses` + `evals/live/GATE.md`
- 安全默认：`GET /api/v1/security/defaults` + docs/security-defaults.md
- companion：InnerState 话题写回（`WHALE_INNER_STATE_WRITEBACK`，默认开）
- 主动问候：真发冷却/日额度 + 情境注入；默认仍 off/dry-run

## 下一回合候选

- 主动问候 dry-run 日志观察后考虑真发 + 与 ProactiveOutbox 额度统一
- QQ runner 同样回报分阶段耗时
- #59 清理无效限制（companion-first）小刀慢割
- 关已兑现 issue（#50/#51/#52 等）与 whale-io companion-roadmap 对齐
