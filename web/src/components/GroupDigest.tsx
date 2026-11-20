import { useEffect, useState } from 'react';
import { studioApi, type GroupDigest as Digest, type ArchiveSpeech, type MemberPortrait, type PortraitExample, type StoredGroupProfile } from '../api';
import MemoryEvidence from './MemoryEvidence';
import {traitNames} from './graphSources';

const time = (stamp:string) => new Date(stamp).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false});

function Examples({rows,gid}:{rows:PortraitExample[];gid:string}) {
  const [open,setOpen] = useState('');
  return <ul>{rows.map(r=><li key={r.message_id}><time>{time(r.sent_at)}</time> · {r.sender_id} · #{r.message_id}
    <p style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{r.text}</p>
    <button onClick={()=>setOpen(open===r.message_id?'':r.message_id)}>查看前后文</button>
    {r.reply_to && <button onClick={()=>setOpen(open===r.reply_to?'':r.reply_to)}>查看被回复的消息</button>}
    {open && (open===r.message_id || open===r.reply_to) && <Transcript key={open} gid={gid} around={open}/>}
  </li>)}</ul>;
}

function Portrait({p,gid}:{p:MemberPortrait;gid:string}) {
  return <section><h4>群内语言画像</h4>
    <p>本人文字 {p.text_count} 条，分布在 {p.active_days} 天。单条中位长度 {p.median_chars ?? '—'} 字符，
      {p.no_end_punct} 条以文字或数字结尾，{p.questions} 条以问号结尾。</p>
    <h5>常用表达</h5>
    {p.expressions.map(x=><details key={x.term}><summary>{x.term} · 本人 {x.count}/{p.text_count} 条，
      跨 {x.days} 天 · 其他成员 {x.other_count}/{x.other_total} 条</summary><Examples rows={x.examples} gid={gid}/></details>)}
    <h5>反复聊到的话题</h5>
    {p.topics?.map(x=><details key={x.term}><summary>{x.term} · {x.count} 条，跨 {x.days} 天</summary><Examples rows={x.examples} gid={gid}/></details>)}
    <h5>不同场景下的说法</h5>
    {p.scenes.map(x=><details key={x.label}><summary>{x.label} · {x.count} 条符合句式</summary><Examples rows={x.examples} gid={gid}/></details>)}
    {p.self_report_candidates.length>0 && <details><summary>待核对的第一人称自述</summary><Examples rows={p.self_report_candidates} gid={gid}/></details>}
  </section>;
}

function StoredPortrait({profile,gid,original}:{profile:StoredGroupProfile;gid:string;original?:MemberPortrait|null}) {
  const [selected,setSelected] = useState('');
  const [around,setAround] = useState('');
  const mid=selected.split(':').pop() ?? '';
  const reply=original && [...original.expressions,...(original.topics??[]),...original.scenes]
    .flatMap(t=>t.examples).find(e=>e.message_id===mid)?.reply_to;
  return <section><h5>群内画像观察</h5>
    <p>{profile.active_days} 天内的 {profile.text_count} 条本人文字，单条中位长度 {profile.median_chars ?? '—'} 字符。</p>
    {(['topics','expressions','scenes'] as const).map(kind=><details key={kind}>
      <summary>{traitNames[kind]}（{profile.traits.filter(t=>t.kind===kind).length}）</summary>
      {profile.traits.filter(t=>t.kind===kind).map(t=><div key={t.label}>
        <strong>{t.label}</strong> · {t.count} 条{t.days ? '，跨 '+t.days+' 天':''}
        {t.other_total !== undefined && <> · 其他成员 {t.other_count}/{t.other_total} 条</>}
        <div>{t.evidence_ids.map((ref,i)=><button key={ref} onClick={()=>{setSelected(selected===ref?'':ref);setAround('');}}>原话 {i+1}</button>)}</div>
        {t.evidence_ids.includes(selected) && <><MemoryEvidence recordKey={profile.key} evidenceId={selected}/>
          <button onClick={()=>setAround(around===mid?'':mid)}>查看前后文</button>
          {reply && <button onClick={()=>setAround(around===reply?'':reply)}>查看被回复的消息</button>}
          {around && <Transcript key={around} gid={gid} around={around}/>}</>}
      </div>)}
    </details>)}</section>;
}

