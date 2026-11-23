// @ts-check
/**
 * 对话 Chat: conversation list · streaming message view · composer with images ·
 * per-reply "思维轨迹" drawer (recall / think / tool / approval / reply, live).
 */
import { html, useEffect, useMemo, useRef, useState } from '/vendor/preact-htm.js';
import { Icon } from '../components/icons.js';
import { Button, Badge, Empty, Skeleton, Drawer, ConfirmButton, cx, When, ErrorBox } from '../components/ui.js';
import { StepList } from '../components/trace.js';
import { ModelsOfflineBanner } from './bridge.js';
import { useStore, act, tryAct, sendChat, loadRun, useTopic, store, toast } from '../lib/store.js';
import { useRpc, navigate, linkClick, useDebouncedCallback } from '../lib/hooks.js';
import { renderMarkdown } from '../lib/markdown.js';
import { errorText } from '../lib/api.js';
import { timeAgo, fmtTime, CLIENT_ROLES } from '../lib/format.js';

const MAX_IMAGES = 4;

/** Read an image file as a data URL, downscaling large photos (keeps requests small). @param {File} file */
export function readImage(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onerror = () => reject(new Error('无法读取图片'));
    fr.onload = () => {
      const url = String(fr.result);
      if (file.size < 1_200_000 || typeof document === 'undefined') { resolve(url); return; }
      const img = new Image();
      img.onload = () => {
        const scale = Math.min(1, 1600 / Math.max(img.width, img.height));
        const c = document.createElement('canvas');
        c.width = Math.round(img.width * scale); c.height = Math.round(img.height * scale);
        c.getContext('2d')?.drawImage(img, 0, 0, c.width, c.height);
        resolve(c.toDataURL('image/jpeg', 0.86));
      };
      img.onerror = () => resolve(url);
      img.src = url;
    };
    fr.readAsDataURL(file);
  });
}

/** @param {{convs: any[], activeId: string, loading: boolean}} p */
function ConvList({ convs, activeId, loading }) {
  const [q, setQ] = useState('');
  const shown = q ? convs.filter((c) => (c.title || '').includes(q)) : convs;
  return html`<aside class="conv-list" aria-label="会话列表">
    <div class="conv-list-head">
      <${Button} variant="primary" icon="plus" href="/chat/new" class="grow">新对话<//>
    </div>
    <div class="conv-search"><${Icon} name="search" size=${15} /><input value=${q} placeholder="筛选会话" aria-label="筛选会话" onInput=${(e) => setQ(e.currentTarget.value)} /></div>
    ${loading && !convs.length ? html`<${Skeleton} rows=${5} height=${34} />` : null}
    <ul>${shown.map((c) => html`<li key=${c.id}>
      <a href=${`/chat/${c.id}`} onClick=${linkClick} class=${cx('conv-item', c.id === activeId && 'is-active')} aria-current=${c.id === activeId ? 'page' : undefined}>
        <span class="conv-title">${c.title || '新的对话'}</span>
        <span class="conv-meta">${c.channel !== 'console' ? html`<${Badge} tone="info">${CLIENT_ROLES[c.channel] || c.channel}<//>` : null}
          ${c.peer && c.peer !== 'owner' ? html`<${Badge} tone="violet">${c.peer}<//>` : null}
          <${When} ms=${c.updated_at} fmt=${timeAgo} /> · ${c.turns} 轮</span>
      </a></li>`)}
    </ul>
    ${!loading && !convs.length ? html`<p class="muted small pad">还没有对话。</p>` : null}
  </aside>`;
}

