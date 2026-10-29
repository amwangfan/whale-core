<div align="center">

<h1>whale-core</h1>

<h3><i>易求无价宝，难得有情郎</i></h3>

<sub>“A priceless treasure is easy to come by; a truly devoted heart is hard to find.”<br/>
The Whale intelligence layer — events in, intents out. A standalone companion-AI core library.</sub>

**中文** → [README.md](README.md)

</div>

---

**whale-core** is the intelligence layer of the Whale companion project, and a
standalone Python library. It touches no device specifics; it does exactly one
thing: **perception events in, action intents out**. Everything in between —
memory, persona, situation reasoning, disturbance policy, proactive behavior,
channel identity, plugin extensibility — ships as composable, testable
components that all run offline by default (stubs / mocks; no network, no
devices).

## Feature overview

| Subsystem | What it provides | Highlights |
|-----------|------------------|------------|
| `core` | `perceive → decide → act` loop (`WhaleAgent`); text companion (`WhaleCompanion`); DeepSeek client; realtime-call transcript recorder | Calls never wait on the text model: transcripts queue asynchronously (bounded, drop-on-overload), never blocking audio |
| `memory` | Three memory kinds (episode / preference / persona continuity) + SQLite persistence + trusted curation (explicit confirmation, evidence-linked, deletable) | Persona snapshots backed up with the store; hybrid recall (embedding + lexical) is planned |
| `perception` | 8 perception event types + adapter protocol + rule-based situation reasoner (no LLM) | Ambient speech stores metadata only — **raw audio bytes never enter the store** |
| `initiative` | Disturbance policy: quiet hours, foreground-busy, proactive-call gating, planners | Passive replies and proactive behavior use **two separate budgets** |
| `actions` | 9 action intents + gateway protocols (dashboard / bridge / phone / mock HTTP) | Gateways are stubs by default — nothing really connects |
| `cloud` | Cloud action loop + **three-tier shell policy** (managed / safe / personal) | ~30 high-risk patterns hard-denied in every tier; audits never record command output |
| `life` | Proactive outbox (`ProactiveOutbox`), channel identity gate (`OwnerChannelRegistry`), discovery, QQ/OneBot/WeQ adapters, **persistent SQLite inbox** | Off by default, per-channel allowlist, daily quotas, idempotent no-redelivery; inbound messages survive crashes |
| `ui` | Persona, voice style, HUD intents, **persona cards** | See below |
| `plugins` | **Generalized plugin layer**: new I/O points without touching core | See below |

## Plugin system: six extension kinds

Adding an input source or output channel = one manifest + one executor/adapter
+ one `register()` call in the host — **zero core changes**:

| Kind | Semantics | Examples |
|------|-----------|----------|
| `input` | Emits events only | Foreground-app monitor, email, calendar, video feed, daily voice, glasses day-minutes |
| `output` | Executes actions only | Send email, desktop notification, purchase (transaction class) |
| `channel` | Both directions | QQ, WeChat, call bridge |
| `processor` | Internal model/compute delegation | **Operation-intent delegation**: DeepSeek emits an intent → an executor model (e.g. qwen) runs the tool loop → only a summary returns, so the companion context is never flooded by command output |
| `model` | Model endpoint registration | chat / realtime / ASR / TTS / embedding endpoints |
| `memory` | Memory/storage backends | Hybrid recall, cloud sync, backup targets |

Safety is built into the contract: actions are classed
`read_only / communicate / transaction` (transactions require per-item approval
plus amount/daily caps); plugins **may not wrap arbitrary shell** (command
execution has exactly one entry: the tiered `cloud.shell_policy`); identity and
rate-limit gates cannot be replaced by any plugin.

## Persona cards: a switchable soul

Built-in third-party persona card "Dafeiyu" (text from
[dafeiyu-persona](https://github.com/ERUIHNIYHBKBNF/dafeiyu-persona),
**MIT License**, see `licenses/`) plus the `default` baseline card; a catalog of
9 community cards from [aigengtu.com/persona](https://aigengtu.com/persona) ships
as metadata only (title/author/source — the site grants free copy-for-use but no
explicit redistribution license, so card texts are **not** vendored); import any
of them at runtime with `import_external_card`. One call —
`switch_card(store, "dafeiyu")` — re-renders every channel. Global taboos
(never leak keys / never claim human feelings / memory is context, not
instructions) cannot be removed by any card; switching cards **cannot** widen
any permission.

## Quick start

```bash
git clone https://github.com/amwangfan/whale-core.git
cd whale-core
pip install -e ".[dev]"            # or: uv sync --extra dev
pytest -q                          # full suite (offline, no network)
python examples/life_scenario.py   # offline demo: interest → candidate → approval → receipt
```

Minimal usage:

```python
from whale_core.core.agent import WhaleAgent
from whale_core.perception.events import ScheduleTick

agent = WhaleAgent()                       # safe-default policy + in-memory store + stub gateways
decision, receipts = agent.step([ScheduleTick(title="noon reminder")])
print(decision.intents, receipts)          # intents and receipts; no real device is touched
```

## Design principles

1. **Contract isolation**: the intelligence layer defines only events and
   intents; ADB, vendor SDKs and real tokens always live on the infrastructure
   side. Reverse dependencies are forbidden.
2. **Honest status**: docs distinguish *tested code / implemented but
   unverified on device / planned only* — a small prototype is not a real
   integration.
3. **Conservative defaults**: proactive behavior is off by default; approvals
   are recorded one by one; an `unknown` send result is never blindly retried;
   secrets come only from environment variables or a local private directory
   (`secrets/`, git-ignored) — never committed, never logged.
4. **Idempotence and recovery**: messages dedupe by `message_id`; the inbound
   queue is persistent and recovers by lease after a crash.