export function Transcript({gid, sender='', segment='', word='', around=''}: {gid:string; sender?:string; segment?:string; word?:string;around?:string}) {
  const [page,setPage] = useState(0);
  const [data,setData] = useState<{items:ArchiveSpeech[];more:boolean} | null>(null);
  const [error,setError] = useState('');
  useEffect(() => {
    let current = true;
    setData(null); setError('');
    studioApi.groupSpeech({group_id:gid,sender_id:sender,segment_id:segment,word,around,offset:String(page*50)})
      .then(v => {if(current) setData(v);}).catch(e => {if(current) setError(String(e));});
    return () => {current=false;};
  },[gid,sender,segment,word,around,page]);
  if(error) return <p role="alert">{error}</p>;
  if(!data) return <p>正在读取发言…</p>;
  return <div><ol start={page*50+1}>{data.items.map(m => <li key={m.message_id}>
    <strong>{m.display_name || m.sender_name || m.sender_id}</strong>{m.display_name && m.display_name!==m.sender_name && m.sender_name && <>（归档昵称 {m.sender_name}）</>} · <time>{time(m.sent_at)}</time>
    {around===m.message_id && <strong> · 选中原话</strong>}
    <p style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{m.text}</p>
  </li>)}</ol>{!data.items.length && <p>没有匹配发言</p>}
  {!around && <><button disabled={!page} onClick={()=>setPage(page-1)}>上一页发言</button>
  <button disabled={!data.more} onClick={()=>setPage(page+1)}>下一页发言</button></>}</div>;
}

