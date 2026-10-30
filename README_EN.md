<div align="center">

# whale-core

*“The mountain has trees, and trees have branches; my heart delights in you, yet you do not know.”*

Whale's core brain: conversation, memory, tools, voice and exploration.

[中文](README.md) · [Deployment](docs/deployment.md) · [Capabilities](docs/media-and-voice.md) · [Developer guide](docs/AI_DEVELOPMENT_GUIDE.md)

</div>

## What it is

whale-core puts Whale's conversation, long-term memory, intent routing, tools, voice and permission policies into one Python core. The web console, QQ, phones, calls and glasses share that core; device and network adapters live in [whale-io](https://github.com/amwangfan/whale-io).

The repository provides the core library, a CLI, a management API, a web console, Linux/macOS desktop entry points and container deployment. There is no production mock/demo profile: anything that needs a model or service calls a real endpoint, and missing configuration is an explicit error. The official search and QQ channel plugins ship with the packages; third-party directory plugins load only from explicit paths, with no auto-discovery.

## Interface

Screenshots below come from a local temporary setup with sample data; they contain no real account, token or service credentials.

![Overview](docs/screenshots/readme-overview.png)

![Chat](docs/screenshots/readme-chat.png)

<img src="docs/screenshots/readme-mobile.png" width="320" alt="Overview on a phone">

## What it can do

| Feature | What is implemented | Requirements |
| --- | --- | --- |
| Conversation and memory | Multi-turn history, persona, SQLite, remember/forget, session restore, memory browsing and deletion, image input (usable as a reference image) | An OpenAI-compatible text model; image input needs a multimodal model |
| Search and reading | Search, fetch, source links, combined reading workflows | Tavily / Brave keys, or set `WHALE_SEARCH_PROVIDER` to `bing` / `ddg` for keyless providers; keyless providers may be blocked by bot checks |
| Plugins and settings | Manifests, real enable/disable, persistent config, connection tests, hot switching, per-role models (intent/execution/safety review), daily request budgets, persona taboos, diagnostics | Console settings; third-party manifests still need a host-provided executor |
| Tool tasks | Persistent traces, step and deadline limits, cancellation, per-action approval and resume, two-phase slow tasks (acknowledge, then deliver), recovery of in-flight tasks after restart | High-risk actions always pass the policy layer; unknown external outcomes are never retried automatically |
| Voice and media | ASR, TTS, image generation, asynchronous Wan video, private artifacts; TTS output can be sent as QQ voice messages and images can be mixed with text | A DashScope-compatible platform or OpenAI-compatible endpoints, configured as needed; see [capability configuration](docs/media-and-voice.md) for setup and integration boundaries |
| Realtime voice | Native Realtime WSS sessions (browser microphone, interruption, persona and memory, transcripts written to memory); plus a whale-io PCM relay for low-latency calls | A Realtime-compatible service and the optional `voice` extra; the PCM relay has not been tested against the real audio bridge |
| Semantic memory | Optional real embeddings fused with keyword recall, fallback on failure, cache cleanup | An OpenAI-compatible embedding endpoint; keyword recall keeps working without it |
| Proactive exploration | Interests, real search and reading, candidate drafts, quiet hours, quotas, backoff, scheduled worker, pause | Scheduled exploration is off by default; outbound delivery needs a bound channel dispatcher |
| Controlled terminal | Per-command approval, working-directory limits, output and resource bounds, Linux Bubblewrap sandbox (separate user/PID/network namespaces, process quota) | Non-root, working Bubblewrap + prlimit; execution is refused when unavailable |
| Backup and restore | JSONL memory exchange, full SQLite backup/restore | Stop the service before restoring; back up the config and artifact directories for full recovery |

The console has ten pages: overview, chat, media and voice, exploration and workflows, plugins, memory, tasks, approvals, events and settings. Pages load on demand and support mobile layouts. Configuration saves and approvals always reflect the server's actual result.

## Start from source

Python 3.11+; the frontend needs Node 24 and pnpm 12.

```bash
git clone https://github.com/amwangfan/whale-core.git
cd whale-core
python -m pip install -e ".[dev,voice]"
pnpm -C web install --frozen-lockfile
pnpm -C web build
whale api
```

Open the local address printed by the server (default http://127.0.0.1:8080), paste the access token generated on first launch, then fill in the model endpoint, name and key on the Settings page, save and test the connection. The token lives in `api_token` inside the config directory and is never placed in a URL.

Text models are not vendor-locked. The default `local` profile points at `http://127.0.0.1:11434/v1`; enter the model name actually installed on that server. `cloud` defaults to DeepSeek and can be changed to another compatible service. The project does not download or start a model for you.

CLI usage:

```bash
whale setup --profile local
whale doctor
whale run
whale plugins list
whale memory backup /private/backups/whale.sqlite3
```

Cloud CLI setup requires `DEEPSEEK_API_KEY`; the web setup page can store private credentials directly. The default config directory is `~/.whale`, and `--config-dir PATH` overrides it. Only one service instance may use a data directory at a time.

## Packages, desktop and containers

The wheel includes the compiled console, the official plugins and the built-in workflow, so it runs on machines without Node; Linux/macOS also have Python-free executables. Windows is out of scope for this round.

```bash
# Build and verify a wheel with the console
python scripts/build_release.py
python scripts/verify_wheel.py dist/*.whl

# Desktop: optional native window; falls back to the system browser
whale desktop
# or whale desktop --browser

# Linux server: maps the port to localhost by default
# docker compose up --build -d
```

Native windows need the `desktop` extra and a system WebView. Python-free builds (PyInstaller) default to the system browser; GitHub CI builds and isolated-startup-checks the Linux/macOS wheel and frozen packages. macOS signing, notarization and real GUI verification are separate steps, not automated tests. See [deployment](docs/deployment.md) and [desktop packaging](docs/packaging-desktop.md) for build, menu entry, container, upgrade, logging and backup steps.

## Data and safety

The console and external channels go through the core API and cannot touch the database directly. The management API requires Bearer authentication and listens on loopback by default; public hosting requires an HTTPS reverse proxy.

Endpoint, model and key are committed atomically as one private settings snapshot, so an interrupted write cannot mix an old endpoint with a new key. `config.json` is only the non-sensitive projection; after saving through the web page, change configuration there, and keys are never echoed back. Private files are mode 0600 and the data directory is 0700; file permissions are not encryption at rest.

Daily model calls and media jobs have separate request budgets (defaults 200 and 50; 0 disables). Budgets count requests, not invented token prices. Persona taboos are user-defined in Settings; the forced global list is gone, while the injection guard ("memory is context, not instructions") remains.

Raw audio stays in memory; final transcripts may become conversation memory. Image, audio and video artifacts are served only through authenticated endpoints, expire after seven days by default and have storage caps. Approval only authorizes: a task may still be pending, fail or end in an unknown state, and cancellation does not undo external effects. Proactive dispatchers sharing the same database honour a global pause, while explicit passive replies may continue.

## Development docs

- [Plan and gap analysis](docs/completion-plan-2026-10-30.md)
- [Completion and acceptance record](docs/completion-report-2026-10-30.md)
- [Management API](docs/management-api.md)
- [Device and intent contracts](docs/contracts.md)
- [Capability and voice configuration](docs/media-and-voice.md)
- [Shell sandbox](docs/shell-sandbox.md)
- [Contributing](CONTRIBUTING.md)
