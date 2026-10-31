# G4：运维可靠性、扩展性与开发者自检

本文档面向运维与插件开发者，说明 G4 引入的四块能力：

1. **`whale doctor` 自检**：默认档完全离线，机器可读 JSON；`--deep` 显式开启真实连通检查；
2. **按 Capability 的五层健康视图**：`capability_health()`；
3. **Provider 运营指标**：调用计数、近 24h 失败类别、真实延迟分位——**无真实来源一律标 `unavailable`，绝不猜数**；
4. **插件信任级别**：`trust: bundled / external` 与策略层最小钩子。

贯穿约定（与仓内既有安全约定一致）：任何诊断/自检输出**只含计数、状态名、
错误类别名、端点 host 与时间戳——绝不含 api_key / token / 用户正文**。

---

## 1. `whale doctor` 自检

```bash
whale doctor                     # 人类可读 [ok]/[fail]/[warn] 逐行报告
whale doctor --json              # 机器可读 JSON（供 CI / 运维采集）
whale doctor --deep              # 显式同意后追加真实网络连通检查
whale doctor --config-dir DIR    # 指定配置目录（默认 ~/.whale/）
```

### 默认档（离线安全，不触付费操作）

逐项检查并给出 `[ok] / [warn] / [fail]`：

| 检查 | 失败含义 |
| --- | --- |
| Python ≥ 3.11 | 运行时不满足最低版本 |
| `config.json` 存在且可解析 | 先运行 `whale setup`；不可解析需修复/重建 |
| 记忆库 `memory.sqlite3` 可打开可写 | 存储损坏或权限问题 |
| 模型 provider 配置 | cloud 档报 key「已配置/未配置」（只报 env 引用名，不回显 key 本体）；local 档报端点 |
| `manifests/` 目录可读 | 先运行 `whale setup` |
| 已加载插件健康 | 逐插件 `healthy / unknown / disabled / unhealthy`（含清单加载错误） |

退出码：任一 `[fail]` → `1`，否则 `0`。**默认档不发任何网络请求**——
「key 已配置」「端点合法」只代表配置层面成立（仅配置合法），不代表对端可用。

### `--json` 输出契约

```json
{
  "ok": true,
  "deep": false,
  "checks": [
    {"status": "ok", "detail": "python 3.13.x >= 3.11", "live": false}
  ]
}
```

- `checks[].status` ∈ `ok / warn / fail`；
- `checks[].live`：`true` 表示该项是**真实请求已验证**（仅 `--deep` 产生）；
  `false` 表示**仅配置/本地状态合法**；
- 空配置目录、无 key、无网络的环境下也能跑通并输出诚实 JSON（未配置的
  项如实报 fail/warn，不假装可用）。

### `--deep` 真实连通检查

只有显式加 `--deep` 才会触网：

- cloud 档且 key 已配置：`GET {base_url}/models`（模型列表端点，不产生生成
  费用；直连不走代理；超时 5s），结果只含 HTTP 状态码或错误类型名；
- key 未配置 / local 档：对应探测**跳过并标注**，不假装探测过；
- 探测失败计入 `[fail]`（`--deep` 是显式运维动作，失败应可见）。

---

## 2. 按 Capability 的五层健康视图

`whale_core.core.diagnostics.capability_health(manager, tools, store=None)`
对每个已声明能力输出五层状态（`manager` 也可传裸 `PluginRegistry`，此时
enabled 以注册表成员资格为准——管理器只在启用时注册）：

| 层 | 含义 | 取值 |
| --- | --- | --- |
| `declared` | 有工具声明该能力（`ToolRegistry` 中存在） | `true / false` |
| `registered` | 工具映射的插件已注册进注册表 | `true / false` |
| `enabled` | 插件当前启用（`PluginManager.status()`） | `true / false` |
| `reachable` | 配置校验通过（provider `healthcheck()`，仓内约定为纯配置校验、不发真实呼叫） | `true / false / "unavailable"` |
| `verified` | 有**真实成功回执证据** | `true / "unavailable"` |

`verified` 的证据只认两个来源，皆读自 MemoryStore：

1. **任务台账回执**：`tasks:recent` 索引的 `task:{id}` 记录里出现
   `tool:<name>:ok:…` / `plugin:<pid>:<action>:ok:…` 回执；
2. **插桩统计**：`provider:health:{name}` 中该 provider 有成功调用计数。

没有证据标 `"unavailable"`——**「声明了」绝不冒充「能用」**。
无插件映射的纯本地 handler 工具（配置即可用）的 registered/enabled/reachable
记 `true`；声明了工具但插件未注册的能力（孤儿）如实显示
`registered: false, enabled: false`。

返回形状：`{"capabilities": [...按能力名排序...], "summary": {"total", "verified", "unverified"}}`。

---

## 3. Provider 运营指标

`InstrumentedProvider`（`core/diagnostics.py`；装配层在 `profiles.py` 给
`llm.chat` / 各角色模型自动挂上）把每次调用统计写入 kv
`provider:health:{name}`。`provider_health_report(store)` 汇总全部
provider，每个视图包含：

- `calls` / `errors` / `error_rate` / `chars_in` / `chars_out`：计数器
  （chars 是字符数，仅粗略成本代理；真实 token 计数属供应商侧）；