export default function GroupDigest({gid,onUpdated}:{gid:string;onUpdated?:()=>void}) {
  const [data,setData] = useState<Digest|null>(null);
  const [error,setError] = useState('');
  const [mode,setMode] = useState('members');
  const [selected,setSelected] = useState('');
  const [word,setWord] = useState('');
  const [filter,setFilter] = useState('');
  const [page,setPage] = useState(0);
  const [claim,setClaim] = useState('');
  const [updating,setUpdating] = useState(false);
  const [notice,setNotice] = useState('');
  async function update() {
    setUpdating(true);
    setNotice('');
    try {
      const result=await studioApi.updateGroupMemory(gid);
      setNotice(result.processed ? '已处理 '+result.processed+' 条新发言' : '没有待处理的新发言');
      if(result.processed) onUpdated?.();
      setData(await studioApi.groupDigest(gid));
    } catch(e) {
      setNotice(String(e));
    } finally {
      setUpdating(false);
    }
  }
  useEffect(()=>{
    let current=true;
    studioApi.groupDigest(gid).then(v=>{if(current)setData(v);}).catch(e=>{if(current)setError(String(e));});
    return ()=>{current=false;};
  },[gid]);
  if(error) return <p role="alert">{error}</p>;
  if(!data) return <p>正在加载群聊索引…</p>;
  const choose=(id:string)=>{setSelected(id);setWord('');setClaim('');};
  const person=data.members.find(m=>m.id===selected);
  const segments=data.segments.filter(s=>s.keywords.join(' ').includes(filter.trim()));
  return <section className="memory-browse" aria-label="群成员喜好与画像"><h3>群成员喜好与画像</h3>
    {data.pending_messages != null && <div>
      <button onClick={update} disabled={updating}>{updating?'正在更新…':'更新群记忆'}</button>
      <span> · 上次查看时有 {data.pending_messages} 条待处理发言</span>
      <p>群聊告一段落后点击，统一更新喜好、语言画像和关键词。</p>
      <span role="status">{notice}</span>
    </div>}
    {data.group && <><h4>{data.group.name}</h4><p>{data.group.description}</p>
      <p>{data.group.people} 位成员：{data.members.map(m=>m.name).join('、')}</p>
      <p>{data.group.bots.map(b=>b.name+'（'+b.aliases.join('、')+'，共 '+b.count+' 条）').join('、')}</p></>}
    <p>{data.messages} 条归档发言 · {data.segments.length} 段对话 · {data.group?.archived_accounts ?? data.members.length} 个归档发送标识</p>
    <button aria-pressed={mode==='segments'} onClick={()=>{setMode('segments');choose('');}}>按对话看</button>
    <button aria-pressed={mode==='members'} onClick={()=>{setMode('members');choose('');}}>按群成员看</button>
    {mode==='segments' ? <><p>间隔 20 分钟或满 80 条分段。{data.keyword_extractor ? '关键词来自群友原话中的名词、动词、作品名和短语。' : '关键词从本段文字提取。'}</p>
      <input placeholder="筛选对话关键词" value={filter} onChange={e=>{setFilter(e.target.value);setPage(0);choose('');}}/>
      <ul>{segments.slice(page*10,page*10+10).map(s=><li key={s.id}><button onClick={()=>choose(selected===s.id?'':s.id)}>{s.keywords.join(' · ') || '无可提取关键词'}</button>
        <small> {time(s.start)} 至 {time(s.end)} · {s.count} 条 · {s.members} 个账号</small>
        {selected===s.id && <Transcript key={s.id} gid={gid} segment={s.id}/>}</li>)}</ul>
      <button disabled={!page} onClick={()=>setPage(page-1)}>上一页对话</button>
      <button disabled={(page+1)*10>=segments.length} onClick={()=>setPage(page+1)}>下一页对话</button>
    </> : <><select aria-label="选择群成员" value={selected} onChange={e=>choose(e.target.value)}><option value="">选择群成员</option>
      {data.members.map(m=><option key={m.id} value={m.id}>{m.name || m.id}（{m.id}）· {m.count} 条</option>)}</select>
      {person && <><h4>{person.name || person.id}</h4>
        {person.stored_profile && <p>已写入群画像 · {person.stored_profile.traits.length} 项观察，依据 {person.stored_profile.text_count} 条本人文字</p>}
        <h5>本人明确表达的当前喜好</h5>
        {!person.claims.some(c=>c.current_preference) && <p>暂无已核对来源的明确喜好。</p>}
        {person.claims.filter(c=>c.current_preference).map(c=><details key={c.key}><summary>{c.summary}</summary>
          <MemoryEvidence recordKey={c.key} evidenceId={c.evidence_id}/></details>)}
        <p>称呼与别名：{person.aliases.join('、')}{person.first && <> · {time(person.first)} 至 {time(person.last)}</>}</p>
        {person.stored_profile ? <StoredPortrait key={person.id} profile={person.stored_profile} gid={gid} original={person.portrait}/> :
          person.portrait && <Portrait key={person.id} p={person.portrait} gid={gid}/>}
        <p>发言关键词（按词查看原话）</p><button aria-pressed={!word} onClick={()=>setWord('')}>全部发言</button>
        {person.keywords.map(w=><button aria-pressed={word===w} key={w} onClick={()=>setWord(w)}>{w}</button>)}
        <details><summary>其他自述与历史记录（{person.claims.filter(c=>!c.current_preference).length}）</summary>
        <ul>{person.claims.filter(c=>!c.current_preference).map(c=><li key={c.key}><button onClick={()=>setClaim(claim===c.key?'':c.key)}>{c.summary}</button> · {c.status}{c.expires_at && new Date(c.expires_at).getTime()<=Date.now()?' · 已过期':''}
          {claim===c.key && <MemoryEvidence recordKey={c.key} evidenceId={c.evidence_id}/>}</li>)}</ul></details>
        <h4>{word?'含「'+word+'」的发言':'按时间排列的发言'}</h4>
        <Transcript key={person.id+':'+word} gid={gid} sender={person.id} word={word}/>
      </>}</>}
  </section>;
}
