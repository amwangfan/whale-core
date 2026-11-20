import { useEffect, useRef, useState } from 'react';
import ForceGraph3D from '3d-force-graph';
import type { MemoryGraphNode } from '../api';

export default function MemoryGraph3D({ nodes, links, onSelect }: {
  nodes: MemoryGraphNode[]; links: {source: string; target: string; label: string}[];
  onSelect: (id: string) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [error, setError] = useState('');
  const [monitor, setMonitor] = useState(false);
  const [stats, setStats] = useState({ fps:0, ms:0, calls:0, triangles:0 });
  const activeGraph = useRef<InstanceType<typeof ForceGraph3D>>();
  useEffect(() => {
    if (!host.current) return;
    const el = host.current;
    let graph: InstanceType<typeof ForceGraph3D> | undefined;
    let resize: ResizeObserver | undefined;
    try {
      graph = new ForceGraph3D(el).backgroundColor('#18171d').height(560)
        .width(el.clientWidth).nodeColor(n => (n as {category?:string}).category==='person'?'#b8a5cf':'#bbb8c4').nodeVal(.7)
        .nodeLabel(n => { const label = document.createElement('span'); label.textContent = String((n as {label?: string}).label || ''); return label; })
        .linkColor(() => '#aaa6b4').linkOpacity(.24).linkDirectionalArrowLength(l => (l as {label?:string}).label==='同段出现'?0:1.5)
        .linkLabel(l => { const label = document.createElement('span'); label.textContent = String((l as {label?: string}).label || ''); return label; })
        .graphData({nodes: nodes.map(n => ({...n})), links: links.map(l => ({...l}))})
        .onNodeClick(n => { onSelect(String(n.id)); const ratio = 1 + 100 / Math.max(1, Math.hypot(n.x || 0, n.y || 0, n.z || 0)); graph?.cameraPosition({x:(n.x || 0)*ratio,y:(n.y || 0)*ratio,z:(n.z || 0)*ratio+1}, {x:n.x || 0,y:n.y || 0,z:n.z || 0}, 700); });
      resize = new ResizeObserver(() => graph?.width(el.clientWidth));
      resize.observe(el);
      activeGraph.current = graph;
      setError('');
    } catch { setError('此浏览器无法启用 WebGL，请切换二维图谱'); }
    return () => { activeGraph.current = undefined; resize?.disconnect(); graph?._destructor(); el.replaceChildren(); };
  }, [nodes, links, onSelect]);
  useEffect(() => {
    const graph = activeGraph.current;
    if (!monitor || !graph) return;
    const scene = graph.scene();
    const previous = scene.onAfterRender;
    let frames = 0;
    let start = performance.now();
    scene.onAfterRender = function(...args: Parameters<typeof previous>) { previous.apply(this,args); frames++; };
    const timer = setInterval(() => {
      const now=performance.now(), elapsed=now-start;
      const render = graph.renderer().info.render;
      setStats({fps:Math.round(frames*1000/elapsed),ms:frames ? elapsed/frames : 0,
        calls:render.calls,triangles:render.triangles});
      start=now; frames=0;
    },1000);
    return () => {clearInterval(timer);scene.onAfterRender=previous;};
  }, [monitor,nodes,links,onSelect]);
  return <div style={{height:560,width:'100%',position:'relative'}}>
    {error && <p role="alert">{error}</p>}<div ref={host} style={{width:'100%',height:'100%'}} />
    {!error && <div style={{position:'absolute',left:10,top:10,background:'#101c30ee',color:'white',padding:8,borderRadius:8}}>
      <label><input type="checkbox" checked={monitor} onChange={e=>setMonitor(e.target.checked)}/> 3D 性能</label>
      {monitor && <div>{stats.fps} FPS · {stats.ms.toFixed(1)} ms/帧<br/>{stats.calls} 次绘制 · {stats.triangles} 个三角形<br/>{nodes.length} 个节点 · {links.length} 条关系</div>}
    </div>}
  </div>;
}
