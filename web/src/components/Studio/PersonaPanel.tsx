/**
 * 人物卡管理面板（owner 决策 2026-11-01：工作台可直接添加/修改人物卡）。
 *
 * 纪律：
 * - 新建/修改都经服务端严格字段表校验，前端不做安全决策；
 * - 内置卡不可改：只能「复制为新卡」（预填底稿，新 id）；
 * - 编辑先拉详情（含 external_prompt 等完整字段），保存后由父组件整体刷新；
 * - issue #37：表单分区（默认 5+1 字段 / 进阶设置折叠）、从模板开始、
 *   导入预览（不写库，确认后才填进表单）、即时试玩（草稿对比默认人格）、
 *   localStorage 草稿（恢复/放弃/撤销）。
 * - 表单只出现人格内容字段：不出现权限/工具 scope/Vault/安全策略控件。
 */

import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  studioApi,
  type PersonaCardInput,
  type PersonaEffectiveSnapshot,
  type PersonaImportPreview,
  type PersonaPlaytestResult,
  type PersonaPlaytestSide,
  type StudioPersona,
  type StudioPersonaDetail,
} from "../../api";

/** issue #36：字段去向徽标——保存的字段到底去哪里，一眼可见。 */
const FIELD_BADGE = {
  model: { text: "模型上下文", cls: "dest-model" },
  tts: { text: "仅语音", cls: "dest-tts" },
  meta: { text: "元数据", cls: "dest-meta" },
} as const;

function DestBadge({ kind, note }: { kind: keyof typeof FIELD_BADGE; note?: string }) {
  const b = FIELD_BADGE[kind];
  return (
    <span className={`dest-badge ${b.cls}`} title={note || b.text}>
      {b.text}
    </span>
  );
}

interface Props {
  personas: StudioPersona[];
  busy: boolean;
  onChanged: (message: string) => void;
  onError: (message: string) => void;
  /** issue #44：首页「新建人物卡」快捷入口——值变化时自动打开新建表单。 */
  createSignal?: number;
  /** 消费回调：打开表单后通知外部复位信号（避免视图切换重挂时重复打开）。 */
  onCreateSignalConsumed?: () => void;
}

type EditorMode =
  | { kind: "closed" }
  | { kind: "create"; base?: StudioPersonaDetail }
  | { kind: "edit"; id: string };

interface FormState {
  card_id: string;
  title: string;
  name: string;
  category: string;
  summary: string;
  temperament: string;
  relationship_style: string;
  example_dialogue: string;
  tone: string;
  address_user_as: string;
  voice_hint: string;
  speaking_rate: string;
  style_rules: string;
  extra_taboos: string;
  external_prompt: string;
  description: string;
  author: string;
  source_url: string;
  license: string;
  artwork_ref: string;
}

const EMPTY_FORM: FormState = {
  card_id: "",
  title: "",
  name: "",
  category: "custom",
  summary: "",
  temperament: "",
  relationship_style: "",
  example_dialogue: "",
  tone: "",
  address_user_as: "你",
  voice_hint: "",
  speaking_rate: "1.0",
  style_rules: "",
  extra_taboos: "",
  external_prompt: "",
  description: "",
  author: "",
  source_url: "",
  license: "",
  artwork_ref: "",
};

const CATEGORIES: { value: string; label: string }[] = [
  { value: "companion", label: "陪伴" },
  { value: "group_chat", label: "群聊" },
  { value: "private", label: "私聊" },
  { value: "mini", label: "轻量" },
  { value: "custom", label: "自定义" },
];

function formFromDetail(d: StudioPersonaDetail): FormState {
  return {
    card_id: d.id,
    title: d.title ?? "",
    name: d.name ?? "",
    category: d.category ?? "custom",
    summary: d.summary ?? "",
    temperament: d.temperament ?? "",
    relationship_style: d.relationship_style ?? "",
    example_dialogue: d.example_dialogue ?? "",
    tone: d.tone ?? "",
    address_user_as: d.address_user_as ?? "你",
    voice_hint: d.voice_hint ?? "",
    speaking_rate: String(d.speaking_rate ?? 1.0),
    style_rules: (d.style_rules ?? []).join("\n"),
    extra_taboos: (d.extra_taboos ?? []).join("\n"),
    external_prompt: d.external_prompt ?? "",
    description: d.description ?? "",
    author: d.author ?? "",
    source_url: d.source_url ?? "",
    license: d.license ?? "",
    artwork_ref: d.artwork_ref ?? "",
  };
}

