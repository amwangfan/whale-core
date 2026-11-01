"""S5 依赖装配：StudioServices / services_for / management_context。

``StudioServices.from_store(ctx.store)`` 现场构建，与 S1 resolver 共用同一个
MemoryStore（profiles.py 的装配点）；也可由宿主显式挂到 ``ApiContext.studio``
（cli/main.py 这么做）。
"""

from __future__ import annotations

from dataclasses import dataclass

from whale_core.api.studio.shared import _owner_convs
from whale_core.life.context_resolver import API_OWNER
from whale_core.life.conversation_context import (
    ConversationKind,
    ConversationRef,
    Principal,
    ResolvedContext,
)
from whale_core.life.temporary_auth import TemporaryAuthService
from whale_core.memory.bindings import BindingStore
from whale_core.memory.consent import MemberConsentStore, MemberMemoryDirectory
from whale_core.memory.fork import ForkService
from whale_core.memory.spaces import SpaceStore
from whale_core.memory.store import MemoryStore
from whale_core.ui.persona_cards import PersonaCardLibrary


@dataclass
class StudioServices:
    """S5 依赖集合。全部是 MemoryStore 上的薄封装，现场构建即共享同一 store。"""

    spaces: SpaceStore
    bindings: BindingStore
    forks: ForkService
    auth: TemporaryAuthService
    personas: PersonaCardLibrary
    consents: MemberConsentStore
    directory: MemberMemoryDirectory

    @classmethod
    def from_store(cls, store: MemoryStore) -> "StudioServices":
        spaces = SpaceStore(store)
        bindings = BindingStore(store, spaces, owner_conversations=_owner_convs())
        consents = MemberConsentStore(store)
        return cls(
            spaces=spaces,
            bindings=bindings,
            forks=ForkService(store, spaces),
            auth=TemporaryAuthService(store),
            personas=PersonaCardLibrary(),
            consents=consents,
            directory=MemberMemoryDirectory(bindings, spaces, consents),
        )


def services_for(ctx) -> StudioServices:
    """取 ctx.studio；宿主未显式装配时从 ctx.store 现场构建（惰性、幂等）。"""
    svc = getattr(ctx, "studio", None)
    if isinstance(svc, StudioServices):
        return svc
    with ctx.lock:
        svc = getattr(ctx, "studio", None)
        if not isinstance(svc, StudioServices):
            svc = StudioServices.from_store(ctx.store)
            try:
                ctx.studio = svc
            except AttributeError:
                pass  # 宿主自定义 ctx：不缓存也行，下次重建即可
    return svc


def management_context() -> ResolvedContext:
    """管理通道自身的 owner ResolvedContext（供 graph 解释信息使用）。

    Bearer token 即主人管理通道；本对象仅用于解释/展示，不作为授权证据外传。
    """
    return ResolvedContext(
        conversation=ConversationRef("api", ConversationKind.API, "studio"),
        principal=Principal("paired:api:studio", paired=True, is_owner=True,
                            evidence="api:bearer-token"),
        persona_id=None,
        capability_scopes=API_OWNER,
    )
