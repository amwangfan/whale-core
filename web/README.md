# Whale-Core v1 · Web 控制台

The console is a no-build web app made of native ES modules, Preact and htm, and handwritten CSS.
It is served statically by `whale serve`. Its UI language is Simplified Chinese.

```
web/
  index.html                 shell page (no inline script; CSP script-src 'self')
  vendor/preact-htm.js       htm 3.1.1 Preact standalone ESM (vendored, do not edit)
  app/
    main.js                  bootstrap · router · shell · page error boundary · first-run wizard gate
    styles.css               entry: @imports css/* in order
    icon.svg
    css/                     tokens · base · shell · ui · bridge · chat · memory · tasks · devices · pages · groups · setup
    lib/
      api.js                 rpc() over HTTP, Link (WS: hello/welcome, events, rpc, ping, backoff), token, globs
      store.js               global store + live reducers (agent, approvals, runs, activity, toasts, setup.status), useStore/useTopic, act()
      hooks.js               useRpc, useNow, useMedia, useDebounced*, useLocal, useEscape, history router (hash mode on file://)
      format.js              vocabulary (kinds, scopes, statuses, risks…), time formats, humanize(event)
      markdown.js            safe markdown-lite → vnodes (never HTML strings; http/https/mailto links only)
      graph.js               ForceGraph: own force simulation + canvas renderer, pan/zoom/drag/hover; source nodes (person/evidence/space)
      star3d.js              StarField: 3D layout, rotation/perspective math, depth-sorted canvas starfield (星空)
      mcp-graph.js           MCP knowledge-graph parser (JSON / arrays / JSONL) + preview selection for 导入图谱
    components/
      icons.js               inline SVG icon set
      ui.js                  Button, Switch, Segmented, Badge, Chip, Card, Modal, Drawer, TagEditor, Field, …
      shell.js               Nav, Topbar, ConnIndicator, Palette (Ctrl/⌘+K)
      live.js                Orb, ApprovalCard/ApprovalTray, Toasts, ActivityStream
      trace.js               思维轨迹 step list (recall/think/tool/approval/reply), shared by chat & tasks
      task-flow.js           task FLOW VIEW (steps as nodes, sub-agent branches, SVG connectors)
      memory-graph.js        graph canvas + legend + tooltip (+ graphData(): memory.graph extras → nodes/edges)
      memory-detail.js       memory detail (edit/evidence/依据/relations/lifecycle) + add form
      star-view.js           星空 3D view: controls, 性能 overlay, shared node tooltip
      graph-import.js        导入图谱: file picker + read-only preview graph with type filters, search, entity detail
      evidence.js            依据: EvidenceItem/List/Button (popover)/Modal over memory.evidence
      network.js             LAN switch, phone URLs, restart-and-reconnect (wizard, 设置 → 服务, pairing)
      setup-steps.js         wizard steps that talk to the backend: 选择大脑, 连接手机 (pairing code)
      group-form.js          群空间 create/edit form + roster editor (validateRoster), chat-log import, delete
      group-views.js         群空间 detail: 话题时间线, 关键词, 群友 + 画像/喜好, 成员发言, message lists
      phone.js               CSS phone illustration + battery gauge
      device-privacy.js      personal-phone privacy panel
      schema-form.js         plugin config form from config_schema + secrets_schema
    pages/                   login · setup · bridge · chat · memory · groups · tasks · capabilities · devices · persona · settings
  tests/                     verification tooling and the screenshot demo (see “Verification” and “Demo”)
  BACKEND_REQUESTS.md        backend changes the console would benefit from (all degrade gracefully)
```

## Runtime model

The server mounts `/app` → `web/app` and `/vendor` → `web/vendor`. Every other path that does
not start with `/api` serves `index.html`, so history routing works.

**Auth.** The token is accepted from `?token=` once and then stripped from the URL, or typed on
the first screen. It is kept in `localStorage['whale.token']`. Requests send
`Authorization: Bearer …` to `POST /api/rpc/{method}`. Artifacts use
`/api/artifacts/{id}?token=…` (`artifactUrl()`). A 401 or a WS close with code 4401 returns the
console to the token screen.

**Realtime.** There is one `Link` WebSocket to `/api/link`:

