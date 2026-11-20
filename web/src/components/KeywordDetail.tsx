import { useState } from 'react';
import type { ArchiveSegment } from '../api';
import { Transcript } from './GroupDigest';

export default function KeywordDetail({word, gid, segments}:{word:string;gid:string;segments:ArchiveSegment[]}) {
  const matches = segments.filter(s=>s.keywords.includes(word));
  const [choice,setChoice] = useState('');
  const [all,setAll] = useState(false);
  const current = matches.find(s=>s.id===choice) ?? matches[0];
  if (!current) return null;
  return <section aria-label="关键词相关群聊">
    <p>「{word}」是 {matches.length} 段群聊的关键词</p>
    <label>选择对话 <select value={current.id} onChange={e=>setChoice(e.target.value)}>
      {matches.map(s=><option value={s.id} key={s.id}>{new Date(s.start).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})} · {s.count} 条</option>)}
    </select></label>
    <p>{current.members} 个账号参与 · {current.keywords.join(' / ')}</p>
    <label><input type="checkbox" checked={all} onChange={e=>setAll(e.target.checked)}/> 展开整段上下文</label>
    <Transcript key={JSON.stringify([gid,word,current.id,all])} gid={gid} segment={current.id} word={all?'':word}/>
  </section>;
}
