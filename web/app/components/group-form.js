// @ts-check
/** 群空间 forms: create/edit (roster editor), chat-log import (file + drag-drop), delete. */
import { html, useEffect, useState } from '/vendor/preact-htm.js';
import { Icon } from './icons.js';
import { Button, AsyncButton, Field, Modal, TagEditor, cx } from './ui.js';
import { act, toast } from '../lib/store.js';
import { AUTO_REFRESH } from '../lib/format.js';

const ACCOUNT_RE = /^\d{5,12}$/;
export const MAX_IMPORT_MB = 20;
let rowSeq = 0;
/** @param {any} [r] */
export const newRow = (r = {}) => ({ key: `r${++rowSeq}`, name: r.name || '', accounts: [...(r.accounts || [])], aliases: [...(r.aliases || [])] });

/**
 * Roster validation: names unique (members + bots), every filled row has a name and an account,
 * accounts are QQ numbers and unique, at least one member.
 * @param {{key: string, name: string, accounts: string[], aliases: string[]}[]} members @param {typeof members} bots
 * @returns {{ok: boolean, rows: Record<string, string>, message: string}}
 */
export function validateRoster(members, bots) {
  /** @type {Record<string, string>} */
  const rows = {};
  const names = new Map(); const accounts = new Map();
  const all = [...members.map((r) => ({ r, bot: false })), ...bots.map((r) => ({ r, bot: true }))];
  let filledMembers = 0;
  for (const { r, bot } of all) {
    const name = r.name.trim();
    const empty = !name && !r.accounts.length && !r.aliases.length;
    if (empty) continue;
    if (!bot) filledMembers++;
    if (!name) { rows[r.key] = '请填写名字'; continue; }
    if (!r.accounts.length) { rows[r.key] = '至少填一个 QQ 号'; continue; }
    const bad = r.accounts.find((a) => !ACCOUNT_RE.test(a));
    if (bad) { rows[r.key] = `「${bad}」不是有效的 QQ 号`; continue; }
    const k = name.toLowerCase();
    if (names.has(k)) { rows[r.key] = `名字「${name}」重复了`; continue; }
    names.set(k, r.key);
    const dup = r.accounts.find((a) => accounts.has(a));
    if (dup) { rows[r.key] = `QQ 号 ${dup} 已经属于「${accounts.get(dup)}」`; continue; }
    r.accounts.forEach((a) => accounts.set(a, name));
  }
  const message = Object.keys(rows).length ? '名单里有需要修改的地方' : filledMembers ? '' : '至少添加一位群友';
  return { ok: !message, rows, message };
}

/** @param {{key: string, name: string, accounts: string[], aliases: string[]}[]} rows */
const clean = (rows) => rows.filter((r) => r.name.trim()).map((r) => ({ name: r.name.trim(), accounts: r.accounts, aliases: r.aliases.filter((a) => a.trim()) }));

/** @param {{title: string, hint: string, rows: any[], onChange: (rows: any[]) => void, errors: Record<string, string>, bot?: boolean}} p */
export function RosterEditor({ title, hint, rows, onChange, errors, bot }) {
  const patch = (key, p) => onChange(rows.map((r) => (r.key === key ? { ...r, ...p } : r)));
  return html`<fieldset class="roster">
    <legend><strong>${title}</strong> <span class="muted small">${hint}</span></legend>
    ${rows.length ? html`<div class="roster-head" aria-hidden="true"><span>名字</span><span>QQ 号（可多个）</span><span>别名 / 昵称</span><span></span></div>` : null}
    <ul class="roster-rows">${rows.map((r, i) => html`<li key=${r.key} class=${cx('roster-row', errors[r.key] && 'is-bad')}>
      <input class="input" value=${r.name} placeholder=${bot ? '例如：群管家' : '例如：小王'} aria-label=${`${title}第 ${i + 1} 行：名字`}
        onInput=${(e) => patch(r.key, { name: e.currentTarget.value })} />
      <${TagEditor} value=${r.accounts} mono label=${`${title}第 ${i + 1} 行：QQ 号`} placeholder="QQ 号，回车添加" validate=${(t) => ACCOUNT_RE.test(t)} onChange=${(v) => patch(r.key, { accounts: v })} />
      <${TagEditor} value=${r.aliases} label=${`${title}第 ${i + 1} 行：别名`} placeholder="群里怎么叫 TA" onChange=${(v) => patch(r.key, { aliases: v })} />
      <button type="button" class="icon-btn" aria-label=${`删除第 ${i + 1} 行`} onClick=${() => onChange(rows.filter((x) => x.key !== r.key))}><${Icon} name="trash" size=${16} /></button>
      ${errors[r.key] ? html`<span class="field-error roster-err">${errors[r.key]}</span>` : null}
    </li>`)}</ul>
    <${Button} size="sm" variant="ghost" icon="plus" onClick=${() => onChange([...rows, newRow()])}>${bot ? '添加机器人' : '添加群友'}<//>
  </fieldset>`;
}