- `hello` sends `{token, role:"console", name:"Web 控制台", subscribe:["*"]}`, and the server answers `welcome`.
- Events are dispatched to glob subscribers through `link.on('run.*', fn)` or `useTopic()`.
- After a drop, the link reconnects with exponential backoff and jitter. It also reconnects when
  the tab becomes visible again or the browser comes back online.
- A heartbeat ping is sent every 20 s, and the link reconnects after 65 s of silence.
- `chat.send` goes over WS when the link is online, so the channel is `console`; otherwise it uses HTTP.

**Store.** `store.js` holds only cross-page state:

- connection state and `system.status`
- `agent.state`
- pending approvals
- recent runs: steps, streamed text, plan, progress
- the activity feed
- toasts
- theme

`snapshot()` runs on every `welcome`. It calls `system.status`, `approvals.list` and
`events.query`, then live events keep the state current. Pages load their own data with
`useRpc` and refresh on relevant topics, debounced.

## Page → RPC map

| Page | Reads | Writes / actions | Live topics |
| --- | --- | --- | --- |
| 首次设置 Setup `/setup` (also shown automatically while `setup.status.completed` is false, until 「稍后再说」) | `setup.status`, `models.presets`, `personas.list`, `system.network` | `setup.quick_model {preset, api_key?, base_url?, chat_model?}`, `personas.activate`, `system.set_lan`, `system.restart`, `devices.pair_start`, `setup.complete {name}` | `device.paired`, link state (restart → reconnect) |
| 总览 Bridge `/` (`?focus=memory\|caps\|tasks\|devices`) | `system.status`, `events.query`, `setup.status` (完成设置 card while `steps.model` is false); tiles: `memory.list`, `capabilities.list`, `tasks.list`, `schedule.list`, `devices.list` | `memory.update` (adopt candidate), `memory.maintain`, `tasks.create` | `agent.state`, everything (activity), `memory.*`, `task.*`, `schedule.*`, `device.*`, `capability.*` |
| 对话 Chat `/chat[/:id]` (`/chat/new?draft=` or `?send=`; `/chat/:id?trace=<run_id>` opens the 思维轨迹 drawer) | `conversations.list`, `conversations.messages`, `runs.get` | `chat.send {wait:false, images[]}`, `chat.cancel {run_id}`, `conversations.update/delete`, `approvals.resolve` | `run.started/step/delta/completed/failed/cancelled`, `run.status`, `approval.*` |
| 记忆 Memory `/memory[/:id]` (`?q=`, `?view=inbox`, `?add=1`) | `memory.stats`, `memory.graph {limit, scope?, sources?}` (one selected scope is sent server-side; 来源 adds person/evidence/space nodes), `memory.list`, `memory.search`, `memory.get`, `memory.evidence` (依据, evidence nodes) | `memory.remember`, `memory.update` (content/kind/importance/tags/status), `memory.forget`, `memory.maintain`; 导入图谱 is local-only (never sent) | `memory.*` |
| 群聊 Groups `/groups[/:id]` (`?tab=timeline\|keywords\|members\|transcript`, `&member=`, `&sender=`, `?new=1`) | `groups.list`, `groups.get`, `groups.messages {sender?, segment?, word?, offset}`, `groups.context`, `memory.evidence` | `groups.enable`, `groups.update`, `groups.set_paused`, `groups.set_opt_out`, `groups.import {content}`, `groups.refresh` (`conflict` → 正在更新), `groups.delete {wipe_memories}` | `group.updated`, `group.refreshed`, `group.archived` |
| 任务 Tasks `/tasks[/:id]` (`?new=1`) | `tasks.list`, `tasks.get`, `runs.get` (children), `schedule.list` | `tasks.create {goal,title,schedule}`, `tasks.start/cancel/delete`, `schedule.set_enabled/run_now/delete` | `task.*`, `run.*`, `run.plan`, `run.progress`, `schedule.*` |
| 能力 Capabilities `/capabilities` | `capabilities.list`, `plugins.list` | `policy.set {pattern, verdict\|null}`, `plugins.set_enabled`, `plugins.configure {id, config, secrets}` | `capability.*`, `plugin.changed`, `config.changed` |
| 设备 Devices `/devices[/:id]` (`?pair=1`) | `devices.list`, `devices.get`, `devices.privacy`, `link.clients`, `system.network` (phone address / LAN hint on 配对新设备) | `devices.set_role`, `devices.action` (observe, key home/back), `devices.notify`, `devices.set_scopes`, `devices.set_privacy`, `devices.rename/revoke`, `devices.pair_start`, `devices.digest_now`, `devices.search_events` | `device.*`, `clients.changed` |
| 人格 Persona `/persona` | `personas.list`, `memory.list {kind:"style"}` | `personas.save/activate/delete`, `personas.import {card}` (JSON or PNG with `chara` chunk), `memory.update/forget` | `persona.*`, `memory.*` |
| 设置 Settings `/settings[/:section]` | `config.get`, `models.status`, `autonomy.status`, `link.clients`, `system.network` | `config.update {patch}`, `models.upsert_provider/remove_provider/list_remote/test`, `memory.maintain`, `autonomy.explore`, `system.set_lan`, `system.restart` (two-step confirm), 重新运行引导 → `/setup` | `config.changed`, `autonomy.*`, `clients.changed` |
| Global | `approvals.list` | `approvals.resolve {id, approve, remember}` | `approval.*`, `notify` (toast) |

