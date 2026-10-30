> Current console/runtime completion work: see [implementation status](docs/completion-status.md), [deployment](docs/deployment.md), and [service bridges](docs/service-bridges.md). These documents supersede older progress counts below. The browser realtime/PCM relay, Wan video provider and native device adapters are implemented, but real service/device and target deployment verification are still incomplete; Windows is excluded.

<div align="center">

# whale-core

*“The mountain has trees, and trees have branches; my heart delights in you, yet you do not know.”*

The intelligence layer for Whale: conversations, memory, intents, tools and policies.

[中文](README.md) · [Developer guide](docs/AI_DEVELOPMENT_GUIDE.md) · [Contracts](docs/contracts.md) · [Progress notes](docs/gap-analysis-2026-10-30.md)

</div>

## What it is

whale-core is a companion-AI core built on the Python standard library (Python 3.11+; Windows also needs `tzdata`). Conversations, long-term memory, intent routing, tool calls and permission policies live here, so QQ, phone calls, mobile apps, glasses and the browser can share one core instead of each keeping its own logic.

The repository contains three parts:

- the core library: `src/whale_core/`
- the CLI: `whale setup`, `whale doctor`, `whale run`, `whale plugins`, `whale memory`, `whale api`
- a management API (stdlib REST + SSE) with a web console: `api/`, `web/`

