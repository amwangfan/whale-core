import { useEffect, useState } from "react";
import { api, MemoryHit } from "../api";

export default function MemoryView() {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<MemoryHit[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    void search();
  }, []);

  const search = async () => {
    setLoading(true);
    setError(null);
    setNotice(null);
    try {
      setHits(
        q.trim()
          ? await api.searchMemory(q.trim())
          : (await api.listMemory()).hits,
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
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
        <div className="actions">
          <label className="upload-button">
            导入 NDJSON
            <input
              type="file"
              accept=".ndjson,.jsonl"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) {
                  void file
                    .text()
                    .then(api.importMemory)
                    .then((r) => {
                      setNotice(
                        `已导入 ${r.imported} 条，跳过 ${r.skipped} 条`,
                      );
                      void search();
                    })
                    .catch((e) => setError(String(e)));
                }
                e.target.value = "";
              }}
            />
          </label>
          <button onClick={() => void exportAll()}>导出 NDJSON</button>
        </div>
      </div>

      <div className="searchbar">
        <input
          placeholder="搜索记忆…"
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
                {h.reasons.length > 0 && (
                  <div className="muted">命中：{h.reasons.join("、")}</div>
                )}
              </div>
              <button className="danger" onClick={() => void remove(h.key)}>
                删除
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
