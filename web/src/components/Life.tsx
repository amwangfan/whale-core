import { useCallback, useState } from "react";
import { api } from "../api";
import { useApi } from "../hooks";
import Icon from "./Icon";

/**
 * 主动生活的兴趣台账与探索日记（移植自 PR#4 Life 页的真实差异部分）。
 *
 * 去重纪律：暂停开关、每日上限、静默时段与「探索一次」已在 Workflows 页；
 * 草稿审批在 Approvals 页。本组件只提供 main 此前没有的两个交互：
 * 兴趣的查看/添加/移除/反馈调权，以及探索日记的时间线浏览。
 * 反馈只调整兴趣权重与探索退避，不触发探索、不发送消息。
 */
export default function Life() {
  const state = useApi(useCallback(() => api.life(), []));
  const [topic, setTopic] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const run = async (fn: () => Promise<void>, message = "") => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await fn();
      state.reload();
      if (message) setNotice(message);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <section className="surface" style={{ marginTop: 22 }}>
        <div className="section-head">
          <h2>兴趣台账</h2>
          <span className="badge">{state.data?.interests.length ?? 0} 个话题</span>
        </div>
        <p className="muted small">
          兴趣驱动探索的话题选择与权重；「晚点」会拉长下次探索的间隔。
        </p>
        {(error || state.error) && (
          <div className="banner error" role="alert">
            {error || state.error}
          </div>
        )}
        {notice && (
          <div className="banner ok" role="status">
            {notice}
          </div>
        )}
        <form
          className="searchbar"
          onSubmit={(e) => {
            e.preventDefault();
            const value = topic.trim();
            if (!value) return;
            void run(async () => {
              await api.addInterest(value);
              setTopic("");
            });
          }}
        >
          <input
            aria-label="兴趣话题"
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
            placeholder="比如：AR 眼镜、电子音乐…"
            maxLength={100}
          />
          <button className="primary" disabled={busy || !topic.trim()}>
            添加
          </button>
        </form>
        {state.data?.interests.length === 0 && (
          <div className="empty">还没有兴趣话题。添加后探索才会围绕它们展开。</div>
        )}
        {Boolean(state.data?.interests.length) && (
          <ul className="list">
            {state.data?.interests.map((i) => (
              <li className="list-item" key={i.topic}>
                <div className="list-main">
                  <strong>{i.topic}</strong>
                  <span className="muted small">兴趣权重 {i.weight.toFixed(1)}</span>
                </div>
                <button
                  className="text-button"
                  disabled={busy}
                  onClick={() =>
                    void run(async () => {
                      await api.lifeFeedback(i.topic, "liked");
                    }, "已提升这个话题的权重。")
                  }
                >
                  喜欢
                </button>
                <button
                  className="text-button"
                  disabled={busy}
                  onClick={() =>
                    void run(async () => {
                      await api.lifeFeedback(i.topic, "ignored");
                    }, "已延长下次探索间隔。")
                  }
                >
                  晚点
                </button>
                <button
                  className="text-button"
                  disabled={busy}
                  onClick={() =>
                    void run(async () => {
                      await api.removeInterest(i.topic);
                    }, "已移除这个话题。")
                  }
                >
                  移除
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section className="surface" style={{ marginTop: 22 }}>
        <div className="section-head">
          <h2>探索日记</h2>
          <Icon name="explore" size={20} />
        </div>
        {!state.data?.diary.length ? (
          <div className="empty">
            还没有日记。添加兴趣后，用「一起探索」开始第一次探索。
          </div>
        ) : (
          <ul className="list">
            {state.data.diary
              .slice(-10)
              .reverse()
              .map((d, index) => (
                <li className="list-item" key={`${d.day}-${index}`}>
                  <div className="list-main">
                    <span className="muted small">{d.day}</span>
                    <span>{d.note}</span>
                    {d.sources.map((url) => (
                      <a key={url} href={url} target="_blank" rel="noreferrer">
                        阅读来源 ↗
                      </a>
                    ))}
                  </div>
                </li>
              ))}
          </ul>
        )}
      </section>
    </>
  );
}
