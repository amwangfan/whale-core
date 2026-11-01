# Runtime / plugin / device hardening

This patch is intentionally separate from the optional-persona refactor (PR #48).
It does not deploy services or modify whale-mobile contracts.

## Durable task dispatch boundary

Before dispatching any handler, the runtime writes
`runtime:task:<id>.dispatch_started = {step, tool, at, fingerprint}`.
Success/failure journal updates reload the latest journal to avoid erasing that
marker. On restart, a task with that marker, legacy `in_flight`, prior
receipts, or a nonzero `next_step` is not automatically replayed.
Such uncertain tasks finish as `unknown`.

This is **at-most-once dispatch on crash recovery**, NOT guaranteed
exactly-once execution. It favors a visible unresolved action over a duplicate
side effect. Genuine exactly-once external effects require remote idempotency
keys and durable receipt reconciliation.

## Plugin trust and registration

Untrusted directory-loaded plugin manifests cannot mark themselves `bundled`.
`PluginManager.load_dir` treats its directory as external;
`load_plugin_dir` wraps registration to keep declared manifests external.
Trusted host code can register official bundled manifests explicitly with
`load_manifest`. The register signature is inspected prior to execution; a
`TypeError` from inside the plugin does not retry the registration.

**Warning:** importlib runs third-party Python code in the host process.
Trust labels govern action approval but do *not* sandbox arbitrary code.
Use only audited Python plugin paths until isolated workers are implemented.

## Device action reconciliation

The server persists the idempotency reservation and reverse action-id mapping
before sending `action.request`. Intermediate/terminal receipts update the
record; the first terminal outcome wins. Unknown action IDs are discarded
without persisted receipt spam; per-action receipt event history is limited.

The owner-authenticated API endpoint
`POST /api/v1/devices/<device_id>/action/status` with
`{"idempotency_key":"..."}` reads the latest persisted status without
resending the action. Expired nonterminal reservations are shown as `unknown`.
A late terminal receipt can reconcile that uncertainty.

The wire protocol in `contracts/v1/` is unchanged.

## Memory isolation

The hybrid recall provider is required to accept `namespace=<space_id>`.
Legacy embedding providers lacking the namespace argument degrade only that
space to lexical recall; they are not re-invoked without isolation.

## Suggested regression gates

```bash
python -m pytest tests/test_task_engine.py tests/test_runtime_lifecycle.py -q
python -m pytest tests/test_plugin_loader.py tests/security/test_security_plugin_trust.py tests/test_plugin_manager.py -q
python -m pytest tests/test_devices_ws.py tests/test_memory_hybrid.py tests/test_embedding_provider.py -q
python -m pytest tests -q
```

This repository runs CI on manual `workflow_dispatch`. These tests are
**not claimed to have passed** merely because code was pushed.
Run the selected tests on the candidate branch before merge.

## Not solved in this patch

A hung synchronous Python plugin cannot be safely terminated by killing a
thread. Worker slots are already bounded, but process isolation and cooperative
cancellation are still needed for a strict execution timeout.
Persistent vector indexing and recall performance benchmarking remain future
work. The device action-id mapping and reservation records likewise require a
retention policy and compaction migration once long-running volume is known.