/** @param {{m: any, onTrace: (runId: string) => void, traceOpen: string}} p */
function Message({ m, onTrace, traceOpen }) {
  const meta = m.meta || {};
  if (m.role === 'system') return html`<div class="msg msg-system" role="note"><${Icon} name="alert" size=${14} />${m.content}</div>`;
  const mine = m.role === 'user';
  return html`<div class=${cx('msg', mine ? 'msg-user' : 'msg-assistant', m.pending && 'is-pending')}>
    <div class="msg-bubble">
      ${m.images?.length ? html`<div class="msg-images">${m.images.map((src, i) => html`<img key=${i} src=${src} alt=${`附图 ${i + 1}`} />`)}</div>` : null}
      ${!m.images?.length && meta.images ? html`<span class="msg-attach"><${Icon} name="image" size=${14} />${meta.images} 张图片</span>` : null}
      <div class="md">${mine ? m.content.split('\n').map((l, i) => html`<p key=${i}>${l || '\u00a0'}</p>`) : renderMarkdown(m.content)}</div>
      ${m.streaming ? html`<span class="caret" aria-hidden="true"></span>` : null}
    </div>
    <div class="msg-foot">
      <span class="muted">${fmtTime(m.ts)}</span>
      ${!mine && m.run_id ? html`<button type="button" class=${cx('trace-btn', traceOpen === m.run_id && 'is-on')} onClick=${() => onTrace(m.run_id)} aria-label="查看思维轨迹">
        <${Icon} name="branch" size=${14} />思维轨迹${meta.memories?.length ? html` · ${meta.memories.length} 条记忆` : ''}${meta.tools?.length ? html` · ${meta.tools.length} 次能力` : ''}
      </button>` : null}
    </div>
  </div>`;
}

/** A live, streaming reply for a running run. @param {{runId: string, onTrace: (id: string) => void, traceOpen: string}} p */
function LiveReply({ runId, onTrace, traceOpen }) {
  const view = useStore((s) => s.runs[runId]);
  if (!view) return null;
  const steps = view.steps || [];
  const lastStep = steps[steps.length - 1];
  const recall = steps.find((s) => s.kind === 'recall' && s.status !== 'running');
  const finished = ['completed', 'failed', 'cancelled', 'interrupted'].includes(view.run?.status);
  const text = view.text || (finished ? view.run?.output || '' : '');
  const waiting = view.run?.status === 'waiting' || (lastStep?.kind === 'approval' && lastStep.status === 'running');
  const phase = finished ? '' : waiting ? '等待你的批准…' : lastStep?.kind === 'tool' && lastStep.status === 'running' ? `正在调用「${lastStep.title}」` :
    lastStep?.kind === 'recall' && lastStep.status === 'running' ? '正在回忆…' : text ? '' : '思考中…';
  if (finished && !text) return null;
  return html`<div class="msg msg-assistant is-live">
    <div class="msg-bubble">
      ${phase ? html`<p class="live-phase"><span class="typing"><i></i><i></i><i></i></span>${phase}</p>` : null}
      ${text ? html`<div class="md">${renderMarkdown(text)}</div>` : null}
      ${text && !finished ? html`<span class="caret" aria-hidden="true"></span>` : null}
    </div>
    <div class="msg-foot">
      ${recall ? html`<span class="muted">${recall.title}</span>` : null}
      <button type="button" class=${cx('trace-btn', traceOpen === runId && 'is-on', waiting && 'tone-warn')} onClick=${() => onTrace(runId)}><${Icon} name="branch" size=${14} />${finished ? '思维轨迹' : '实时轨迹'} · ${steps.length} 步</button>
    </div>
  </div>`;
}

/** @param {{runId: string}} p */
function TraceView({ runId }) {
  const view = useStore((s) => s.runs[runId]);
  const [err, setErr] = useState(null);
  useEffect(() => {
    setErr(null);
    const v = store.get().runs[runId];
    if (!v || !v.loadedAt) loadRun(runId).catch(setErr);
  }, [runId]);
  if (err) return html`<${ErrorBox} error=${err} onRetry=${() => loadRun(runId).catch(setErr)} />`;
  if (!view) return html`<${Skeleton} rows=${6} />`;
  return html`<div>
    <p class="trace-explain">鲸鱼每一次回复都是一次 Run：先回忆相关记忆，再思考是直接回答还是调用能力；需要你确认的动作会停下来等你。点击记忆可跳到记忆页。</p>
    <${StepList} steps=${view.steps} run=${view.run} progress=${view.progress} />
  </div>`;
}

