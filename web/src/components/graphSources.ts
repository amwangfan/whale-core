import type { GroupDigest, MemoryGraphData, MemoryGraphNode } from '../api';

export const importBytes = 8 * 1024 * 1024;
type Entry = Record<string, unknown>;
const isObject = (v: unknown): v is Entry => typeof v === 'object' && v !== null && !Array.isArray(v);
function field(row: Entry, key: string): string {
  const value = row[key];
  if (typeof value !== 'string' || !value.trim()) throw new Error('缺少有效字段 ' + key);
  return value;
}

export function parseMcp(input: string): MemoryGraphData {
  if (new Blob([input]).size > importBytes) throw new Error('单次最多导入 8 MiB');
  const raw = input.replace(/^\uFEFF/, '').trim();
  if (!raw) throw new Error('请选择文件或粘贴 JSON / JSONL');
  let parsed: unknown;
  try { parsed = JSON.parse(raw); }
  catch {
    parsed = raw.split(/\r?\n/).flatMap((line, i) => {
      if (!line.trim()) return [];
      try { return [JSON.parse(line)]; } catch { throw new Error('JSON 解析失败，第 ' + (i + 1) + ' 行'); }
    });
  }
  let rows: unknown[];
  if (isObject(parsed) && ('entities' in parsed || 'relations' in parsed)) {
    if (!Array.isArray(parsed.entities) || !Array.isArray(parsed.relations ?? [])) throw new Error('entities 和 relations 需要是数组');
    rows = [...parsed.entities, ...(parsed.relations as unknown[] ?? [])];
  } else rows = Array.isArray(parsed) ? parsed : [parsed];
  const nodes = new Map<string,MemoryGraphNode>();
  const links: MemoryGraphData['links'] = [];
  const seenLinks = new Set<string>();
  for (const row of rows) {
    if (!isObject(row)) throw new Error('记录必须为 JSON 对象');
    if (row.type === 'relation' || 'relationType' in row) {
      const link = {source:field(row,'from'),target:field(row,'to'),label:field(row,'relationType')};
      const key = JSON.stringify(link);
      if (!seenLinks.has(key)) links.push(link);
      seenLinks.add(key);
    } else {
      const name = field(row,'name'), entityType = field(row,'entityType');
      const observations = row.observations ?? [];
      if (!Array.isArray(observations) || observations.some(o => typeof o !== 'string')) throw new Error('observations 需要是字符串数组');
      const prior = nodes.get(name);
      if (prior && prior.entityType !== entityType) throw new Error('同名实体的类型冲突：' + name);
      nodes.set(name,{id:name,label:name,category:'mcp',entityType,observations:[...new Set([...(prior?.observations ?? []), ...observations as string[]])]});
    }
  }
  for (const link of links) if (!nodes.has(link.source) || !nodes.has(link.target)) throw new Error('关系引用了未定义实体：' + link.source + ' → ' + link.target);
  return {nodes:[...nodes.values()], links, total_records:nodes.size,shown_records:nodes.size,truncated:false};
}

export function keywordGraph(digest: GroupDigest): MemoryGraphData {
  const counts = new Map<string,number>();
  const pairs = new Map<string,MemoryGraphData['links'][number]>();
  for (const segment of digest.segments) {
    const words = [...new Set(segment.keywords)].sort();
    words.forEach((word,i) => {
      counts.set(word,(counts.get(word) ?? 0) + 1);
      for (const other of words.slice(i+1)) {
        const key = JSON.stringify([word,other]);
        const link = pairs.get(key) ?? {source:word,target:other,label:'同段出现',count:0};
        link.count = (link.count ?? 0) + 1;
        pairs.set(key,link);
      }
    });
  }
  const nodes = [...counts].sort((a,b)=>b[1]-a[1] || a[0].localeCompare(b[0])).map(([word,occurrences])=>({id:word,label:word,category:'keyword',entityType:digest.keyword_types?.[word],occurrences}));
  return {nodes,links:[...pairs.values()],total_records:nodes.length,shown_records:nodes.length,truncated:false};
}

