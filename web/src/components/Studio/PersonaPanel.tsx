/**
 * 人物卡管理面板（owner 决策 2026-11-01：工作台可直接添加/修改人物卡）。
 *
 * 纪律：
 * - 新建/修改都经服务端严格字段表校验，前端不做安全决策；
 * - 内置卡不可改：只能「复制为新卡」（预填底稿，新 id）；
 * - 编辑先拉详情（含 external_prompt 等完整字段），保存后由父组件整体刷新。
 */

import { useEffect, useState } from "react";
import {
  ApiError,
  studioApi,
  type PersonaCardInput,
  type PersonaEffectiveSnapshot,
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
  name: "鲸鱼",
  category: "custom",
  summary: "",
  temperament: "",
  relationship_style: "",
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
    name: d.name ?? "鲸鱼",
    category: d.category ?? "custom",
    summary: d.summary ?? "",
    temperament: d.temperament ?? "",
    relationship_style: d.relationship_style ?? "",
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

export default function PersonaPanel({
  personas,
  busy,
  onChanged,
  onError,
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

  const openCreate = () => {
    setForm(EMPTY_FORM);
    setFormError("");
    setMode({ kind: "create" });
  };

  const openCopy = (id: string) => {
    setLoading(true);
    setFormError("");
    void (async () => {
      try {
        const detail = await studioApi.personaDetail(id);
        setForm({ ...formFromDetail(detail), card_id: "", title: `${detail.title}（副本）` });
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
        setForm(formFromDetail(detail));
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
    const input: PersonaCardInput = {
      title,
      name: form.name.trim() || "鲸鱼",
      category: form.category,
      summary: form.summary,
      temperament: form.temperament,
      relationship_style: form.relationship_style,
      tone: form.tone,
      address_user_as: form.address_user_as.trim() || "你",
      voice_hint: form.voice_hint,
      speaking_rate: rate,
      style_rules: lines(form.style_rules),
      extra_taboos: lines(form.extra_taboos),
      external_prompt: form.external_prompt,
      description: form.description,
      author: form.author,
      source_url: form.source_url,
      license: form.license,
      artwork_ref: form.artwork_ref,
    };
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
        setMode({ kind: "closed" });
      } catch (e) {
        setFormError(e instanceof ApiError ? e.message : "保存失败，请重试。");
      } finally {
        setSaving(false);
      }
    })();
  };

  const editing = mode.kind !== "closed";

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
            {preview.snap.model_context || "（空——对话将使用低负担默认人格）"}
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
          <div className="persona-form-grid">
            <label>
              标题 * <DestBadge kind="meta" />
              <input value={form.title} onChange={set("title")} required />
            </label>
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
              角色名 <DestBadge kind="model" />
              <input value={form.name} onChange={set("name")} />
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
              <input value={form.address_user_as} onChange={set("address_user_as")} />
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
            简短设定（summary） <DestBadge kind="model" />
            <textarea rows={2} value={form.summary} onChange={set("summary")} />
          </label>
          <label className="persona-form-wide">
            气质 / 性情倾向 <DestBadge kind="model" />
            <textarea rows={2} value={form.temperament} onChange={set("temperament")} />
          </label>
          <label className="persona-form-wide">
            关系倾向 <DestBadge kind="model" />
            <textarea
              rows={2}
              value={form.relationship_style}
              onChange={set("relationship_style")}
            />
          </label>
          <label className="persona-form-wide">
            人设原文（external_prompt，作为外部上下文注入，非指令） <DestBadge kind="model" />
            <textarea
              rows={6}
              value={form.external_prompt}
              onChange={set("external_prompt")}
            />
          </label>
          <label className="persona-form-wide">
            风格规则（每行一条） <DestBadge kind="model" />
            <textarea rows={3} value={form.style_rules} onChange={set("style_rules")} />
          </label>
          <label className="persona-form-wide">
            禁忌（每行一条） <DestBadge kind="model" />
            <textarea rows={2} value={form.extra_taboos} onChange={set("extra_taboos")} />
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
          {formError && (
            <p className="banner error" role="alert">
              {formError}
            </p>
          )}
          <div className="persona-form-actions">
            <button type="submit" className="primary" disabled={saving || busy}>
              {saving ? "保存中…" : "保存"}
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
