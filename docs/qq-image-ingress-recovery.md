# QQ image ingress and host regressions

The image adapter previously resolved QQ relative download paths only when the SSE message arrived. If download rkeys were expired at that moment, it saved empty URL candidates and discarded the original file token/path. Later key refreshes could not recover that attachment.

The adapter now retains `original_url`, `file_token`, and `chat_type`. The workflow resolves candidates at download time using the current QQ keys. If necessary, it invokes the existing WeQ rkey refresh once per event and retries source resolution. On-demand full-mode installations use the existing window wrapper for this read/refresh operation; it does not send a message or introduce another background loop.

The original image validator still handles local paths, downloads and image validation. File-token-only paths percent-encode the token, and current rkeys replace old QQ download query values rather than being appended twice. QQ credentials are not appended to unrelated CDN origins. Existing single-image artifacts can be reused without collapsing multiple current attachments into one.

The same review found three host assembly defects:

- Video pending registration used a singular variable name while the poller consumed the plural queue; pending video tasks were silently lost.
- A discovered bot UID was not copied back into the main/heavy companions after automatic account discovery.
- Completion DND was checked after audio/image/file sends; it now applies before any completion delivery.

These are fixed without restoring discontinued promise-search decisions or mandatory tone/vision claims. Actual image bytes, not a prompt asserting image capability, establish vision availability.

Regression tests cover empty initial keys followed by refresh, source preservation, token encoding, absolute/relative download URLs, single/multiple image handling, refresh authentication and window behavior, video registration, bot identity assembly, and media completion DND. The tests use synthetic data and fake ports. Live diagnosis additionally verified one affected image through the existing QQ metadata/download/validator chain and the configured model, without sending a QQ reply.
