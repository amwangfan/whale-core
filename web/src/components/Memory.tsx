/**
 * 记忆管理面（主体优先视图）：
 * - 顶部按「主体」分组：主人空间 / 群友 / 群聊 / 其他空间（fork + 未分区遗留计数）；
 * - 选中一个主体后才加载其记录（GET /studio/memory/browse），不混排；
 * - 群友私有空间对管理面（owner）全量可见（owner 决策 2026-11-01）；对话召回侧仍仅本人可见；
 * - 行级治理：纠错 / 标记过期 / 删除；空间级「暂停记录 / 恢复记录」开关；
 * - 底部「高级」保留原全局关键词搜索与 NDJSON 导出。
 */
import { useCallback, useEffect, useState } from "react";
import {
  api,
  ApiError,
  studioApi,
  type MemoryBrowseItem,
  type MemoryBrowseResult,
  type MemoryHit,
  type MemoryMemberSubject,
  type MemorySpaceSubject,
  type MemorySubjects,
} from "../api";

/** 当前选中的浏览主体：空间直选 或 群友（按 principal_id）。 */
type Selector =
  | {
      kind: "space";
      spaceId: string;
      name: string;
      paused: boolean;
      scopeNote: string;
    }
  | {
      kind: "principal";
      principalId: string;
      spaceId: string;
      name: string;
      paused: boolean;
      scopeNote: string;
    };

function selectorParams(sel: Selector): {
  space_id?: string;
  principal_id?: string;
} {
  return sel.kind === "space"
    ? { space_id: sel.spaceId }
    : { principal_id: sel.principalId };
}

function fmtTime(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString("zh-CN", { hour12: false });
}

function isExpired(iso?: string | null): boolean {
  if (!iso) return false;
  const t = new Date(iso).getTime();
  return !Number.isNaN(t) && t <= Date.now();
}

/** 403 受限场景换成友好文案，其余沿用错误消息。 */
function friendlyError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.code === "member_private_restricted")
      return "群友私有记忆只能由本人管理，此处不可改写。";
    if (e.code === "vault_restricted")
      return "原始库记录为只读，不能在管理面改写；如需纠错请新建工作副本。";
    return e.message;
  }
  return e instanceof Error ? e.message : String(e);
}

