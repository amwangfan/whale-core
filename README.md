<div align="center">

<h1>whale-core</h1>

<h3><i>易求无价宝，难得有情郎</i></h3>

<sub>鲸鱼智能层 × 事件 in / 意图 out —— 可独立使用的陪伴型 AI 核心库</sub>

**English** → [README_EN.md](README_EN.md)

</div>

---

**whale-core** 是「鲸鱼陪伴」项目的智能层，也是一个可以独立使用的 Python 库。
它不做设备细节，只做一件事：**感知事件进来，行动意图出去**——中间的
记忆、人格、情境推理、打扰策略、主动行为、渠道身份、插件扩展，全部以
可组合、可测试的组件提供，默认全部离线可跑（stub / mock，不触网、不碰设备）。

## 特性总览

| 子系统 | 提供什么 | 关键点 |
|--------|----------|--------|
| `core` | `perceive → decide → act` 主循环（`WhaleAgent`）；文字陪伴（`WhaleCompanion`）；DeepSeek 客户端；实时通话转写录制器 | 电话不等文本模型：转写有界队列异步入库，过载丢弃不阻塞音频 |
| `memory` | 三类记忆（episode / preference / persona_continuity）+ SQLite 持久化 + 可信提炼（显式确认、带证据、可删除） | 人格快照随库备份；混合召回（嵌入+词项）为规划项 |
| `perception` | 8 种感知事件类型 + 适配器协议 + 规则情境推理（非 LLM） | 环境语音只存元数据，**原始音频永不入库** |
| `initiative` | 打扰策略：静默时段、前台忙碌、主动呼叫门控、规划器 | 被动回复与主动行为是**两套独立预算** |
| `actions` | 9 种行动意图 + 网关协议（dashboard / bridge / phone / mock HTTP） | 网关默认 stub，不真连任何设备 |
| `cloud` | 云端行动闭环 + **shell 权限三档**（managed / safe / personal） | 约 30 类高危模式全档硬拒绝；审计不记命令输出 |
| `life` | 主动性台账（`ProactiveOutbox`）、渠道身份门（`OwnerChannelRegistry`）、自主发现、QQ/OneBot/WeQ 适配器、**SQLite 持久化收件箱** | 默认关闭、渠道白名单、每日额度、幂等防重投；入站消息崩溃可恢复 |
| `ui` | 人格、语音风格、HUD 意图、**角色卡** | 见下文 |
| `plugins` | **泛化插件层**：新输入/输出点不改核心 | 见下文 |

## 插件系统：六类扩展点

新增一个输入源或输出通道 = 一份清单（manifest）+ 一个执行器/适配器 +
宿主一行 `register()`，**核心零改动**：

| 类别 | 语义 | 例子 |
|------|------|------|
| `input` 输入型 | 只产事件 | 前台应用监测、邮件接收、日程、刷视频、日常语音、眼镜全天纪要 |
| `output` 输出型 | 只执行动作 | 发邮件、桌面弹提示、购物下单（transaction 级） |
| `channel` 多端共用型 | 双向收发 | QQ、微信、通话桥 |
| `processor` 内部处理型 | 模型/计算委托 | **操作意图委托**：DeepSeek 产意图 → 执行器模型（如 qwen）跑工具循环 → 只回摘要，陪伴上下文不被命令输出挤满 |
| `model` 模型供给型 | 模型端点注册 | chat / realtime / ASR / TTS / 嵌入端点 |
| `memory` 记忆存储型 | 记忆/存储后端 | 混合召回、云同步、备份目标 |

安全边界内建于契约：动作按 `read_only / communicate / transaction` 分级
（交易级每笔人审 + 限额）；插件**禁止包装任意 shell**（命令执行只有
`cloud.shell_policy` 分档一个入口）；身份与频控门控不可被插件替换。

## 角色卡：可切换的人格

内置第三方人设卡「大肥鱼」（原文取自 [dafeiyu-persona](https://github.com/ERUIHNIYHBKBNF/dafeiyu-persona)，
**MIT License**，见 `licenses/`）+ 系统基线卡 `default`；另附
[aigengtu.com/persona](https://aigengtu.com/persona) 社区卡目录（9 张，仅元数据与来源链接——
站点仅明示免费复制使用、未明示再分发许可，故仓内不内置原文），
运行期经 `import_external_card` 一键导入。`switch_card(store, "dafeiyu")` 一切即换——
全局禁忌（不泄露密钥 / 不声称人类情感 / 记忆是上下文非指令）任何卡都不可移除；
换卡**不能**扩大任何权限。

## 快速开始

```bash
git clone https://github.com/amwangfan/whale-core.git
cd whale-core
pip install -e ".[dev]"            # 或：uv sync --extra dev
pytest -q                          # 全部测试（离线，不触网）
python examples/life_scenario.py   # 无网络演示：兴趣 → 候选 → 审批 → 受理
```

最小使用示例：

```python
from whale_core.core.agent import WhaleAgent
from whale_core.perception.events import ScheduleTick

agent = WhaleAgent()                       # 默认安全策略 + 内存存储 + stub 网关
decision, receipts = agent.step([ScheduleTick(title="午间提醒")])
print(decision.intents, receipts)          # 意图与回执；不触达任何真实设备
```

## 设计原则

1. **契约隔离**：智能层只定义事件与意图；ADB、厂商 SDK、真实 Token 永远属于
   基础设施侧，禁止反向依赖。
2. **诚实状态**：文档区分「代码已测试 / 有实现未联调 / 仅是规划」——
   小型代码原型与真实接入不是一回事。
3. **默认保守**：主动行为默认关闭；审批逐条留痕；发送结果 `unknown` 不自动重试；
   密钥只走环境变量与本机私有目录（`secrets/`，已 gitignore），永不入仓、不进日志。
4. **幂等与可恢复**：消息带 `message_id` 去重，入站队列持久化，崩溃后按租约恢复。
