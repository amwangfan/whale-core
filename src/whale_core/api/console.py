"""Extended console routes. All calls pass the server's bearer gate first."""

from __future__ import annotations
import io
import json
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import unquote


class InputError(ValueError):
    pass


def dispatch(handler, method, parts, query):
    """Return False for routes owned by the original API."""
    ctx = handler.ctx

    def send(value, status=200):
        handler._send(
            status, json.dumps({"result": value}, ensure_ascii=False).encode()
        )

    def body():
        data = handler._body_json()
        if data is None:
            raise InputError("Expected a JSON object within the request size limit")
        return data

    head = parts[0]
    if head == "diagnostics" and method == "GET" and len(parts) == 1:
        from whale_core.core.diagnostics import provider_health_report

        report = {
            "version": ctx.version,
            "profile": ctx.profile,
            "plugins": ctx.manager.status() if ctx.manager else [],
            "providers": provider_health_report(ctx.store),
            "checks": [],
            "runtime": bool(ctx.orchestrator),
        }
        if ctx.manager:
            report["checks"] = [
                ctx.manager.healthcheck(p["plugin_id"]) for p in ctx.manager.status()
            ]
        send(report)
    elif head == "settings" and len(parts) == 1:
        from whale_core.life.service import settings, validate_settings
        from whale_core.ui.persona_cards import BUILTIN_CARDS, active_card, switch_card
        from whale_core.core.budget import settings as budget_settings

        if method == "GET":
            send(
                {
                    "profile": ctx.profile,
                    "life": settings(ctx.store),
                    "persona": active_card(ctx.store).card_id,
                    "personas": [
                        {
                            "card_id": c.card_id,
                            "title": c.title,
                            "description": c.description,
                        }
                        for c in BUILTIN_CARDS
                    ],
                    "shell": ctx.store.get("settings:shell")
                    or {"tier": "managed", "timeout_s": 30, "output_chars": 4000},
                    "bindings": ctx.store.get("settings:bindings") or [],
                    "budget": budget_settings(ctx.store),
                    "intent_model_enabled": bool(
                        ctx.orchestrator
                        and ctx.orchestrator.intent_model
                        and ctx.orchestrator.intent_model.enable_llm
                    ),
                }
            )
        elif method == "POST":
            value = body()
            if set(value) - {
                "life",
                "persona",
                "shell",
                "bindings",
                "intent_model_enabled",
                "budget",
            }:
                raise InputError("Unknown setting")
            # Validate the entire change before applying any part.
            validated = {}
            if "budget" in value:
                from whale_core.core.budget import validate

                validated["budget"] = validate(value["budget"])
            if "life" in value:
                if not isinstance(value["life"], dict):
                    raise InputError("life must be an object")
                validated["life"] = validate_settings(
                    {**settings(ctx.store), **value["life"]}
                )
            if "bindings" in value:
                bindings = value["bindings"]
                if (
                    not isinstance(bindings, list)
                    or len(bindings) > 20
                    or any(
                        not isinstance(b, dict)
                        or set(b) != {"channel", "account"}
                        or any(
                            not isinstance(x, str) or not x or len(x) > 100
                            for x in b.values()
                        )
                        for b in bindings
                    )
                ):
                    raise InputError("bindings must be explicit channel/account pairs")
                validated["bindings"] = bindings
            if "shell" in value:
                shell = value["shell"]
                if not isinstance(shell, dict) or set(shell) - {
                    "tier",
                    "timeout_s",
                    "output_chars",
                }:
                    raise InputError("shell accepts tier, timeout_s, output_chars")
                from whale_core.cloud.container_executor import ContainerShellExecutor

                ContainerShellExecutor(**shell)  # validates limits; no execution
                validated["shell"] = shell
            if "intent_model_enabled" in value:
                if not isinstance(value["intent_model_enabled"], bool):
                    raise InputError("intent_model_enabled must be boolean")
            if "persona" in value:
                from whale_core.ui.persona_cards import (
                    PersonaCardLibrary,
                    _load_stored_card,
                )

                card = value["persona"]
                if not isinstance(card, str) or not (
                    PersonaCardLibrary().get(card) or _load_stored_card(ctx.store, card)
                ):
                    raise InputError("Unknown persona")
            for name, data in validated.items():
                ctx.store.put("settings:" + name, data)
            if "persona" in value:
                switch_card(ctx.store, value["persona"])
            if "intent_model_enabled" in value and ctx.orchestrator:
                ctx.store.put(
                    "settings:intent_model_enabled", value["intent_model_enabled"]
                )
                ctx.orchestrator.intent_model.enable_llm = value["intent_model_enabled"]
            ctx.log_event("settings:saved")
            send({"saved": True})
        else:
            return False
    elif head == "conversations" and len(parts) <= 2:
        from whale_core.memory.history import load_history, clear_history

        if len(parts) == 1 and method == "GET":
            send(
                {
                    "conversations": [
                        {
                            "conversation_id": key[13:],
                            "messages": len(load_history(ctx.store, key[13:])),
                        }
                        for key in ctx.store.keys()
                        if key.startswith("chat:history:")
                    ]
                }
            )
        elif len(parts) == 2 and method == "GET":
            send({"history": load_history(ctx.store, unquote(parts[1]))})
        elif len(parts) == 2 and method == "DELETE":
            cid = unquote(parts[1])
            clear_history(ctx.store, cid)
            # Conversation clearing means remove raw chat episodes for this session
            # only when provenance exists; older untagged episodes remain visible.
            from whale_core.memory.store import MemoryKind

            for record in ctx.store.list_by_kind(MemoryKind.EPISODE):
                if record.content.get("conversation_id") == cid:
                    ctx.store.delete(record.key)
            send({"deleted": True})
        else:
            return False
    elif head == "memory" and len(parts) == 1 and method == "GET":
        from whale_core.memory.store import MemoryKind

        offset = max(0, int(query.get("offset", ["0"])[0]))
        limit = min(100, max(1, int(query.get("limit", ["30"])[0])))
        records = [r for k in MemoryKind for r in ctx.store.list_by_kind(k)]
        records.sort(key=lambda r: r.created_at.isoformat(), reverse=True)
        send(
            {
                "total": len(records),
                "hits": [
                    {
                        "key": r.key,
                        "kind": r.kind.value,
                        "summary": str(
                            r.content.get("summary")
                            or r.content.get("persona_name")
                            or ""
                        ),
                        "created_at": r.created_at.isoformat(),
                        "score": 0,
                        "reasons": [],
                        "content": dict(r.content),
                    }
                    for r in records[offset : offset + limit]
                ],
            }
        )
    elif head == "memory" and parts == ["memory", "import"] and method == "POST":
        from whale_core.memory.export import import_jsonl

        value = body()
        raw = value.get("text")
        if not isinstance(raw, str) or len(raw) > 8 * 1024 * 1024:
            raise InputError("Expected NDJSON text up to 8 MiB")
        count, skipped = import_jsonl(ctx.store, io.StringIO(raw))
        ctx.log_event("memory:import", str(count))
        send({"imported": count, "skipped": skipped})
    elif head == "backup" and len(parts) == 1 and method == "POST":
        import tempfile
        from whale_core.memory.backup import create_backup

        if not hasattr(ctx.store, "db"):
            raise InputError("SQLite storage is required")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.sqlite3"
            create_backup(ctx.store, path)
            handler._send(
                200, path.read_bytes(), content_type="application/vnd.sqlite3"
            )
        ctx.log_event("memory:backup")
    elif head == "jobs" and ctx.jobs is not None:
        if len(parts) == 1 and method == "POST":
            value = body()
            with ctx.control_lock:
                send(
                    ctx.jobs.submit(
                        value.get("kind"),
                        value.get("args", {}),
                        request_id=value.get("request_id", ""),
                    ),
                    202,
                )
        elif len(parts) == 1 and method == "GET":
            send({"tasks": ctx.jobs.list()})
        elif len(parts) == 2 and method == "GET":
            send(ctx.jobs.get(parts[1]))
        elif len(parts) == 3 and parts[2] == "cancel" and method == "POST":
            send(ctx.jobs.cancel(parts[1]))
        elif len(parts) == 3 and parts[2] == "refresh" and method == "POST":
            send(ctx.jobs.refresh_video(parts[1]))
        elif len(parts) == 3 and parts[2] == "decision" and method == "POST":
            send(ctx.jobs.decide(parts[1], body().get("decision")))
        else:
            return False
    elif (
        head == "artifacts"
        and ctx.artifacts is not None
        and len(parts) == 2
        and method == "GET"
    ):
        meta, raw = ctx.artifacts.read(parts[1])
        handler._send(200, raw, content_type=meta["media_type"])
    elif (
        head == "voice"
        and parts == ["voice", "session"]
        and method == "POST"
        and ctx.bundle
    ):
        ctx.bundle.manager.refresh()
        bridge = ctx.bundle.registry.provider_of("voice_bridge")
        if bridge is not None:
            if ctx.voice is None:
                try:
                    from whale_core.api.voice import VoiceGateway
                    import websockets
                except ImportError:
                    handler._send(
                        503,
                        b'{"error":{"code":"voice_dependency_missing","message":"Install whale-core[voice] to enable browser audio"}}',
                    )
                    return True
                ctx.voice = VoiceGateway(ctx, host=ctx.voice_host, port=ctx.voice_port)
            send(ctx.voice.ticket(bridge))
        else:
            from whale_core.memory.continuity import build_continuity_context
            import uuid

            http_bridge = ctx.bundle.registry.provider_of("voice_http")
            if http_bridge is None:
                raise InputError("Configure and enable a realtime voice provider")
            send(
                http_bridge.start_session(
                    persona=build_continuity_context(ctx.store).persona_summary,
                    session_id=uuid.uuid4().hex,
                )
            )
    elif head == "transcripts" and method == "POST" and len(parts) == 1:
        from whale_core.memory.continuity import remember_episode
        import uuid

        value = body()
        if (
            value.get("role") not in ("user", "assistant")
            or not isinstance(value.get("text"), str)
            or not isinstance(value.get("session_id"), str)
        ):
            raise InputError("Expected final transcript role, text and session_id")
        if (
            not value["text"].strip()
            or len(value["text"]) > 4000
            or len(value["session_id"]) > 100
        ):
            raise InputError("Invalid transcript length")
        remember_episode(
            ctx.store,
            key="ep:call:" + uuid.uuid4().hex,
            summary=value["text"],
            tags=("call", value["role"], "session:" + value["session_id"]),
            extra={"role": value["role"], "session_id": value["session_id"]},
        )
        send({"saved": True})
    elif (
        head == "channels"
        and parts == ["channels", "inbound"]
        and method == "POST"
        and ctx.jobs
    ):
        from whale_core.life.channels import Incoming, OwnerChannelRegistry

        value = body()
        fields = ("channel", "account", "conversation", "message_id", "text")
        if any(
            not isinstance(value.get(k), str)
            or not value[k]
            or len(value[k]) > (4000 if k == "text" else 160)
            for k in fields
        ):
            raise InputError("Expected channel/account/conversation/message_id/text")
        bindings = {
            (b["channel"], b["account"]): "owner"
            for b in ctx.store.get("settings:bindings") or []
        }
        event = OwnerChannelRegistry(owner_id="owner", bindings=bindings).accept(
            Incoming(**{k: value[k] for k in fields}, transport_authenticated=True)
        )
        if event is None:
            handler._send(
                403,
                b'{"error":{"code":"unpaired","message":"Channel identity not paired"}}',
            )
        else:
            import hashlib

            rid = hashlib.sha256(
                json.dumps([value[k] for k in fields[:4]]).encode()
            ).hexdigest()
            send(
                ctx.jobs.submit(
                    "chat",
                    {
                        "text": event.text,
                        "conversation_id": event.channel + ":" + event.conversation,
                    },
                    request_id=rid,
                ),
                202,
            )
    elif head == "life" and len(parts) == 1 and method == "GET":
        from whale_core.life.exploration import InterestTracker
        from whale_core.life.service import settings

        diaries = [
            ctx.store.get(k)
            for k in ctx.store.keys()
            if k.startswith("life:explore:diary:")
        ]
        send(
            {
                "settings": settings(ctx.store),
                "interests": [asdict(i) for i in InterestTracker(ctx.store).topics()],
                "diary": [e for day in diaries[-7:] for e in day],
            }
        )
    elif head == "life" and parts == ["life", "interests"] and method == "POST":
        from whale_core.life.exploration import InterestTracker

        value = body()
        if (
            not isinstance(value.get("topic"), str)
            or not value["topic"].strip()
            or len(value["topic"]) > 100
        ):
            raise InputError("Expected a topic within 100 characters")
        tracker = InterestTracker(ctx.store)
        if value.get("remove") is True:
            send({"removed": tracker.remove(value["topic"])})
        else:
            if (
                len(tracker.topics()) >= 20
                and value["topic"] not in tracker.as_interest_list()
            ):
                raise InputError("At most 20 interests")
            send(asdict(tracker.add(value["topic"])))
    elif head == "life" and parts == ["life", "feedback"] and method == "POST":
        from whale_core.life.exploration import ExplorationManager
        from whale_core.life.discovery import DiscoveryPlanner

        value = body()
        manager = ExplorationManager(
            ctx.store, planner=DiscoveryPlanner(allowed_hosts=frozenset())
        )
        manager.feedback(str(value.get("topic") or ""), str(value.get("signal") or ""))
        send({"saved": True})
    else:
        return False
    return True
