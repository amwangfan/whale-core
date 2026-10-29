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

> **This is an actively developed project, not a fully integrated product.** The offline demo intentionally uses a mock model; its responses are labeled `[mock]`. Some capabilities have working interfaces and offline tests but still need real credentials, backend services, devices, or end-to-end verification.

## What can it do?

| Experience | What's available today |
| --- | --- |
| Remember conversations | Multi-turn history, SQLite storage, episodic/preferences/persona memory, retrieval, JSONL import/export |
| Decide whether to chat or act | Structured intent plans, tool registry, execution receipts and task records; automatic intent-model wiring in the default chat path is still incomplete |
| Extend with tools | Plugin registry and manager, web search/fetch code, controlled shell interfaces and policy |
| Speak or create media | ASR/TTS, image generation, and asynchronous video-job contracts with optional providers/mocks |
| Reach multiple channels | QQ/OneBot/WeQ adapters and event/action contracts for phones, VoIP and glasses; device integrations live separately |
| Act proactively | Disturbance policies, candidate actions, approvals, deduplication, feedback and exploration components |
| Manage it visually | A React console with Overview, Plugins, Memory, Approvals, Chat, Tasks and Events |

An implemented interface is **not** the same as a fully verified live integration. See the [developer guide](docs/AI_DEVELOPMENT_GUIDE.md) and [progress notes](docs/gap-analysis-2026-10-30.md) for specifics.

## Get started

Requires **Python 3.11+**. Start with the no-key offline demo:

```bash
git clone https://github.com/amwangfan/whale-core.git
cd whale-core

python -m pip install -e ".[dev]"
whale setup --profile demo
whale doctor
whale run
```

`whale run` currently always starts a **mock REPL**; type `/quit` to exit. This checks installation and the local conversation pipeline, not the quality of a live language model.

Run the offline examples and tests:

```bash
python -m pytest tests -q
python examples/runtime_demo.py
python examples/life_scenario.py
```

You can also use `uv sync --extra dev` and `uv run whale ...`.

### Connect a real model

The project offers three configuration profiles:

- **`demo`:** offline mock model and canned examples; no keys.
- **`cloud`:** a real DeepSeek/OpenAI-compatible text endpoint; the default configuration expects `DEEPSEEK_API_KEY`.
- **`local`:** a running OpenAI-compatible local endpoint, configurable with `LOCAL_MODEL_BASE_URL` and `LOCAL_MODEL_NAME`.

For cloud mode, supply `DEEPSEEK_API_KEY` in your environment, then:

```bash
whale setup --profile cloud
whale api
```

Use the web console described below for actual conversations. **Changing the profile does not change `whale run`: it is still a mock REPL in the current implementation.** Existing configuration isn't overwritten automatically by `setup`; check `~/.whale/config.json` before changing profiles.

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

- Offline demo responses are mocked, not generated by a real LLM.
- Real search, speech, image/video and device integrations require appropriate providers and verification.
- The default conversation runtime does not yet automatically turn natural-language search requests into search-tool calls.
- The shell executor includes controls, but production-grade isolation still depends on the host OS sandbox.
- Proactive and external actions remain subject to identity, permissions and other applicable policies.
- A project-level LICENSE has not yet been selected. Third-party persona licenses do not apply to this entire repository.

For architecture, source navigation, tests and technical constraints:

[Developer & AI guide](docs/AI_DEVELOPMENT_GUIDE.md) · [Contracts](docs/contracts.md) · [Progress](docs/gap-analysis-2026-10-30.md) · [Roadmap](docs/roadmap-2026-10-30.md) · [Contributing](CONTRIBUTING.md)

---

*Whale Core is meant to become an AI foundation that people can understand, extend, and live with. The next step is to make that foundation reliable, useful, and genuinely connected.*
