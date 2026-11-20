import { useRef, useState } from 'react';
import type { MemoryGraphData } from '../api';
import { importBytes, parseMcp } from './graphSources';

export default function GraphImport({onLoad}:{onLoad:(data:MemoryGraphData)=>void}) {
  const [text,setText] = useState('');
  const [notice,setNotice] = useState('');
  const [error,setError] = useState('');
  const ticket = useRef(0);
  async function load(source:File|string) {
    const version = ++ticket.current;
    try {
      if (typeof source !== 'string' && source.size > importBytes) throw new Error('单次最多导入 8 MiB');
      const graph = parseMcp(typeof source === 'string' ? source : await source.text());
      if (version !== ticket.current) return;
      onLoad(graph); setError(''); setText('');
      setNotice(`已载入 ${graph.nodes.length} 个实体、${graph.links.length} 条关系`);
    } catch(e) { if (version === ticket.current) {setNotice('');setError(e instanceof Error ? e.message : String(e));} }
  }
  return <details><summary>导入 MCP 图谱</summary><div style={{border:'1px dashed #8198b1',padding:16}}
    onDragOver={e=>e.preventDefault()} onDrop={e=>{e.preventDefault();const file=e.dataTransfer.files[0];if(file)void load(file);}}>
    <label>拖入或选择 JSON / JSONL 文件 <input type="file" accept=".json,.jsonl,.ndjson" onChange={e=>{
      const file=e.target.files?.[0];if(file)void load(file);e.target.value='';
    }}/></label>
    <p><textarea aria-label="MCP 图谱文本" placeholder="粘贴 MCP JSON 或每行一条的 JSONL" value={text} onChange={e=>setText(e.target.value)}/></p>
    <button disabled={!text.trim()} onClick={()=>void load(text)}>载入粘贴内容</button>
    <small> 仅在当前页面预览，刷新后清除</small>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
  </div></details>;
}