Device integration, networking bridges and hardware adapters live in [whale-io](https://github.com/amwangfan/whale-io). The two repos talk over event and intent contracts and evolve independently.

The core is not tied to a vendor: text models, speech, search and image generation are plugins, and switching providers is a matter of assembly. Plugin actions still pass through the core's identity and permission checks.

## Where the project stands

Version 0.1.0, early development. Module interfaces will still change.

As of 2026-10-30:

- `python -m pytest tests -q`: 808 passed, fully offline, no network access
- No mock/demo profile. Conversations always hit a real model endpoint and fail loudly without credentials; test doubles live only under `tests/`
- The console has 7 tabs: Overview, Plugins, Memory, Approvals, Chat, Tasks, Events
- The QQ / WeQ chain (inbound message → model → human approval → send) has been verified end to end on a real account

Current state by module:

| Module | Status | Notes |
| --- | --- | --- |
| Conversation and memory | Working | Multi-turn history, SQLite storage, episodic/preference records, recall, JSONL import/export, `记住：`/`忘掉：` commands |
| Intents and tools | Working | Rule-based routing; explicit search requests call a configured search tool; optional LLM intent planning (`WHALE_INTENT_MODEL_ENABLED=1`) grounds defaults in memory and recent context (e.g. location defaults to your city); clarify/approval/denial replies are voiced by the whale persona |
| Model access | Needs a real endpoint | Any OpenAI-compatible endpoint; the `local` profile points at a local service, `cloud` defaults to DeepSeek |
| Search and fetch | Partly working | Needs `TAVILY_API_KEY` or `BRAVE_API_KEY`; the DDG fallback is often blocked by bot checks |
| Plugin system | Working | Manifest loading, enable/disable, health checks, redacted config, per-risk approval exemptions |
| Shell execution | Implemented, not isolated | Command tiers, timeouts, output truncation, environment scrubbing; no OS-level sandbox |
| Speech / image / video | Contracts + providers | ASR/TTS, image generation and async video jobs have interfaces and offline tests; no real service has been wired up yet |
| Messaging channels | QQ / WeQ integrated | OneBot as well; event and action contracts cover phones, VoIP and glasses |
| Proactive behaviour | Working, conservative defaults | Candidates, quotas, quiet hours, deduplication, feedback, exploration ledger |
| Desktop packaging | Design only | `docs/packaging-desktop.md`, not implemented |

Evidence and acceptance records for each row live in the [progress notes](docs/gap-analysis-2026-10-30.md).

## Quick start

Requires Python 3.11+. For real conversations you also need a reachable OpenAI-compatible endpoint (a local Ollama instance will do).

```bash
git clone https://github.com/amwangfan/whale-core.git
cd whale-core
python -m pip install -e ".[dev]"

whale setup --profile local   # writes ~/.whale/config.json
whale doctor                  # per-item checks, [ok]/[warn]/[fail]
whale run                     # REPL, /quit to exit
```

The `local` profile needs no key and defaults to `http://127.0.0.1:11434/v1`. If the endpoint is down, `whale run` reports a network error instead of producing canned replies.

Tests and offline examples:

```bash
python -m pytest tests -q
python examples/runtime_demo.py   # multi-turn chat, intent planning, tool loop, all deterministic doubles
python examples/life_scenario.py
```

`uv` works too: `uv sync --extra dev`, then `uv run whale ...`.

## Configuration

There are two profiles, stored in `~/.whale/config.json`:

| Profile | Connects to | What you need |
| --- | --- | --- |
| `local` | A local OpenAI-compatible endpoint, default `http://127.0.0.1:11434/v1` | A running endpoint; override with `LOCAL_MODEL_BASE_URL` and `LOCAL_MODEL_NAME` |
| `cloud` | DeepSeek (OpenAI-compatible) | `DEEPSEEK_API_KEY` in the environment |

Cloud example:

```bash
export DEEPSEEK_API_KEY=...
whale setup --profile cloud
whale run
```

`config.json` stores only non-sensitive settings and environment variable names; keys are read from the environment and never written to disk. `setup` does not overwrite an existing config; pass `--force` to rewrite it. The project does not install models or start a model server for you.

## Web console

The frontend needs Node 24 and pnpm 12 (the versions CI uses).

```bash
pnpm -C web install
pnpm -C web build
whale api
```

Then open http://127.0.0.1:8080 . The first launch generates an access token, prints it once and stores it in `~/.whale/api_token` (mode 600); enter it in the console.

The console is an administration UI, not the runtime itself: the approvals tab records decisions, while actual sending depends on the corresponding dispatcher; the events tab is pushed over SSE. The API listens on `127.0.0.1` only, so do not expose it publicly without authentication.

## Repository layout

```text
src/whale_core/
  core/        conversation orchestration, intent plans, model access, diagnostics
  memory/      SQLite storage, session history, hybrid recall, import/export
  plugins/     manifests, registry, manager, gateway, plus model/search/fetch/speech/image/video
  perception/  perception event collection and reasoning
  initiative/  proactive policy, call planning
  actions/     intent-to-action gateway
  cloud/       controlled shell (policy tiers + executor)
  life/        QQ/OneBot/WeQ chain, inbox, proactive ledger, exploration
  ui/          persona cards, voice, HUD
  api/         management REST + SSE + static console hosting
  cli/         the whale CLI
  skills.py    Markdown-driven tool workflows
  profiles.py  local / cloud assembly
web/           console (React 18 + Vite + TypeScript)
tests/         pytest suite, fully offline
docs/          contracts, roadmap, progress, developer guide
```

## Known limitations

- The repository has no project-level LICENSE yet. `licenses/` only covers third-party persona cards, not the project as a whole.
- Mock/demo modes have been removed: missing credentials surface as config/network errors, not fabricated results.
- Search, speech, image and video have interfaces and offline tests but no real-service integration testing.
- The shell executor has tiers, timeouts, truncation and environment scrubbing, but no OS-level sandbox. Do not feed it untrusted commands.
- Proactive and outbound actions are bounded by quotas, quiet hours and channel allowlists; an approval is not a delivery.
- Desktop packaging is a design document (Tauri sidecar), with no code behind it.
- The frontend needs a Node toolchain, and build artifacts are not committed.

## Documentation

- [Developer & AI guide](docs/AI_DEVELOPMENT_GUIDE.md): code map, conventions, known issues
- [Contracts](docs/contracts.md): boundary with whale-io
- [Progress and gap analysis](docs/gap-analysis-2026-10-30.md): evidence and status per feature
- [Roadmap](docs/roadmap-2026-10-30.md)
- [Desktop packaging plan](docs/packaging-desktop.md) (design draft)
- [Contributing](CONTRIBUTING.md)
