"""Persistent user-invoked workflows and evidence-backed exploration.

All tool execution uses ConversationOrchestrator; this service never executes a
handler or sends a message directly. A pending workflow requires an explicit
per-task decision/resume followed by resume_workflow. Exploration produces
outbox drafts only, never unsolicited outbound sends.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, time, timezone
from pathlib import Path
import hashlib
import re
import threading
from uuid import uuid4
from weakref import WeakKeyDictionary
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from whale_core.core.runtime import ConversationOrchestrator
from whale_core.life.discovery import DiscoveredContent, DiscoveryPlanner
from whale_core.life.exploration import ExplorationManager
from whale_core.skills import parse_skill, SkillError, _render_template, _first_url

DEFAULTS = {"paused": False, "daily_limit": 10, "quiet_start": "22:00",
            "quiet_end": "07:00", "timezone": "Asia/Shanghai",
            "schedule_enabled": False, "interval_s": 3600}
_NAME = re.compile(r"[A-Za-z0-9_-]{1,80}")
_URLS = re.compile(r"https?://[^\s\"'<>\]\)（）]+")


_SERVICE_LOCK = threading.Lock()
_SERVICE_STATES: WeakKeyDictionary = WeakKeyDictionary()


class WorkflowsService:
    def __init__(self, orchestrator: ConversationOrchestrator, *, skill_dir: Path | None = None):
        self.runtime = orchestrator
        self.store = orchestrator.store
        self.skill_dir = (skill_dir or Path(__file__).resolve().parent.parent / "workflow_skills").resolve()
        with _SERVICE_LOCK:
            state = _SERVICE_STATES.get(self.store)
            if state is None:
                state = (threading.RLock(), threading.Lock())
                _SERVICE_STATES[self.store] = state
            self._lock, self._execution_lock = state

    def get_settings(self) -> dict:
        raw = self.store.get("workflows:settings") or {}
        return {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}

    def update_settings(self, patch: dict) -> dict:
        if not isinstance(patch, dict) or set(patch) - set(DEFAULTS):
            raise ValueError("unknown workflow setting")
        with self._lock:
            settings = {**self.get_settings(), **patch}
            if not isinstance(settings["paused"], bool) or not isinstance(settings["schedule_enabled"], bool):
                raise ValueError("paused and schedule_enabled must be boolean")
            interval = settings["interval_s"]
            if isinstance(interval, bool) or not isinstance(interval, int) or not 60 <= interval <= 604800:
                raise ValueError("interval_s must be an integer in [60,604800]")
            limit = settings["daily_limit"]
            if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 1000:
                raise ValueError("daily_limit must be an integer in [0,1000]")
            for field in ("quiet_start", "quiet_end"):
                if not isinstance(settings[field], str) or not re.fullmatch(r"\d{2}:\d{2}", settings[field]):
                    raise ValueError(field + " must use HH:MM")
                time.fromisoformat(settings[field])
            ZoneInfo(settings["timezone"])
            self.store.put("workflows:settings", settings)
            return settings

    def _skill(self, name: str):
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("invalid skill name")
        path = (self.skill_dir / (name + ".md")).resolve()
        if path.parent != self.skill_dir or not path.is_file():
            raise KeyError(name)
        if path.stat().st_size > 64 * 1024:
            raise ValueError("skill is too large")
        skill = parse_skill(path.read_text(encoding="utf-8"))
        if skill.name != name or len(skill.steps) > 16:
            raise ValueError("skill identity or step limit invalid")
        return skill

    def list_skills(self) -> list[dict]:
        if not self.skill_dir.is_dir():
            return []
        result = []
        for path in sorted(self.skill_dir.glob("*.md")):
            try:
                skill = self._skill(path.stem)
            except (KeyError, ValueError, OSError):
                continue
            result.append({"name": skill.name, "description": skill.description,
                           "steps": [{"name": s.name, "tool": s.tool,
                                      "available": self.runtime.tools.get(s.tool) is not None}
                                     for s in skill.steps]})
        return result

    def run_skill(self, name: str, input_text: str, *, conversation_id: str = "workflow") -> dict:
        skill = self._skill(name)
        if self.get_settings()["paused"]:
            raise ValueError("workflows are paused")
        if not isinstance(input_text, str) or len(input_text) > 8000:
            raise ValueError("input_text must contain at most 8000 characters")
        workflow_id = uuid4().hex
        with self._lock:
            record = {"workflow_id": workflow_id, "name": name, "input": input_text,
                      "conversation_id": conversation_id[:160], "next_step": 0,
                      "status": "running", "step_outputs": {}, "outputs": {}, "tasks": [],
                      "steps": [asdict(step) for step in skill.steps]}
            self.store.put(f"workflow:{workflow_id}", record)
        return self.resume_workflow(workflow_id)

    @staticmethod
    def _output(task: dict) -> str:
        for receipt in reversed(task.get("receipts", [])):
            if receipt.startswith(("tool:", "plugin:")) and ":ok:" in receipt:
                return receipt.split(":ok:", 1)[1]
        return ""

    def _view(self, record: dict) -> dict:
        return {k: record[k] for k in ("workflow_id", "name", "status", "next_step", "outputs", "tasks")}

    def resume_workflow(self, workflow_id: str) -> dict:
        if not _NAME.fullmatch(workflow_id):
            raise ValueError("invalid workflow id")
        with self._execution_lock:
            record = self.store.get(f"workflow:{workflow_id}")
            if not isinstance(record, dict):
                raise KeyError(workflow_id)
            if record["status"] in {"succeeded", "failed", "denied", "cancelled", "unknown"}:
                return self._view(record)
            if self.get_settings()["paused"]:
                raise ValueError("workflows are paused")
            if record.get("pending_task"):
                task = self.store.get(f"task:{record['pending_task']}") or {}
                if task.get("status") != "answered":
                    record["status"] = task.get("status", "unknown")
                    self.store.put(f"workflow:{workflow_id}", record)
                    return self._view(record)
                step = record["steps"][record["next_step"]]
                output = self._output(task)
                record["step_outputs"][step["name"]] = output
                if step["save_as"]:
                    record["outputs"][step["save_as"]] = output
                record["next_step"] += 1
                record.pop("pending_task", None)
            while record["next_step"] < len(record["steps"]):
                if self.get_settings()["paused"]:
                    record["status"] = "paused"
                    break
                step = record["steps"][record["next_step"]]
                try:
                    args = {key: _render_template(value, input_text=record["input"],
                                                  step_outputs=record["step_outputs"])
                            for key, value in step["args"].items()}
                    # Persist intent-to-dispatch before the external call. On a
                    # crash, resume inspects this task instead of replaying it.
                    task_id = uuid4().hex
                    record["pending_task"] = task_id
                    record["tasks"].append(task_id)
                    self.store.put(f"workflow:{workflow_id}", record)
                    result = self.runtime.execute_tool(step["tool"], args,
                              conversation_id=record["conversation_id"], source="api", timeout_s=60,
                              task_id=task_id)
                except (SkillError, KeyError, ValueError):
                    record["status"] = "failed"
                    break
                task = self.store.get(f"task:{result.task_id}") or {}
                if result.status != "answered":
                    record["status"] = result.status
                    if result.status == "pending_approval":
                        record["pending_task"] = result.task_id
                    break
                output = self._output(task)
                record["step_outputs"][step["name"]] = output
                if step["save_as"]:
                    record["outputs"][step["save_as"]] = output
                record["next_step"] += 1
                record.pop("pending_task", None)
                # Persist after each completed step; never replay completed effects.
                self.store.put(f"workflow:{workflow_id}", record)
            else:
                record["status"] = "succeeded"
            self.store.put(f"workflow:{workflow_id}", record)
            return self._view(record)

    def explore(self, topics: list[str] | None = None, *, stop_event: threading.Event | None = None) -> dict:
        with self._execution_lock:
            settings = self.get_settings()
            if settings["paused"]:
                return {"status": "paused", "drafts": [], "tasks": []}
            now = datetime.now(timezone.utc)
            local = now.astimezone(ZoneInfo(settings["timezone"]))
            start, end = (time.fromisoformat(settings[k]) for k in ("quiet_start", "quiet_end"))
            t = local.time().replace(tzinfo=None)
            quiet = ((start <= t < end) if start < end else (t >= start or t < end)) if start != end else False
            if quiet:
                return {"status": "quiet_hours", "drafts": [], "tasks": []}
            quota_key = f"workflows:explore:quota:{local.date().isoformat()}"
            used = int(self.store.get(quota_key) or 0)
            if used >= settings["daily_limit"]:
                return {"status": "quota_exhausted", "drafts": [], "tasks": []}
            from whale_core.life.exploration import InterestTracker
            tracker = InterestTracker(self.store)
            if topics is not None:
                if not isinstance(topics, list) or len(topics) > 8 or any(not isinstance(v, str) or not v.strip() or len(v) > 120 for v in topics):
                    raise ValueError("topics must be up to eight non-empty short strings")
                for topic in topics:
                    tracker.add(topic)
            selected = tracker.as_interest_list()[:3]
            if not selected:
                return {"status": "no_interests", "drafts": [], "tasks": []}
            search = next(iter(self.runtime.tools.for_capability("web.search")), None)
            fetch = next(iter(self.runtime.tools.for_capability("web.fetch")), None)
            if search is None or fetch is None:
                return {"status": "unavailable", "drafts": [], "tasks": []}
            from whale_core.life.exploration import LAST_AT_KEY, BACKOFF_KEY
            last = self.store.get(LAST_AT_KEY)
            if isinstance(last, str):
                try:
                    at = datetime.fromisoformat(last)
                    if at.tzinfo is None:
                        at = at.replace(tzinfo=timezone.utc)
                    backoff = max(0, min(4, int(self.store.get(BACKOFF_KEY) or 0)))
                    if (now - at).total_seconds() < 21600 * (2 ** backoff):
                        return {"status": "backoff", "drafts": [], "tasks": []}
                except (ValueError, TypeError):
                    pass
            # Reserve before the request, so repeated failures cannot bypass budget.
            self.store.put(quota_key, used + 1)
            items, task_ids = [], []
            for topic in selected:
                if self.get_settings()["paused"] or (stop_event is not None and stop_event.is_set()):
                    break
                result = self.runtime.execute_tool(search.name, {"query": topic, "limit": 3},
                                                   conversation_id="exploration", source="api", timeout_s=30)
                task_ids.append(result.task_id)
                if result.status != "answered":
                    continue
                output = self._output(self.store.get(f"task:{result.task_id}") or {})
                urls = list(dict.fromkeys(u.rstrip(".,;:!?。，；：！？") for u in _URLS.findall(output)))[:2]
                for url in urls:
                    if self.get_settings()["paused"] or (stop_event is not None and stop_event.is_set()):
                        break
                    if urlsplit(url).scheme != "https":
                        continue
                    read = self.runtime.execute_tool(fetch.name, {"url": url},
                                                     conversation_id="exploration", source="api", timeout_s=30)
                    task_ids.append(read.task_id)
                    if read.status != "answered":
                        continue
                    page = self._output(self.store.get(f"task:{read.task_id}") or {})
                    if not page.strip():
                        continue
                    host = urlsplit(url).hostname or ""
                    items.append(DiscoveredContent(hashlib.sha256(url.encode()).hexdigest()[:24],
                        title=page.splitlines()[0][:80] or host, url=url, source_name=host,
                        summary=page[:500], inspected=True))
            hosts = frozenset(urlsplit(item.url).hostname or "" for item in items)
            manager = ExplorationManager(self.store, planner=DiscoveryPlanner(allowed_hosts=hosts))
            drafts = manager.maybe_explore(items, now=now)
            return {"status": "completed", "sources_read": len(items),
                    "drafts": [d.as_dict() for d in drafts], "tasks": task_ids,
                    "execution_status": "not_dispatched", "verification": "source_fetched_not_fact_checked"}
