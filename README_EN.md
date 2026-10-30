<div align="center">

# whale-core

Whale's shared runtime for conversation, memory, tools, voice and exploration.

[中文](README.md) · [Deployment](docs/deployment.md) · [Capabilities](docs/media-and-voice.md) · [API](docs/management-api.md)

</div>

whale-core is the Python intelligence layer shared by the web console, QQ, phones, voice calls and glasses. Device and network adapters live in [whale-io](https://github.com/amwangfan/whale-io). Production profiles call real services; missing configuration is an error, never a canned answer.

The runtime includes persistent multi-turn conversation, SQLite memory, optional semantic retrieval, dynamic plugin lifecycle, bounded tool tasks, exact-action approval and resume, cancellation, source-linked search/reading, ASR/TTS, image generation, asynchronous Wan video, private media artifacts, native Realtime voice and opt-in scheduled exploration. The React console exposes setup, diagnostics and daily operations. Linux Shell execution requires an available Bubblewrap sandbox and explicit approval.

## Start from source

Python 3.11+, Node 24 and pnpm 12 are required for development.

```bash
git clone https://github.com/amwangfan/whale-core.git
cd whale-core
python -m pip install -e ".[dev,voice]"
pnpm -C web install --frozen-lockfile
pnpm -C web build
whale api
```

Open the local address printed by the server, paste the access token, then configure a real OpenAI-compatible model in Settings and test the connection. The default local endpoint is `http://127.0.0.1:11434/v1`; select a model actually installed on that server. The cloud default is DeepSeek, and other compatible endpoints can be configured.

```bash
whale setup --profile local
whale doctor
whale run
whale plugins list
whale memory backup /private/backups/whale.sqlite3
whale desktop --browser
```

Cloud CLI setup requires `DEEPSEEK_API_KEY`. Web setup can save private credentials directly. The default CLI data directory is `~/.whale`; use `--config-dir` to choose another directory. Desktop data has a separate platform default.

Build an installable wheel with `python scripts/build_release.py`. It includes the compiled console, so installed users do not need Node. Linux/macOS desktop launchers, optional native windows, frozen executables without Python, and non-root containers are documented in [deployment](docs/deployment.md) and [desktop packaging](docs/packaging-desktop.md). Windows is excluded from this implementation round.

## Configuration, data and execution

The authenticated API is local by default. Public hosting requires a TLS reverse proxy. Endpoints and credentials are atomically committed as one private settings snapshot; `config.json` is a nonsecret projection. Secrets are never returned through the API. Mode-0600 files and mode-0700 directories provide local permission protection, not encryption at rest.

Raw audio remains in memory. Final transcripts may enter memory; media artifacts require authentication and have bounded retention and storage. Portable JSONL memory exchange and complete SQLite backup/restore are separate operations. Stop services before restore, and also back up the configuration/artifact directory for full recovery.

Approval authorizes later execution; it does not claim delivery. In-flight cancellation cannot undo external effects. Unknown outcomes are not automatically retried. Active tasks block configuration replacement, and old plugin references are revoked after switching.

Scheduled exploration is disabled by default and creates candidate drafts rather than sending messages. Global pause affects proactive dispatchers sharing the same canonical store, while explicitly passive replies may continue.

## Validation and limitations

```bash
python -m pytest tests -q
pnpm -C web test
pnpm -C web build
```

See the [completion report](docs/completion-report-2026-10-30.md) for current evidence. Tests cover real local HTTP transport, provider contracts, configuration faults, task/approval semantics, audio privacy, memory recovery, frontend interactions, installed wheels and frozen Linux executables. They do not establish successful paid-provider or physical-device integration.

Prior QQ/WeQ account integration is recorded in the project. This round does not send real account messages or invoke paid providers. The current container cannot run user-namespace isolation, so successful sandbox isolation still requires target-host validation; fail-closed behavior is tested. macOS, Docker and actual GUI validation have CI/target-environment paths. Signed/notarized macOS releases remain a separate distribution step.

The project-level license remains owner-pending. Third-party notices under `licenses/` do not grant a license for the entire repository. Resolve the license before public redistribution.
