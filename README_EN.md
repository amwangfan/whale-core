<div align="center">

# whale-core

*“The mountain has trees, and trees have branches; my heart delights in you, yet you do not know.”*

**An extensible AI core for Whale: conversations, memory, context, and tools.**

[中文](README.md) · [Get started](#get-started) · [Web console](#web-console) · [Developer & AI guide](docs/AI_DEVELOPMENT_GUIDE.md)

</div>

---

## What is Whale?

Whale explores a simple idea: an AI companion shouldn't have to live inside a single chat window. Conversations, important memories, context, and permitted actions should be able to follow you across channels and devices.

**whale-core** is the shared intelligence layer. It brings together dialogue, memory, structured intentions, a plugin system, and policies that decide which actions can actually be performed. QQ, calls, a phone, glasses, and a browser can all be interfaces to the same core.

This repository now includes a Python library, command-line tools, plugin management, a management API, and an early React web console.

> **This is an actively developed project, not a fully integrated product.** There is no demo/mock mode — conversations always come from a real model endpoint. Some capabilities have working interfaces and offline tests but still need real credentials, backend services, devices, or end-to-end verification; missing credentials fail honestly instead of faking success.

## What can it do?

| Experience | What's available today |
| --- | --- |
| Remember conversations | Multi-turn history, SQLite storage, episodic/preferences/persona memory, retrieval, JSONL import/export |
| Decide whether to chat or act | Structured intent plans, tool registry, execution receipts and task records; explicit search requests can use a configured search tool, with optional LLM intent planning |
| Extend with tools | Plugin registry and manager, web search/fetch code, controlled shell interfaces and policy |
| Speak or create media | ASR/TTS, image generation, and asynchronous video-job contracts with pluggable providers |
| Reach multiple channels | QQ/OneBot/WeQ adapters and event/action contracts for phones, VoIP and glasses; device integrations live separately |
| Act proactively | Disturbance policies, candidate actions, approvals, deduplication, feedback and exploration components |
| Manage it visually | A React console with Overview, Plugins, Memory, Approvals, Chat, Tasks and Events |

An implemented interface is **not** the same as a fully verified live integration. See the [developer guide](docs/AI_DEVELOPMENT_GUIDE.md) and [progress notes](docs/gap-analysis-2026-10-30.md) for specifics.

## Get started

Requires **Python 3.11+**. A running local model service (e.g. Ollama) lets you start with zero keys; the full test suite is offline regardless.

```bash
git clone https://github.com/amwangfan/whale-core.git
cd whale-core

python -m pip install -e ".[dev]"
whale setup --profile local      # zero keys: points at a local model endpoint (default 127.0.0.1:11434)
whale doctor
whale run                        # REPL against the real model endpoint; /quit to exit
```

There is no demo/mock mode: `whale run` always talks to a real model endpoint. If the local endpoint is not running you get a clear network error, not a canned reply.

Run the offline examples and tests:

```bash
python -m pytest tests -q
python examples/runtime_demo.py
python examples/life_scenario.py
```

You can also use `uv sync --extra dev` and `uv run whale ...`.

### Connect a real model

The project offers two configuration profiles:

- **`local`:** a running OpenAI-compatible local endpoint, configurable with `LOCAL_MODEL_BASE_URL` and `LOCAL_MODEL_NAME`; no key needed.
- **`cloud`:** a real DeepSeek/OpenAI-compatible text endpoint; the default configuration expects `DEEPSEEK_API_KEY`.

For cloud mode, supply `DEEPSEEK_API_KEY` in your environment, then:

```bash
whale setup --profile cloud
whale api
```

Use `whale run` or the web console below for conversations. **The CLI now follows the configured profile**; missing credentials or unavailable endpoints produce failures, not fake successful replies. Existing configuration isn't overwritten automatically by `setup`; check `~/.whale/config.json` before changing profiles.

The local profile does not install or start your model server for you.

## Web console

To build the frontend, install Node.js and `pnpm` (frontend CI uses Node.js 24 and pnpm 12).

```bash
pnpm -C web install
pnpm -C web build
whale api
```

Visit **http://127.0.0.1:8080** on the same machine. The API generates a bearer token on first launch and saves it to `~/.whale/api_token`; enter it in the console. Treat that token as a secret. Do not expose the management API publicly without appropriate protections.

The current console contains seven sections: **Overview, Plugins, Memory, Approvals, Chat, Tasks and Events**. It is an early management UI, not proof that every external integration is online.

## Plugins and customization

Whale Core aims to remain independent of any single model or service. One provider might offer chat, another speech, another web search or image generation. The core combines those capabilities while enforcing identity and action policies.

Plugin manifests, lifecycle controls, health checks and several capability implementations are present. However, this **isn't yet a complete one-click plugin marketplace**: some components require explicit configuration and host-side wiring.

## Relationship to whale-io

- **[whale-core](https://github.com/amwangfan/whale-core):** the reusable intelligence layer—conversations, memory, intentions, tools, and policies.
- **[whale-io](https://github.com/amwangfan/whale-io):** device infrastructure and designs for phones, VoIP and RayNeo glasses.

They belong to the same Whale project but can evolve independently.

## Current limitations

To keep expectations clear:

- There are no canned replies: all answers come from a real model endpoint; test doubles live only in tests/.
- Real search, speech, image/video and device integrations require appropriate providers and verification.
- The default runtime now routes clear, explicit search requests to `web.search` when configured; broader natural-language intent planning is optional (`WHALE_INTENT_MODEL_ENABLED=1`).
- The shell executor includes controls, but production-grade isolation still depends on the host OS sandbox.
- Proactive and external actions remain subject to identity, permissions and other applicable policies.
- A project-level LICENSE has not yet been selected. Third-party persona licenses do not apply to this entire repository.

For architecture, source navigation, tests and technical constraints:

[Developer & AI guide](docs/AI_DEVELOPMENT_GUIDE.md) · [Contracts](docs/contracts.md) · [Progress](docs/gap-analysis-2026-10-30.md) · [Roadmap](docs/roadmap-2026-10-30.md) · [Contributing](CONTRIBUTING.md)

---

*Whale Core is meant to become an AI foundation that people can understand, extend, and live with. The next step is to make that foundation reliable, useful, and genuinely connected.*
