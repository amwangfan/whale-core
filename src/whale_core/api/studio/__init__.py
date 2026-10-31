"""S5：Whale Studio 关系图的服务端单一事实源 + 临时授权管理 API。

挂在既有 ``WhaleApiServer`` 的 Bearer 鉴权之后（token = 主人管理通道），
本模块不引入第二套鉴权；所有写操作在 ``ctx.lock`` 内串行化并带版本乐观锁，
拒绝原因用稳定机器码（``vault_not_bindable`` / ``scope_ceiling`` /
``version_conflict`` / ``not_found`` / ``invalid_request``）。

纪律（PRD §7.2/§9 与 goal-chain S5 的落地）：

- 画布上的边 = 服务端已批准的绑定；本模块是唯一写入点，前端不做安全决策。
- MEM-HARD-01：``protected_original`` 绑定请求一律 403，且不留下残边（INV-01）。
- 写路径全量审计到 kv ``audit:studio:{ts}:{id}``（只元数据，不存秘密/正文，
  INV-10）；授权与拒绝都留痕。
- Fork 不自动连线：创建副本与绑定会话是两个独立的已授权动作。
- 会话 key 形如 ``channel:kind:conversation_id``（ConversationRef.as_key），
  路由段做 URL decode + 长度/字符/结构校验。
- 写请求强制 ``Content-Type: application/json``（配合 Bearer 挡表单 CSRF）。

依赖装配：``StudioServices.from_store(ctx.store)`` 现场构建，与 S1 resolver
共用同一个 MemoryStore（profiles.py 的装配点）；也可由宿主显式挂到
``ApiContext.studio``（cli/main.py 这么做）。

包结构（issue #30：原 ``api/studio.py`` 按领域拆分，本 __init__ 为 facade，
旧 ``from whale_core.api.studio import X`` 全部兼容）：

- ``shared.py``：StudioError / _ok / _err / _audit / _require_json_body /
  _reject_unknown_fields 等共享件与常量（权限/校验逻辑单一实现）。
- ``services.py``：StudioServices / services_for / management_context。
- ``personas.py``：人物卡 list/detail/create/update + 会话 persona 绑定。
- ``memory.py``：memory-spaces / vault / forks / 会话 memories 绑定。
- ``capabilities_rhythm.py``：capabilities + rhythm 端点。
- ``auth.py``：temporary codes / grants。
- ``members.py``：member consent / memory 端点。
- ``conversations.py``：会话清单与绑定聚合视图。
- ``graph.py``：_graph。
- ``router.py``：dispatch_studio / dispatch_auth / _path_known。
"""

from whale_core.api.studio.auth import (
    _create_code,
    _list_codes,
    _list_grants,
    _revoke_code,
    _revoke_grant,
)
from whale_core.api.studio.capabilities_rhythm import (
    KNOWN_CAPABILITY_SCOPES,
    _get_rhythm,
    _parse_rhythm_body,
    _put_capabilities,
    _put_rhythm,
    _rhythm_view,
)
from whale_core.api.studio.conversations import (
    _conversation_entry,
    _conversation_keys,
    _known_conversations,
    _list_conversations,
)
from whale_core.api.studio.graph import _graph
from whale_core.api.studio.members import (
    _consent_view,
    _member_consent_grant,
    _member_consent_list,
    _member_consent_revoke,
    _member_memory_correct,
    _member_memory_delete,
    _member_memory_list,
    _member_record_for_write,
    _member_record_view,
    _member_space_or_404,
    _plugin_registry,
    _require_member_actor,
)
from whale_core.api.studio.memory import (
    _fork_create,
    _fork_preview,
    _list_memory_spaces,
    _memory_binding_view,
    _parse_record_keys,
    _parse_target_type,
    _put_memories,
    _record_count,
    _space_view,
    _vault_records,
)
from whale_core.api.studio.personas import (
    _create_persona,
    _find_persona,
    _get_persona_detail,
    _list_personas,
    _parse_persona_fields,
    _persona_detail_view,
    _persona_view,
    _put_persona,
    _update_persona,
)
from whale_core.api.studio.router import (
    _dispatch_auth,
    _dispatch_studio,
    _path_known,
    dispatch_auth,
    dispatch_studio,
)
from whale_core.api.studio.services import (
    StudioServices,
    management_context,
    services_for,
)
from whale_core.api.studio.shared import (
    StudioError,
    _audit,
    _binding_versions,
    _ceiling_for,
    _check_expected_version,
    _err,
    _now_iso,
    _ok,
    _owner_convs,
    _parse_conversation_key,
    _parse_redact_mode,
    _reject_unknown_fields,
    _require_json_body,
    builtin_redactor,
)

__all__ = [
    "KNOWN_CAPABILITY_SCOPES",
    "StudioError",
    "StudioServices",
    "builtin_redactor",
    "dispatch_auth",
    "dispatch_studio",
    "management_context",
    "services_for",
]