function lines(text: string): string[] {
  return text
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}

function sameForm(a: FormState, b: FormState): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

const SOURCE_TEXT: Record<string, string> = {
  none: "未选择角色",
  global_active: "全局活跃卡",
  global_legacy: "全局旧卡",
  conversation_bound: "会话绑定卡",
  unsaved_draft: "未保存草稿",
};

function sourceText(source: string): string {
  return SOURCE_TEXT[source] ?? (source || "-");
}

export default function PersonaPanel({
  personas,
  busy,
  onChanged,
  onError,
  createSignal,
  onCreateSignalConsumed,
}: Props) {
  const [mode, setMode] = useState<EditorMode>({ kind: "closed" });
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState("");
  const [preview, setPreview] = useState<{
    id: string;
    snap: PersonaEffectiveSnapshot;
  } | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  // issue #37：草稿 / 导入 / 试玩
  const [pendingDraft, setPendingDraft] = useState<FormState | null>(null);
  const [importText, setImportText] = useState("");
  const [importPreview, setImportPreview] = useState<PersonaImportPreview | null>(null);
  const [importError, setImportError] = useState("");
  const [importLoading, setImportLoading] = useState(false);
  const [playInput, setPlayInput] = useState("");
  const [playResult, setPlayResult] = useState<PersonaPlaytestResult | null>(null);
  const [playError, setPlayError] = useState("");
  const [playLoading, setPlayLoading] = useState(false);
  /** 本次打开表单时的初始值（「撤销」重置目标 + 草稿差异判断基准）。 */
  const initialRef = useRef<FormState>(EMPTY_FORM);

  const editing = mode.kind !== "closed";
  const draftKey =
    mode.kind === "edit" ? `whale.persona-draft.${mode.id}` : "whale.persona-draft.new";

  /** 打开表单时读取 localStorage 草稿；与初始值不同才提示恢复。 */
  const readDraft = (key: string, initial: FormState): FormState | null => {
    try {
      const raw = localStorage.getItem(key);
      if (!raw) return null;
      const draft = { ...EMPTY_FORM, ...(JSON.parse(raw) as Partial<FormState>) };
      return sameForm(draft, initial) ? null : draft;
    } catch {
      return null;
    }
  };

  // 表单变化自动存草稿（等待恢复决策、未改动时不写）。
  useEffect(() => {
    if (!editing || pendingDraft !== null) return;
    if (sameForm(form, initialRef.current)) return;
    try {
      localStorage.setItem(draftKey, JSON.stringify(form));
    } catch {
      /* 存储配额满等异常忽略 */
    }
  }, [form, editing, pendingDraft, draftKey]);

  const openPreview = (id: string) => {
    setPreviewLoading(true);
    void (async () => {
      try {
        const snap = await studioApi.personaEffective(id);
        setPreview({ id, snap });
      } catch (e) {
        onError(e instanceof ApiError ? e.message : "读取生效预览失败。");
      } finally {
        setPreviewLoading(false);
      }
    })();
  };

  /** 打开表单前的通用复位：导入/试玩/错误都清掉。 */
  const resetTransient = () => {
    setFormError("");
    setImportText("");
    setImportPreview(null);
    setImportError("");
    setPlayInput("");
    setPlayResult(null);
    setPlayError("");
  };

  const openCreate = () => {
    resetTransient();
    setForm(EMPTY_FORM);
    initialRef.current = EMPTY_FORM;
    setPendingDraft(readDraft("whale.persona-draft.new", EMPTY_FORM));
    setMode({ kind: "create" });
  };

  // issue #44：外部快捷入口触发新建（信号消费后由外部复位，重挂不重复打开）。
  const consumedSignalRef = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (createSignal && createSignal !== consumedSignalRef.current) {
      consumedSignalRef.current = createSignal;
      openCreate();
      onCreateSignalConsumed?.();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [createSignal]);

  const openCopy = (id: string) => {
    setLoading(true);
    setFormError("");
    void (async () => {
      try {
        const detail = await studioApi.personaDetail(id);
        const base = { ...formFromDetail(detail), card_id: "", title: `${detail.title}（副本）` };
        resetTransient();
        setForm(base);
        initialRef.current = base;
        setPendingDraft(readDraft("whale.persona-draft.new", base));
        setMode({ kind: "create", base: detail });
      } catch (e) {
        onError(e instanceof ApiError ? e.message : "读取卡详情失败。");
      } finally {
        setLoading(false);
      }
    })();
  };

  const openEdit = (id: string) => {
    setLoading(true);
    setFormError("");
    void (async () => {
      try {
        const detail = await studioApi.personaDetail(id);
        const base = formFromDetail(detail);
        resetTransient();
        setForm(base);
        initialRef.current = base;
        setPendingDraft(readDraft(`whale.persona-draft.${id}`, base));
        setMode({ kind: "edit", id });
      } catch (e) {
        onError(e instanceof ApiError ? e.message : "读取卡详情失败。");
      } finally {
        setLoading(false);
      }
    })();
  };

  // 切换编辑目标时清掉旧错误。
  useEffect(() => {
    setFormError("");
  }, [mode.kind]);

  const set = (key: keyof FormState) => (
    e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>,
  ) => setForm((f) => ({ ...f, [key]: e.target.value }));

  /** 从表单内容构造卡字段（保存与试玩共用；语速非法时试玩侧回退 1.0）。 */
  const buildCardInput = (): PersonaCardInput => {
    const rate = Number.parseFloat(form.speaking_rate);
    return {
      title: form.title.trim(),
      name: form.name.trim() || "鲸鱼",
      category: form.category,
      summary: form.summary,
      temperament: form.temperament,
      relationship_style: form.relationship_style,
      example_dialogue: form.example_dialogue,
      tone: form.tone,
      address_user_as: form.address_user_as.trim() || "你",
      voice_hint: form.voice_hint,
      speaking_rate: Number.isFinite(rate) ? rate : 1.0,
      style_rules: lines(form.style_rules),
      extra_taboos: lines(form.extra_taboos),
      external_prompt: form.external_prompt,
      description: form.description,
      author: form.author,
      source_url: form.source_url,
      license: form.license,
      artwork_ref: form.artwork_ref,
    };
  };

  const save = () => {
    const title = form.title.trim();
    if (!title) {
      setFormError("标题不能为空。");
      return;
    }
    const rate = Number.parseFloat(form.speaking_rate);
    if (!Number.isFinite(rate) || rate < 0.5 || rate > 2.0) {
      setFormError("语速须是 [0.5, 2.0] 内的数字。");
      return;
    }
    const input = buildCardInput();
    setSaving(true);
    setFormError("");
    void (async () => {
      try {
        if (mode.kind === "edit") {
          await studioApi.updatePersona(mode.id, input);
          onChanged(`人物卡「${title}」已保存。`);
        } else {
          const cid = form.card_id.trim().toLowerCase();
          const created = await studioApi.createPersona(
            cid ? { ...input, card_id: cid } : input,
          );
          onChanged(`人物卡「${created.title}」已创建（${created.id}）。`);
        }
        try {
          localStorage.removeItem(draftKey);
        } catch {
          /* 忽略 */
        }
        setMode({ kind: "closed" });
      } catch (e) {
        setFormError(e instanceof ApiError ? e.message : "保存失败，请重试。");
      } finally {
        setSaving(false);
      }
    })();
  };

  // ---- issue #37：导入预览（仅新建；预览不写库） ----

  const runImportPreview = () => {
    const text = importText.trim();
    if (!text) {
      setImportError("请先粘贴卡文本（SillyTavern JSON 或纯文本）。");
      return;
    }
    setImportLoading(true);
    setImportError("");
    setImportPreview(null);
    void (async () => {
      try {
        setImportPreview(
          await studioApi.personaImportPreview(
            text,
            form.card_id.trim() || undefined,
          ),
        );
      } catch (e) {
        setImportError(
          e instanceof ApiError ? e.message : "解析失败，请检查文本格式后重试。",
        );
      } finally {
        setImportLoading(false);
      }
    })();
  };

  const applyImport = () => {
    if (!importPreview) return;
    const p = importPreview.parsed;
    setForm((f) => ({
      ...f,
      ...(p.name ? { name: p.name } : {}),
      ...(p.summary ? { summary: p.summary } : {}),
      ...(p.temperament ? { temperament: p.temperament } : {}),
      ...(p.relationship_style
        ? { relationship_style: p.relationship_style }
        : {}),
      ...(p.example_dialogue ? { example_dialogue: p.example_dialogue } : {}),
      ...(p.external_prompt ? { external_prompt: p.external_prompt } : {}),
      // 有冲突时不预填卡 ID（保存将由服务端生成新 id）
      ...(importPreview.suggested_card_id &&
        importPreview.conflicts.length === 0
        ? { card_id: importPreview.suggested_card_id }
        : {}),
    }));
  };

  // ---- issue #37：即时试玩（当前表单内容作为草稿卡） ----

  const runPlaytest = () => {
    setPlayLoading(true);
    setPlayError("");
    setPlayResult(null);
    void (async () => {
      try {
        setPlayResult(
          await studioApi.personaPlaytest({
            card: buildCardInput(),
            ...(playInput.trim() ? { input: playInput.trim() } : {}),
          }),
        );
      } catch (e) {
        if (e instanceof ApiError && e.status === 503) {
          // 模型未装配：友好提示，不当成表单错误
          setPlayError(e.message || "模型未装配：请先在设置页配置模型。");
        } else {
          setPlayError(
            e instanceof ApiError ? e.message : "试玩失败，请稍后重试。",
          );
        }
      } finally {
        setPlayLoading(false);
      }
    })();
  };

  // ---- issue #37：草稿操作 ----

  const restoreDraft = () => {
    if (pendingDraft) setForm(pendingDraft);
    setPendingDraft(null);
  };

  const discardDraft = () => {
    try {
      localStorage.removeItem(draftKey);
    } catch {
      /* 忽略 */
    }
    setPendingDraft(null);
  };

  /** 撤销：重置为本次打开表单时的初始值（草稿一并清掉）。 */
  const undoChanges = () => {
    setForm(initialRef.current);
    try {
      localStorage.removeItem(draftKey);
    } catch {
      /* 忽略 */
    }
  };

  return (
    <section className="studio-inspector persona-panel" aria-label="人物卡管理">
      <h3>人物卡管理</h3>
      <div className="persona-list">
        {personas.map((p) => (
          <div className="persona-row" key={p.id}>
            <span className="persona-row-title">
              {p.title}
              <code className="persona-row-id">{p.id}</code>
              {p.builtin && <span className="persona-badge">内置</span>}
            </span>
            <span className="persona-row-actions">
              {!p.builtin && (
                <button
                  type="button"
                  disabled={busy || loading}
                  onClick={() => openEdit(p.id)}
                >
                  编辑
                </button>
              )}
              <button
                type="button"
                disabled={busy || loading || previewLoading}
                onClick={() => openPreview(p.id)}
                title="查看这张卡保存后实际进入模型请求的内容"
              >
                生效预览
              </button>
              <button
                type="button"
                disabled={busy || loading}
                onClick={() => openCopy(p.id)}
              >
                复制为新卡
              </button>
            </span>
          </div>
        ))}
      </div>
      {preview && (
        <div className="persona-effective" aria-label="生效预览">
          <h4>
            生效预览：{preview.snap.card_title}
            <code className="persona-row-id">{preview.id}</code>
            <button type="button" onClick={() => setPreview(null)}>
              关闭
            </button>
          </h4>
          <p className="persona-effective-meta">
            来源：
            {preview.snap.source === "conversation_bound"
              ? "会话绑定卡"
              : preview.snap.source === "global_active"
                ? "全局活跃卡"
                : "内置默认卡"}
            {"　"}内容指纹 <code>{preview.snap.content_sha}</code>
            {preview.snap.fallback_reason &&
              `　回退原因：${preview.snap.fallback_reason}`}
          </p>
          <pre className="persona-effective-context">
            {preview.snap.model_context || "（无角色设置——使用中性表达）"}
          </pre>
          <p className="persona-effective-meta">
            语音专用：音色 {preview.snap.voice.timbre_hint || "-"} · 语速{" "}
            {preview.snap.voice.speaking_rate ?? "-"}
          </p>
        </div>
      )}
      {!editing && (
        <button
          type="button"
          className="primary"
          disabled={busy || loading}
          onClick={openCreate}
        >
          新建人物卡
        </button>
      )}

      {editing && (
        <form
          className="persona-form"
          onSubmit={(e) => {
            e.preventDefault();
            save();
          }}
        >
          <h4>{mode.kind === "edit" ? `编辑：${mode.id}` : "新建人物卡"}</h4>

          {pendingDraft && (
            <div className="banner warn" role="status">
              检测到未保存的草稿。
              <button type="button" onClick={restoreDraft}>
                恢复草稿
              </button>
              <button type="button" onClick={discardDraft}>
                放弃草稿
              </button>
            </div>
          )}

          {mode.kind === "create" && (
            <details className="persona-import">
              <summary>导入 SillyTavern / 纯文本卡</summary>
              <textarea
                aria-label="导入文本"
                rows={4}
                placeholder="粘贴 SillyTavern v1/v2 JSON 或纯文本卡…"
                value={importText}
                onChange={(e) => setImportText(e.target.value)}
              />
              <div className="actions">
                <button
                  type="button"
                  disabled={importLoading}
                  onClick={runImportPreview}
                >
                  {importLoading ? "解析中…" : "解析预览"}
                </button>
              </div>
              {importError && (
                <p className="banner error" role="alert">
                  {importError}
                </p>
              )}
              {importPreview && (
                <div className="import-preview" aria-label="导入预览">
                  {importPreview.warnings.map((w) => (
                    <p className="banner warn" key={w}>
                      {w}
                    </p>
                  ))}
                  {importPreview.missing.length > 0 && (
                    <p className="banner warn">
                      缺少建议字段：{importPreview.missing.join("、")}
                      （可导入后手动补全）。
                    </p>
                  )}
                  {importPreview.conflicts.map((c) => (
                    <p className="banner warn" key={c}>
                      {c}
                    </p>
                  ))}
                  {Object.keys(importPreview.mapping).length > 0 && (
                    <table className="table">
                      <thead>
                        <tr>
                          <th>原字段</th>
                          <th>目标字段</th>
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(importPreview.mapping).map(([k, v]) => (
                          <tr key={k}>
                            <td className="mono">{k}</td>
                            <td className="mono">{v}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                  <span className="section-label">解析出的字段值</span>
                  {Object.entries(importPreview.parsed).map(([k, v]) => (
                    <div className="kv-row" key={k}>
                      <span className="key mono">{k}</span>
                      <span className="value">{v}</span>
                    </div>
                  ))}
                  {importPreview.unmapped.length > 0 && (
                    <>
                      <span className="section-label">未映射字段</span>
                      {importPreview.unmapped.map((u) => (
                        <div className="kv-row" key={u.field}>
                          <span className="key mono">{u.field}</span>
                          <span className="value">
                            {u.note}
                            {u.preview ? `（${u.preview}）` : ""}
                          </span>
                        </div>
                      ))}
                    </>
                  )}
                  <div className="actions">
                    <button
                      type="button"
                      className="primary"
                      onClick={applyImport}
                    >
                      确认导入
                    </button>
                  </div>
                </div>
              )}
            </details>
          )}

          <div className="persona-form-basic">
            <div className="persona-form-grid">
              <label>
                标题 * <DestBadge kind="meta" />
                <input value={form.title} onChange={set("title")} required />
              </label>
              <label>
                角色名 <DestBadge kind="model" />
                <input value={form.name} onChange={set("name")} />
              </label>
            </div>
            <label className="persona-form-wide">
              简短介绍 <DestBadge kind="model" />
              <textarea rows={2} value={form.summary} onChange={set("summary")} />
            </label>
            <label className="persona-form-wide">
              性格特点 <DestBadge kind="model" />
              <textarea
                rows={2}
                value={form.temperament}
                onChange={set("temperament")}
              />
            </label>
            <label className="persona-form-wide">
              相处方式 <DestBadge kind="model" />
              <textarea
                rows={2}
                value={form.relationship_style}
                onChange={set("relationship_style")}
              />
            </label>
            <label className="persona-form-wide">
              示例对话 <DestBadge kind="model" />
              <textarea
                rows={3}
                placeholder={"用户：…\n角色：…"}
                value={form.example_dialogue}
                onChange={set("example_dialogue")}
              />
            </label>
          </div>

          <details className="persona-form-advanced">
            <summary>进阶设置</summary>
            <div className="persona-form-grid">
              <label>
                卡 ID（留空自动生成） <DestBadge kind="meta" />
                <input
                  value={form.card_id}
                  onChange={set("card_id")}
                  disabled={mode.kind === "edit"}
                  placeholder="小写字母/数字/连字符"
                />
              </label>
              <label>
                分类 <DestBadge kind="meta" />
                <select value={form.category} onChange={set("category")}>
                  {CATEGORIES.map((c) => (
                    <option key={c.value} value={c.value}>
                      {c.label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                称呼用户为 <DestBadge kind="model" />
                <input
                  value={form.address_user_as}
                  onChange={set("address_user_as")}
                />
              </label>
              <label>
                语速（0.5–2.0） <DestBadge kind="tts" />
                <input
                  type="number"
                  step="0.05"
                  min="0.5"
                  max="2.0"
                  value={form.speaking_rate}
                  onChange={set("speaking_rate")}
                />
              </label>
              <label>
                语气 <DestBadge kind="model" />
                <input value={form.tone} onChange={set("tone")} />
              </label>
              <label>
                音色提示 <DestBadge kind="tts" />
                <input value={form.voice_hint} onChange={set("voice_hint")} />
              </label>
            </div>
            <label className="persona-form-wide">
              人设原文（external_prompt，作为外部上下文注入，非指令）{" "}
              <DestBadge kind="model" />
              <textarea
                rows={6}
                value={form.external_prompt}
                onChange={set("external_prompt")}
              />
            </label>
            <label className="persona-form-wide">
              风格规则（每行一条） <DestBadge kind="model" />
              <textarea
                rows={3}
                value={form.style_rules}
                onChange={set("style_rules")}
              />
            </label>
            <label className="persona-form-wide">
              禁忌（每行一条） <DestBadge kind="model" />
              <textarea
                rows={2}
                value={form.extra_taboos}
                onChange={set("extra_taboos")}
              />
            </label>
            <div className="persona-form-grid">
              <label>
                描述 <DestBadge kind="meta" />
                <input value={form.description} onChange={set("description")} />
              </label>
              <label>
                作者 <DestBadge kind="meta" />
                <input value={form.author} onChange={set("author")} />
              </label>
              <label>
                来源链接 <DestBadge kind="meta" />
                <input value={form.source_url} onChange={set("source_url")} />
              </label>
              <label>
                许可证 <DestBadge kind="meta" />
                <input value={form.license} onChange={set("license")} />
              </label>
              <label>
                形象引用 <DestBadge kind="meta" />
                <input value={form.artwork_ref} onChange={set("artwork_ref")} />
              </label>
            </div>
          </details>

          <div className="persona-playtest">
            <span className="section-label">即时试玩（不保存、不写记忆）</span>
            <div className="memory-toolbar">
              <input
                aria-label="试玩输入"
                placeholder="留空使用固定试玩场景…"
                value={playInput}
                onChange={(e) => setPlayInput(e.target.value)}
              />
              <button
                type="button"
                disabled={playLoading || busy}
                onClick={runPlaytest}
              >
                {playLoading ? "试玩中…" : "试玩"}
              </button>
            </div>
            {playError && (
              <p className="banner warn" role="status">
                {playError}
              </p>
            )}
            {playResult && (
              <div aria-label="试玩对比">
                <p className="muted small">
                  输入：{playResult.input}　{playResult.note}
                </p>
                <div className="playtest-compare">
                  <PlaytestSideView title="当前生效配置（可能无角色）" side={playResult.baseline} />
                  <PlaytestSideView title="当前卡（草稿）" side={playResult.trial} />
                </div>
              </div>
            )}
          </div>

          {formError && (
            <p className="banner error" role="alert">
              {formError}
            </p>
          )}
          <div className="persona-form-actions">
            <button type="submit" className="primary" disabled={saving || busy}>
              {saving ? "保存中…" : "保存"}
            </button>
            <button type="button" disabled={saving} onClick={undoChanges}>
              撤销
            </button>
            <button
              type="button"
              disabled={saving}
              onClick={() => setMode({ kind: "closed" })}
            >
              取消
            </button>
          </div>
        </form>
      )}
    </section>
  );
}

/** 试玩对比的一侧：reply 或 error、content_sha、生效字段标签。 */
function PlaytestSideView({
  title,
  side,
}: {
  title: string;
  side: PersonaPlaytestSide;
}) {
  return (
    <div className="playtest-side">
      <h5>{title}</h5>
      <div className="muted small">
        {sourceText(side.source)} · 内容指纹{" "}
        <code>{side.content_sha || "-"}</code>
      </div>
      {side.reply !== undefined ? (
        <pre className="playtest-reply">{side.reply}</pre>
      ) : (
        <p className="banner warn">生成失败：{side.error ?? "未知错误"}</p>
      )}
      {side.fields_used && side.fields_used.length > 0 && (
        <div className="record-tags">
          <span className="muted small">生效字段：</span>
          {side.fields_used.map((f) => (
            <span key={f} className="badge soft">
              {f}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
