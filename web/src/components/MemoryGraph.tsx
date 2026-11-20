import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import { ReactFlow, Controls, Handle, Position, useNodesState, useNodesInitialized, useReactFlow, type Node, type NodeProps } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { studioApi, type GroupDigest, type MemoryGraphData, type MemoryGraphNode } from '../api';
import { filterGraph, keywordGraph, memberGraph, traitNames } from './graphSources';
import GraphImport from './GraphImport';
import KeywordDetail from './KeywordDetail';
import MemoryEvidence from './MemoryEvidence';
import './memoryGraph.css';

const colors: Record<string, string> = { memory: '#8595aa', person: '#9387ab', space: '#688f86', evidence: '#afa18b', preference:'#9a654b', topics:'#537d85', expressions:'#7d6590', scenes:'#918252' };
const names: Record<string, string> = { memory: '记忆', person: '人物', space: '空间', evidence: '证据', keyword:'关键词', mcp:'MCP 实体', preference:'自述喜好',tool:'群工具',...traitNames };
const MemoryGraph3D = lazy(() => import('./MemoryGraph3D'));
type Dot = Node<{label:string; color:string; size:number; prominent:boolean},'dot'>;
function DotNode({data}:NodeProps<Dot>) {
  return <div className="mg-hit" title={data.label}>
    <Handle type="target" position={Position.Left} isConnectable={false}/>
    <span className="mg-dot" style={{background:data.color,width:data.size,height:data.size}} />
    <span className={'mg-label'+(data.prominent?' prominent':'')}>{data.label.slice(0,24)}</span>
    <Handle type="source" position={Position.Right} isConnectable={false}/>
  </div>;
}
const dotTypes = {dot:DotNode};
function FitDots({layout}:{layout:Dot[]}) {
  const ready=useNodesInitialized();
  const {fitView}=useReactFlow();
  // Dragging changes positions but keeps the viewport. Reframe only on a changed node set.
  const ids=JSON.stringify(layout.map(n=>n.id));
  useEffect(()=>{if(ready) void fitView({padding:.14,maxZoom:1.5});},[ready,ids,fitView]);
  return null;
}