/** @param {{images: string[], setImages: (v: string[]) => void, onSend: (text: string) => void, busy: boolean, runId: string, initial?: string}} p */
function Composer({ images, setImages, onSend, busy, runId, initial = '' }) {
  const [text, setText] = useState(initial);
  const [drag, setDrag] = useState(false);
  const ta = useRef(/** @type {HTMLTextAreaElement|null} */ (null));
  const file = useRef(/** @type {HTMLInputElement|null} */ (null));
  useEffect(() => { setText(initial); }, [initial]);
  useEffect(() => {
    const el = ta.current;
    if (el) { el.style.height = 'auto'; el.style.height = Math.min(220, el.scrollHeight) + 'px'; }
  }, [text]);
  const addFiles = async (files) => {
    const list = [...(files || [])].filter((f) => f.type?.startsWith('image/')).slice(0, MAX_IMAGES - images.length);
    if (!list.length) return;
    try { setImages([...images, ...(await Promise.all(list.map(readImage)))]); } catch (e) { toast({ kind: 'error', title: '图片读取失败', body: String(/** @type {any} */ (e)?.message || e) }); }
  };
  const submit = () => {
    const t = text.trim();
    if (!t || busy) return;
    onSend(t);
    setText('');
  };
  return html`<div class=${cx('composer', drag && 'is-drag')}
    onDragOver=${(e) => { e.preventDefault(); setDrag(true); }} onDragLeave=${() => setDrag(false)}
    onDrop=${(e) => { e.preventDefault(); setDrag(false); addFiles(e.dataTransfer?.files); }}>
    ${images.length ? html`<div class="composer-images">${images.map((src, i) => html`<figure key=${i}><img src=${src} alt=${`待发送图片 ${i + 1}`} />
      <button type="button" aria-label="移除图片" onClick=${() => setImages(images.filter((_, j) => j !== i))}><${Icon} name="x" size=${12} /></button></figure>`)}</div>` : null}
    <div class="composer-row">
      <input ref=${file} type="file" accept="image/*" multiple hidden onChange=${(e) => { addFiles(e.currentTarget.files); e.currentTarget.value = ''; }} />
      <button type="button" class="icon-btn" aria-label="添加图片" title=${images.length >= MAX_IMAGES ? '最多 4 张' : '添加图片（也可粘贴或拖入）'} disabled=${images.length >= MAX_IMAGES}
        onClick=${() => file.current?.click()}><${Icon} name="image" /></button>
      <textarea ref=${ta} rows="1" value=${text} placeholder="和鲸鱼说点什么… Enter 发送，Shift+Enter 换行" aria-label="消息"
        onInput=${(e) => setText(e.currentTarget.value)}
        onPaste=${(e) => { const fs = e.clipboardData?.files; if (fs?.length) { e.preventDefault(); addFiles(fs); } }}
        onKeyDown=${(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); } }}></textarea>
      ${runId ? html`<${Button} variant="danger" icon="stop" title="停止这次回复" onClick=${() => tryAct('chat.cancel', { run_id: runId }, { ok: '已停止' })}>停止<//>`
        : html`<${Button} variant="primary" icon="send" disabled=${!text.trim() || busy} loading=${busy} onClick=${submit} aria-label="发送" />`}
    </div>
  </div>`;
}

export function ChatPage({ route }) {
  const isNew = route.id === 'new';
  const convs = useRpc('conversations.list', { limit: 60 });
  const convId = isNew ? '' : route.id || convs.data?.conversations?.[0]?.id || '';
  const msgs = useRpc(convId ? 'conversations.messages' : null, { id: convId, limit: 120 }, [convId]);
  const [pending, setPending] = useState(/** @type {any} */ (null));
  const [images, setImages] = useState(/** @type {string[]} */ ([]));
  const [trace, setTrace] = useState(route.query.trace || '');
  const [sendErr, setSendErr] = useState('');
  const [editing, setEditing] = useState('');
  const [settling, setSettling] = useState('');
  const scroller = useRef(/** @type {HTMLElement|null} */ (null));
  const sentAuto = useRef('');

  // The run streaming into this conversation: our pending send, or one started elsewhere (phone, QQ…).
  // Server and browser clocks may differ, so the time window is generous; the newest match wins.
  const liveRunId = useStore((s) => {
    const runs = Object.values(s.runs).map((v) => v.run)
      .filter((r) => r && r.status !== 'completed' && r.status !== 'failed' && r.status !== 'cancelled' && r.status !== 'interrupted')
      .sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    const mine = pending && runs.find((r) => r.mode === 'chat' && r.input === pending.text && Math.abs((r.created_at || pending.at) - pending.at) < 600000 && (!pending.convId || r.conversation_id === pending.convId));
    if (mine) return mine.id;
    const here = convId && runs.find((r) => r.conversation_id === convId && r.mode === 'chat');
    return here ? here.id : '';
  });

  const refreshMsgs = useDebouncedCallback(() => { msgs.reload(true); convs.reload(true); }, 250);
  useTopic(['run.completed', 'run.failed', 'run.cancelled'], (ev) => {
    const r = ev.data.run || {};
    if (r.mode !== 'chat') return;
    if ((convId && r.conversation_id === convId) || (pending && r.input === pending.text)) {
      setPending((p) => (p && r.input === p.text ? null : p));
      setSettling(r.id); // keep the streamed reply on screen until the stored message arrives
      if (r.status === 'failed' && r.error) setSendErr(r.error);
      refreshMsgs();
    } else convs.reload(true);
  });

  useEffect(() => { setTrace(route.query.trace || ''); setSendErr(''); setSettling(''); }, [convId]); // ?trace=<run_id> opens the drawer
  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [msgs.data, pending, liveRunId]);

  const send = async (text, imgs = images) => {
    setSendErr('');
    const at = Date.now();
    setPending({ text, images: imgs, convId, at });
    setImages([]);
    try {
      const res = await sendChat({ text, conversation_id: convId || undefined, images: imgs, wait: false });
      setPending((p) => (p ? { ...p, convId: res.conversation_id } : p));
      if (!convId && res.conversation_id) { navigate(`/chat/${res.conversation_id}`, { replace: true }); convs.reload(true); }
    } catch (e) {
      setPending(null);
      setImages(imgs);
      setSendErr(errorText(e));
    }
  };

  // ?send=… from the bridge quick-ask: send once.
  useEffect(() => {
    const s = route.query.send;
    if (isNew && s && sentAuto.current !== s) { sentAuto.current = s; send(s, []); }
  }, [isNew, route.query.send]);

  const conv = msgs.data?.conversation || convs.data?.conversations?.find((c) => c.id === convId);
  const list = useMemo(() => {
    const out = [...(msgs.data?.messages || [])];
    if (pending && !out.some((m) => m.role === 'user' && m.content === pending.text && Math.abs(m.ts - pending.at) < 600000)) {
      out.push({ id: 'pending', role: 'user', content: pending.text, ts: pending.at, images: pending.images, pending: true });
    }
    return out;
  }, [msgs.data, pending]);
  const busy = !!pending && !liveRunId;
  const showSettling = settling && settling !== liveRunId && !list.some((m) => m.run_id === settling && m.role !== 'user');

  return html`<div class=${cx('page', 'chat', trace && 'has-trace', route.id && 'has-conv')}>
    <${ConvList} convs=${convs.data?.conversations || []} activeId=${convId} loading=${convs.loading} />
    <section class="chat-main" aria-label="对话">
      <header class="chat-head">
        <button type="button" class="icon-btn chat-back" aria-label="返回会话列表" onClick=${() => navigate('/chat')}><${Icon} name="left" /></button>
        <div class="chat-title">
          ${editing && conv ? html`<form class="inline-form" onSubmit=${async (e) => { e.preventDefault(); await tryAct('conversations.update', { id: conv.id, title: editing.trim() }); setEditing(''); convs.reload(true); msgs.reload(true); }}>
            <input class="input" value=${editing} aria-label="会话标题" autofocus onInput=${(e) => setEditing(e.currentTarget.value)} onKeyDown=${(e) => e.key === 'Escape' && setEditing('')} />
            <${Button} size="sm" variant="primary" icon="check" type="submit" aria-label="保存标题" /></form>`
            : html`<h2>${conv?.title || (isNew || !convId ? '新的对话' : '对话')}</h2>`}
          ${conv ? html`<span class="muted small">${CLIENT_ROLES[conv.channel] || conv.channel} · ${conv.turns} 轮 · 创建于 ${fmtTime(conv.created_at)}</span>` : html`<span class="muted small">鲸鱼会记得重要的事，并在每次回复时告诉你它想起了什么。</span>`}
        </div>
        ${conv ? html`<div class="chat-actions">
          <${Button} size="sm" variant="ghost" icon="edit" title="重命名" onClick=${() => setEditing(conv.title || '新的对话')} aria-label="重命名" />
          <${ConfirmButton} icon="trash" confirmText="删除会话？" onConfirm=${async () => { await act('conversations.delete', { id: conv.id }, { ok: '会话已删除' }); navigate('/chat', { replace: true }); convs.reload(); }} aria-label="删除会话" />
        </div>` : null}
      </header>
      <${ModelsOfflineBanner} />
      <div class="chat-scroll" ref=${scroller} aria-live="polite">
        ${msgs.loading && !msgs.data ? html`<${Skeleton} rows=${4} height=${40} />` : null}
        ${msgs.error ? html`<${ErrorBox} error=${msgs.error} onRetry=${() => msgs.reload()} />` : null}
        ${!list.length && !msgs.loading ? html`<${Empty} icon="chat" title="开始一段新的对话" hint="试试：“记住我对花生过敏”“明早 8 点提醒我开会”“帮我查查今天的天气”。">
          ${['你还记得我什么？', '帮我规划一下这周的学习', '你现在能做哪些事？'].map((t) => html`<${Button} key=${t} size="sm" variant="ghost" onClick=${() => send(t, [])}>${t}<//>`)}
        <//>` : null}
        ${list.map((m) => html`<${Message} key=${m.id} m=${m} onTrace=${setTrace} traceOpen=${trace} />`)}
        ${showSettling ? html`<${LiveReply} runId=${settling} onTrace=${setTrace} traceOpen=${trace} />` : null}
        ${liveRunId ? html`<${LiveReply} runId=${liveRunId} onTrace=${setTrace} traceOpen=${trace} />` : null}
        ${busy ? html`<div class="msg msg-assistant is-live"><div class="msg-bubble"><p class="live-phase"><span class="typing"><i></i><i></i><i></i></span>正在唤起鲸鱼…</p></div></div>` : null}
        ${sendErr ? html`<div class="msg msg-system" role="alert"><${Icon} name="alert" size=${14} />${sendErr}</div>` : null}
      </div>
      <${Composer} images=${images} setImages=${setImages} onSend=${(t) => send(t)} busy=${busy} runId=${liveRunId} initial=${route.query.draft || ''} />
    </section>
    <${Drawer} open=${!!trace} onClose=${() => setTrace('')} title="思维轨迹" class="trace-drawer">
      ${trace ? html`<${TraceView} runId=${trace} />` : null}
    <//>
  </div>`;
}

