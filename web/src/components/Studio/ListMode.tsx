/**
 * S6 窄屏（<768px）退化模式：卡片列表 + 关系编辑表单。
 * 与画布共用同一组已授权 API；所有判断仍以服务端返回为准。
 */

import { useState } from "react";
import type {
  ConversationRhythm,
  StudioConversation,
  StudioMemorySpace,
  StudioPersona,
} from "../../api";
import { CapabilityCard, RhythmEditor } from "./cards";

export default function StudioListMode({
  conversations,
  personas,
  spaces,
  busy,
  onBindPersona,
  onBindMemory,
  onSetCapabilities,
  onSetRhythm,
  onFork,
}: {
  conversations: StudioConversation[];
  personas: StudioPersona[];
  spaces: StudioMemorySpace[];
  busy: boolean;
  onBindPersona: (key: string, personaId: string | null, version?: number) => void;
  onBindMemory: (
    key: string,
    spaceId: string,
    bind: boolean,
    version?: number,
  ) => void;
  onSetCapabilities: (key: string, scopes: string[], version?: number) => void;
  onSetRhythm: (key: string, rhythm: ConversationRhythm, version?: number) => void;
  onFork: (spaceId: string) => void;
}) {
  if (conversations.length === 0) {
    return <p className="muted">还没有任何会话绑定。先在某个会话里和鲸鱼说句话吧。</p>;
  }
  return (
    <div className="studio-list" data-testid="studio-list">
      {conversations.map((conv) => (
        <ConversationEditor
          key={conv.key}
          conv={conv}
          personas={personas}
          spaces={spaces}
          busy={busy}
          onBindPersona={onBindPersona}
          onBindMemory={onBindMemory}
          onSetCapabilities={onSetCapabilities}
          onSetRhythm={onSetRhythm}
          onFork={onFork}
        />
      ))}
    </div>
  );
}

function ConversationEditor({
  conv,
  personas,
  spaces,
  busy,
  onBindPersona,
  onBindMemory,
  onSetCapabilities,
  onSetRhythm,
  onFork,
}: {
  conv: StudioConversation;
  personas: StudioPersona[];
  spaces: StudioMemorySpace[];
  busy: boolean;
  onBindPersona: (key: string, personaId: string | null, version?: number) => void;
  onBindMemory: (
    key: string,
    spaceId: string,
    bind: boolean,
    version?: number,
  ) => void;
  onSetCapabilities: (key: string, scopes: string[], version?: number) => void;
  onSetRhythm: (key: string, rhythm: ConversationRhythm, version?: number) => void;
  onFork: (spaceId: string) => void;
}) {
  const boundIds = new Set((conv.memory_spaces ?? []).map((m) => m.space_id));
  const ceiling = conv.ceiling ?? [];
  const [caps, setCaps] = useState<string[]>(conv.capabilities ?? []);

  return (
    <section className="studio-list-conv" data-testid={`list-${conv.key}`}>
      <h3>{conv.key}</h3>

      <label>
        角色卡
        <select
          value={conv.persona_id ?? ""}
          disabled={busy}
          onChange={(e) =>
            onBindPersona(conv.key, e.target.value || null, conv.persona_version)
          }
        >
          <option value="">默认人格（无绑定）</option>
          {personas.map((p) => (
            <option key={p.id} value={p.id}>
              {p.title || p.name}
            </option>
          ))}
        </select>
      </label>

      <fieldset>
        <legend>记忆空间</legend>
        {spaces.map((s) => {
          if (s.restricted || !s.bindable) {
            return (
              <div key={s.space_id} className="studio-list-space restricted">
                <span>
                  {s.name}（个人原始库）——永不可直接连线
                </span>
                <button type="button" onClick={() => onFork(s.space_id)}>
                  创建受控副本
                </button>
              </div>
            );
          }
          return (
            <label key={s.space_id} className="studio-list-space">
              <input
                type="checkbox"
                checked={boundIds.has(s.space_id)}
                disabled={busy}
                onChange={(e) =>
                  onBindMemory(
                    conv.key,
                    s.space_id,
                    e.target.checked,
                    conv.memory_version,
                  )
                }
              />
              {s.name}（{s.space_type}，{s.record_count ?? 0} 条）
            </label>
          );
        })}
      </fieldset>

      <fieldset>
        <legend>能力（上限由会话类型决定）</legend>
        {ceiling.map((scope) => (
          <CapabilityCard
            key={scope}
            scope={scope}
            enabled={caps.includes(scope)}
            withinCeiling
            disabled={busy}
            onToggle={(s, next) =>
              setCaps((cur) =>
                next ? [...cur, s] : cur.filter((x) => x !== s),
              )
            }
          />
        ))}
        <button
          type="button"
          disabled={busy}
          onClick={() => onSetCapabilities(conv.key, caps, conv.capabilities_version)}
        >
          保存能力配置
        </button>
      </fieldset>

      <RhythmEditor
        rhythm={conv.rhythm}
        busy={busy}
        resetKey={conv.key}
        onSave={(rhythm) => onSetRhythm(conv.key, rhythm, conv.rhythm_version)}
      />
    </section>
  );
}
