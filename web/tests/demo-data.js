/* Screenshot demo data: a lived-in Whale instance (agent 鲸鱼, owner 林舟). Shapes follow the real
   backend (see tests/mocks.js for the field-by-field reference). Used only by tests/demo.py.
   buildDemo(now) → {call(method, params, emit) → result | Promise, activity: [...]}; state is mutable so
   actions in the demo (adopt a candidate, send a chat, refresh a group…) behave plausibly. */
function buildDemo(now) {
  const H = 3600000; const D = 86400000; const M = 60000;
  const G = buildGroupMock(now);
  const AGENT_PHONE = 'a7f3c9e1b2d4e6f8091a2b3c4d5e6f70';
  const DAILY_PHONE = '5e8d2c1b0a9f8e7d6c5b4a3928170615';
  let seq = 100;
  const id = (p) => `${p}_${(++seq).toString(36)}${Math.random().toString(36).slice(2, 6)}`;

  // -- memories --------------------------------------------------------------------------------------
  const mem = (mid, kind, content, x = {}) => ({
    id: mid, kind, content, subject: 'user', status: 'active', importance: 0.6, confidence: 0.85, strength: 0.7,
    source: { conversation_id: 'conv_cat', run_id: 'run_cat', channel: 'console' }, tags: [], scope: 'owner',
    created_at: now - 9 * D, updated_at: now - 2 * D, last_access_at: now - 5 * H, access_count: 4, strength_now: 0.68, ...x,
  });
  const quote = (q, x = {}) => ({ source: { conversation_id: 'conv_cat', run_id: 'run_cat', channel: 'console', quote: q }, ...x });
  const memories = [
    mem('m_name', 'fact', '用户叫林舟，在杭州一家互联网公司做产品经理', { importance: 0.92, access_count: 31, strength_now: 0.95, tags: ['身份'], ...quote('我叫林舟，在杭州做产品'), created_at: now - 58 * D }),
    mem('m_cat', 'fact', '用户养了一只叫年糕的橘猫，三岁', { importance: 0.82, access_count: 18, strength_now: 0.9, tags: ['宠物'], ...quote('我家年糕三岁了，是只橘猫') }),
    mem('m_peanut', 'fact', '用户对花生过敏', { importance: 0.97, access_count: 9, strength_now: 0.93, tags: ['健康'], ...quote('我花生过敏，点外卖要注意'), created_at: now - 40 * D }),
    mem('m_swim', 'fact', '用户每周二、四晚上去游泳', { importance: 0.6, tags: ['运动'], ...quote('我现在改成周二周四晚上游泳了') }),
    mem('m_bday', 'fact', '用户的生日是 3 月 14 日', { importance: 0.86, access_count: 3, tags: ['重要日子'], ...quote('我生日是 3 月 14 号，圆周率日') }),
    mem('m_home', 'fact', '用户住在西湖区，通勤骑电动车，大约 20 分钟', { importance: 0.5 }),
    mem('m_jp', 'fact', '用户在学日语，目标是明年 7 月考 N2', { importance: 0.78, access_count: 12, tags: ['学习'], ...quote('我打算明年七月考 N2') }),
    mem('m_tea', 'preference', '用户喜欢喝无糖乌龙茶', { importance: 0.6, ...quote('无糖乌龙茶是我的续命水') }),
    mem('m_cilantro', 'preference', '用户不吃香菜', { importance: 0.72, ...quote('香菜就算了，我不吃') }),
    mem('m_sleep', 'preference', '周末喜欢睡到自然醒，上午 9 点前不要主动打扰', { importance: 0.76, tags: ['作息'], ...quote('周末九点前别叫我') }),
    mem('m_brief', 'preference', '用户喜欢简洁的回答：先给结论，再给理由', { importance: 0.9, access_count: 44, strength_now: 0.97, ...quote('说重点，先给结论') }),
    mem('m_ghibli', 'preference', '用户喜欢宫崎骏的电影，最爱《千与千寻》', { importance: 0.5 }),
    mem('m_wechat', 'preference', '比起电话，用户更喜欢用微信文字联系', { importance: 0.48 }),
    mem('m_vet', 'episode', '11-23 用户带年糕去宠物医院打了三联疫苗，医生说要控制体重', { importance: 0.55, created_at: now - 1 * D, updated_at: now - 1 * D }),
    mem('m_listen', 'episode', '11-22 21:10–21:50 用户在手机上做了 40 分钟 N2 听力练习', { importance: 0.45, created_at: now - 2 * D,
      source: { device_id: DAILY_PHONE, device: '日常机', role: 'personal', span: '11-22 21:10–21:50', event_ids: ['ev_l1', 'ev_l2', 'ev_l3'], from: '手机' } }),
    mem('m_release', 'episode', '11-21 用户加班到 22 点，新版本顺利发布', { importance: 0.42, created_at: now - 3 * D }),
    mem('m_hotpot', 'episode', '11-20 用户和大学同学在湖滨吃了火锅', { importance: 0.4, created_at: now - 4 * D }),
    mem('m_selfweather', 'episode', '我在自己的手机上帮林舟查了周末杭州的天气和展览', { importance: 0.4, subject: 'self', scope: 'shared', created_at: now - 6 * H,
      source: { device_id: AGENT_PHONE, device: '鲸鱼的手机', role: 'agent', span: '今天 09:12–09:20', event_ids: ['ev_a1', 'ev_a2'], from: '手机' } }),
    mem('m_lilei', 'person', '李雷是用户的大学室友，现在在上海做设计', { importance: 0.62 }),
    mem('m_mom', 'person', '妈妈每周日晚上会打视频电话', { importance: 0.74, tags: ['家人'] }),
    mem('m_boss', 'person', '王经理是用户的直属领导，每周一早上 10 点开周会', { importance: 0.66, tags: ['工作'] }),
    mem('m_k_lake', 'knowledge', '西湖工作日傍晚游客最少，适合散步', { scope: 'shared', subject: 'self', importance: 0.4, source: { autonomy: true, topic: '杭州', url: 'https://www.hangzhou.gov.cn/' } }),
    mem('m_k_cat', 'knowledge', '成年家猫每天约需 60–70 kcal/kg，橘猫更容易发胖', { scope: 'shared', subject: 'self', importance: 0.55, source: { autonomy: true, topic: '养猫', url: 'https://www.wsava.org/' } }),
    mem('m_k_n2', 'knowledge', '日语 N2 听力约 50 分钟，共 5 个大题', { scope: 'shared', subject: 'self', importance: 0.56, source: { autonomy: true, topic: '日语', url: 'https://www.jlpt.jp/' } }),
    mem('m_c_whale', 'knowledge', '一头鲸的遗骸（鲸落）能维持深海生态系统长达数十年', { scope: 'shared', subject: 'self', status: 'candidate', importance: 0.5, created_at: now - 3 * H,
      source: { autonomy: true, topic: '深海', url: 'https://ocean.si.edu/' } }),
    mem('m_c_py', 'knowledge', 'Python 3.14 的自由线程构建仍是可选安装', { scope: 'shared', subject: 'self', status: 'candidate', importance: 0.45, created_at: now - 20 * H,
      source: { autonomy: true, topic: '编程', url: 'https://docs.python.org/3.14/whatsnew/3.14.html' } }),
    mem('m_diary1', 'diary', '今天读了关于鲸落的文章：一头鲸的死亡能养活一整片深海。我有点喜欢这个名字了。', { scope: 'shared', subject: 'self', importance: 0.5, created_at: now - 3 * H, source: { autonomy: true, topic: '深海' } }),
    mem('m_diary2', 'diary', '和林舟聊日语时我发现，他更喜欢练听力而不是背单词。', { scope: 'shared', subject: 'self', importance: 0.45, created_at: now - 2 * D }),
    mem('m_skill_report', 'skill', '整理周报：先列完成项，再写风险，最后是下周计划', { scope: 'shared', subject: 'self', importance: 0.7, access_count: 6 }),
    mem('m_skill_remind', 'skill', '处理提醒请求时，先确认时间、时区和是否重复，再创建调度', { scope: 'shared', subject: 'self', importance: 0.66 }),
    mem('m_style1', 'style', '说话偶尔用「嗯哼」开头', { scope: 'shared', subject: 'self', importance: 0.5, source: { quote: '嗯哼，这个我记得' } }),
    mem('m_style2', 'style', '给建议时习惯说「可以先试试……」', { scope: 'shared', subject: 'self', importance: 0.5, source: { quote: '可以先试试每天十分钟听力' } }),
    mem('m_zs1', 'fact', '张三想请鲸鱼帮忙改简历', { scope: 'qq:88001122', subject: 'qq:88001122', importance: 0.5, source: { channel: 'qq', conversation_id: 'conv_zs' } }),
    mem('m_zs2', 'preference', '张三喜欢打羽毛球', { scope: 'qq:88001122', subject: 'qq:88001122', importance: 0.4, source: { channel: 'qq', conversation_id: 'conv_zs', quote: '我周末一般打羽毛球' } }),
    mem('m_old_cat', 'fact', '用户养了一只猫', { status: 'superseded', importance: 0.4, strength_now: 0.3 }),
    mem('m_old_swim', 'fact', '用户每周一、三游泳', { status: 'superseded', importance: 0.4, strength_now: 0.25 }),
    mem('m_old_doc', 'episode', '11-02 用户在地铁上看了一部关于深海的纪录片', { status: 'archived', importance: 0.2, strength_now: 0.08, created_at: now - 22 * D }),
    ...G.groupMemories,
  ];
  const link = (src, dst, relation = 'related', weight = 0.5) => ({ src, dst, relation, weight, created_at: now - D });
  const links = [
    link('m_cat', 'm_old_cat', 'supersedes', 0.9), link('m_swim', 'm_old_swim', 'supersedes', 0.9), link('m_cat', 'm_vet', 'related', 0.7), link('m_cat', 'm_k_cat', 'related', 0.6),
    link('m_vet', 'm_k_cat', 'related', 0.5), link('m_jp', 'm_listen', 'related', 0.7), link('m_jp', 'm_k_n2', 'related', 0.6), link('m_diary2', 'm_jp', 'derived_from', 0.6),
    link('m_cilantro', 'm_hotpot', 'related', 0.4), link('m_lilei', 'm_hotpot', 'related', 0.5), link('m_diary1', 'm_c_whale', 'derived_from', 0.7), link('m_name', 'm_boss', 'related', 0.4),
    link('m_name', 'm_release', 'related', 0.4), link('m_sleep', 'm_k_lake', 'related', 0.3), link('m_brief', 'm_skill_report', 'related', 0.5), link('m_old_doc', 'm_diary1', 'related', 0.3),
    link('m_selfweather', 'm_k_lake', 'related', 0.5), link('m_zs1', 'm_zs2', 'related', 0.5), link('m_mom', 'm_name', 'related', 0.3), link('m_peanut', 'm_hotpot', 'related', 0.3),
    link('mem_gp_2345678', 'mem_gc_2345678_0', 'related', 0.6), link('mem_gp_3456789', 'mem_gc_3456789_0', 'related', 0.6), link('mem_gp_1234567', 'mem_gc_1234567_0', 'related', 0.6),
    link('mem_gc_1234567_0', 'mem_gc_1234567_1', 'supersedes', 0.8), link('mem_gp_4567890', 'mem_gc_4567890_0', 'related', 0.6), link('m_tea', 'mem_gc_1234567_0', 'related', 0.2),
  ];
  const scopeCat = (s) => (!s || s === 'owner' ? 'owner' : s === 'shared' ? 'shared' : s.startsWith('group:') ? 'group' : 'person');
  const stats = () => {
    const live = memories.filter((m) => m.status !== 'forgotten');
    const count = (f) => live.reduce((o, m) => { const k = f(m); o[k] = (o[k] || 0) + 1; return o; }, {});
    const act = live.filter((m) => m.status === 'active');
    return { by_kind: count((m) => m.kind), by_status: count((m) => m.status), by_scope: act.reduce((o, m) => { const k = scopeCat(m.scope); o[k] = (o[k] || 0) + 1; return o; }, {}),
      links: links.length, active: act.length, candidates: live.filter((m) => m.status === 'candidate').length, queue_processed: 214 };
  };

  // -- conversations, runs, steps -----------------------------------------------------------------------
  const step = (rid, n, kind, title, input, output, t0, dur, status = 'done') => ({ id: `${rid}_s${n}`, run_id: rid, seq: n, kind, title, status, input, output, started_at: t0, ended_at: t0 + dur });
  const T = now - 26 * M;
  const runs = {
    run_cat: { run: { id: 'run_cat', conversation_id: 'conv_cat', parent_id: null, task_id: null, mode: 'chat', status: 'completed', input: '年糕最近好像胖了，晚饭该喂多少？', output: '先给结论：…',
      error: '', channel: 'console', model: 'deepseek-chat', created_at: T, ended_at: T + 6200, stats: { latency_ms: 6200, memories: 3, tools: 1 } },
    steps: [
      step('run_cat', 1, 'recall', '回忆到 3 条记忆', { query: '年糕 胖 喂食' }, { memories: [
        { id: 'm_cat', kind: 'fact', content: '用户养了一只叫年糕的橘猫，三岁', score: 0.91, why: ['关键词', '语义', '重要'] },
        { id: 'm_vet', kind: 'episode', content: '11-23 用户带年糕去宠物医院打了三联疫苗，医生说要控制体重', score: 0.84, why: ['语义', '近期'] },
        { id: 'm_k_cat', kind: 'knowledge', content: '成年家猫每天约需 60–70 kcal/kg，橘猫更容易发胖', score: 0.77, why: ['语义'] }] }, T + 80, 140),
      step('run_cat', 2, 'think', '决定先查一下猫粮热量', { turn: 1, model: 'deepseek-chat' }, { latency_ms: 1180, decision: 'tools', tools: ['web.search'], text: '', reasoning: '需要常见猫粮的热量范围来换算克数' }, T + 230, 1180),
      step('run_cat', 3, 'tool', '网页搜索', { capability: 'web.search', args: { query: '成猫 干粮 热量 kcal/100g' } }, { status: 'ok', data: { results: [] }, summary: '找到 6 条结果：常见干粮 350–420 kcal/100g', error: '', latency_ms: 1630 }, T + 1420, 1630),
      step('run_cat', 4, 'think', '整理回答', { turn: 2, model: 'deepseek-chat' }, { latency_ms: 2900, decision: 'reply', text: '', reasoning: '' }, T + 3060, 2900),
      step('run_cat', 5, 'reply', '回复', {}, { text: '' }, T + 5990, 10),
    ], text: '', plan: [], progress: '', children: [] },
  };
  const msg = (cid, role, content, ts, x = {}) => ({ id: id('msg'), conversation_id: cid, role, content, run_id: x.run_id || null, ts, meta: x.meta || {} });
  const conversations = [
    { id: 'conv_cat', title: '年糕的晚饭', channel: 'console', peer: 'owner', persona_id: null, created_at: T, updated_at: T + 6200, turns: 2, meta: {} },
    { id: 'conv_jp', title: 'N2 复习计划', channel: 'console', peer: 'owner', persona_id: null, created_at: now - 2 * D, updated_at: now - 2 * D + 20 * M, turns: 6, meta: {} },
    { id: 'conv_qq', title: 'QQ 私聊', channel: 'qq', peer: 'owner', persona_id: null, created_at: now - 5 * D, updated_at: now - 3 * H, turns: 14, meta: {} },
    { id: 'conv_grp', title: '周末跑团小分队', channel: 'qq', peer: G.SCOPE, persona_id: null, created_at: now - 12 * D, updated_at: now - 40 * M, turns: 23, meta: {} },
    { id: 'conv_trip', title: '周末去哪儿', channel: 'mobile', peer: 'owner', persona_id: null, created_at: now - 7 * H, updated_at: now - 6 * H, turns: 3, meta: {} },
  ];
  const messages = {
    conv_cat: [
      msg('conv_cat', 'user', '年糕前两天在医院称了一下，6.2 公斤，医生说有点胖了。年糕最近好像胖了，晚饭该喂多少？', T - 60000),
      msg('conv_cat', 'user', '年糕最近好像胖了，晚饭该喂多少？', T, { run_id: 'run_cat' }),
      msg('conv_cat', 'assistant', '先给结论：**年糕每天大约 55–60 克干粮**，晚饭占一半就是 **28 克左右**。\n\n理由：\n- 它 6.2 kg，减重期按目标体重 5.5 kg 算，每天约 330 kcal；\n- 常见干粮 350–420 kcal/100g，换算下来 80–95 克 / 天，再扣掉零食和湿粮；\n- 医生上周说要控制体重，可以先试试**每周减 5%**，别一下子减太多。\n\n要不要我每周日晚上提醒你给年糕称一次体重？', T + 6200, { run_id: 'run_cat', meta: { memories: ['m_cat', 'm_vet', 'm_k_cat'], tools: [{ name: 'web.search', status: 'ok' }] } }),
    ],
    conv_jp: [
      msg('conv_jp', 'user', '帮我做个 N2 的复习计划，我每天只有 40 分钟', now - 2 * D),
      msg('conv_jp', 'assistant', '嗯哼，按你明年 7 月考试、每天 40 分钟来排：\n\n1. **听力 20 分钟**（你更喜欢练听力，就把它放在最前面）\n2. 语法 10 分钟：每天 2 个句型\n3. 单词 10 分钟：用 App 复习，不背新词表\n\n每周日做一套真题听力部分（约 50 分钟），我来帮你记录正确率。', now - 2 * D + 40000, { meta: { memories: ['m_jp', 'm_diary2', 'm_k_n2'] } }),
    ],
    conv_qq: [msg('conv_qq', 'user', '今晚游泳吗', now - 3 * H - 60000), msg('conv_qq', 'assistant', '今天周二，按习惯是游泳日～ 19:30 场馆人少一些。', now - 3 * H, { meta: { memories: ['m_swim'] } })],
    conv_grp: [msg('conv_grp', 'user', '[小雨] 鲸鱼，今晚开团吗？', now - 41 * M), msg('conv_grp', 'assistant', '阿杰说九点开团，雪山疗养院模组～ 老周记得先检查一下理智值 😄', now - 40 * M)],
    conv_trip: [msg('conv_trip', 'user', '周末杭州有什么展？', now - 7 * H), msg('conv_trip', 'assistant', '这周末天气晴，浙江美术馆有宫崎骏手稿展（周六 9:30 开门，不过你周末喜欢睡懒觉，下午去正好）。', now - 6 * H, { meta: { memories: ['m_ghibli', 'm_sleep'] } })],
  };

  // -- tasks, schedule -----------------------------------------------------------------------------------
  const job = (jid, name, x = {}) => ({ id: jid, name, handler: 'task.run', args: {}, spec: { every_min: 30 }, enabled: true, system: false, next_at: now + 2 * H, last_at: now - 4 * H,
    last_status: 'ok', last_error: '', runs: 6, created_at: now - 20 * D, ...x });
  const nextFri = (() => { const d = new Date(now); d.setDate(d.getDate() + ((5 - d.getDay() + 7) % 7 || 7)); d.setHours(17, 0, 0, 0); return d.getTime(); })();
  const jobs = [
    job('sys_explore', '自主探索', { system: true, handler: 'autonomy.explore', spec: { every_min: 180 }, next_at: now + 70 * M }),
    job('sys_phone_digest', '手机经历整理成记忆', { system: true, handler: 'phone.digest', next_at: now + 12 * M, last_at: now - 18 * M }),
    job('sys_group_refresh', '群空间自动更新', { system: true, handler: 'groups.refresh', spec: { every_min: 120 }, next_at: now + 73 * M }),
    job('job_report', '任务：每周五生成周报', { spec: { cron: '0 17 * * 5' }, args: { task_id: 'task_report' }, next_at: nextFri, runs: 7 }),
    job('job_cat', '提醒：给年糕称体重', { spec: { cron: '0 20 * * 0' }, handler: 'notify', next_at: now + 4 * D, runs: 0, last_at: null, last_status: '' }),
  ];
  const task = (tid, title, goal, status, x = {}) => ({ id: tid, title, goal, status, plan: [], result: '', error: '', origin: 'user', parent_id: null, conversation_id: null, job_id: null,
    notify: true, runs: 1, last_run_id: `run_${tid}`, created_at: now - 3 * H, updated_at: now - 2 * M, ...x });
  const tasks = [
    task('task_trip', '规划周末杭州一日游', '规划这周六的杭州一日游：下午出门，避开人多的地方，晚上吃不放香菜的本帮菜', 'running', {
      plan: [{ step: '查天气与展览', status: 'done' }, { step: '调研餐厅（子 Agent）', status: 'done' }, { step: '排路线和时间', status: 'doing' }, { step: '发给我确认', status: 'todo' }], created_at: now - 50 * M }),
    task('task_report', '每周五生成周报', '每周五下午 5 点，按我的习惯整理本周工作周报', 'scheduled', { job_id: 'job_report', origin: 'user', runs: 7, result: '上周周报已发送', schedule: jobs[3] }),
    task('task_vaccine', '提醒年糕打疫苗', '查一下猫三联疫苗的间隔，并在合适的时间提醒我', 'done', { result: '已创建提醒：明年 11 月 23 日带年糕加强免疫。', created_at: now - 3 * D, updated_at: now - 3 * D + 9 * M }),
  ];
  const TR = now - 50 * M;
  const tripRun = { id: 'run_task_trip', conversation_id: null, parent_id: null, task_id: 'task_trip', mode: 'goal', status: 'running', input: tasks[0].goal, output: '', error: '', channel: 'task',
    model: 'deepseek-chat', created_at: TR, ended_at: null, stats: { latency_ms: 0, memories: 4, tools: 4 } };
  const child = { id: 'run_child_food', conversation_id: null, parent_id: 'run_task_trip', task_id: null, mode: 'goal', status: 'completed', input: '调研西湖附近不放香菜也好吃的本帮菜，给 3 家',
    output: '1. 新白鹿（湖滨店）…\n2. 外婆家（龙翔桥）…\n3. 知味观（湖滨）…', error: '', channel: 'subagent', model: 'deepseek-chat', created_at: TR + 4 * M, ended_at: TR + 9 * M, stats: { latency_ms: 300000, memories: 1, tools: 3 } };
  const tripSteps = [
    step('run_task_trip', 1, 'recall', '回忆到 4 条记忆', { query: '周末 杭州 出门' }, { memories: [{ id: 'm_sleep', kind: 'preference', content: '周末喜欢睡到自然醒', score: 0.8, why: ['语义'] }, { id: 'm_cilantro', kind: 'preference', content: '用户不吃香菜', score: 0.78, why: ['关键词'] }] }, TR, 200),
    step('run_task_trip', 2, 'plan', '制定计划（4 步）', {}, { plan: tasks[0].plan }, TR + 300, 1500),
    step('run_task_trip', 3, 'tool', '实时天气', { capability: 'weather.now', args: { city: '杭州' } }, { status: 'ok', data: {}, summary: '周六晴，12–19℃，东北风 2 级', error: '', latency_ms: 380 }, TR + 2 * M, 380),
    step('run_task_trip', 4, 'tool', '网页搜索', { capability: 'web.search', args: { query: '杭州 本周末 展览' } }, { status: 'ok', data: {}, summary: '浙江美术馆宫崎骏手稿展、良渚博物院特展', error: '', latency_ms: 1400 }, TR + 3 * M, 1400),
    step('run_task_trip', 5, 'tool', '委派子 Agent', { capability: 'agent.delegate', args: { objective: child.input } }, { status: 'ok', data: { status: 'completed', result: child.output, run_id: child.id }, summary: '子 Agent 推荐了 3 家', error: '', latency_ms: 300000 }, TR + 4 * M, 300000),
    step('run_task_trip', 6, 'think', '排路线和时间', { turn: 4, model: 'deepseek-chat' }, { latency_ms: 0, decision: '', text: '', reasoning: '' }, now - 40000, 0, 'running'),
  ];
  runs.run_task_trip = { run: tripRun, steps: tripSteps, text: '', plan: tasks[0].plan, progress: '正在排路线和时间', children: [child] };
  runs.run_child_food = { run: child, steps: [step('run_child_food', 1, 'tool', '网页搜索', { capability: 'web.search', args: { query: '湖滨 本帮菜 推荐' } }, { status: 'ok', data: {}, summary: '找到 9 条', error: '', latency_ms: 1200 }, TR + 5 * M, 1200)], text: '', plan: [], progress: '', children: [] };

  // -- devices -----------------------------------------------------------------------------------------
  const dev = (did, name, x) => ({ id: did, name, model: 'Pixel 8', flavor: 'daily', app_version: '2.4.1', android: '15', online: true, status: 'online', battery: 54, network: 'wifi', foreground: 'com.tencent.mm',
    capabilities: ['device.status', 'device.observe', 'device.key', 'phone.notification.post', 'phone.notification.read', 'screen.context.read', 'screen.history.search'], tools: [],
    features: { screen_memo: true }, scopes: ['device.status', 'device.observe', 'phone.notification.post', 'phone.notification.read', 'screen.context.read', 'screen.history.search'],
    last_seen: now - 8000, connected_at: now - 5 * H, session_id: id('sess'), revoked: false, created_at: now - 30 * D, paired_via: 'owner', role: 'personal', role_explicit: false,
    privacy: { excluded_apps: ['com.eg.android.AlipayGphone', 'cmb.pb'], excluded_domains: ['*.bank.com', 'mail.qq.com'], redact_secrets: true, drop_sensitive: true }, ...x });
  const devices = [
    dev(AGENT_PHONE, '鲸鱼的手机', { model: 'Redmi Note 12', flavor: 'root', role: 'agent', battery: 82, foreground: 'com.android.chrome', network: 'wifi', privacy: null,
      capabilities: ['device.status', 'device.observe', 'device.tap', 'device.swipe', 'device.type', 'device.key', 'device.app.launch', 'device.app.list', 'phone.notification.post', 'screen.context.read'],
      scopes: ['device.status', 'device.observe', 'device.tap', 'device.swipe', 'device.type', 'device.key', 'device.app.launch', 'device.app.list', 'phone.notification.post', 'screen.context.read'],
      tools: ['device.redmi.device.tap', 'device.redmi.device.observe'], created_at: now - 45 * D, connected_at: now - 26 * H }),
    dev(DAILY_PHONE, '日常机', { model: 'iQOO 12', battery: 54, network: '5g', foreground: 'com.ss.android.ugc.aweme' }),
  ];
  const devEvents = {
    [AGENT_PHONE]: [{ event_id: 'ev_a1', kind: 'screen.segment_ready', occurred_at: now - 6 * H, device_id: AGENT_PHONE, source: 'screen_memo', summary: '在浏览器里查了杭州周末天气', privacy_class: 'agent', extras: { package: 'com.android.chrome', app_name: 'Chrome' }, received_at: now - 6 * H },
      { event_id: 'ev_a2', kind: 'screen.segment_ready', occurred_at: now - 6 * H + 4 * M, device_id: AGENT_PHONE, source: 'screen_memo', summary: '打开美术馆小程序看了展览排期', privacy_class: 'agent', extras: { package: 'com.tencent.mm', app_name: '微信' }, received_at: now - 6 * H + 4 * M }],
    [DAILY_PHONE]: [{ event_id: 'ev_l1', kind: 'screen.segment_ready', occurred_at: now - 2 * D, device_id: DAILY_PHONE, source: 'screen_memo', summary: '做了 N2 听力练习第 3 套', privacy_class: 'owner_private', extras: { package: 'com.hujiang.normandy', app_name: '沪江' }, received_at: now - 2 * D },
      { event_id: 'ev_d1', kind: 'dropped', dropped: 'excluded_app', received_at: now - 50 * M }, { event_id: 'ev_d2', kind: 'dropped', dropped: 'system_private_app', received_at: now - 30 * M },
      { event_id: 'ev_l4', kind: 'screen.segment_ready', occurred_at: now - 25 * M, device_id: DAILY_PHONE, source: 'screen_memo', summary: '刷了 15 分钟猫咪减肥的短视频', privacy_class: 'owner_private', extras: { package: 'com.ss.android.ugc.aweme', app_name: '抖音' }, received_at: now - 24 * M }],
  };

  // -- capabilities, plugins, config ---------------------------------------------------------------------
  const cap = (name, title, risk, provider, group, x = {}) => ({ name, title, description: title, params: {}, risk, provider, group, modes: ['chat', 'goal'], available: true, owner_only: false,
    policy: risk === 'external' || risk === 'dangerous' ? 'ask' : 'allow', stats: { calls: 0, errors: 0, total_ms: 0 }, ...x });
  const caps = [
    cap('memory.remember', '记住', 'write', 'builtin', 'memory', { stats: { calls: 86, errors: 0, total_ms: 4300 } }), cap('memory.recall', '回忆', 'read', 'builtin', 'memory', { stats: { calls: 412, errors: 0, total_ms: 20600 } }),
    cap('web.search', '网页搜索', 'read', 'builtin', 'web', { stats: { calls: 133, errors: 4, total_ms: 186000 } }), cap('web.read', '阅读网页', 'read', 'builtin', 'web', { stats: { calls: 61, errors: 2, total_ms: 98000 } }),
    cap('schedule.create', '创建提醒', 'write', 'builtin', 'schedule', { stats: { calls: 12, errors: 0, total_ms: 600 } }), cap('task.create', '创建任务', 'write', 'builtin', 'task', { owner_only: true, stats: { calls: 9, errors: 0, total_ms: 450 } }),
    cap('agent.delegate', '委派子 Agent', 'write', 'builtin', 'agent', { stats: { calls: 7, errors: 0, total_ms: 1900000 } }), cap('phone.recall', '回想手机经历', 'read', 'builtin', 'phone', { owner_only: true, stats: { calls: 15, errors: 0, total_ms: 3000 } }),
    cap('shell.run', '执行命令', 'dangerous', 'builtin', 'shell', { owner_only: true, policy: 'deny' }), cap('qq.send', '发送 QQ 消息', 'external', 'plugin:qq', 'QQ', { owner_only: true, stats: { calls: 21, errors: 1, total_ms: 8000 } }),
    cap('weather.now', '实时天气', 'read', 'plugin:qweather', '和风天气', { stats: { calls: 38, errors: 0, total_ms: 11000 } }), cap('image.generate', '生成图片', 'external', 'plugin:media', '生图与语音', { available: false }),
    cap('device.redmi.device.tap', '点击屏幕', 'external', 'device:a7f3c9e1', '鲸鱼的手机', { owner_only: true, policy: 'allow', stats: { calls: 44, errors: 2, total_ms: 52000 } }),
    cap('device.redmi.device.observe', '观察界面', 'read', 'device:a7f3c9e1', '鲸鱼的手机', { owner_only: true, stats: { calls: 70, errors: 0, total_ms: 61000 } }),
    cap('client.glasses.card', '眼镜卡片', 'external', 'client:cli_glass', 'Rokid 眼镜', { available: false }),
  ];
  const plugins = [
    { id: 'qq', name: 'QQ', version: '1.2.0', description: 'OneBot v11（NapCat / Lagrange）私聊与群聊', enabled: true, status: 'active', error: '', capabilities: ['qq.send'],
      config_schema: { api_base: { type: 'string', title: 'OneBot HTTP API', default: '' }, group_mode: { type: 'string', title: '群聊模式', enum: ['off', 'mention', 'smart'], default: 'mention' },
        allowed_groups: { type: 'array', title: '允许的群', default: [] }, reply_split: { type: 'boolean', title: '分条发送', default: true } },
      secrets_schema: { access_token: 'OneBot Access Token' }, secrets: { access_token: true }, config: { enabled: true, api_base: 'http://127.0.0.1:3000', group_mode: 'smart', allowed_groups: ['745812309', '600112233'] }, builtin: true, icon: '🐧' },
    { id: 'qweather', name: '和风天气', version: '1.0.0', description: '实时天气与预报', enabled: true, status: 'active', error: '', capabilities: ['weather.now'],
      config_schema: { api_host: { type: 'string', title: 'API Host', default: '' }, default_city: { type: 'string', title: '默认城市', default: '' } },
      secrets_schema: { key: 'API KEY' }, secrets: { key: true }, config: { enabled: true, api_host: 'devapi.qweather.com', default_city: '杭州' }, builtin: true, icon: '🌤️' },
    { id: 'media', name: '生图与语音', version: '1.0.0', description: '文生图、语音合成', enabled: false, status: 'needs_config', error: '', capabilities: ['image.generate'],
      config_schema: { provider: { type: 'string', title: '服务商', enum: ['dashscope', 'openai'], default: 'dashscope' } }, secrets_schema: { api_key: 'API Key' }, secrets: { api_key: false }, config: {}, builtin: true, icon: '🎨' },
  ];
  const config = {
    server: { host: '0.0.0.0', port: 8080, cors_origins: [] },
    models: { providers: [{ id: 'deepseek', kind: 'openai', base_url: 'https://api.deepseek.com/v1', label: 'DeepSeek' }, { id: 'dashscope', kind: 'openai', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', label: '通义千问' }],
      chat: { provider: 'deepseek', model: 'deepseek-chat', temperature: 0.7, max_tokens: 1024 }, fast: { provider: 'deepseek', model: 'deepseek-chat', temperature: 0.2, max_tokens: 800 },
      vision: { provider: 'dashscope', model: 'qwen-vl-plus', temperature: 0.7, max_tokens: 1024 }, embedding: { provider: 'dashscope', model: 'text-embedding-v3', temperature: 0.7, max_tokens: 1024 }, daily_request_budget: 2000 },
    agent: { name: '鲸鱼', persona_id: 'default', timezone: 'Asia/Shanghai', language: 'zh-CN', max_steps: 12, history_turns: 16, context_budget_chars: 24000, reply_gear: 'fast' },
    memory: { extraction: true, consolidation_interval_min: 30, half_life_days: 30, recall_limit: 8 },
    autonomy: { explore: true, explore_interval_min: 180, interests: ['深海生物', '日语', '养猫', 'AI 新闻'], proactive: true, quiet_hours: [23, 9], proactive_daily_limit: 3 },
    policy: { rules: { 'shell.run': 'deny', 'qq.send': 'ask', 'device.redmi.*': 'allow' } },
    devices: { heartbeat_interval_s: 30, heartbeat_timeout_s: 90, challenge_ttl_s: 30, receipt_grace_s: 3, pair_ttl_s: 600, digest_interval_min: 30 },
    plugins: {}, secrets: { 'provider.deepseek': true, 'provider.dashscope': true },
  };
  const modelsStatus = () => ({ roles: { chat: { provider: 'deepseek', model: 'deepseek-chat', configured: true }, fast: { provider: 'deepseek', model: 'deepseek-chat', configured: true },
    vision: { provider: 'dashscope', model: 'qwen-vl-plus', configured: true }, embedding: { provider: 'dashscope', model: 'text-embedding-v3', configured: true } }, offline: false,
  budget: { used: 386, limit: 2000 }, providers: { deepseek: { requests: 361, errors: 3, avg_ms: 1240.2, last_error: '', last_ok_at: now / 1000, tokens_in: 812000, tokens_out: 96000 },
    dashscope: { requests: 25, errors: 0, avg_ms: 410.7, last_error: '', last_ok_at: now / 1000, tokens_in: 21000, tokens_out: 0 } } });
  const clients = [
    { id: 'cli_console', role: 'console', name: 'Web 控制台', connected_at: now - 2 * M, subscriptions: ['*'], capabilities: [], meta: {} },
    { id: 'cli_qq', role: 'bridge', name: 'QQ（NapCat）', connected_at: now - 26 * H, subscriptions: ['notify'], capabilities: [], meta: {} },
    { id: 'cli_glass', role: 'glasses', name: 'Rokid 眼镜', connected_at: now - 40 * M, subscriptions: ['notify'], capabilities: ['card'], meta: { version: '1.0' } },
  ];
  const personas = [
    { id: 'default', name: '鲸鱼', tagline: '记得你的一切，也有自己的小习惯', description: '一头住在你电脑里的鲸鱼：温和、好奇，说话简洁。', personality: '温和、好奇、可靠', style: '简洁、先结论后理由', scenario: '', greeting: '嗯哼，我在。', examples: '', boundaries: '', avatar: '🐋', color: '#2ee6d6', builtin: true, extra: {} },
    { id: 'persona_xj', name: '小鲸', tagline: '活泼版鲸鱼，爱用表情', description: '', personality: '活泼', style: '口语化，偶尔用颜文字', scenario: '', greeting: '嗨嗨～', examples: '', boundaries: '', avatar: '🐳', color: '#38bdf8', builtin: false, extra: {} },
    { id: 'persona_scholar', name: '深海学者', tagline: '严谨、爱讲知识的老教授', description: '', personality: '严谨', style: '有条理', scenario: '', greeting: '请坐。', examples: '', boundaries: '', avatar: '🦑', color: '#a78bfa', builtin: false, extra: {} },
  ];
  const approvals = [{ id: 'aprv_grp', run_id: 'run_grp_live', capability: 'qq.send', args: { group: '745812309', text: '今晚九点开团，雪山疗养院！记得带上车卡～' }, reason: 'QQ 群「周末跑团小分队」· 小雨：鲸鱼帮忙通知一下大家',
    risk: 'external', status: 'pending', created_at: now - 3 * M, resolved_at: null, resolved_by: null, note: null, title: '在群里发通知' }];

  const status = () => ({ version: '1.0.0', uptime_s: 26 * 3600 + 1240, python: '3.12.4',
    agent: { name: '鲸鱼', persona: { id: 'default', name: '鲸鱼', avatar: '🐋', tagline: personas[0].tagline, color: '#2ee6d6' }, phase: tripRun.status === 'running' ? 'acting' : 'idle',
      active: tripRun.status === 'running' ? [{ run_id: tripRun.id, mode: 'goal', status: 'acting', conversation_id: null, title: '规划周末杭州一日游', channel: 'task' }] : [] },
    models: modelsStatus(), memory: stats(), capabilities: { total: caps.length, by_provider: { builtin: 9, plugin: 3, device: 2, client: 1 } },
    tasks: { total: tasks.length, active: tasks.filter((t) => t.status === 'running').length, scheduled: 1 }, approvals: { pending: approvals.length },
    devices: { total: 2, online: devices.filter((d) => d.online).length }, clients, autonomy: { explore: true, proactive: true } });

  const ev = (topic, ago, data) => ({ id: id('ev'), topic, ts: now - ago, data });
  const activity = [
    ev('run.step', 40000, { step: tripSteps[5] }), ev('approval.requested', 3 * M, approvals[0]), ev('group.archived', 6 * M, { id: G.group.id, name: G.group.name, count: 4 }),
    ev('run.completed', 9 * M, { run: { ...child } }), ev('memory.created', 14 * M, { item: memories.find((m) => m.id === 'm_vet') }),
    ev('device.event_filtered', 30 * M, { device_id: DAILY_PHONE, reason: 'system_private_app' }), ev('device.digest', 18 * M, { devices: 2, segments: 3, episodes: 2, facts: 1 }),
    ev('group.refreshed', 47 * M, { id: G.group.id, name: G.group.name, counts: { profiles_added: 1, profiles_updated: 4, preferences_added: 2, preferences_updated: 0, preferences_expired: 1, scanned: 37 } }),
    ev('autonomy.explored', 3 * H, { topic: '深海鲸落', findings: ['m_c_whale'], diary: 'm_diary1' }), ev('memory.reinforced', 3 * H + 5 * M, { item: memories.find((m) => m.id === 'm_brief') }),
    ev('run.completed', 26 * M, { run: runs.run_cat.run }), ev('device.connected', 5 * H, { device: devices[1] }), ev('autonomy.proactive', 2 * H, { text: '年糕今天该称体重了吗？', sent: false, why: '静默时段之外，但今天已经提醒过' }),
    ev('task.updated', 3 * D, { task: tasks[2] }), ev('memory.superseded', 2 * D, { old: 'm_old_swim', new: 'm_swim' }),
  ].sort((a, b) => b.ts - a.ts);

  return { G, memories, links, stats, runs, conversations, messages, jobs, tasks, devices, devEvents, caps, plugins, config, modelsStatus, clients, personas, approvals, status, activity, msg, id, scopeCat, AGENT_PHONE, DAILY_PHONE };
}