**First-run wizard.** After `welcome`, `snapshot()` also loads `setup.status` into the store. While it
reports `completed: false`, the shell renders the full-screen wizard instead of the pages. 「稍后再说」
sets `localStorage['whale.setup.later']`; the wizard stays reachable at `/setup` (设置 → 服务, command
palette), and 总览 shows a 完成设置 card while `steps.model` is false. A backend without
`setup.status` (`not_found`) never blocks the console. 「立即重启服务」 calls `system.restart`, then
follows the live link down and back up (it nudges `link.retryNow()` after 3 s) and reloads `system.network`.

## Design

The visual identity is "deep ocean". Dark mode is the default: a navy base, glass panels and a
luminous cyan/teal accent. Light mode is a clean daylight variant. The theme is either `auto`,
which follows `prefers-color-scheme`, or a manual choice from the top bar or Settings. The choice
is saved in `localStorage['whale.theme']`.

| Token | Dark | Light | Use |
| --- | --- | --- | --- |
| `--bg` / `--bg-grad` | `#050e1c` + radial glows | `#f4f8fb` | page |
| `--panel` | `rgba(14,33,60,.62)` + blur | `rgba(255,255,255,.78)` | glass cards |
| `--text` / `-2` / `-3` | `#e3f1fb` / `#a9c2d8` / `#6f8aa5` | `#0c2236` / `#3d5a73` / `#6d8499` | text hierarchy |
| `--accent` / `--accent-2` | `#2ee6d6` / `#38bdf8` | `#0d9488` / `#0284c7` | primary, focus, live |
| `--ok --warn --danger --info --violet` (+ `-soft`) | | | status tones |
| `--k-<kind>` | fact `#38bdf8` · preference `#f472b6` · episode `#fbbf24` · knowledge `#34d399` · diary `#a78bfa` · person `#fb923c` · skill `#2ee6d6` · style `#f9a8d4` | darker equivalents | memory kinds (graph, chips) |
| `--sc-<scope>` | owner teal · shared gold · group violet · person rose | | memory scope rings |
| `--s1…--s8` | 4 · 8 · 12 · 16 · 24 · 32 · 48 · 64 px | | 8px grid |
| `--r1…--r4` | 6 · 10 · 14 · 20 px | | radii |
| `--t-fast / --t / --t-slow` | 160 / 240 / 320 ms, `--ease` = cubic-bezier(.2,.8,.2,1) | | motion |

The font stack is Inter, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans SC" and system-ui.

**Reduced motion.** `prefers-reduced-motion` sets every duration to 1 ms. The memory graph then
lays itself out synchronously instead of animating.

**Accessibility:**

- focus rings via `:focus-visible`
- a skip link
- `aria-*` on switches, segmented radios, dialogs, the tray and the palette (combobox/listbox)
- keyboard: Ctrl/⌘+K, Esc, arrow keys in segmented controls and the palette, Enter in forms
- `aria-live` on the connection indicator, toasts and the chat stream

