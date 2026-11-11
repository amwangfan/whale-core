# Glasses joint integration

This release reuses the existing companion, tool registry, TTS providers and QQ delivery pipeline. It does not introduce a separate glasses agent or change proactive-send defaults.

## Chat context

`POST /v1/chat` retains `text`, `mode`, `sessionId` and `followup: "poll"`.

The shipped `[现在]`, `[今日日程]`, `[明日日程]`, `[天气]` prefix followed by `[用户]` remains supported. The actual user utterance is separated before intent routing and memory/history writes. Facts are request-only background; parallel tool followups inherit that turn's snapshot.

An optional structured `context` can replace the text prefix:

```json
{
  "text": "明天有什么安排？",
  "mode": "companion",
  "sessionId": "same-conversation",
  "followup": "poll",
  "context": {
    "calendar": {
      "2026-11-11": [],
      "2026-11-12": [{"id":"exam-1","title":"考试","start":"09:00","end":"11:00","allDay":false}]
    },
    "weather": {"text":"晴 18°C","ageSec":120}
  }
}
```

Dates use Asia/Shanghai. Only today's and tomorrow's date keys are rendered. `[]` means successfully read with no events; `null` means unreadable/unknown. Missing data does not mean an empty schedule. `allDay` is a boolean. Client time does not replace the server clock. Weather older than 30 minutes is labelled as cached.

## Calendar synchronization

`POST /v1/context/sync` uses the same phone bearer authentication:

```json
{
  "source":"android.calendar",
  "observedAt":"2026-11-11T08:00:00+08:00",
  "calendar":{
    "2026-11-11":[],
    "2026-11-12":[{"id":"exam-1","title":"考试","start":"09:00","end":"11:00","allDay":false}]
  }
}
```

This replaces the complete reported window in the existing SQLite KV store, rather than appending events. Repeated snapshots do not duplicate events. Send a fresh complete window after deletions or rescheduling. `calendar: {}` clears the snapshot. Responses contain `ok`, server `receivedAt`, and normalized `dates`. Calendar synchronization does not persist weather or start proactive notifications.

Current request facts override the corresponding synchronized dates. Legacy facts with a stale date anchor cannot hide a current synchronized calendar.

## Replies and voice

Clients should display the first reply immediately and poll `/v1/chat/followup` using the returned **sessionId and exact turnId**. Handle `pending`, `ready`, `none`, and `unknown`; display a ready result once. Legacy merged replies remain compatible.

The glasses tool registry now includes configured optional capabilities, including existing `tts.synthesize`. Successful audio artifacts use the existing result-delivery path to the configured owner QQ peer. No new sender or provider is introduced. Per-turn artifact tracking prevents duplicate sends. `audioDelivery` exposes actual delivery results in synchronous replies and followup responses; generation alone is not reported as delivery success. `/v1/qq/reply` remains the existing text-dictation endpoint.

## Identity, persona and memory

Authenticated phone requests use a server-owned phone identity with the existing context resolver. Terminal session histories remain separate. Explicit session persona bindings take precedence, followed by the configured owner's QQ persona binding and the global active card. Task replies retain the persisted persona selection. Delivery notes use the resolved persona rather than an arbitrary binding from the database.

A host-invoked, idempotent migration moves only unscoped legacy `chat` episodes whose conversation starts with `glasses:` into the owner working space, preserving IDs and timestamps. Other channels, member spaces, and protected vault records are not moved.

Ordinary chat episodes can now be retrieved by relevance beyond the recent-history window. This is bounded, same-conversation lexical retrieval with IDF weighting, curated memories first, and no extra model calls or keyword lists. It does not automatically promote model-generated text into confirmed facts or claim complete cross-terminal memory consolidation.

Proactive greeting decisions and text generation share the active persona and authorized memory/history context. Enabled/dry-run, cooldown, daily quota and send callbacks are unchanged. Fixed host tone/punctuation rules were removed; role cards retain expression control.

## Compatibility and verification

- No `/v1/agent` alias or high-privilege executor is added. The separate client/server agent contract must be reconciled with the service that actually owns that route.
- Firmware images, sticker display, ASR routing, calendar permission handling and APK installation belong to the glasses client integration.
- Full **local** backend regression for the initial integration: 2821 passed, 1 skipped. This is not an Actions result; GitHub CI results are recorded separately on the PR, with their run URLs and exact head SHA.
- Dedicated tests cover prefix/structured context, local HTTP synchronization, concurrent first/followup snapshots, bound persona continuity, TTS-to-QQ fake delivery, legacy memory migration, historical episode recall and proactive scope boundaries.
