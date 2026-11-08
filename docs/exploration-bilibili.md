# Exploration, Bilibili and the two diaries

The existing exploration worker continues to own scheduling, quiet hours,
quotas, backoff and outbox policy. `WorkflowsService.explore` now runs both
its general web search and a read-only Bilibili adapter through the registered
`web.search` and `web.fetch` capabilities.

## Bilibili boundary

- Search is constrained to public `bilibili.com/video/BV...` pages.
- A result becomes `inspected=True` only after the public page text is fetched.
- The current adapter reads page/description text; it does not download or play
  the video stream, fetch login-only subtitles, use cookies, scrape comments,
  or post comments.
- Therefore the diary says "Bilibili page/description read; video not played".
  It must not claim that Whale watched or finished the video.

## Two diaries

- `life:explore:diary:{day}` is the factual source diary: date, title, source,
  URL, inspection method and the short text actually fetched.
- `life:explore:reflection:{day}` is the interaction diary: recommendation
  candidates and the owner's explicit accept/reject feedback. It is not mixed
  into the factual source record.
- `GET /api/v1/life` returns both `diary` and `reflection_diary`, including
  the per-item source details.

Diary dates use the configured workflow timezone (default Asia/Shanghai).
Owner-private chat can recall yesterday/today's entries. Group chats do not
receive this owner-only exploration context.

## Recommendation confirmation and delivery

A model response that explicitly offers a pending candidate binds that
candidate to the current conversation for 24 hours. Only a short, unambiguous
affirmative reply (for example "要") resolves the offer. A rejection closes
the draft and adjusts the topic feedback; unrelated replies do nothing.
Accepted offers are attempted once; uncertain delivery is marked unknown and
is not automatically replayed.

When QQ is online, the existing WeQ owner-DM route remains preferred. When QQ
is offline, Core submits `chat.message` notifications to active paired devices
through `POST /api/v1/devices/{device_id}/notify`. Configure `WHALE_API`
(default `http://127.0.0.1:8080`) and `WHALE_API_TOKEN` for immediate delivery
through the running Core notification gateway. If that bridge is unavailable,
Core writes to the shared SQLite notification queue for device sync/reconnect.
A successful gateway acceptance is not a claim that the device displayed or
the owner read the notification.

The receiving phone/glasses client still needs to implement the V2
`notification.event` WSS downlink and receipt/sync behavior. This PR does not
change whale-mobile or whale-io.

If an HTTP request to the live gateway times out or the response is
ambiguous, Core reports an unknown result and does **not** create a second
notification with a new event ID in the SQLite fallback queue.