**Dynamic focus.** On the Bridge, the four tiles share one flex row. Opening a tile animates
`flex-grow` and `flex-basis` so it takes the working space, and the other tiles shrink into 76 px
vertical rails. On a phone the tiles stack instead, and the rails become one-line headers.

## Verification

There is no browser, node or npm here. The only JavaScript runtime is a VM sandbox that has no
`vm`, no `eval` and no `new Function`. Tooling (all under `tests/`):

- `bundle.js` — `makeBundler(fs, path)(webDir, testFiles, outName)` rewrites every module's
  import/export into a factory inside one script, `tests/out/<outName>`. `/vendor/preact-htm.js`
  becomes a shim in which `html` is the real htm parser, extracted verbatim from the vendored
  file and bound to `React.createElement`. A syntax error in any file fails compilation of that
  bundle, so running it is the syntax check. `bundle.py` is the same tool in Python.
- `static.js` — `staticChecks(fs, path, webDir)` checks that:
  - `index.html` has no inline script or handlers and loads `/app/main.js` (module) and `/app/styles.css`;
  - no `eval`, `new Function`, `innerHTML`, `dangerouslySetInnerHTML` or string timers are used;
  - every named import resolves to a file that exports that name, including the vendor exports;
  - CSS `@import` targets exist and braces balance;
  - files stay under 600 lines.
- `mocks.js` — RPC results shaped exactly like the backend, e.g. `MemoryItem.to_dict`, `devices._view`, `RunLedger` rows and `PluginRecord.to_dict`.
- `mocks-groups.js` — `buildGroupMock(now)`: the 2026-11-24 API (`docs/API-2026-11-24.md`): a QQ group space with 6 members + a bot, ~45 messages, topic segments, keywords, portraits with traits and evidence ids, claims (current/expired), an opted-out member, a paused empty group; `memory.evidence`, `memory.graph {sources}`, `setup.*` (incomplete), `models.presets` (DeepSeek, 通义千问, Kimi, 智谱, 硅基流动, OpenAI, Ollama), `system.network/set_lan/restart`. It merges itself into `MOCK.R` and is reused by the demo.
- `smoke.js` — renders every page and route variant to a string with `react-dom/server`, in narrow
  and wide layouts, with `useRpc` mocked. It also renders the shell, login, tray, palette, toasts,
  trace and orb phases, every wizard step (preset chosen, local preset, testing, success, error, phone
  with URLs + pairing code), the wizard gate (auto / 稍后再说 / `/setup`), the 完成设置 card, groups
  (empty, list, every detail tab, portrait, opted-out member, empty digest, forms, import, delete),
  evidence popover and unavailable reasons, memory graph with 来源, 星空 3D, import preview (incl.
  truncation), settings 服务 card (LAN off / on / restart required). It fails on any exception, a
  visible `undefined` or `NaN`, or `[object Object]`.
- `unit.js` — covers:
  - `Link` against a fake WebSocket: hello shape, welcome, glob dispatch, RPC resolve/reject,
    ping/pong, backoff, 4401;
  - store reducers;
  - markdown safety and the URL token;
  - the schedule builders;
  - `ForceGraph` on a real `canvas` (convergence, no overlaps, hit-test, fit);
  - PNG Character Card extraction;
  - the MCP graph parser (object / array / flat list / JSONL, merging, implicit entities, skipped lines, rejects) and the 1000-node preview;
  - 3D math (rotation, inverse, length preservation, projection, near plane, zoom, Fibonacci sphere, depth alpha, deterministic layout);
  - `StarField` on a real canvas (auto-rotate, fps, hit-test, click, drag-rotate, zoom clamp, pause when hidden, reduced motion);
  - source nodes in `ForceGraph`, roster validation, route parsing for group ids.
- `demo-smoke.js` — the same render checks over the **demo** data (every demo page, narrow and wide), plus the fake fetch and the fake WebSocket streaming a `chat.send` run.
- `minidom.js` + `demo-dom.js` — boot the real demo (vendored Preact, no React) on a tiny DOM shim inside the sandbox and walk every page, click 「更新」 and stream a chat run (built by `demo.py` as `tests/out/demo-dom.js`).

Run them from `run_javascript` (`WEB` = the absolute path of this `web/` directory). Bundles must be built
with the JS bundler inside the sandbox, because files written from Python reach the JS sandbox late
(if a run reports old results, run it again a few seconds later):

