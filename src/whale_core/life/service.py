"""Compose existing exploration/outbox policies for the unified API host."""

from __future__ import annotations
import hashlib
import time
from datetime import datetime, timezone, time as clock
from urllib.parse import urlsplit
from dataclasses import asdict
from whale_core.life.discovery import DiscoveryPlanner, DiscoveredContent
from whale_core.life.exploration import ExplorationManager, InterestTracker
from whale_core.life.initiative import ProactiveOutbox, ProactivePrefs

DEFAULT_LIFE = {
    "paused": True,
    "daily_limit": 2,
    "quiet_start": "22:00",
    "quiet_end": "07:00",
    "allowed_channels": ["hud"],
    "allow_calls": False,
    "allowed_hosts": [],
    "interval_s": 21600,
}


def settings(store):
    return {**DEFAULT_LIFE, **(store.get("settings:life") or {})}


def validate_settings(changes):
    if set(changes) - set(DEFAULT_LIFE):
        raise ValueError("unknown life setting")
    data = {**DEFAULT_LIFE, **changes}
    if not isinstance(data["paused"], bool) or not isinstance(
        data["allow_calls"], bool
    ):
        raise ValueError("pause/call settings must be booleans")
    if (
        isinstance(data["daily_limit"], bool)
        or not isinstance(data["daily_limit"], int)
        or not 0 <= data["daily_limit"] <= 100
    ):
        raise ValueError("daily limit must be 0..100")
    if (
        not isinstance(data["interval_s"], int)
        or not 300 <= data["interval_s"] <= 604800
    ):
        raise ValueError("exploration interval must be 300..604800 seconds")
    if (
        not isinstance(data["allowed_hosts"], list)
        or len(data["allowed_hosts"]) > 50
        or any(
            not isinstance(h, str) or not h or len(h) > 253 or "/" in h or ":" in h
            for h in data["allowed_hosts"]
        )
    ):
        raise ValueError("expected hostname allowlist")
    if not isinstance(data["allowed_channels"], list):
        raise ValueError("expected allowed channels")
    prefs_from(data)
    return data


def prefs_from(data):
    return ProactivePrefs(
        enabled=not data["paused"],
        daily_limit=data["daily_limit"],
        allowed_channels=frozenset(data["allowed_channels"]),
        allow_calls=data["allow_calls"],
        quiet_start=clock.fromisoformat(data["quiet_start"]),
        quiet_end=clock.fromisoformat(data["quiet_end"]),
    )


def explore_once(bundle, cancel):
    store = bundle.store
    config = settings(store)
    if config["paused"]:
        raise ValueError("initiative paused")
    manager = ExplorationManager(
        store,
        planner=DiscoveryPlanner(allowed_hosts=frozenset(config["allowed_hosts"])),
        base_interval_s=config["interval_s"],
    )
    last = store.get("life:explore:last_at")
    if (
        last
        and (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
        < config["interval_s"] * 2 ** manager.backoff_level()
    ):
        return {"status": "deferred", "drafts": []}
    executor = bundle.registry.executor("web_search")
    if executor is None:
        raise ValueError("search disabled")
    items = []
    for interest in InterestTracker(store).topics()[:5]:
        if cancel.is_set():
            break
        for hit in executor.provider.search(interest.topic, limit=3):
            if urlsplit(hit.url).hostname not in config["allowed_hosts"]:
                continue
            inspected = False
            fetch = bundle.tools.get("fetch_url")
            if fetch:
                try:
                    inspected = bool(fetch.handler({"url": hit.url}))
                except Exception:
                    pass
            items.append(
                DiscoveredContent(
                    item_id=hashlib.sha256(hit.url.encode()).hexdigest()[:20],
                    title=hit.title,
                    url=hit.url,
                    summary=hit.snippet,
                    source_name=hit.source,
                    inspected=inspected,
                )
            )
    drafts = manager.maybe_explore(items)
    return {"status": "explored", "drafts": [d.as_dict() for d in drafts]}


def dispatch_once(bundle, draft_id):
    dispatcher = getattr(bundle, "outbox_dispatcher", None)
    if dispatcher is not None and draft_id.startswith("qq-reply:"):
        return dispatcher(draft_id)
    config = settings(bundle.store)
    outbox = ProactiveOutbox(bundle.store)
    prefs = prefs_from(config)
    draft = outbox.get(draft_id)
    if draft is None:
        raise KeyError(draft_id)
    bridge = bundle.registry.provider_of("channel_bridge")
    if bridge is None:
        raise ValueError("channel bridge disabled")
    target_config = bundle.manager.runtime_config("channel_bridge")
    target = target_config.get("target", "")
    if not target:
        raise ValueError("fixed owner target not configured")

    class Port:
        def send(self, d):
            value = bridge.send(d.channel, target, d.text, idempotency_key=d.draft_id)
            return value.get("accepted") is True

    # Only JobService calls this function after its persisted human approval.
    bundle.store.put(
        "life:decision:" + draft_id, {"decision": "approve", "at": time.time()}
    )
    verdict = outbox.dispatch(
        draft_id,
        prefs,
        now=datetime.now(timezone.utc),
        port=Port(),
        explicitly_approved=True,
    )
    return asdict(verdict)