export default function MemoryView() {
  const [subjects, setSubjects] = useState<MemorySubjects | null>(null);
  const [subjectsLoading, setSubjectsLoading] = useState(false);
  const [sel, setSel] = useState<Selector | null>(null);
  const [browse, setBrowse] = useState<MemoryBrowseResult | null>(null);
  const [browseLoading, setBrowseLoading] = useState(false);
  const [kindFilter, setKindFilter] = useState("");
  const [qInput, setQInput] = useState("");
  const [qApplied, setQApplied] = useState("");
  const [includeSuperseded, setIncludeSuperseded] = useState(false);
  const [limit, setLimit] = useState(50);
  const [offset, setOffset] = useState(0);
  const [reloadKey, setReloadKey] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // ---- 高级区：全局关键词搜索（旧版行为保留） ----
  const [advQ, setAdvQ] = useState("");
  const [advHits, setAdvHits] = useState<MemoryHit[] | null>(null);
  const [advLoading, setAdvLoading] = useState(false);

  const loadSubjects = useCallback(async () => {
    setSubjectsLoading(true);
    try {
      const subs = await studioApi.memorySubjects();
      setSubjects(subs);
      // 刷新后同步选中主体的暂停态（pause 开关操作后状态会变）。
      setSel((prev) => {
        if (!prev) return prev;
        const spaces: MemorySpaceSubject[] = [
          ...subs.owner.spaces,
          ...subs.groups,
          ...subs.forks,
        ];
        const sid = prev.spaceId;
        const hit =
          spaces.find((s) => s.space_id === sid) ??
          subs.members.find((m) => m.space_id === sid);
        return hit ? { ...prev, paused: hit.paused } : prev;
      });
    } catch (e) {
      setError(friendlyError(e));
    } finally {
      setSubjectsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSubjects();
  }, [loadSubjects]);

  useEffect(() => {
    if (!sel) return;
    let cancelled = false;
    void (async () => {
      setBrowseLoading(true);
      setError(null);
      try {
        const res = await studioApi.memoryBrowse({
          ...selectorParams(sel),
          kind: kindFilter || undefined,
          q: qApplied || undefined,
          include_superseded: includeSuperseded || undefined,
          limit,
          offset,
        });
        if (!cancelled) setBrowse(res);
      } catch (e) {
        if (!cancelled) {
          setBrowse(null);
          setError(friendlyError(e));
        }
      } finally {
        if (!cancelled) setBrowseLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sel, kindFilter, qApplied, includeSuperseded, limit, offset, reloadKey]);

  const pickSpace = (s: MemorySpaceSubject) => {
    setSel({
      kind: "space",
      spaceId: s.space_id,
      name: s.name,
      paused: s.paused,
      scopeNote: s.scope_note,
    });
    setBrowse(null);
    setOffset(0);
    setQApplied("");
    setQInput("");
    setNotice(null);
    setError(null);
  };

  const pickMember = (m: MemoryMemberSubject) => {
    setSel({
      kind: "principal",
      principalId: m.principal_id,
      spaceId: m.space_id,
      name: m.principal_id,
      paused: m.paused,
      scopeNote: m.scope_note,
    });
    setBrowse(null);
    setOffset(0);
    setQApplied("");
    setQInput("");
    setNotice(null);
    setError(null);
  };

  const isActiveSpace = (spaceId: string) =>
    sel !== null && sel.spaceId === spaceId;
  const isActiveMember = (m: MemoryMemberSubject) =>
    sel !== null && sel.kind === "principal" && sel.principalId === m.principal_id;

  const togglePause = async (
    spaceId: string,
    paused: boolean,
    name: string,
  ) => {
    setError(null);
    setNotice(null);
    try {
      await studioApi.memoryPause(spaceId, !paused);
      setNotice(!paused ? `已暂停记录：${name}` : `已恢复记录：${name}`);
      await loadSubjects();
    } catch (e) {
      setError(friendlyError(e));
    }
  };

  const correct = async (item: MemoryBrowseItem) => {
    const input = prompt(
      `为记录「${item.key}」输入新的表述：`,
      item.summary ?? "",
    );
    if (input === null) return;
    const newSummary = input.trim();
    if (!newSummary) {
      setError("新表述不能为空。");
      return;
    }
    setError(null);
    setNotice(null);
    try {
      const r = await studioApi.memoryCorrect(item.key, newSummary);
      setNotice(`已纠正：${r.old_key} → ${r.new_key}`);
      setReloadKey((k) => k + 1);
      void loadSubjects();
    } catch (e) {
      setError(friendlyError(e));
    }
  };

  const expire = async (key: string) => {
    if (!confirm(`将记录「${key}」标记为过期？过期后不再参与召回，但内容保留。`))
      return;
    setError(null);
    setNotice(null);
    try {
      await studioApi.memoryExpire(key);
      setNotice(`已标记过期：${key}`);
      setReloadKey((k) => k + 1);
    } catch (e) {
      setError(friendlyError(e));
    }
  };

  const remove = async (key: string) => {
    if (!confirm(`永久删除记录「${key}」？此操作不可撤销。`)) return;
    setError(null);
    setNotice(null);
    try {
      const r = await studioApi.memoryDelete(key);
      setNotice(r.deleted ? `已删除：${key}` : `未找到：${key}`);
      setReloadKey((k) => k + 1);
      void loadSubjects();
    } catch (e) {
      setError(friendlyError(e));
    }
  };

  // ---- 高级区行为 ----

  const advSearch = async () => {
    setAdvLoading(true);
    setError(null);
    setNotice(null);
    try {
      setAdvHits(await api.searchMemory(advQ.trim()));
    } catch (e) {
      setError(friendlyError(e));
    } finally {
      setAdvLoading(false);
    }
  };

  const advRemove = async (key: string) => {
    if (!confirm(`删除记忆「${key}」？`)) return;
    try {
      const r = await api.deleteMemory(key);
      setNotice(r.deleted ? `已删除：${key}` : `未找到：${key}`);
      if (advHits) setAdvHits(advHits.filter((h) => h.key !== key));
    } catch (e) {
      setError(friendlyError(e));
    }
  };

  const exportAll = async () => {
    setError(null);
    try {
      const text = await api.exportMemory();
      const blob = new Blob([text], { type: "application/x-ndjson" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `whale-memory-${new Date().toISOString().slice(0, 10)}.ndjson`;
      a.click();
      URL.revokeObjectURL(url);
      setNotice("已导出 NDJSON。");
    } catch (e) {
      setError(friendlyError(e));
    }
  };

  const renderSpaceRow = (s: MemorySpaceSubject) => (
    <li className="subject-item" key={s.space_id}>
      <button
        className={`subject-pick${isActiveSpace(s.space_id) ? " active" : ""}`}
        onClick={() => pickSpace(s)}
      >
        <span className="subject-name">{s.name}</span>
        <span className="muted small">{s.record_count} 条记录</span>
        {s.paused && <span className="badge warn">已暂停</span>}
      </button>
      <button
        className="text-button"
        onClick={() => void togglePause(s.space_id, s.paused, s.name)}
      >
        {s.paused ? "恢复记录" : "暂停记录"}
      </button>
    </li>
  );

  const renderMemberRow = (m: MemoryMemberSubject) => (
    <li className="subject-item" key={m.principal_id}>
      <button
        className={`subject-pick${isActiveMember(m) ? " active" : ""}`}
        onClick={() => pickMember(m)}
      >
        <span className="subject-name mono">{m.principal_id}</span>
        <span className="muted small">{m.record_count} 条记录</span>
        {m.opted_out && <span className="badge bad">已退出</span>}
        {m.paused && <span className="badge warn">已暂停</span>}
        {!m.consent_active && !m.opted_out && (
          <span className="badge soft">授权失效</span>
        )}
      </button>
      <button
        className="text-button"
        onClick={() => void togglePause(m.space_id, m.paused, m.principal_id)}
      >
        {m.paused ? "恢复记录" : "暂停记录"}
      </button>
    </li>
  );

  const renderRecord = (it: MemoryBrowseItem) => {
    const metaOnly = (browse?.metadata_only ?? false) || it.restricted === true;
    return (
      <li
        key={it.key}
        className={`list-item${it.superseded ? " superseded" : ""}`}
      >
        <div className="list-main">
          <div className="record-tags">
            <span className="badge soft">{it.kind}</span>
            {it.source && <span className="badge info">{it.source}</span>}
            {it.source_group && (
              <span className="badge soft">{it.source_group}</span>
            )}
            {it.confidence > 0 && (
              <span className="badge ok">
                置信度 {it.confidence.toFixed(2)}
              </span>
            )}
            {it.expires_at && (
              <span className={`badge ${isExpired(it.expires_at) ? "bad" : "warn"}`}>
                {isExpired(it.expires_at)
                  ? "已过期"
                  : `将于 ${fmtTime(it.expires_at)} 过期`}
              </span>
            )}
            {it.superseded && <span className="badge bad">已被纠正</span>}
          </div>
          {metaOnly || !it.summary ? (
            <div className="muted">群友私有记忆——对话召回仅本人可见，管理面可治理</div>
          ) : (
            <div className="record-summary">{it.summary}</div>
          )}
          <div className="muted small">
            <span className="mono">{it.key}</span>
            {it.created_at && <> · 创建于 {fmtTime(it.created_at)}</>}
            {it.scope_note && <> · 谁能用：{it.scope_note}</>}
          </div>
          {it.tags && it.tags.length > 0 && (
            <div className="muted small">标签：{it.tags.join("、")}</div>
          )}
        </div>
        {!metaOnly && (
          <div className="actions">
            <button onClick={() => void correct(it)}>纠错</button>
            <button onClick={() => void expire(it.key)}>标记过期</button>
            <button className="danger" onClick={() => void remove(it.key)}>
              删除
            </button>
          </div>
        )}
      </li>
    );
  };

  return (
    <div>
      <div className="page-head">
        <h2>记忆</h2>
        <button onClick={() => void exportAll()}>导出 NDJSON</button>
      </div>

      {error && <div className="banner error">{error}</div>}
      {notice && <div className="banner ok">{notice}</div>}

      {subjectsLoading && !subjects && (
        <p className="muted">正在加载主体总览…</p>
      )}

      {subjects && (
        <section className="memory-subjects" aria-label="记忆主体">
          <div className="subject-group">
            <h3>主人</h3>
            {subjects.owner.spaces.length === 0 ? (
              <p className="muted small">暂无主人空间。</p>
            ) : (
              <ul className="subject-list">
                {subjects.owner.spaces.map(renderSpaceRow)}
              </ul>
            )}
            {subjects.owner.vault_present && (
              <p className="muted small">
                原始库（受限）：存在。内容为只读，脱敏视图请前往 Studio Vault。
              </p>
            )}
          </div>

          <div className="subject-group">
            <h3>群友</h3>
            {subjects.members.length === 0 ? (
              <p className="muted small">暂无群友空间。</p>
            ) : (
              <ul className="subject-list">
                {subjects.members.map(renderMemberRow)}
              </ul>
            )}
          </div>

          <div className="subject-group">
            <h3>群聊</h3>
            {subjects.groups.length === 0 ? (
              <p className="muted small">暂无群聊空间。</p>
            ) : (
              <ul className="subject-list">
                {subjects.groups.map(renderSpaceRow)}
              </ul>
            )}
          </div>

          <div className="subject-group">
            <h3>其他空间</h3>
            {subjects.forks.length === 0 ? (
              <p className="muted small">暂无其他空间。</p>
            ) : (
              <ul className="subject-list">
                {subjects.forks.map(renderSpaceRow)}
              </ul>
            )}
            <p className="muted small">
              未分区遗留记录：{subjects.legacy_unscoped_records}
              条（无主体归属，可经底部高级搜索定位）。
            </p>
          </div>
        </section>
      )}

      {!sel && subjects && (
        <div className="empty">选中上方任一主体或空间后，在此浏览其记忆记录。</div>
      )}

      {sel && (
        <section className="memory-browse" aria-label="记忆记录">
          <div className="section-head">
            <h2>{sel.name}</h2>
            <span className="muted small">
              {sel.paused ? "记录已暂停" : "记录中"}
              {sel.scopeNote ? ` · 谁能用：${sel.scopeNote}` : ""}
            </span>
          </div>

          {browse?.metadata_only && (
            <div className="banner warn">
              群友私有记忆：对话召回仅本人可见；管理面（你）可见并可治理。
            </div>
          )}

          <div className="memory-toolbar">
            <select
              aria-label="类型筛选"
              value={kindFilter}
              onChange={(e) => {
                setKindFilter(e.target.value);
                setOffset(0);
              }}
            >
              <option value="">全部类型</option>
              <option value="episode">episode</option>
              <option value="preference">preference</option>
            </select>
            <input
              aria-label="在当前主体内筛选"
              placeholder="筛选摘要 / 键名…"
              value={qInput}
              onChange={(e) => setQInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  setQApplied(qInput.trim());
                  setOffset(0);
                }
              }}
            />
            <button
              onClick={() => {
                setQApplied(qInput.trim());
                setOffset(0);
              }}
            >
              筛选
            </button>
            <label className="check-row">
              <input
                type="checkbox"
                checked={includeSuperseded}
                onChange={(e) => {
                  setIncludeSuperseded(e.target.checked);
                  setOffset(0);
                }}
              />
              含已被纠正
            </label>
            <select
              aria-label="每页条数"
              value={limit}
              onChange={(e) => {
                setLimit(Number(e.target.value));
                setOffset(0);
              }}
            >
              {[20, 50, 100, 200].map((n) => (
                <option key={n} value={n}>
                  每页 {n} 条
                </option>
              ))}
            </select>
          </div>

          {browseLoading && <p className="muted">加载中…</p>}
          {browse && browse.items.length === 0 && !browseLoading && (
            <div className="empty">该主体下暂无匹配记录。</div>
          )}
          {browse && browse.items.length > 0 && (
            <ul className="list">{browse.items.map(renderRecord)}</ul>
          )}
          {browse && (
            <div className="memory-pager">
              <button
                disabled={offset === 0 || browseLoading}
                onClick={() => setOffset(Math.max(0, offset - limit))}
              >
                上一页
              </button>
              <span className="muted small">
                {browse.total === 0
                  ? "共 0 条"
                  : `第 ${offset + 1}–${Math.min(offset + limit, browse.total)} 条 / 共 ${browse.total} 条`}
              </span>
              <button
                disabled={offset + limit >= browse.total || browseLoading}
                onClick={() => setOffset(offset + limit)}
              >
                下一页
              </button>
            </div>
          )}
        </section>
      )}

      <details className="memory-advanced">
        <summary>高级：全局关键词搜索（跨主体）</summary>
        <div className="searchbar">
          <input
            aria-label="全局搜索记忆"
            placeholder="输入关键词全局查找，留空浏览最近记忆…"
            value={advQ}
            onChange={(e) => setAdvQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void advSearch();
            }}
          />
          <button disabled={advLoading} onClick={() => void advSearch()}>
            {advLoading ? "搜索中…" : "搜索"}
          </button>
        </div>
        {advHits && advHits.length === 0 && (
          <div className="empty">没有匹配的记忆。</div>
        )}
        {advHits && advHits.length > 0 && (
          <ul className="list">
            {advHits.map((h) => (
              <li key={h.key} className="list-item">
                <div className="list-main">
                  <div className="mono">{h.key}</div>
                  <div className="muted">
                    {h.kind} · 相关度 {h.score.toFixed(2)} · {h.created_at}
                  </div>
                  {h.summary && <div>{h.summary}</div>}
                  {h.reasons.length > 0 && (
                    <div className="muted">命中：{h.reasons.join("、")}</div>
                  )}
                </div>
                <div className="actions">
                  <button className="danger" onClick={() => void advRemove(h.key)}>
                    删除
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </details>
    </div>
  );
}