/**
 * Create (`groups.enable`) or edit (`groups.update`) a group space.
 * @param {{open: boolean, onClose: () => void, group?: any, onSaved?: (g: any) => void}} p  group: G & {roster} when editing
 */
export function GroupForm({ open, onClose, group, onSaved }) {
  const editing = !!group;
  const init = () => ({
    group_id: group?.group_id || '', name: group?.name || '', description: group?.description || '',
    auto_refresh_min: group?.auto_refresh_min ?? 0,
    members: (group?.roster?.members || []).map(newRow), bots: (group?.roster?.bots || []).map(newRow),
  });
  const [f, setF] = useState(init);
  const [tried, setTried] = useState(false);
  useEffect(() => { if (open) { const v = init(); if (!v.members.length) v.members = [newRow()]; setF(v); setTried(false); } }, [open, group?.id]);
  const v = validateRoster(f.members, f.bots);
  const gidOk = editing || /^\d{5,12}$/.test(f.group_id.trim());
  const ok = v.ok && gidOk && !!f.name.trim();
  const submit = async () => {
    setTried(true);
    if (!ok) return;
    const body = { name: f.name.trim(), description: f.description.trim(), members: clean(f.members), bots: clean(f.bots), auto_refresh_min: Number(f.auto_refresh_min) };
    const g = editing
      ? await act('groups.update', { id: group.id, ...body }, { ok: '群空间已更新' })
      : await act('groups.enable', { platform: 'qq', group_id: f.group_id.trim(), ...body }, { ok: `已把「${body.name}」设为记忆空间` });
    onSaved?.(g);
    onClose();
  };
  return html`<${Modal} open=${open} onClose=${onClose} wide title=${editing ? `编辑「${group.name}」` : '新建群空间'} footer=${html`
    ${tried && !ok ? html`<span class="field-error grow">${!gidOk ? '群号应为 5–12 位数字' : !f.name.trim() ? '请填写群名称' : v.message}</span>` : null}
    <${Button} variant="ghost" onClick=${onClose}>取消<//>
    <${AsyncButton} variant="primary" icon="check" onClick=${submit}>${editing ? '保存' : '设为记忆空间'}<//>`}>
    ${editing ? null : html`<p class="muted small">把一个 QQ 群设为记忆空间后，鲸鱼会存档群消息（只存在本机），在你点「更新」时整理话题、群友的说话方式和明确说过的喜好。只有名单里的群友会被整理画像；随时可以让某位群友退出记录。</p>`}
    <div class="form-grid">
      <${Field} label="群号" error=${tried && !gidOk ? '5–12 位数字' : ''} hint=${editing ? '群号创建后不能修改' : 'QQ 群资料里的号码'}>
        <input class="input mono" inputmode="numeric" value=${f.group_id} disabled=${editing} placeholder="123456789" onInput=${(e) => setF({ ...f, group_id: e.currentTarget.value.replace(/\D/g, '') })} /><//>
      <${Field} label="名称" error=${tried && !f.name.trim() ? '必填' : ''}><input class="input" value=${f.name} maxlength="40" placeholder="例如：周末跑团群" onInput=${(e) => setF({ ...f, name: e.currentTarget.value })} /><//>
    </div>
    <${Field} label="描述" hint="可选：这个群是做什么的，帮助鲸鱼理解语境"><textarea class="input" rows="2" value=${f.description} placeholder="例如：大学同学，每周六线上跑团" onInput=${(e) => setF({ ...f, description: e.currentTarget.value })}></textarea><//>
    <${Field} label="自动更新"><select class="input" value=${String(f.auto_refresh_min)} onChange=${(e) => setF({ ...f, auto_refresh_min: Number(e.currentTarget.value) })}>
      ${AUTO_REFRESH.map((o) => html`<option key=${o.value} value=${String(o.value)}>${o.label}</option>`)}</select><//>
    <${RosterEditor} title="群友" hint="会被整理画像与喜好的人" rows=${f.members} errors=${tried ? v.rows : {}} onChange=${(rows) => setF({ ...f, members: rows })} />
    <${RosterEditor} title="机器人" hint="消息会存档，但不会被画像" bot rows=${f.bots} errors=${tried ? v.rows : {}} onChange=${(rows) => setF({ ...f, bots: rows })} />
  <//>`;
}

/** @param {number} n */
const mb = (n) => `${(n / 1048576).toFixed(n > 10485760 ? 0 : 1)} MB`;

/**
 * Import a QQ Chat Exporter JSON file (`groups.import`). Accepts a dropped/picked File.
 * @param {{open: boolean, onClose: () => void, group: any, initialFile?: any, onDone?: (r: any) => void}} p
 */