export default function MemoryGraph({ spaceId, groupId, revision:groupRevision=0 }: { spaceId: string; groupId?:string; revision?:number }) {
  const [source,setSource] = useState<'memory'|'keywords'|'members'|'mcp'>(groupId?'members':'memory');
  const [memory,setData] = useState<MemoryGraphData | null>(null);
  const [digest,setDigest] = useState<GroupDigest|null>(null);
  const [imported,setImported] = useState<MemoryGraphData|null>(null);
  const keywordData = useMemo(()=>digest ? keywordGraph(digest) : null,[digest]);
  const members = useMemo(()=>digest ? memberGraph(digest) : null,[digest]);
  const data = source==='mcp' ? imported : source==='keywords' ? keywordData : source==='members' ? members : memory;
  const [entity,setEntity] = useState('');
  const [relation,setRelation] = useState('');
  const [mode, setMode] = useState('2d');
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState('');
  const [selected, setSelected] = useState('');
  const [live, setLive] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let disposed = false;
    setSelected(''); setError('');
    if (!spaceId || source==='mcp') return;
    async function load() {
      try {
        if((source==='keywords' || source==='members') && groupId) {
          const next=await studioApi.groupDigest(groupId);
          if(!disposed) {
            setDigest(next);
            if(source==='members' && !next.group) setSource('keywords');
          }
        } else { const next=await studioApi.memoryGraph(spaceId); if(!disposed)setData(next); }
        if(!disposed)setError('');
      }
      catch (e) { if (!disposed) setError(String(e)); }
    }
    void load();
    const timer = live ? setInterval(() => { if (!document.hidden) void load(); }, 10000) : undefined;
    return () => { disposed = true; clearInterval(timer); };
  }, [spaceId, groupId, source, live, revision, groupRevision]);
  function switchSource(next:typeof source) {
    setSource(next);setEntity('');setRelation('');setStatus('');setQuery('');setSelected('');
  }
  const visible = useMemo(() => {
    return data ? filterGraph(data,query,entity,relation,status,source==='keywords' ? 80 : 1000) : {nodes:[],links:[],total:0};
  }, [data, query, status,entity,relation,source]);
  const [nodes, setNodes, onNodesChange] = useNodesState<Dot>([]);
  useEffect(() => {
    if(mode==='3d') return;
    // Bounded force layout; visual links carry only relationships stored by the server.
    const points = visible.nodes.map((n, i) => ({ ...n, x: Math.cos(i * 2.399) * Math.sqrt(i + 1) * 55, y: Math.sin(i * 2.399) * Math.sqrt(i + 1) * 55 }));
    const byId = new Map(points.map(p => [p.id, p]));
    for (let step = 0; step < (points.length>400 ? 8 : 40); step++) {
      for (let i = 0; i < points.length; i++) for (let j = i + 1; j < points.length; j++) {
        const a = points[i], b = points[j], dx = a.x - b.x, dy = a.y - b.y;
        const d = Math.max(30, Math.hypot(dx, dy)), f = Math.min(3, 1800 / (d * d));
        a.x += dx / d * f; a.y += dy / d * f; b.x -= dx / d * f; b.y -= dy / d * f;
      }
      for (const l of visible.links) {
        const a = byId.get(l.source)!, b = byId.get(l.target)!;
        const dx = b.x - a.x, dy = b.y - a.y, d = Math.max(1, Math.hypot(dx, dy)), f = (d - 175) * .015;
        a.x += dx / d * f; a.y += dy / d * f; b.x -= dx / d * f; b.y -= dy / d * f;
      }
    }
    setNodes(points.map((n,i) => ({ id:n.id, type:'dot', ariaLabel:n.label, position:{x:n.x*.58,y:n.y*.58},
      data:{label:n.label,color:colors[n.category] ?? '#878299',size:6+Math.min(5,Math.log2(1+(n.occurrences ?? 1))),prominent:i<5} })));
  }, [visible, setNodes,mode]);
  const detail: MemoryGraphNode | undefined = visible.nodes.find(n => n.id === selected);
  const edges = visible.links.map((l,i) => {
    const active = detail && (l.source===selected || l.target===selected);
    return {id:String(i),source:l.source,target:l.target,type:'straight',style:{
      stroke:active?'#917ac2':'#9b9aa6',strokeWidth:active?1.5:1,opacity:active ? 0.95 : detail ? 0.12 : 0.40
    }};
  });
  const words = visible.nodes.filter(n=>n.category==='keyword' && n.label.toLowerCase().includes(query.trim().toLowerCase())).slice(0,10);
  return <section aria-label="记忆图谱" className="mg">
    <header className="mg-header"><h3>记忆图谱</h3><div className="mg-source">
    {spaceId && <button aria-pressed={source==='memory'} onClick={()=>switchSource('memory')}>空间记忆</button>}
    {groupId && digest?.group && <button aria-pressed={source==='members'} onClick={()=>switchSource('members')}>喜好与画像</button>}
    {groupId && <button aria-pressed={source==='keywords'} onClick={()=>switchSource('keywords')}>群聊关键词</button>}
    {imported && <button aria-pressed={source==='mcp'} onClick={()=>switchSource('mcp')}>已导入 MCP</button>}
    </div></header>
    <div className="mg-toolbar">
      <input type="search" aria-label="搜索图谱" placeholder={source==='keywords'?'搜索全部群聊关键词…':'搜索节点或观察记录…'} value={query} onChange={e => setQuery(e.target.value)} />
      <div className="mg-mode" aria-label="图谱维度">{(['2d','3d'] as const).map(m=><button key={m} aria-pressed={mode===m} onClick={()=>setMode(m)}>{m.toUpperCase()}</button>)}</div>
      <details className="mg-settings"><summary>筛选与设置</summary><div>
      <select aria-label="实体类型" value={entity} onChange={e=>setEntity(e.target.value)}><option value="">全部实体类型</option>{[...new Set(data?.nodes.map(n=>n.entityType ?? n.category))].sort().map(t=><option key={t} value={t}>{source==='mcp'?t:names[t] ?? t}</option>)}</select>
      <select aria-label="关系类型" value={relation} onChange={e=>setRelation(e.target.value)}><option value="">全部关系类型</option>{[...new Set(data?.links.map(l=>l.label))].sort().map(t=><option key={t}>{t}</option>)}</select>
      {source==='memory' && <select aria-label="图谱状态" value={status} onChange={e => setStatus(e.target.value)}><option value="">全部状态</option>{['active','superseded','revoked','pending','needs_revalidation'].map(s => <option key={s}>{s}</option>)}</select>}
      {spaceId && source!=='mcp' && <button onClick={() => setRevision(x => x + 1)}>刷新图谱</button>}
      {spaceId && source!=='mcp' && <label><input type="checkbox" checked={live} onChange={e => setLive(e.target.checked)} /> 每 10 秒更新</label>}
      </div></details>
    </div>
    {source==='keywords' && data && <div className="mg-keywords" aria-label="可点击的群聊关键词"><strong>点关键词看原话</strong>
      {words.map(n=><button key={n.id} aria-pressed={selected===n.id} onClick={()=>setSelected(n.id)}>{n.label}</button>)}
      {!words.length && <span>没有匹配的关键词</span>}</div>}
    {error && <p role="alert">{error}</p>}
    {!data && !error && <p>{spaceId?'正在加载图谱…':'选择记忆空间或导入 MCP 文件'}</p>}
    {data && <><div className={'mg-body'+(detail?' has-detail':'')}>
        <div className="mg-plot">
          {mode === '3d' ? <Suspense fallback={<p>正在加载三维图谱…</p>}><MemoryGraph3D nodes={visible.nodes} links={visible.links} onSelect={setSelected} /></Suspense> : <ReactFlow key={spaceId} nodeTypes={dotTypes} nodes={nodes.map(n=>({...n,selected:n.id===selected}))} edges={edges} onNodesChange={onNodesChange} onNodeClick={(_, n) => setSelected(n.id)} nodesConnectable={false} minZoom={.08} fitView><FitDots layout={nodes}/><Controls showInteractive={false}/></ReactFlow>}
        </div>
        {detail && <aside className="mg-detail">
          <header><div><small>{names[detail.category]}</small><h4>{detail.label}</h4></div><button aria-label="关闭节点详情" onClick={()=>setSelected('')}>×</button></header>
          {detail?.category==='keyword' && source==='keywords' && groupId && digest && <KeywordDetail key={detail.id} word={detail.label} gid={groupId} segments={digest.segments}/>}
          {detail?.observations && <><h4>观察记录</h4><ul>{detail.observations.map(o=><li key={o}>{o}</li>)}</ul></>}
          {detail?.category === 'evidence' && detail.record_key && detail.evidence_id && <MemoryEvidence key={detail.id} recordKey={detail.record_key} evidenceId={detail.evidence_id} />}
          <>
            <dl>{Object.entries(detail).filter(([k,v]) => !['label','id','category','observations'].includes(k) && v != null && v !== '').map(([k,v]) => <div key={k}><dt>{k==='occurrences'?'相关对话段数':k}</dt><dd>{String(v)}</dd></div>)}</dl>
            <h4>图中关联</h4>{visible.links.filter(l => l.source === detail.id || l.target === detail.id).map((l,i) => {
              const other=l.source===detail.id ? l.target : l.source;
              return <p key={i}>{l.label}{l.count ? '（'+l.count+' 段）':''} {detail.category==='keyword'?'↔':l.source===detail.id?'→':'←'} <button onClick={()=>setSelected(other)}>{visible.nodes.find(n=>n.id===other)?.label}</button></p>;
            })}</>
        </aside>}
      </div><footer>{source==='keywords'?`共 ${data.total_records} 个关键词`:source==='members'?`${digest?.members.length ?? 0} 位成员`:source==='mcp'?`共 ${data.total_records} 个实体`:`共 ${data.total_records} 条记忆`} · 图中 {visible.nodes.length} 个节点、{visible.links.length} 条关系{data.truncated && ' · 取最新 300 条记忆'}</footer></>}
    <GraphImport onLoad={g=>{setImported(g);switchSource('mcp');}}/>
  </section>;
}
