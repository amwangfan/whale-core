import { useEffect, useState } from "react";
import { api, MemoryHit } from "../api";

export default function MemoryView() {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<MemoryHit[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const search = async () => {
    setLoading(true);
    setError(null);
    setNotice(null);
    try {
      setHits(await api.searchMemory(q.trim()));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void search();
  }, []);

  const inspect = async (key: string) => {
    if (selected === key) {
      setSelected(null);
      return;
    }
    setSelected(key);
    setDetail(null);
    try {
      setDetail(await api.getMemory(key));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const remove = async (key: string) => {
    if (!confirm(`删除记忆「${key}」？`)) return;
    try {
      const r = await api.deleteMemory(key);
      setNotice(r.deleted ? `已删除：${key}` : `未找到：${key}`);
      if (hits) setHits(hits.filter((h) => h.key !== key));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
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
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div>
      <div className="page-head">
        <h2>记忆</h2>
        <button onClick={() => void exportAll()}>导出 NDJSON</button>
      </div>

      <div className="searchbar">
        <input
          aria-label="搜索记忆"
          placeholder="输入关键词查找，留空浏览最近记忆…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void search();
          }}
        />
        <button disabled={loading} onClick={() => void search()}>
          {loading ? "搜索中…" : "搜索"}
        </button>
      </div>

      {error && <div className="banner error">{error}</div>}
      {notice && <div className="banner ok">{notice}</div>}
      {hits && hits.length === 0 && (
        <div className="empty">没有匹配的记忆。</div>
      )}

      {hits && hits.length > 0 && (
        <ul className="list">
          {hits.map((h) => (
            <li key={h.key} className="list-item">
              <div className="list-main">
                <div className="mono">{h.key}</div>
                <div className="muted">
                  {h.kind} · 相关度 {h.score.toFixed(2)} · {h.created_at}
                </div>
                {h.summary && <div>{h.summary}</div>}
                {selected === h.key && (
                  <div className="detail-panel">
                    {detail ? (
                      <>
                        <span className="section-label">记忆详情与来源</span>
                        <pre className="memory-json">
                          {JSON.stringify(detail, null, 2)}
                        </pre>
                      </>
                    ) : (
                      <span className="muted">正在读取详情…</span>
                    )}
                  </div>
                )}
                {h.reasons.length > 0 && (
                  <div className="muted">命中：{h.reasons.join("、")}</div>
                )}
              </div>
              <div className="actions">
                <button
                  onClick={() => void inspect(h.key)}
                  aria-expanded={selected === h.key}
                >
                  {selected === h.key ? "收起" : "详情"}
                </button>
                <button className="danger" onClick={() => void remove(h.key)}>
                  删除
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
