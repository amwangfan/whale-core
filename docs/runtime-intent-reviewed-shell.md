# Runtime intent and reviewed Shell fixes

This change removes the discontinued promise-to-search fallback instead of replacing it with another heuristic. The intent model alone chooses tools: slow intent waits until the existing timeout; a timeout leaves only the first reply. `reply_promises_followup` remains because the glasses legacy response merge still uses it. Dead grace/environment switches and fallback-only tests are removed.

Shell results distinguish useful output from command exit success. A completed nonzero command with nonblank stdout can supply a usable result; its original status and exit code remain at the beginning of the receipt. Empty/stderr-only failures and timeout/denied/error/output-limit results remain failures. No executor isolation or command policy is changed.

For authorized non-owner conversations, the existing review decision is recorded in the task journal and passed through the existing Shell authorization path. Review approval is host state, not a model-plan field. It authorizes only that task's Shell capability and does not make the member an owner. Explicit existing leases still use their normal atomic consumption. Plugin policy, personal-tier opt-in and OS sandbox remain in effect.

Regression tests cover both synchronous and parallel reviewed tasks, denied/unauthorized requests, forged model flags, owner execution, real lease consumption, and non-Shell scope isolation. Tests use fake models and executors; no real Shell command or message is sent as part of verification.
