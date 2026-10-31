/**
 * Whale Studio · S6 卡牌画布容器。
 *
 * 数据纪律：
 * - 画布节点/边完全来自 GET /studio/graph（服务端单一事实源）；
 * - 连线不做乐观假成功：成功后整体刷新，失败提示原因；
 * - 解绑做乐观移除，失败回滚并刷新；
 * - 409 版本冲突 → 提示并刷新该关系；
 * - vault（protected_original）任何连线都只打开 Fork 向导，不发请求。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiError,
  studioApi,
  type ConversationRhythm,
  type StudioConversation,
  type StudioGraph,
  type StudioGraphEdge,
  type StudioMemorySpace,
  type StudioPersona,
  type TemporaryCode,
  type TemporaryGrant,
} from "../../api";
import StudioCanvas from "./Canvas";
import ForkWizard from "./ForkWizard";
import GrantsPanel from "./GrantsPanel";
import StudioListMode from "./ListMode";
import PersonaPanel from "./PersonaPanel";
import { CapabilityCard, RhythmEditor } from "./cards";
import { executeIntent, type DropIntent } from "./connect";

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia(query).matches
      : false,
  );
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mql = window.matchMedia(query);
    const onChange = () => setMatches(mql.matches);
    onChange();
    mql.addEventListener?.("change", onChange);
    return () => mql.removeEventListener?.("change", onChange);
  }, [query]);
  return matches;
}

export default function Studio() {
  const [graph, setGraph] = useState<StudioGraph | null>(null);
  const [conversations, setConversations] = useState<StudioConversation[]>([]);
  const [personas, setPersonas] = useState<StudioPersona[]>([]);
  const [spaces, setSpaces] = useState<StudioMemorySpace[]>([]);
  const [codes, setCodes] = useState<TemporaryCode[]>([]);
  const [grants, setGrants] = useState<TemporaryGrant[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [forkSpace, setForkSpace] = useState<string | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now() / 1000);

  const narrow = useMediaQuery("(max-width: 767px)");
  const reducedMotion = useMediaQuery("(prefers-reduced-motion: reduce)");

  const refresh = useCallback(async () => {
    const [g, convs, ps, ss, cs, gs] = await Promise.all([
      studioApi.graph(),
      studioApi.conversations(),
      studioApi.personas(),
      studioApi.memorySpaces(),
      studioApi.listCodes(),
      studioApi.listGrants(),
    ]);
    setGraph(g);
    setConversations(convs);
    setPersonas(ps);
    setSpaces(ss);
    setCodes(cs);
    setGrants(gs);
    setNow(Date.now() / 1000);
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        await refresh();
        if (!cancelled) setError("");
      } catch (e) {
        if (!cancelled) {
          setError(
            e instanceof ApiError
              ? e.message
              : "无法加载工作台数据，请稍后重试。",
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [refresh]);

  // 租约剩余时间每分钟刷新一次显示。
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), 30_000);
    return () => clearInterval(t);
  }, []);

  const convByKey = useMemo(
    () => new Map(conversations.map((c) => [c.key, c])),
    [conversations],
  );
  const personaById = useMemo(
    () => new Map(personas.map((p) => [p.id, p])),
    [personas],
  );
  const spaceById = useMemo(
    () => new Map(spaces.map((s) => [s.space_id, s])),
    [spaces],
  );

  const fail = useCallback(
    (message: string) => {
      setNotice(message);
      void refresh().catch(() => undefined);
    },
    [refresh],
  );

  // 画布连线意图（连线成功不乐观：ok 以后端为准，整体刷新）。
  const handleIntent = useCallback(
    async (intent: DropIntent) => {
      if (intent.type === "invalid") {
        setNotice(intent.reason);
        return;
      }
      if (intent.type === "fork-wizard") {
        setForkSpace(intent.spaceId);
        setNotice(
          "个人原始库不能直接连接任何会话；请通过 Fork 向导创建受控副本。",
        );
        return;
      }
      const conv = convByKey.get(intent.conversationKey);
      setBusy(true);
      setNotice("");
      try {
        const outcome = await executeIntent(intent, conv);
        if (outcome.status === "ok") {
          setNotice(outcome.changed ? "已保存。" : "关系没有变化。");
          await refresh();
        } else if (outcome.status === "conflict") {
          fail(outcome.message);
        } else if (outcome.status === "denied") {
          if (outcome.forkRequired) {
            const sid =
              intent.type === "memory" ? intent.spaceId : null;
            if (sid) setForkSpace(sid);
          }
          fail(outcome.message);
        } else {
          fail(outcome.message);
        }
      } finally {
        setBusy(false);
      }
    },
    [convByKey, fail, refresh],
  );

  // 解绑：乐观移除边，失败回滚并刷新（成功仍以刷新后的服务端边为准）。
  const unbindEdge = useCallback(
    async (edge: StudioGraphEdge) => {
      const convKey = edge.from.replace(/^conv:/, "");
      const conv = convByKey.get(convKey);
      const snapshot = graph;
      if (graph) {
        setGraph({
          ...graph,
          edges: graph.edges.filter((e) => e.id !== edge.id),
        });
      }
      setSelectedEdge(null);
      setBusy(true);
      try {
        if (edge.kind === "persona") {
          await studioApi.putPersona(convKey, null, conv?.persona_version);
        } else {
          const spaceId = edge.to.replace(/^space:/, "");
          await studioApi.putMemory(
            convKey,
            { space_id: spaceId, remove: true },
            conv?.memory_version,
          );
        }
        setNotice("已解绑。");
        await refresh();
      } catch (e) {
        // 回滚乐观 UI
        if (snapshot) setGraph(snapshot);
        if (e instanceof ApiError && (e.status === 409 || e.code === "version_conflict")) {
          fail("绑定已被并发修改，已刷新为最新状态。");
        } else {
          fail(e instanceof ApiError ? e.message : "解绑失败，请重试。");
        }
      } finally {
        setBusy(false);
      }
    },
    [convByKey, fail, graph, refresh],
  );

  const bindPersona = useCallback(
    (key: string, personaId: string | null, version?: number) => {
      if (personaId === null) {
        void (async () => {
          setBusy(true);
          try {
            await studioApi.putPersona(key, null, version);
            setNotice("已恢复默认人格。");
            await refresh();
          } catch (e) {
            fail(
              e instanceof ApiError && e.status === 409
                ? "绑定已被并发修改，已刷新为最新状态。"
                : e instanceof ApiError
                  ? e.message
                  : "操作失败。",
            );
          } finally {
            setBusy(false);
          }
        })();
        return;
      }
      void handleIntent({ type: "persona", conversationKey: key, personaId });
    },
    [fail, handleIntent, refresh],
  );

  const bindMemory = useCallback(
    (key: string, spaceId: string, bind: boolean, _version?: number) => {
      if (!bind) {
        const edge = graph?.edges.find(
          (e) => e.kind === "memory" && e.from === `conv:${key}` && e.to === `space:${spaceId}`,
        );
        if (edge) void unbindEdge(edge);
        return;
      }
      const space = spaceById.get(spaceId);
      void handleIntent({
        type: "memory",
        conversationKey: key,
        spaceId,
        canWrite: Boolean(space?.writable) && space?.space_type !== "shareable_fork",
      });
    },
    [graph, handleIntent, spaceById, unbindEdge],
  );

  const setCapabilities = useCallback(
    (key: string, scopes: string[], version?: number) => {
      void (async () => {
        setBusy(true);
        try {
          await studioApi.putCapabilities(key, scopes, version);
          setNotice("能力配置已保存。");
          await refresh();
        } catch (e) {
          fail(
            e instanceof ApiError && (e.status === 409 || e.status === 403)
              ? e.message
              : e instanceof ApiError
                ? e.message
                : "保存失败。",
          );
        } finally {
          setBusy(false);
        }
      })();
    },
    [fail, refresh],
  );

  // G4：会话节奏（只影响回复节奏；409 版本冲突提示并刷新）。
  const setRhythm = useCallback(
    (key: string, rhythm: ConversationRhythm, version?: number) => {
      void (async () => {
        setBusy(true);
        try {
          await studioApi.putRhythm(key, rhythm, version);
          setNotice("节奏设置已保存。");
          await refresh();
        } catch (e) {
          fail(
            e instanceof ApiError && e.status === 409
              ? "节奏设置已被并发修改，已刷新为最新状态。"
              : e instanceof ApiError
                ? e.message
                : "保存失败。",
          );
        } finally {
          setBusy(false);
        }
      })();
    },
    [fail, refresh],
  );

  const edge = graph?.edges.find((e) => e.id === selectedEdge) ?? null;
  const node = graph?.nodes.find((n) => n.id === selectedNode) ?? null;

  return (
    <div className="studio" data-reduced-motion={reducedMotion || undefined}>
      {error && (
        <div className="banner error" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="banner info" role="status">
          {notice}
        </div>
      )}
      {loading && <p className="muted">正在加载工作台…</p>}

      {!loading && graph && narrow && (
        <StudioListMode
          conversations={conversations.filter((c) => !c.malformed)}
          personas={personas}
          spaces={spaces}
          busy={busy}
          onBindPersona={bindPersona}
          onBindMemory={bindMemory}
          onSetCapabilities={setCapabilities}
          onSetRhythm={setRhythm}
          onFork={setForkSpace}
        />
      )}

      {!loading && graph && !narrow && (
        <>
          <StudioCanvas
            graph={graph}
            conversations={convByKey}
            personas={personaById}
            spaces={spaceById}
            reducedMotion={reducedMotion}
            onIntent={(i) => void handleIntent(i)}
            onEdgeSelect={setSelectedEdge}
            onNodeSelect={setSelectedNode}
          />
          <p className="muted studio-hint">
            拖拽角色卡 / 记忆副本到会话卡即可建立关系；点击连线查看授权详情；
            个人原始库只能通过 Fork 向导创建副本后使用。
          </p>

          {edge && (
            <section className="studio-inspector" aria-label="关系详情">
              <h3>连线详情</h3>
              <dl>
                <dt>类型</dt>
                <dd>{edge.kind === "persona" ? "角色绑定" : "记忆绑定"}</dd>
                <dt>会话</dt>
                <dd>{edge.from.replace(/^conv:/, "")}</dd>
                <dt>目标</dt>
                <dd>{edge.to.replace(/^(persona|space):/, "")}</dd>
                {edge.kind === "memory" && (
                  <>
                    <dt>授权范围</dt>
                    <dd>
                      {edge.can_read ? "可读" : "不可读"}
                      {edge.can_write ? " / 可写" : ""}
                    </dd>
                    <dt>批准者</dt>
                    <dd>{edge.granted_by || "—"}</dd>
                    <dt>创建时间</dt>
                    <dd>{edge.created_at || "—"}</dd>
                  </>
                )}
                {edge.kind === "persona" && (
                  <>
                    <dt>绑定版本</dt>
                    <dd>{edge.version ?? "—"}</dd>
                  </>
                )}
                {(edge.dangling || edge.missing) && (
                  <>
                    <dt>状态</dt>
                    <dd>目标已不存在（残留绑定，可解绑清理）</dd>
                  </>
                )}
              </dl>
              <button
                type="button"
                className="studio-danger"
                disabled={busy}
                onClick={() => void unbindEdge(edge)}
              >
                解绑
              </button>
            </section>
          )}

          {node && !edge && (
            <NodeInspector
              nodeId={node.id}
              kind={node.kind}
              graphNode={node}
              conversations={conversations.filter((c) => !c.malformed)}
              conv={node.kind === "conversation" ? convByKey.get(node.key ?? "") : undefined}
              busy={busy}
              onConnect={(conversationKey) => {
                const intent =
                  node.kind === "persona"
                    ? ({
                        type: "persona",
                        conversationKey,
                        personaId: node.id.replace(/^persona:/, ""),
                      } as const)
                    : node.kind === "memory_space"
                      ? node.restricted
                        ? ({ type: "fork-wizard", spaceId: node.id.replace(/^space:/, "") } as const)
                        : ({
                            type: "memory",
                            conversationKey,
                            spaceId: node.id.replace(/^space:/, ""),
                            canWrite:
                              Boolean(node.writable) &&
                              node.space_type !== "shareable_fork",
                          } as const)
                      : null;
                if (intent) void handleIntent(intent);
              }}
              onSetCapabilities={setCapabilities}
              onSetRhythm={setRhythm}
            />
          )}
        </>
      )}

      {!loading && (
        <PersonaPanel
          personas={personas}
          busy={busy}
          onChanged={(message) => {
            setNotice(message);
            void refresh();
          }}
          onError={setNotice}
        />
      )}

      {!loading && (
        <GrantsPanel
          codes={codes}
          grants={grants}
          now={now}
          onChanged={() => void refresh()}
          onError={setNotice}
        />
      )}

      {forkSpace && (
        <ForkWizard
          sourceSpace={forkSpace}
          onClose={() => setForkSpace(null)}
          onDone={() => void refresh()}
        />
      )}
    </div>
  );
}

/** 节点检查器：会话 → 能力编辑 + 回复节奏；角色/记忆 → 键盘连接表单。 */
function NodeInspector({
  nodeId,
  kind,
  graphNode,
  conv,
  conversations,
  busy,
  onConnect,
  onSetCapabilities,
  onSetRhythm,
}: {
  nodeId: string;
  kind: string;
  graphNode?: import("../../api").StudioGraphNode;
  conv?: StudioConversation;
  conversations: StudioConversation[];
  busy: boolean;
  onConnect: (conversationKey: string) => void;
  onSetCapabilities: (key: string, scopes: string[], version?: number) => void;
  onSetRhythm: (key: string, rhythm: ConversationRhythm, version?: number) => void;
}) {
  const [target, setTarget] = useState("");
  const [caps, setCaps] = useState<string[] | null>(null);

  useEffect(() => {
    setTarget("");
    setCaps(null);
  }, [nodeId]);

  if (kind === "device") {
    // issue #11：设备卡只读——设备授权走一次性确认凭证（issue #10），
    // 不是画布连线；状态/能力/排队水位来自服务端 registry。
    const n = graphNode;
    return (
      <section className="studio-inspector" aria-label="设备详情">
        <h3>设备 · {n?.label ?? nodeId}</h3>
        <dl>
          <dt>状态</dt>
          <dd>{n?.status ?? "—"}</dd>
          <dt>允许能力</dt>
          <dd>{(n?.capabilities ?? []).join("、") || "—"}</dd>
          <dt>待处理事件</dt>
          <dd>{n?.queued_events ?? 0}</dd>
        </dl>
        <p className="muted">
          设备授权经手机确认的一次性凭证签发（不在画布连线）；
          个人原始库永不与设备直连。
        </p>
      </section>
    );
  }

  if (kind === "conversation" && conv) {
    const ceiling = conv.ceiling ?? [];
    const current = caps ?? conv.capabilities ?? [];
    return (
      <section className="studio-inspector" aria-label="会话能力配置">
        <h3>会话能力 · {conv.key}</h3>
        {ceiling.map((scope) => (
          <CapabilityCard
            key={scope}
            scope={scope}
            enabled={current.includes(scope)}
            withinCeiling
            disabled={busy}
            onToggle={(s, next) =>
              setCaps(
                next
                  ? [...current, s]
                  : current.filter((x) => x !== s),
              )
            }
          />
        ))}
        <button
          type="button"
          disabled={busy}
          onClick={() => onSetCapabilities(conv.key, current, conv.capabilities_version)}
        >
          保存能力配置
        </button>
        <RhythmEditor
          rhythm={conv.rhythm}
          busy={busy}
          resetKey={conv.key}
          onSave={(rhythm) => onSetRhythm(conv.key, rhythm, conv.rhythm_version)}
        />
      </section>
    );
  }

  return (
    <section className="studio-inspector" aria-label="键盘连接">
      <h3>连接到会话</h3>
      <label>
        目标会话
        <select value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="">选择会话…</option>
          {conversations.map((c) => (
            <option key={c.key} value={c.key}>
              {c.key}
            </option>
          ))}
        </select>
      </label>
      <button
        type="button"
        className="primary"
        disabled={busy || !target}
        onClick={() => onConnect(target)}
      >
        连接
      </button>
    </section>
  );
}
