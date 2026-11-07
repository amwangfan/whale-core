# contracts/v2

Whale 端云协议 **V2**（`2.0-dev`）。本目录是 whale-mobile 与 whale-core 之间的**唯一协议事实源**（canonical source）；whale-core 仓内的同名目录为镜像，禁止各自修改。镜像一致性由 `scripts/contract_sync_check.py` 双向比对守护。

- 协议文档：[PROTOCOL.md](PROTOCOL.md)（含 V1→V2 破坏性变更汇总 §17）
- `messages/`：WSS 逐消息 JSON Schema（draft 2020-12）
- `objects/`：共享对象合同（DeviceDescriptor / ExperienceEvent / EvidenceRef / AttachmentRef / RunTraceEvent / RecordingManifest / ApprovalRequest）
- `examples/`：黄金示例（交叉一致：同一 device_id / ULID 族 / 时间序列）
- `objects/examples/`：对象合同正例

校验：

```bash
python3 tests/contract_validate.py   # v1 + v2 全量校验（正例/负例/交叉一致性）
```

V1 冻结于 `contracts/v1/`，仅存档，不再演进。V2 相对 V1 的关键变化：回执状态机五终态（succeeded/failed/timeout/cancelled/unknown）、ID 前缀+ULID、能力单一名称空间、experience.event、run.trace.event、recording.*、错误码单表。

**schema 子集约束**：core 侧校验器只实现 draft 2020-12 小子集，禁止使用 `$ref`/`$defs`/`if/then/else`/`oneOf`/`anyOf`/`allOf`/`patternProperties`/相对 `$id`。