```js
// 1) file: tests/bundle.js
const b = makeBundler(require('fs'), require('path'));
b(WEB, ['tests/mocks.js', 'tests/mocks-groups.js', 'tests/smoke.js'], 'smoke.js');
b(WEB, ['tests/mocks.js', 'tests/mocks-groups.js', 'tests/unit.js'], 'unit.js');
b(WEB, ['tests/demo-env.js', 'tests/mocks-groups.js', 'tests/demo-data.js', 'tests/demo-runtime.js', 'tests/demo-smoke.js'], 'demo-smoke.js');
// 2) file: tests/out/smoke.js            → prints "SMOKE: n/n passed"
// 3) file: tests/out/unit.js, code: await globalThis.__UNIT_DONE   → "UNIT: n/n passed"
// 4) file: tests/static.js, code: console.log(staticChecks(require('fs'), require('path'), WEB))
// 5) file: tests/out/demo-smoke.js, code: await globalThis.__DEMO_SMOKE_DONE   → "DEMO SMOKE: n/n passed"
// 6) after `python tests/demo.py`: file: tests/out/demo-dom.js,
//    code: for (let i = 0; i < 150 && __DEMO_DOM_DONE !== __DEMO_RUN; i++) await new Promise((r) => setTimeout(r, 500));   → "DEMO DOM: n/n passed"
```

The sandbox realm persists between runs and freezes `Object.prototype`; `demo-dom.js` copes with both
(a run token, and a `defineProperty` copy of Preact's internal assign helper in that check build only).

## Demo (README screenshots)

`tests/demo.py` builds **one self-contained file**, `tests/out/demo.html`, that opens from `file://`
with no server:

```sh
cd web && python tests/demo.py        # → tests/out/demo.html (+ tests/out/demo-dom.js, the headless check)
```

It inlines `styles.css` (imports resolved), the vendored preact-htm (its ESM `export` rewritten), every
app module (the `bundle.py` transform), `mocks-groups.js`, `demo-data.js` (a lived-in instance: 鲸鱼
and its owner 林舟, five conversations with a recall → think → tool → reply trace, ~50 memories across
owner / shared / group / private scopes with links, a running task with a sub-agent flow, a scheduled
weekly report, two phones — 鲸鱼的手机 (Root, agent) and 日常机 (personal), QQ / weather / media
plugins, the 周末跑团小分队 group space, a pending approval) and `demo-runtime.js`, which pre-seeds the
token, installs a fake `fetch` for `/api/rpc/*` and a fake `WebSocket` (hello → welcome, `rpc.request`,
ping) and streams a simulated run for every `chat.send`. Routing uses hash mode (`#/memory`), which
the router enables on `file://` or when `globalThis.WHALE_HASH_ROUTES` is set.

Open a page directly with `?page=` — `bridge`, `chat`, `memory`, `groups`, `tasks`, `capabilities`,
`devices`, `persona`, `settings`, `setup` — or any route, e.g. `demo.html#/memory/m_cat`. Extras:
`&theme=light|dark`, `&view=list` (memory), `&dim=3d` (星空), `&sources=1` (来源). The console's CSP does
not apply to this test artifact; it is never served by `whale serve`.

No interaction is needed: the token is pre-seeded (no login screen), setup reports `completed`, so the
wizard appears only for `?page=setup`; `chat` opens 年糕的晚饭 with its reply and the 思维轨迹 drawer
expanded; `memory` opens the graph; `groups` opens 周末跑团小分队 → 群友 → 小雨's portrait;
`tasks` opens the running trip task with its sub-agent flow; `devices` opens 鲸鱼的手机. Give the page
~1.5 s after load before a screenshot (fake RPC latency and the graph layout settling), e.g.
`msedge --headless --screenshot=bridge.png --window-size=1440,900 --virtual-time-budget=4000 "file:///…/tests/out/demo.html?page=bridge"`.

The smoke render goes through React, not Preact. Preact-specific behaviour is kept portable:
styles are always objects, and `class` and `stroke-width` are mapped by the shim. Live DOM
behaviour is still worth a manual pass in a real browser: canvas interaction, drag-and-drop,
WebSocket reconnects and CSS transitions.