export function ImportChat({ open, onClose, group, initialFile, onDone }) {
  const [file, setFile] = useState(/** @type {any} */ (null));
  const [info, setInfo] = useState(/** @type {any} */ (null));
  const [over, setOver] = useState(false);
  const [result, setResult] = useState(/** @type {any} */ (null));
  useEffect(() => { if (open) { setResult(null); setInfo(null); setFile(null); if (initialFile) take(initialFile); } }, [open, initialFile]);
  const take = async (f) => {
    setResult(null);
    if (!f) return;
    if (f.size > MAX_IMPORT_MB * 1048576) { setFile(null); setInfo({ error: `文件有 ${mb(f.size)}，超过 ${MAX_IMPORT_MB} MB 上限。可以按时间分段导出后分别导入。` }); return; }
    const text = await f.text();
    let count = null;
    try {
      const j = JSON.parse(text);
      count = Array.isArray(j?.messages) ? j.messages.length : Array.isArray(j) ? j.length : null;
    } catch { setFile(null); setInfo({ error: '这不是有效的 JSON 文件。请使用 QQ Chat Exporter 导出的 JSON 格式。' }); return; }
    setFile({ name: f.name, size: f.size, text });
    setInfo({ count });
  };
  const run = async () => {
    const r = await act('groups.import', { id: group.id, content: file.text }, { timeout: 180000 });
    setResult(r);
    toast({ kind: 'ok', title: `导入了 ${r.added} 条消息`, body: r.skipped ? `跳过 ${r.skipped} 条（重复或来自已退出记录的群友）` : '' });
    onDone?.(r);
  };
  return html`<${Modal} open=${open} onClose=${onClose} title="导入聊天记录" footer=${html`
    <${Button} variant="ghost" onClick=${onClose}>${result ? '完成' : '取消'}<//>
    ${result ? null : html`<${AsyncButton} variant="primary" icon="upload" disabled=${!file} onClick=${run}>导入<//>`}`}>
    <p class="muted small">用 <strong>QQ Chat Exporter</strong> 导出这个群的聊天记录（JSON 格式），拖到下面或点击选择。重复的消息会自动跳过；导入后点「更新」整理。</p>
    <label class=${cx('dropzone', over && 'is-over', file && 'has-file')}
      onDragOver=${(e) => { e.preventDefault(); setOver(true); }} onDragLeave=${() => setOver(false)}
      onDrop=${(e) => { e.preventDefault(); setOver(false); take(e.dataTransfer?.files?.[0]); }}>
      <input type="file" class="sr-only" accept=".json,application/json" onChange=${(e) => take(e.currentTarget.files?.[0])} />
      <${Icon} name=${file ? 'file' : 'upload'} size=${26} />
      ${file ? html`<strong>${file.name}</strong><span class="muted small">${mb(file.size)}${info?.count != null ? ` · 约 ${info.count} 条消息` : ''}</span>`
        : html`<strong>拖入 JSON 文件，或点击选择</strong><span class="muted small">最大 ${MAX_IMPORT_MB} MB</span>`}
    </label>
    ${info?.error ? html`<p class="field-error" role="alert">${info.error}</p>` : null}
    ${result ? html`<p class="banner banner-info small" role="status"><${Icon} name="check" size=${16} />新增 ${result.added} 条，跳过 ${result.skipped} 条。点「更新」让鲸鱼整理这些消息。</p>` : null}
  <//>`;
}

/** @param {{open: boolean, onClose: () => void, group: any, onDeleted?: () => void}} p */
export function DeleteGroup({ open, onClose, group, onDeleted }) {
  const [wipe, setWipe] = useState(true);
  const run = async () => {
    const r = await act('groups.delete', { id: group.id, wipe_memories: wipe });
    toast({ kind: 'ok', title: `已删除群空间「${group.name}」`, body: wipe ? `同时遗忘了 ${r.memories_forgotten ?? 0} 条相关记忆` : '相关记忆已保留' });
    onClose(); onDeleted?.();
  };
  return html`<${Modal} open=${open} onClose=${onClose} title="删除群空间" footer=${html`
    <${Button} variant="ghost" onClick=${onClose}>取消<//><${AsyncButton} variant="danger" icon="trash" onClick=${run}>删除<//>`}>
    <p>删除「${group?.name}」后，这个群的消息存档和整理结果都会被清除，之后不再存档新消息。</p>
    <label class="check-row"><input type="checkbox" checked=${wipe} onChange=${(e) => setWipe(e.currentTarget.checked)} />
      <span><strong>同时遗忘相关记忆</strong><span class="muted small">群友画像和喜好（作用域 group:qq:${group?.group_id}）也一并遗忘。不勾选则保留，鲸鱼仍会记得。</span></span></label>
  <//>`;
}
