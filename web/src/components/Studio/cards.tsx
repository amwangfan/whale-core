/**
 * S6 画布卡牌。卡片只渲染服务端返回的视图；
 * 敏感卡（vault）后端只给「存在 + 名称」，前端不兜底猜元数据（INV-08）。
 */

import { useEffect, useState } from "react";
import { Handle, Position, type NodeProps, type Node } from "@xyflow/react";
import type {
  ConversationRhythm,
  StudioConversation,
  StudioGraphNode,
  StudioMemorySpace,
  StudioPersona,
  TemporaryGrant,
} from "../../api";

export type ConvNodeData = {
  node: StudioGraphNode;
  conv?: StudioConversation;
};
export type PersonaNodeData = {
  node: StudioGraphNode;
  persona?: StudioPersona;
};
export type SpaceNodeData = {
  node: StudioGraphNode;
  space?: StudioMemorySpace;
};

export type StudioFlowNode =
  | Node<ConvNodeData, "conversation">
  | Node<PersonaNodeData, "persona">
  | Node<SpaceNodeData, "memory_space">;

const KIND_LABEL: Record<string, string> = {
  qq_group: "QQ 群",
  qq_private: "QQ 私聊",
  web: "网页",
  voice: "语音",
  phone: "电话",
  api: "API",
};

const SPACE_TYPE_LABEL: Record<string, string> = {
  protected_original: "个人原始库",
  owner_working_copy: "工作副本",
  group_space: "群组记忆",
  shareable_fork: "共享副本",
};

const ACTIVITY_LABEL: Record<string, string> = {
  quiet: "安静",
  balanced: "均衡",
  lively: "活跃",
};

/** 节奏摘要（会话卡展示）：安静/均衡/活跃 · 段上限 · 免打扰。 */
export function rhythmSummary(rhythm?: ConversationRhythm): string {
  if (!rhythm) return "默认";
  const parts = [ACTIVITY_LABEL[rhythm.activity] ?? rhythm.activity];
  parts.push(
    rhythm.max_segments > 0 ? `≤${rhythm.max_segments} 段` : "默认分段",
  );
  if (rhythm.dnd) parts.push("免打扰");
  return parts.join(" · ");
}

export function ConversationCard({ data }: NodeProps<StudioFlowNode>) {
  const { node, conv } = data as ConvNodeData;
  const kindLabel = KIND_LABEL[node.conversation_kind ?? ""] ?? node.conversation_kind;
  return (
    <div className="studio-card studio-conv" data-testid={`card-${node.id}`}>
      <Handle type="target" position={Position.Right} aria-label="连接入口" />
      <div className="studio-card-kind">会话 · {kindLabel}</div>
      <div className="studio-card-title">{node.label}</div>
      <div className="studio-card-meta">
        <span>人格：{conv?.persona_id ?? "默认"}</span>
        <span>记忆副本：{conv?.memory_spaces?.length ?? 0}</span>
        <span>能力：{node.capabilities?.length ?? 0}</span>
        <span>节奏：{rhythmSummary(conv?.rhythm)}</span>
      </div>
    </div>
  );
}

export function PersonaCard({ data }: NodeProps<StudioFlowNode>) {
  const { node, persona } = data as PersonaNodeData;
  return (
    <div className="studio-card studio-persona" data-testid={`card-${node.id}`}>
      <Handle type="source" position={Position.Left} aria-label="拖到会话以绑定角色" />
      <div className="studio-card-kind">
        角色卡{node.builtin ? " · 内置" : ""}
      </div>
      <div className="studio-card-title">{node.label}</div>
      {persona?.summary && <p className="studio-card-desc">{persona.summary}</p>}
      {persona?.temperament && (
        <div className="studio-card-meta">
          <span>气质：{persona.temperament}</span>
        </div>
      )}
    </div>
  );
}

export function MemorySpaceCard({ data }: NodeProps<StudioFlowNode>) {
  const { node, space } = data as SpaceNodeData;
  const restricted = Boolean(node.restricted);
  return (
    <div
      className={`studio-card studio-space${restricted ? " restricted" : ""}`}
      data-testid={`card-${node.id}`}
    >
      {!restricted && (
        <Handle type="source" position={Position.Left} aria-label="拖到会话以绑定记忆" />
      )}
      <div className="studio-card-kind">
        记忆 · {SPACE_TYPE_LABEL[node.space_type ?? ""] ?? node.space_type}
      </div>
      <div className="studio-card-title">{node.label}</div>
      <div className="studio-card-meta">
        {restricted ? (
          <span>受保护：永不可直接连线，仅可 Fork 出受控副本</span>
        ) : (
          <>
            <span>记录：{node.record_count ?? space?.record_count ?? 0} 条</span>
            <span>{node.writable ? "可写" : "只读"}</span>
          </>
        )}
      </div>
    </div>
  );
}

