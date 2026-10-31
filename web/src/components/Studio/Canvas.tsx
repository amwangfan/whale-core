/**
 * S6 卡牌画布（@xyflow/react）。
 *
 * - 节点/边完全来自服务端 graph；连线成功/失败以后端为准。
 * - protected_original 源：预览即禁止态（isValidConnection=false），
 *   松手不发请求，仅回调 onIntent({type:"fork-wizard"})。
 */

import { useCallback, useEffect, useMemo } from "react";
import {
  Background,
  Controls,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type FinalConnectionState,
  type IsValidConnection,
  type NodeTypes,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type {
  StudioConversation,
  StudioGraph,
  StudioMemorySpace,
  StudioPersona,
} from "../../api";
import {
  ConversationCard,
  DeviceCard,
  MemorySpaceCard,
  PersonaCard,
  type StudioFlowNode,
} from "./cards";
import { resolveDrop, type DropIntent } from "./connect";

const nodeTypes: NodeTypes = {
  conversation: ConversationCard,
  persona: PersonaCard,
  memory_space: MemorySpaceCard,
  device: DeviceCard,
};

const COL_X = { conversation: 40, persona: 460, memory_space: 840, device: 1220 };
const ROW_H = 150;

function layout(graph: StudioGraph): StudioFlowNode[] {
  const counters = { conversation: 0, persona: 0, memory_space: 0, device: 0 };
  return graph.nodes.map((n) => {
    const kind = n.kind;
    const row = counters[kind]++;
    return {
      id: n.id,
      type: kind,
      position: { x: COL_X[kind], y: 40 + row * ROW_H },
      data: { node: n },
    } as StudioFlowNode;
  });
}

export default function StudioCanvas({
  graph,
  conversations,
  personas,
  spaces,
  reducedMotion,
  onIntent,
  onEdgeSelect,
  onNodeSelect,
}: {
  graph: StudioGraph;
  conversations: Map<string, StudioConversation>;
  personas: Map<string, StudioPersona>;
  spaces: Map<string, StudioMemorySpace>;
  reducedMotion: boolean;
  onIntent: (intent: DropIntent) => void;
  onEdgeSelect: (edgeId: string) => void;
  onNodeSelect: (nodeId: string | null) => void;
}) {
  const [nodes, setNodes, onNodesChange] = useNodesState<StudioFlowNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);

  // 服务端 graph 是唯一事实源：每次刷新整体重建节点与边。
  useEffect(() => {
    const laidOut = layout(graph).map((n) => {
      const data = { ...(n.data as Record<string, unknown>) };
      if (n.type === "conversation") {
        data.conv = conversations.get(n.data.node.key ?? "");
      } else if (n.type === "persona") {
        data.persona = personas.get(n.id.replace(/^persona:/, ""));
      } else if (n.type === "memory_space") {
        data.space = spaces.get(n.id.replace(/^space:/, ""));
      }
      return { ...n, data } as StudioFlowNode;
    });
    setNodes(laidOut);
    setEdges(
      graph.edges.map((e) => {
        // 后端边方向为 conv -> 资源；画布拖拽方向为 资源 -> conv。
        const source = e.to;
        const target = e.from;
        return {
          id: e.id,
          source,
          target,
          animated: !reducedMotion,
          className: `studio-edge studio-edge-${e.kind}${
            e.dangling || e.missing ? " dangling" : ""
          }`,
          label:
            e.kind === "memory"
              ? `${e.can_read ? "读" : ""}${e.can_write ? "/写" : ""}`
              : undefined,
        };
      }),
    );
  }, [graph, conversations, personas, spaces, reducedMotion, setNodes, setEdges]);

  const nodeById = useMemo(() => {
    const m = new Map<string, StudioFlowNode>();
    for (const n of nodes) m.set(n.id, n);
    return m;
  }, [nodes]);

  const intentFor = useCallback(
    (sourceId: string, targetId: string): DropIntent => {
      const source = nodeById.get(sourceId);
      const target = nodeById.get(targetId);
      if (!source || !target)
        return { type: "invalid", reason: "连线端点已失效，请刷新。" };
      const sd = source.data as { node: StudioGraph["nodes"][number] };
      const td = target.data as { node: StudioGraph["nodes"][number] };
      return resolveDrop(
        {
          kind: sd.node.kind,
          id: source.id,
          restricted: sd.node.restricted,
          spaceType: sd.node.space_type,
          writable: sd.node.writable,
        },
        { kind: td.node.kind, conversationKey: td.node.key },
      );
    },
    [nodeById],
  );

  // 连线预览：vault 源或非法组合 → 禁止态（RF 视觉反馈）。
  const isValidConnection: IsValidConnection<Edge> = useCallback(
    (conn) => {
      const t = intentFor(conn.source ?? "", conn.target ?? "").type;
      return t === "persona" || t === "memory";
    },
    [intentFor],
  );

  const onConnect = useCallback(
    (conn: Connection) => {
      if (!conn.source || !conn.target) return;
      onIntent(intentFor(conn.source, conn.target));
    },
    [intentFor, onIntent],
  );

  // 松手落点不合法时：若源是 vault 且目标是会话 → 打开 Fork 向导（不发请求）。
  const onConnectEnd = useCallback(
    (_event: MouseEvent | TouchEvent, state: FinalConnectionState) => {
      if (state.isValid !== false) return;
      const from = state.fromNode?.id;
      const to = state.toNode?.id;
      if (!from || !to) return;
      const intent = intentFor(from, to);
      if (intent.type === "fork-wizard") onIntent(intent);
    },
    [intentFor, onIntent],
  );

  return (
    <div className="studio-canvas" data-testid="studio-canvas">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onConnectEnd={onConnectEnd}
        isValidConnection={isValidConnection}
        onEdgeClick={(_, edge) => onEdgeSelect(edge.id)}
        onNodeClick={(_, node) => onNodeSelect(node.id)}
        onPaneClick={() => onNodeSelect(null)}
        fitView
        minZoom={0.3}
        maxZoom={1.6}
        proOptions={{ hideAttribution: true }}
        deleteKeyCode={null}
        nodesConnectable
        elementsSelectable
      >
        <Background gap={24} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
