/**
 * S6 连线决策逻辑（纯函数，便于测试）。
 *
 * 纪律：前端不做安全决策——这里只把「拖拽意图」翻译成对应的已授权 API；
 * protected_original（vault）在松手时**不发任何写请求**，只返回打开
 * Fork 向导的意图（MEM-HARD-01，PRD §3.2）。
 */

import {
  ApiError,
  studioApi,
  type StudioConversation,
  type StudioGraphNode,
} from "../../api";

export type DropIntent =
  | { type: "persona"; conversationKey: string; personaId: string }
  | {
      type: "memory";
      conversationKey: string;
      spaceId: string;
      canWrite: boolean;
    }
  | { type: "fork-wizard"; spaceId: string }
  | { type: "invalid"; reason: string };

export interface DropSource {
  kind: StudioGraphNode["kind"];
  id: string;
  /** memory_space 节点 id 形如 space:<id>；persona 节点 id 形如 persona:<id>。 */
  restricted?: boolean;
  spaceType?: string;
  writable?: boolean;
}

/** 把「源卡 → 会话卡」的拖拽解析为一个意图；不发起任何请求。 */
export function resolveDrop(
  source: DropSource,
  target: { kind: StudioGraphNode["kind"]; conversationKey?: string },
): DropIntent {
  if (target.kind !== "conversation" || !target.conversationKey) {
    return { type: "invalid", reason: "连线目标必须是会话卡。" };
  }
  if (source.kind === "persona") {
    return {
      type: "persona",
      conversationKey: target.conversationKey,
      personaId: source.id.replace(/^persona:/, ""),
    };
  }
  if (source.kind === "memory_space") {
    const spaceId = source.id.replace(/^space:/, "");
    // MEM-HARD-01：vault 拖到任何会话都不发 connect 请求，只开 Fork 向导。
    if (source.restricted || source.spaceType === "protected_original") {
      return { type: "fork-wizard", spaceId };
    }
    return {
      type: "memory",
      conversationKey: target.conversationKey,
      spaceId,
      // 共享副本默认只读（服务端另有校验，这里只是提交默认值）。
      canWrite: source.spaceType === "shareable_fork" ? false : Boolean(source.writable),
    };
  }
  return { type: "invalid", reason: "这张卡不能连接到会话。" };
}

export type ConnectOutcome =
  | { status: "ok"; changed: boolean; version: number }
  | { status: "conflict"; message: string }
  | { status: "denied"; code: string; message: string; forkRequired: boolean }
  | { status: "error"; message: string };

/**
 * 执行连线意图（服务端是唯一裁决者；成功/失败都以其返回为准，
 * 前端不做乐观假成功——调用方在 ok 之后刷新画布）。
 */
export async function executeIntent(
  intent: DropIntent,
  conv: StudioConversation | undefined,
): Promise<ConnectOutcome> {
  try {
    if (intent.type === "persona") {
      const res = await studioApi.putPersona(
        intent.conversationKey,
        intent.personaId,
        conv?.persona_version,
      );
      return { status: "ok", changed: res.changed, version: res.version };
    }
    if (intent.type === "memory") {
      const res = await studioApi.putMemory(
        intent.conversationKey,
        {
          space_id: intent.spaceId,
          can_read: true,
          can_write: intent.canWrite,
        },
        conv?.memory_version,
      );
      return { status: "ok", changed: res.changed, version: res.version };
    }
    return { status: "error", message: "不支持的连线操作。" };
  } catch (err) {
    if (err instanceof ApiError) {
      if (err.status === 409 || err.code === "version_conflict") {
        return {
          status: "conflict",
          message: "绑定已被并发修改，已刷新为最新状态，请重试。",
        };
      }
      if (err.status === 403 || err.code === "vault_not_bindable" || err.code === "scope_ceiling") {
        return {
          status: "denied",
          code: err.code,
          message: err.message,
          forkRequired: err.code === "vault_not_bindable",
        };
      }
      return { status: "error", message: err.message };
    }
    return { status: "error", message: "网络异常，连线未保存。" };
  }
}