/** 能力卡（只读展示 + 勾选态；只在会话权限上限内配置，服务端兜底）。 */
export function CapabilityCard({
  scope,
  enabled,
  withinCeiling,
  onToggle,
  disabled,
}: {
  scope: string;
  enabled: boolean;
  withinCeiling: boolean;
  onToggle?: (scope: string, next: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label
      className={`studio-cap-card${withinCeiling ? "" : " over-ceiling"}`}
      data-testid={`cap-${scope}`}
    >
      <input
        type="checkbox"
        checked={enabled}
        disabled={disabled || !withinCeiling}
        onChange={(e) => onToggle?.(scope, e.target.checked)}
      />
      <span className="studio-cap-scope">{scope}</span>
      {!withinCeiling && <span className="studio-cap-note">超出会话上限</span>}
    </label>
  );
}

/** G4 会话节奏编辑器：只调回复节奏，不含任何权限配置（服务端严格字段表兜底）。
 *  本地草稿 + 保存按钮（与能力配置一致）；resetKey 变化时重置草稿。 */
export function RhythmEditor({
  rhythm,
  busy,
  resetKey,
  onSave,
}: {
  rhythm?: ConversationRhythm;
  busy?: boolean;
  resetKey?: string;
  onSave?: (rhythm: ConversationRhythm) => void;
}) {
  const server: ConversationRhythm = rhythm ?? {
    activity: "balanced",
    max_segments: 0,
    dnd: false,
  };
  const [draft, setDraft] = useState<ConversationRhythm | null>(null);
  useEffect(() => setDraft(null), [resetKey]);
  const current = draft ?? server;
  return (
    <fieldset className="studio-rhythm" data-testid="rhythm-editor">
      <legend>回复节奏（不影响能力/记忆授权）</legend>
      <label>
        活跃模式
        <select
          value={current.activity}
          disabled={busy}
          onChange={(e) =>
            setDraft({
              ...current,
              activity: e.target.value as ConversationRhythm["activity"],
            })
          }
        >
          <option value="quiet">安静（单气泡）</option>
          <option value="balanced">均衡</option>
          <option value="lively">活跃</option>
        </select>
      </label>
      <label>
        单条回复分段上限（0 = 跟随默认）
        <input
          type="number"
          min={0}
          max={5}
          value={current.max_segments}
          disabled={busy}
          onChange={(e) =>
            setDraft({
              ...current,
              max_segments: Math.max(
                0,
                Math.min(5, Number(e.target.value) || 0),
              ),
            })
          }
        />
      </label>
      <label className="studio-rhythm-dnd">
        <input
          type="checkbox"
          checked={current.dnd}
          disabled={busy}
          onChange={(e) => setDraft({ ...current, dnd: e.target.checked })}
        />
        免打扰（压制主动/补发消息）
      </label>
      <button
        type="button"
        disabled={busy || draft === null}
        onClick={() => {
          onSave?.(current);
          setDraft(null);
        }}
      >
        保存节奏设置
      </button>
    </fieldset>
  );
}

function fmtRemaining(expiresAt: number, now: number): string {
  const left = Math.max(0, Math.round(expiresAt - now));
  if (left <= 0) return "已过期";
  if (left < 120) return `剩余 ${left} 秒`;
  if (left < 7200) return `剩余 ${Math.round(left / 60)} 分钟`;
  return `剩余 ${Math.round(left / 3600)} 小时`;
}

/** 临时授权卡：只读可视化 + 撤销；不提供任何拖拽扩权入口（PRD §3.1）。 */
export function TemporaryGrantCard({
  grant,
  now,
  onRevoke,
  busy,
}: {
  grant: TemporaryGrant;
  now: number;
  onRevoke?: (grantId: string) => void;
  busy?: boolean;
}) {
  return (
    <div className="studio-card studio-grant" data-testid={`grant-${grant.grant_id}`}>
      <div className="studio-card-kind">临时授权 · {grant.proof_method || "otp"}</div>
      <div className="studio-card-title">{grant.grant_id}</div>
      <div className="studio-card-meta">
        <span>会话：{grant.conversation_id}</span>
        <span>主体：{grant.principal_id}</span>
        <span>范围：{grant.scopes.join(", ")}</span>
        <span>{fmtRemaining(grant.expires_at, now)}</span>
        <span>
          剩余次数：{grant.remaining_uses}/{grant.max_uses}
        </span>
      </div>
      {onRevoke && (
        <button
          type="button"
          className="studio-danger"
          disabled={busy}
          onClick={() => onRevoke(grant.grant_id)}
        >
          撤销授权
        </button>
      )}
    </div>
  );
}
