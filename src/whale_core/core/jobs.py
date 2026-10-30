"""Single-host durable jobs. Approval authorizes exactly the persisted request.

Bounded workers/queue; running requests are never retried on restart. Cancellation
is cooperative for HTTP calls (provider timeouts still apply); external side effects
with an ambiguous outcome are 'unknown', not falsely cancelled/succeeded.
"""

from __future__ import annotations
import base64
import hashlib
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

TERMINAL = frozenset({"succeeded", "failed", "denied", "cancelled", "unknown"})
KINDS = frozenset(
    {
        "chat",
        "image",
        "tts",
        "asr",
        "video",
        "search",
        "fetch",
        "shell",
        "skill",
        "explore",
        "outbox",
        "plugin",
        "channel",
    }
)


class JobService:
    def __init__(
        self, bundle, artifacts, *, on_event=None, max_workers=2, max_pending=20
    ):
        self.bundle, self.store, self.artifacts = bundle, bundle.store, artifacts
        self.on_event = on_event or (lambda *_: None)
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="whale-job"
        )
        self.max_pending = max_pending
        self.closed = False
        self.cancel_flags = {}
        for job in self.list(limit=None):
            if job["status"] in ("queued", "running", "cancelling"):
                job.update(
                    status="unknown",
                    error="process_restarted; inspect external outcome before submitting again",
                )
                self._save(job)

    def _save(self, job):
        job["updated_at"] = time.time()
        self.store.put("job:" + job["task_id"], job)
        self.on_event("task:" + job["status"], job["task_id"])

    def list(self, limit=200):
        return sorted(
            [self.store.get(k) for k in self.store.keys() if k.startswith("job:job-")],
            key=lambda j: j["at"],
            reverse=True,
        )[:limit]

    def get(self, jid):
        job = self.store.get("job:" + jid)
        if not isinstance(job, dict):
            raise KeyError(jid)
        return job

    def submit(self, kind, args, *, request_id=""):
        if kind not in KINDS or not isinstance(args, dict):
            raise ValueError("unknown task kind or invalid arguments")
        raw = json.dumps(
            {"kind": kind, "args": args}, sort_keys=True, ensure_ascii=False
        )
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self.lock:
            if self.closed:
                raise ValueError("service stopping")
            if request_id:
                if not isinstance(request_id, str) or len(request_id) > 100:
                    raise ValueError("invalid request id")
                existing = self.store.get("job:request:" + request_id)
                if existing:
                    if existing["digest"] != digest:
                        raise ValueError(
                            "request id already used with different arguments"
                        )
                    return self.get(existing["id"])
            if (
                sum(j["status"] not in TERMINAL for j in self.list(limit=None))
                >= self.max_pending
            ):
                raise ValueError("task queue full")
            # Shell and outbound dispatch require one persisted explicit decision.
            hold = kind in ("shell", "outbox", "plugin", "channel")
            jid = "job-" + uuid.uuid4().hex
            job = {
                "task_id": jid,
                "kind": kind,
                "at": time.time(),
                "goal": str(
                    args.get("text") or args.get("prompt") or args.get("query") or kind
                )[:500],
                "status": "pending_approval" if hold else "queued",
                "args": args if kind != "asr" else {"audio": "[transient]"},
                "request_digest": digest,
                "capabilities": [kind],
                "receipts": [],
                "result": {},
                "decision": "",
            }
            self._save(job)
            if request_id:
                self.store.put(
                    "job:request:" + request_id, {"digest": digest, "id": jid}
                )
            if not hold:
                self._queue(jid, args)
            return job

    def _queue(self, jid, args):
        self.cancel_flags[jid] = threading.Event()
        self.pool.submit(self._run, jid, args)

    def decide(self, jid, decision):
        if decision not in ("approve", "reject"):
            raise ValueError("decision must be approve|reject")
        with self.lock:
            job = self.get(jid)
            if job.get("decision") == decision:
                return job
            if job["status"] != "pending_approval" or job.get("decision"):
                raise ValueError("task already decided or attempted")
            job["decision"] = decision
            job["status"] = "queued" if decision == "approve" else "denied"
            self._save(job)
            if decision == "approve":
                self._queue(jid, job["args"])
            return job

    def refresh_video(self, jid):
        """Explicit reconciliation after restart; never submits a new generation."""
        with self.lock:
            job = self.get(jid)
            if (
                job["kind"] != "video"
                or not job.get("external_task_id")
                or job["status"] not in ("unknown", "failed")
            ):
                raise ValueError("only interrupted video jobs can be reconciled")
            from whale_core.plugins.media_video import VideoTaskManager

            manager = VideoTaskManager(self.store, self._provider("media_video"))
            task = manager.poll(job["external_task_id"])
            result = {"video_task": task.as_dict()}
            if task.status == "succeeded":
                result["artifacts"] = [self.artifacts.capture(task.url)]
                job["status"] = "succeeded"
            elif task.status in ("failed", "cancelled"):
                job["status"] = task.status
            job["result"] = result
            self._save(job)
            return job

    def cancel(self, jid):
        with self.lock:
            job = self.get(jid)
            if (
                job["status"] == "unknown"
                and job["kind"] == "video"
                and job.get("external_task_id")
            ):
                from whale_core.plugins.media_video import VideoTaskManager

                manager = VideoTaskManager(self.store, self._provider("media_video"))
                if manager.cancel(job["external_task_id"]):
                    job["status"] = "cancelled"
                    self._save(job)
                return job
            if job["status"] in TERMINAL:
                return job
            flag = self.cancel_flags.get(jid)
            if flag:
                flag.set()
            job["status"] = "cancelling" if job["status"] == "running" else "cancelled"
            self._save(job)
            return job

    def _provider(self, pid):
        self.bundle.manager.refresh()
        provider = self.bundle.registry.provider_of(pid)
        if provider is None:
            raise ValueError("capability disabled or not configured: " + pid)
        return provider

    def _run(self, jid, args):
        flag = self.cancel_flags[jid]
        with self.lock:
            job = self.get(jid)
            if job["status"] == "cancelled":
                self.cancel_flags.pop(jid, None)
                return
            job["status"] = "running"
            self._save(job)
        try:
            result = self._execute(job, args, flag)
            with self.lock:
                job = self.get(jid)
                if flag.is_set():
                    job.update(
                        status="unknown"
                        if job["kind"]
                        in ("shell", "outbox", "video", "plugin", "channel")
                        else "cancelled",
                        result=result,
                    )
                else:
                    result_status = result.get("status")
                    mapped = (
                        result_status
                        if result_status in ("unknown", "failed", "denied", "cancelled")
                        else "failed"
                        if job["kind"] in ("outbox", "channel")
                        and result_status != "accepted"
                        else "succeeded"
                    )
                    job.update(status=mapped, result=result)
                job["receipts"].append("execution:" + job["status"])
                self._save(job)
        except Exception as exc:
            with self.lock:
                job = self.get(jid)
                # Keep raw exception text (possibly credentials) out of persistence.
                kind = str(getattr(exc, "kind", type(exc).__name__))[:80]
                job.update(
                    status="unknown"
                    if job["kind"] in ("video", "outbox", "plugin", "channel")
                    and kind in ("network", "TimeoutError")
                    else "failed",
                    error=kind,
                )
                job["receipts"].append("execution:" + job["status"] + ":" + kind)
                self._save(job)
        finally:
            with self.lock:
                self.cancel_flags.pop(jid, None)

    def _execute(self, job, args, flag):
        kind = job["kind"]
        if kind in ("image", "video", "tts", "asr"):
            from whale_core.core.budget import reserve

            reserve(self.store, "media_jobs")
        if flag.is_set():
            return {}
        if kind == "chat":
            result = self.bundle.orchestrator.handle_message(
                str(args.get("conversation_id") or "console"),
                str(args.get("text") or ""),
                source="api",
                cancel=flag,
            )
            return {
                "reply": result.reply,
                "status": result.status,
                "receipts": list(result.receipts),
            }
        if kind in ("search", "fetch"):
            tool = self.bundle.tools.get(
                "search_web" if kind == "search" else "fetch_url"
            )
            if not tool:
                raise ValueError("tool disabled")
            return {"text": tool.handler(args)[:16000]}
        if kind == "image":
            result = self._provider("media_image").generate(
                str(args.get("prompt") or ""),
                size=str(args.get("size") or "1024x1024"),
                n=int(args.get("n", 1)),
            )
            return {"artifacts": [self.artifacts.capture(ref) for ref in result.images]}
        if kind == "tts":
            data = self._provider("speech_tts").synthesize(
                str(args.get("text") or ""),
                voice=str(args.get("voice") or ""),
                rate=float(args.get("rate", 1)),
            )
            return {"artifacts": [self.artifacts.save(data, "audio/mpeg")]}
        if kind == "asr":
            raw = args.get("audio", "")
            if not isinstance(raw, str) or len(raw) > 34 * 1024 * 1024:
                raise ValueError("audio too large")
            data = base64.b64decode(raw, validate=True)
            return {
                "text": self._provider("speech_asr").transcribe(
                    data, lang=str(args.get("lang") or "zh")
                )
            }
        if kind == "video":
            from whale_core.plugins.media_video import VideoTaskManager

            manager = VideoTaskManager(self.store, self._provider("media_video"))
            task = manager.submit(
                str(args.get("prompt") or ""),
                duration_s=int(args.get("duration_s", 5)),
                size=str(args.get("size") or "1280x720"),
            )
            job["external_task_id"] = task.task_id
            self._save(job)
            deadline = time.monotonic() + 600
            while task.status in ("pending", "running"):
                if flag.wait(float(getattr(manager.provider, "poll_interval_s", 2))):
                    manager.cancel(task.task_id)
                    return {"video_task": manager._load(task.task_id).as_dict()}
                if time.monotonic() > deadline:
                    raise TimeoutError()
                task = manager.poll(task.task_id)
            if task.status != "succeeded":
                raise RuntimeError("video failed")
            return {
                "video_task": task.as_dict(),
                "artifacts": [self.artifacts.capture(task.url)],
            }
        if kind == "shell":
            from whale_core.cloud.container_executor import ContainerShellExecutor

            config = self.store.get("settings:shell") or {}
            executor = ContainerShellExecutor(**config)
            result = executor.run(
                str(args.get("command") or ""), is_owner=True, cancel=flag
            )
            if result["status"] != "success":
                result["status"] = (
                    "failed" if result["status"] != "cancelled" else "cancelled"
                )
            return result
        if kind == "skill":
            from whale_core.skills import parse_skill, run_skill

            text = str(args.get("definition") or "")
            if len(text) > 20000:
                raise ValueError("skill too large")
            result = run_skill(
                parse_skill(text),
                self.bundle.tools,
                input_text=str(args.get("input") or ""),
                cancel=flag,
            )
            if result.status != "succeeded":
                raise RuntimeError(result.status)
            return asdict(result)
        if kind == "explore":
            from whale_core.life.service import explore_once

            return explore_once(self.bundle, flag)
        if kind == "plugin":
            from whale_core.plugins.gateway import PluginGateway
            from whale_core.plugins.policy import PluginActionPolicy
            from whale_core.plugins.intents import PluginAction

            gateway = PluginGateway(
                self.bundle.registry,
                PluginActionPolicy(self.bundle.registry),
                owner_verifier=lambda _: True,
                approval_verifier=lambda _: job.get("decision") == "approve",
            )
            pid, action = (
                str(args.get("plugin_id") or ""),
                str(args.get("action") or ""),
            )
            if not isinstance(args.get("args", {}), dict):
                raise ValueError("plugin args must be an object")
            receipt = gateway.dispatch(
                PluginAction(
                    plugin_id=pid,
                    action=action,
                    target=str(args.get("target") or ""),
                    args=args.get("args", {}),
                ).to_intent()
            )
            ok = receipt.startswith(f"plugin:{pid}:{action}:ok:")
            return {
                "status": "succeeded"
                if ok
                else "denied"
                if receipt.startswith("plugin-denied:")
                else "unknown",
                "receipt": receipt,
            }
        if kind == "channel":
            from whale_core.life.initiative import ProactiveDraft, ProactiveOutbox
            from whale_core.life.service import dispatch_once

            draft = ProactiveDraft(
                job["task_id"],
                str(args.get("text") or ""),
                "owner explicitly requested channel delivery",
                ("api:" + job["task_id"],),
                channel=str(args.get("channel") or "hud"),
            )
            ProactiveOutbox(self.store).propose(draft)
            self.store.put(
                "life:decision:" + draft.draft_id,
                {"decision": "approve", "at": time.time()},
            )
            return dispatch_once(self.bundle, draft.draft_id)
        if kind == "outbox":
            from whale_core.life.service import dispatch_once

            return dispatch_once(self.bundle, str(args.get("draft_id") or ""))
        raise ValueError("unsupported task")

    def close(self):
        with self.lock:
            self.closed = True
            for flag in self.cancel_flags.values():
                flag.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