export const traitNames = {expressions:'常用表达',topics:'讨论话题',scenes:'互动方式'};

export function memberGraph(digest: GroupDigest): MemoryGraphData {
  const nodes = new Map<string,MemoryGraphNode>();
  const links: MemoryGraphData['links'] = [];
  const group = 'roster';
  nodes.set(group,{id:group,label:digest.group?.name ?? '本群',category:'space'});
  function addEvidence(parent:string, key:string, refs:string[]) {
    for (const ref of refs) {
      const id='evidence:'+ref;
      if (!nodes.has(id)) nodes.set(id,{id,label:'原话 '+ref.split(':').pop(),category:'evidence',record_key:key,evidence_id:ref});
      links.push({source:parent,target:id,label:'依据'});
    }
  }
  for (const member of digest.members) {
    const person='member:'+member.id;
    nodes.set(person,{id:person,label:member.name,category:'person',observations:member.aliases});
    links.push({source:group,target:person,label:'成员'});
    const profile=member.stored_profile;
    const styles=profile?.traits.filter(t=>t.kind==='expressions') ?? [];
    if (profile && styles.length) {
      const id=person+':style';
      nodes.set(id,{id,label:member.name+'的表达习惯',category:'expressions',
        observations:styles.map(t=>t.label+' · '+t.count+' 条')});
      links.push({source:person,target:id,label:'常用表达'});
      addEvidence(id,profile.key,[...new Set(styles.flatMap(t=>t.evidence_ids))]);
    }
    for (const t of profile?.traits.filter(t=>t.kind!=='expressions') ?? []) {
      const id=JSON.stringify([person,t.kind,t.label]);
      nodes.set(id,{id,label:t.label,category:t.kind,observations:[member.name+' · '+t.count+' 条发言'+(t.days?' · 跨 '+t.days+' 天':'')]});
      links.push({source:person,target:id,label:traitNames[t.kind]});
      addEvidence(id,profile!.key,t.evidence_ids);
    }
    for (const c of member.claims.filter(c=>c.current_preference)) {
      const id='preference:'+c.key;
      nodes.set(id,{id,label:c.summary,category:'preference',key:c.key,status:c.status});
      links.push({source:person,target:id,label:'自述喜好'});
      addEvidence(id,c.key,[c.evidence_id]);
    }
  }
  for (const bot of digest.group?.bots ?? []) {
    const id='tool:'+bot.name;
    nodes.set(id,{id,label:bot.name,category:'tool',observations:bot.aliases});
    links.push({source:group,target:id,label:'工具'});
  }
  return {nodes:[...nodes.values()],links,total_records:nodes.size,shown_records:nodes.size,truncated:false};
}

export function filterGraph(data: MemoryGraphData, search: string, entity: string, relation: string, status: string, limit = 300) {
  const q = search.trim().toLowerCase();
  const eligible = data.nodes.filter(n => (!entity || (n.entityType ?? n.category) === entity) && (!status || n.status === status));
  const ids = new Set(eligible.map(n=>n.id));
  const edges = data.links.filter(l=>ids.has(l.source) && ids.has(l.target) && (!relation || l.label===relation));
  const endpoints = new Set(edges.flatMap(l=>[l.source,l.target]));
  const pool = eligible.filter(n=>!relation || endpoints.has(n.id));
  const matches = pool.filter(n=>!q || [n.label,...(n.observations ?? [])].some(t=>t.toLowerCase().includes(q)));
  const matched = new Set(matches.map(n=>n.id));
  const neighbors = new Set<string>();
  if (q) for (const l of edges) if (matched.has(l.source) || matched.has(l.target) || l.label.toLowerCase().includes(q)) {
    neighbors.add(l.source); neighbors.add(l.target);
  }
  const candidates = [...matches,...pool.filter(n=>!matched.has(n.id) && neighbors.has(n.id))];
  const nodes = candidates.slice(0,limit);
  const shown = new Set(nodes.map(n=>n.id));
  return {nodes,links:edges.filter(l=>shown.has(l.source)&&shown.has(l.target)),total:candidates.length};
}