- `avg_latency_ms`：来自累计真实延迟的平均值；
- `errors_24h`：近 24 小时失败类别计数（`{kind: count}`），来自有界错误
  事件流水（每 provider 至多保留最近 500 条 `{时间戳, kind}`）；窗口内无
  失败事件时为 `"unavailable"`；
- `latency`：真实延迟样本（至多保留最近 500 个）算出的
  `{"p50_ms", "p95_ms", "samples"}`；**无样本时为 `"unavailable"`——
  不编造分位数**；
- `last_error_kind` / `last_error_at` / `updated_at`：只记 `ModelError.kind`
  或异常类型名，**绝不记异常消息文本**。

存量统计键（旧版九字段、无新流水）读取不炸：新字段按 `unavailable` 呈现。
端点 host 信息由 provider 自身的 `info()`（如 `base_url_host`）提供，
永远不含 key。

---

## 4. 插件信任级别（trust）

### 声明

`PluginManifest` 新增 `trust` 字段，合法值：

- `"bundled"`（**默认**）：随仓发行/宿主显式装配的官方插件——既有行为完全不变；
- `"external"`：第三方清单。

声明位置（都是**宿主控制的显式加载面**，插件自身无法借此提权）：

```jsonc
// ~/.whale/manifests/my_plugin.json
{
  "plugin_id": "my_plugin",
  "name": "我的插件",
  "version": "0.1.0",
  "actions": ["send_message"],
  "action_risk": {"send_message": "communicate"},
  "trust": "external"        // 省略即 bundled
}
```

外部目录插件的 `plugin.json` 同样支持 `trust` 键；非法值在加载/构造期即
拒绝（`ValueError` / 记入 `manager.errors`），不会静默降级为 bundled。
`PluginManager.status()` 的状态行含 `trust` 字段（供管理 API / 设置页展示）。

### 策略钩子（最小侵入，不新造状态机）

`PluginActionPolicy.evaluate()` 在既有风险矩阵上叠加一条规则：

> **`trust="external"` 的插件执行非 read_only 动作时，强制
> `needs_approval`**——宿主级 `auto_approve_risks` 放宽（R1）对其不适用。

| 场景 | bundled | external |
| --- | --- | --- |
| read_only | ready（免审，不变） | ready（不变） |
| communicate，无批准，宿主配了 auto_approve | ready | **needs_approval** |
| communicate / transaction，owner 显式批准 | ready（原路径不变） | ready（同一审批通道） |
| 非 owner | denied（身份门不变） | denied |

网关联动：external 插件被拦时走既有 `plugin-hold:{plugin}:{action}` 回执，
人审批准后重派即 `plugin:{plugin}:{action}:ok:…`——审批通道、审批绑定
（任务身份 + 参数指纹）、transaction 限额/日限全部沿用既有实现。

---

## 5. CI 契约守护

`.github/workflows/contract-smoke.yml`：

- **push / PR 到 main**：跑「协议与边界契约」子集——`test_pcm_voice.py`
  （PCM 线协议镜像）、`test_media_input.py`（媒体输入可信边界）、
  `test_task_engine.py`（持久任务引擎）、`test_tool_executor.py`
  （工具执行器）、`test_g2_memory_identity.py`（记忆治理与身份）等；
  这些套件守护「与 whale-io 的线协议」与安全边界，破坏即失败；
- **nightly（schedule）**：跑全量 `python -m pytest tests -q`。

仓内既有 `tests.yml`（全量）继续保留；contract-smoke 是面向契约的更快
反馈环。

---

## 6. 常见排查

| 现象 | 含义与处理 |
| --- | --- |
| doctor `[fail] config.json 不存在` | 未初始化：`whale setup` |
| doctor `[fail] cloud 档 key 未配置` | 只缺 env：`export DEEPSEEK_API_KEY=…`（不会写入配置文件） |
| 能力 `verified: "unavailable"` | 没有任何真实成功回执——不是故障，是「尚未被真实验证过」；真实跑通一次后自动转 `true` |
| 能力 `registered: false` | 工具声明了能力但映射插件未注册（孤儿），检查插件是否启用/清单是否加载 |
| provider `errors_24h` 为 `"unavailable"` | 近 24h 无失败事件记录（含从未调用过） |
| `latency: "unavailable"` | 无真实延迟样本（旧版统计键或从未调用），不展示编造分位 |
| external 插件动作一直 `plugin-hold` | 信任钩子生效：external 的非只读动作必须逐次人审，宿主 auto_approve 不适用——符合预期 |

---

## 7. 测试

- `tests/test_g4_operations.py`：能力五层视图、运营指标（24h 失败类别 /
  延迟分位 / unavailable 纪律）、doctor `--json` / `--deep`（fake opener，
  不触网）、trust 校验冒烟；
- `tests/test_g4_trust.py`：信任级别全链路——manifest 校验、manager /
  loader 透传（含非法值拒绝与默认不回归）、policy 矩阵（external 强制人审
  / auto_approve 不适用 / 显式批准放行）、gateway 端到端 hold→ok。

全部离线：`python3 -m pytest tests/ -q -p no:cacheprovider`。
