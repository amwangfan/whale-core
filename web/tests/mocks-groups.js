/* Mock data for the 2026-11-24 API (docs/API-2026-11-24.md): group memory spaces, evidence, graph
   sources, first-run setup, model presets, network. `buildGroupMock(now)` is pure and shared by the
   smoke/unit tests (merged into MOCK.R below) and the screenshot demo (tests/demo.py). */
function buildGroupMock(now) {
  const GID = '745812309';
  const ID = 'qq:' + GID;
  const SCOPE = 'group:' + ID;
  const min = 60000;
  const members = [
    { name: '阿杰', accounts: ['1234567'], aliases: ['杰哥', 'KP'] },
    { name: '小雨', accounts: ['2345678', '2345679'], aliases: ['雨雨'] },
    { name: '老周', accounts: ['3456789'], aliases: ['周老师'] },
    { name: '柚子', accounts: ['4567890'], aliases: [] },
    { name: '木木', accounts: ['5678901'], aliases: ['木头'] },
    { name: '大熊', accounts: ['6789012'], aliases: ['熊熊'] },
  ];
  const bots = [{ name: '骰娘', accounts: ['9988776'], aliases: ['骰子机器人'] }];
  const nameOf = Object.fromEntries([...members, ...bots].flatMap((m) => m.accounts.map((a) => [a, m.name])));
  // [account, text, minutes ago]
  const raw = [
    ['1234567', '今晚开团吗？上次的克苏鲁模组还差最后一幕', 2930], ['2345678', '开！我超喜欢克苏鲁跑团，这周就盼着这个了', 2928],
    ['3456789', '我九点后有空，先说好我的调查员理智只剩 23 了', 2925], ['9988776', '【骰娘】老周 进行理智检定：1D100=71，失败，失去 1D6 理智', 2920],
    ['3456789', '笑死，骰娘今天又针对我', 2919], ['4567890', '绝了，老周每次检定都失败', 2918], ['1234567', 'KP 表示这是命运的安排', 2916],
    ['6789012', '我晚点到，还在加班', 2905], ['2345678', '熊熊又加班，资本家狠狠爱了', 2903], ['1234567', '那就十点开团，大家先把车卡检查一下', 2900],
    ['4567890', '周六有人去吃火锅吗？新开了一家牛油锅', 1490], ['3456789', '我去，但是我不吃香菜啊，提前说', 1487], ['2345678', '我也去！火锅必须点毛肚', 1485],
    ['6789012', '周六我可以，终于不加班了', 1482], ['4567890', '那就周六晚上六点，我订位', 1479], ['1234567', '绝了，柚子效率太高', 1477],
    ['2345678', '订好了说一声，我来拉个小群', 1470],
    ['5678901', '有人在看葬送的芙莉莲吗？第二季太好哭了', 610], ['2345678', '在看！芙莉莲那集回忆我直接泪目', 606], ['4567890', '我喜欢猫，所以我只关心芙莉莲里那只猫', 603],
    ['5678901', '哪有猫啊哈哈哈', 601], ['3456789', '新番我一般攒着周末看', 598], ['1234567', '笑死，柚子的关注点永远在猫身上', 596],
    ['6789012', '最近在健身，晚上不吃火锅了，改吃沙拉', 300], ['2345678', '熊熊这是要变成瘦熊了吗', 297], ['6789012', '目标是减十斤，你们监督我', 295],
    ['1234567', '我最近在戒奶茶，已经三天没喝了', 292], ['4567890', '阿杰上个月还说你喜欢喝奶茶的', 290], ['1234567', '那是以前，现在改喝无糖茶了', 288],
    ['3456789', '戒奶茶比戒理智检定失败还难', 286],
    ['1234567', '下周的模组我写好了，是个雪山疗养院的故事', 95], ['2345678', '雪山疗养院！听起来就很克苏鲁', 93], ['3456789', '我这次换个调查员，老的那个快疯了', 91],
    ['9988776', '【骰娘】阿杰 发起了新团报名：雪山疗养院（4/5）', 90], ['4567890', '报名！这次我要车一个医生', 88], ['6789012', '我也报，健身完就来', 86],
    ['1234567', '好嘞，周六火锅之后开团，完美的一天', 84], ['2345678', '绝了，这安排我喜欢', 82], ['5678901', '我就不跑团了，围观你们', 80],
    ['3456789', '车卡的时候记得别把理智点太低', 78], ['1234567', '下雨天最适合跑团了', 12], ['2345678', '外面真的在下雨，今晚开团吗？', 10],
    ['4567890', '我喜欢下雨天在家撸猫', 8], ['1234567', '那就今晚开团，九点见', 5],
  ];
  const messages = raw.map(([acc, text, ago], i) => ({
    message_id: `m${String(i + 1).padStart(3, '0')}`, sent_at: now - ago * min, sender_id: acc,
    sender_name: nameOf[acc], display_name: nameOf[acc] === '骰娘' ? '骰娘 Bot' : nameOf[acc], text,
  }));
  const evId = (m) => `qq:group:${GID}:${m.message_id}`;
  const KW = [
    ['跑团', '名词'], ['克苏鲁', '作品名'], ['开团', '动词'], ['理智检定', '词组'], ['车卡', '动词'], ['火锅', '名词'], ['周六', '专名'],
    ['加班', '动词'], ['葬送的芙莉莲', '作品名'], ['芙莉莲', '专名'], ['新番', '名词'], ['猫', '名词'], ['健身', '动词'], ['奶茶', '名词'],
    ['绝了', '原文短语'], ['笑死', '原文短语'], ['模组', '名词'], ['雪山疗养院', '作品名'], ['调查员', '名词'], ['下雨', '动词'], ['香菜', '名词'], ['毛肚', '名词'],
  ];
  // topic segments: a new segment after a 40-minute gap
  const segs = [];
  messages.forEach((m, i) => {
    if (!i || m.sent_at - messages[i - 1].sent_at > 40 * min) segs.push([]);
    segs[segs.length - 1].push(m);
  });
  const segments = segs.map((list, i) => {
    const sources = {};
    KW.forEach(([w]) => { const ids = list.filter((m) => m.text.includes(w)).map((m) => m.message_id); if (ids.length) sources[w] = ids; });
    const keywords = Object.keys(sources).sort((a, b) => sources[b].length - sources[a].length).slice(0, 6);
    return { id: `seg_${i + 1}`, start: list[0].sent_at, end: list[list.length - 1].sent_at, count: list.length,
      members: new Set(list.map((m) => m.sender_id)).size, keywords, keyword_sources: Object.fromEntries(keywords.map((k) => [k, sources[k]])), example_id: list[0].message_id };
  }).reverse();
  const keywords = KW.map(([word, type]) => ({ word, type, segments: segments.filter((s) => s.keywords.includes(word)).length }))
    .filter((k) => k.segments).sort((a, b) => b.segments - a.segments);
  const ofMember = (r) => messages.filter((m) => r.accounts.includes(m.sender_id));
  const ids = (r, word) => ofMember(r).filter((m) => m.text.includes(word)).map(evId);
  const T = (kind, label, r, word) => { const e = ids(r, word || label); return { kind, label, count: Math.max(1, e.length), days: Math.max(1, Math.min(3, e.length)), evidence_ids: e }; };
  const portraits = {
    阿杰: { summary: '群里的 KP（主持人），负责写模组和组织开团；说话干脆，常用「笑死」「绝了」接梗，最近在戒奶茶。',
      traits: (r) => [T('expressions', '笑死', r), T('expressions', '绝了', r), T('topics', '跑团与模组', r, '模组'), T('topics', '开团安排', r, '开团'), T('scenes', '有人起哄时把话题拉回开团', r, '开团')] },
    小雨: { summary: '最积极的跑团玩家，对克苏鲁题材特别上心；热情、爱接话，经常帮大家拉群和张罗。',
      traits: (r) => [T('expressions', '绝了', r), T('topics', '克苏鲁', r), T('topics', '火锅', r), T('scenes', '别人提议时第一个响应', r, '！')] },
    老周: { summary: '调查员总是理智告急的老玩家，自嘲型幽默；吃火锅不吃香菜，新番习惯攒到周末看。',
      traits: (r) => [T('expressions', '笑死', r), T('topics', '理智检定', r, '理智'), T('scenes', '检定失败后自嘲', r, '理智')] },
    柚子: { summary: '行动派，订位、报名都很快；关注点常常在猫身上。',
      traits: (r) => [T('expressions', '绝了', r), T('topics', '猫', r), T('scenes', '把话题绕回猫', r, '猫')] },
    大熊: { summary: '经常加班的上班族，最近开始健身减脂，晚上改吃沙拉。',
      traits: (r) => [T('topics', '加班', r), T('topics', '健身', r), T('scenes', '迟到时先报备', r, '晚')] },
  };
  const claimsOf = {
    小雨: [{ quote: '我超喜欢克苏鲁跑团', content: '小雨喜欢克苏鲁题材的跑团', match: '我超喜欢克苏鲁', current: true }],
    老周: [{ quote: '我不吃香菜', content: '老周不吃香菜', match: '不吃香菜', current: true }],
    柚子: [{ quote: '我喜欢猫', content: '柚子喜欢猫', match: '我喜欢猫', current: true }, { quote: '我喜欢下雨天在家撸猫', content: '柚子喜欢下雨天在家撸猫', match: '下雨天在家撸猫', current: true }],
    阿杰: [{ quote: '我最近在戒奶茶', content: '阿杰在戒奶茶，改喝无糖茶', match: '戒奶茶，已经', current: true },
      { quote: '你喜欢喝奶茶的（转述）', content: '阿杰喜欢喝奶茶', match: '还说你喜欢喝奶茶', current: false }],
    大熊: [{ quote: '晚上不吃火锅了，改吃沙拉', content: '大熊在健身期间晚上吃沙拉', match: '改吃沙拉', current: true }],
  };
  const opted = ['5678901'];
  const digestMembers = members.map((r) => {
    const own = ofMember(r);
    const out = opted.includes(r.accounts[0]);
    const pd = portraits[r.name];
    const memId = `mem_gp_${r.accounts[0]}`;
    const kws = KW.map(([w]) => w).filter((w) => own.some((m) => m.text.includes(w))).slice(0, 6);
    return {
      id: r.accounts[0], accounts: r.accounts, name: r.name, aliases: r.aliases, count: own.length,
      first: own[0]?.sent_at || null, last: own[own.length - 1]?.sent_at || null, keywords: kws, opted_out: out,
      portrait: out || !pd ? null : {
        memory_id: memId, summary: pd.summary, text_count: own.length, active_days: new Set(own.map((m) => new Date(m.sent_at).toDateString())).size,
        median_chars: own.length ? [...own.map((m) => m.text.length)].sort((a, b) => a - b)[Math.floor(own.length / 2)] : 0,
        questions: own.filter((m) => /[？?吗]$/.test(m.text)).length, traits: pd.traits(r),
      },
      claims: out ? [] : (claimsOf[r.name] || []).map((c, i) => {
        const m = own.find((x) => x.text.includes(c.match)) || messages.find((x) => x.text.includes(c.match));
        return { memory_id: `mem_gc_${r.accounts[0]}_${i}`, content: c.content, quote: c.quote, evidence_id: m ? evId(m) : '', sent_at: m ? m.sent_at : now, current: c.current };
      }),
    };
  });
  const group = {
    id: ID, platform: 'qq', group_id: GID, name: '周末跑团小分队', description: '大学同学群，每周六线上跑团，偶尔约火锅。', enabled: true, paused: false,
    auto_refresh_min: 120, members: members.length, bots: bots.length, messages: messages.length, pending: 4, refreshed_at: now - 47 * min, extractor: 'jieba', created_at: now - 12 * 86400000,
  };
  const group2 = {
    id: 'qq:600112233', platform: 'qq', group_id: '600112233', name: '家里人', description: '爸妈和表姐', enabled: true, paused: true,
    auto_refresh_min: 0, members: 4, bots: 0, messages: 312, pending: 0, refreshed_at: now - 6 * 86400000, extractor: 'ngram', created_at: now - 40 * 86400000,
  };
  const digest = { messages: messages.length, keyword_extractor: 'jieba', segments, members: digestMembers, keywords };
  const emptyDigest = { messages: 312, keyword_extractor: 'ngram', segments: [], members: [], keywords: [] };
  const byEvidence = new Map(messages.map((m) => [evId(m), m]));

  // memories created by refresh (portraits = person, claims = preference), with evidence
  const groupMemories = [];
  digestMembers.forEach((m) => {
    if (m.portrait) {
      groupMemories.push({ id: m.portrait.memory_id, kind: 'person', content: `${m.name}：${m.portrait.summary}`, subject: `qq:${m.id}`, status: 'active', importance: 0.7, confidence: 0.85,
        strength: 0.8, source: { group_id: GID, subject_name: m.name, evidence_ids: m.portrait.traits.flatMap((t) => t.evidence_ids).slice(0, 8) }, tags: ['群友画像'], scope: SCOPE,
        created_at: now - 3 * 86400000, updated_at: now - 47 * min, last_access_at: now - 30 * min, access_count: 2, strength_now: 0.78 });
    }
    m.claims.forEach((c) => groupMemories.push({ id: c.memory_id, kind: 'preference', content: c.content, subject: `qq:${m.id}`, status: c.current ? 'active' : 'superseded', importance: 0.55, confidence: 0.9,
      strength: 0.7, source: { group_id: GID, subject_name: m.name, evidence_id: c.evidence_id, quote: c.quote }, tags: ['群友喜好'], scope: SCOPE,
      created_at: c.sent_at, updated_at: now - 47 * min, last_access_at: null, access_count: 0, strength_now: c.current ? 0.66 : 0.3 }));
  });

  const R = {
    'groups.list': () => ({ groups: [group, group2] }),
    'groups.get': (p) => (p.id === group2.id
      ? { group: { ...group2, roster: { members: [{ name: '妈妈', accounts: ['11112222'], aliases: ['老妈'] }, { name: '爸爸', accounts: ['11113333'], aliases: [] }, { name: '表姐', accounts: ['11114444'], aliases: ['姐'] }, { name: '我', accounts: ['10000001'], aliases: [] }], bots: [] }, opted_out: [] }, digest: emptyDigest }
      : { group: { ...group, roster: { members, bots }, opted_out: opted }, digest }),
    'groups.enable': (p) => ({ ...group, id: 'qq:' + p.group_id, group_id: p.group_id, name: p.name, messages: 0, pending: 0, refreshed_at: null }),
    'groups.update': (p) => ({ ...group, ...p }),
    'groups.set_paused': (p) => ({ ...group, paused: !!p.paused }),
    'groups.set_opt_out': (p) => ({ opted_out: p.opted_out ? [...opted, p.account] : opted.filter((a) => a !== p.account) }),
    'groups.import': () => ({ added: 1288, skipped: 37 }),
    'groups.refresh': () => ({ processed: 4, pending: 0, counts: { profiles_added: 0, profiles_updated: 2, preferences_added: 1, preferences_updated: 0, preferences_expired: 1, scanned: 4 } }),
    'groups.messages': (p) => {
      let list = messages;
      if (p.sender) list = list.filter((m) => m.sender_id === p.sender || (members.find((r) => r.accounts.includes(p.sender))?.accounts || []).includes(m.sender_id));
      if (p.segment) { const s = segments.find((x) => x.id === p.segment); list = s ? list.filter((m) => m.sent_at >= s.start && m.sent_at <= s.end) : []; }
      if (p.word) list = list.filter((m) => m.text.includes(p.word));
      const off = p.offset || 0;
      return { items: list.slice(off, off + 50), more: list.length > off + 50 };
    },
    'groups.context': (p) => { const i = messages.findIndex((m) => m.message_id === p.message_id); return { items: i < 0 ? [] : messages.slice(Math.max(0, i - 1), i + 2) }; },
    'groups.delete': (p) => ({ deleted: p.id, memories_forgotten: p.wipe_memories ? groupMemories.length : 0 }),
    'memory.evidence': (p) => {
      const eid = String(p.evidence_id || '');
      if (!eid.startsWith('qq:group:')) return { available: false, reason: 'unsupported_source', message: '这条记忆来自对话，没有群聊原文' };
      const m = byEvidence.get(eid);
      if (!m) return { available: false, reason: 'source_missing', message: '消息不在存档里' };
      return { available: true, evidence_id: eid, group_id: GID, message_id: m.message_id, sender_id: m.sender_id, sender_name: m.sender_name, sent_at: m.sent_at, text: m.text, note: '' };
    },
  };

  /** extra graph nodes/edges for a set of memory nodes (memory.graph {sources:true}) */
  function graphSources(nodes) {
    const extra = new Map(); const edges = [];
    const add = (n) => { if (!extra.has(n.id)) extra.set(n.id, n); };
    nodes.forEach((m) => {
      const sc = m.scope || 'owner';
      if (sc.startsWith('group:')) {
        const sid = `space:${sc}`;
        add({ id: sid, label: sc === SCOPE ? group.name : sc, category: 'space', scope: sc });
        edges.push({ src: sid, dst: m.id, relation: '包含' });
      }
      if (m.subject && m.subject.startsWith('qq:')) {
        const pid = `person:${sc}:${m.subject}`;
        add({ id: pid, label: nameOf[m.subject.slice(3)] || m.subject, category: 'person', scope: sc, subject: m.subject });
        edges.push({ src: m.id, dst: pid, relation: '主体' });
      }
      const evs = [...(m.source?.evidence_ids || []), ...(m.source?.evidence_id ? [m.source.evidence_id] : [])].slice(0, 2);
      evs.forEach((e) => {
        const msg = byEvidence.get(e);
        add({ id: `evidence:${e}`, label: msg ? `${msg.sender_name}：${msg.text.slice(0, 18)}` : e, category: 'evidence', memory_id: m.id, evidence_id: e });
        edges.push({ src: m.id, dst: `evidence:${e}`, relation: '依据' });
      });
    });
    return { extra_nodes: [...extra.values()], extra_edges: edges };
  }

  const setupIncomplete = { completed: false, steps: { model: false, persona: true, phone: false, qq: false }, models_offline: true };
  const network = { host: '127.0.0.1', port: 8080, lan_enabled: false, addresses: ['192.168.1.23'], urls: ['http://192.168.1.23:8080'], restart_required: false };
  const networkOn = { host: '0.0.0.0', port: 8080, lan_enabled: true, addresses: ['192.168.1.23', '10.0.0.8'], urls: ['http://192.168.1.23:8080', 'http://10.0.0.8:8080'], restart_required: false };
  const presets = [
    { id: 'deepseek', name: 'DeepSeek', base_url: 'https://api.deepseek.com/v1', chat_model: 'deepseek-chat', fast_model: 'deepseek-chat', key_url: 'https://platform.deepseek.com/api_keys', needs_key: true, local: false, note: '中文好、价格低，推荐新手首选' },
    { id: 'dashscope', name: '通义千问', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', chat_model: 'qwen-plus', fast_model: 'qwen-turbo', vision_model: 'qwen-vl-plus', embedding_model: 'text-embedding-v3', key_url: 'https://bailian.console.aliyun.com/?apiKey=1', needs_key: true, local: false, note: '阿里云百炼，支持看图和语义检索' },
    { id: 'moonshot', name: 'Kimi', base_url: 'https://api.moonshot.cn/v1', chat_model: 'kimi-k2-0905-preview', fast_model: 'moonshot-v1-8k', key_url: 'https://platform.moonshot.cn/console/api-keys', needs_key: true, local: false, note: '月之暗面，长文本与工具调用表现好' },
    { id: 'zhipu', name: '智谱 GLM', base_url: 'https://open.bigmodel.cn/api/paas/v4', chat_model: 'glm-4.5', fast_model: 'glm-4.5-flash', vision_model: 'glm-4v-flash', key_url: 'https://open.bigmodel.cn/usercenter/apikeys', needs_key: true, local: false, note: '有免费的 Flash 模型可以先试' },
    { id: 'siliconflow', name: '硅基流动', base_url: 'https://api.siliconflow.cn/v1', chat_model: 'Qwen/Qwen2.5-72B-Instruct', fast_model: 'Qwen/Qwen2.5-7B-Instruct', embedding_model: 'BAAI/bge-m3', key_url: 'https://cloud.siliconflow.cn/account/ak', needs_key: true, local: false, note: '一个 Key 用多家开源模型' },
    { id: 'openai', name: 'OpenAI', base_url: 'https://api.openai.com/v1', chat_model: 'gpt-4o-mini', fast_model: 'gpt-4o-mini', vision_model: 'gpt-4o-mini', embedding_model: 'text-embedding-3-small', key_url: 'https://platform.openai.com/api-keys', needs_key: true, local: false, note: '需要能访问 OpenAI 的网络' },
    { id: 'ollama', name: 'Ollama（本机）', base_url: 'http://127.0.0.1:11434/v1', chat_model: 'qwen2.5:7b', fast_model: 'qwen2.5:3b', embedding_model: 'nomic-embed-text', needs_key: false, local: true, note: '完全离线，在自己电脑上运行，需要较好的显卡' },
  ];
  Object.assign(R, {
    'setup.status': () => ({ ...setupIncomplete, network }),
    'models.presets': () => ({ presets }),
    'setup.quick_model': (p) => ({ ok: true, test: { ok: true, model: (presets.find((x) => x.id === p.preset) || presets[0]).chat_model, reply: '你好！我是鲸鱼，很高兴认识你。', latency_ms: 842 }, status: {} }),
    'setup.complete': () => ({ completed: true }),
    'system.network': () => network,
    'system.set_lan': (p) => ({ ...network, lan_enabled: !!p.enabled, host: p.enabled ? '0.0.0.0' : '127.0.0.1', restart_required: true }),
    'system.restart': () => ({ restarting: true }),
    'devices.pair_start': () => ({ code: '482913', expires_at: now + 540000, ttl_s: 600 }),
  });
  return { R, group, group2, digest, messages, members, bots, groupMemories, graphSources, presets, network, networkOn, setupIncomplete, SCOPE, evId };
}

if (typeof MOCK !== 'undefined') {
  MOCK.G = buildGroupMock(Date.now());
  Object.assign(MOCK.R, MOCK.G.R);
  // a memory carrying evidence ids (memory detail 依据 section)
  MOCK.memories.push(...MOCK.G.groupMemories.slice(0, 3));
  const baseGraph = MOCK.R['memory.graph'];
  MOCK.R['memory.graph'] = (p = {}) => {
    const g = baseGraph(p);
    const nodes = p.scope ? g.nodes.filter((m) => (p.scope === 'group' ? (m.scope || '').startsWith('group:') : p.scope === 'owner' ? (m.scope || 'owner') === 'owner' : true)) : g.nodes;
    const res = { nodes, edges: g.edges, total: nodes.length, truncated: false, extra_nodes: [], extra_edges: [] };
    return p.sources ? { ...res, ...MOCK.G.graphSources(nodes) } : res;
  };
}
