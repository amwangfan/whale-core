import { useEffect, useState } from "react";
import { api, type DeviceReport, type DiagnosticReport } from "../api";

type Snapshot<T> = { data: T | null; error: string | null; at: number | null };
const empty = <T,>(): Snapshot<T> => ({ data: null, error: null, at: null });
const time = (value?: number | null) => value ? new Date(value).toLocaleTimeString() : "尚未更新";
const count = (value?: number) => typeof value === "number" && Number.isFinite(value) ? value : "—";

export default function RuntimeDiagnostics() {
  const [devices, setDevices] = useState(empty<DeviceReport>);
  const [runtime, setRuntime] = useState(empty<DiagnosticReport>);
  const [automatic, setAutomatic] = useState(true);
  const [tick, setTick] = useState(0);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    let active = true, generation = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let deadline: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    const refresh = () => {
      if (document.hidden) return;
      clearTimeout(timer);
      clearTimeout(deadline);
      controller?.abort();
      controller = new AbortController();
      const signal = controller.signal;
      const current = ++generation;
      setLoading(true);
      const load = <T,>(promise: Promise<T>, setter: (value: (old: Snapshot<T>) => Snapshot<T>) => void) => {
        let onAbort: () => void = () => {};
        const aborted = new Promise<never>((_, reject) => {
          onAbort = () => reject(new Error("读取超时，请重试"));
          signal.addEventListener("abort", onAbort, { once: true });
          if (signal.aborted) onAbort();
        });
        return Promise.race([promise, aborted]).then(value => {
          if (active && current === generation) setter(() => ({ data: value, error: null, at: Date.now() }));
        }).catch(reason => {
          if (active && current === generation) setter(old => ({ ...old, error: reason instanceof Error ? reason.message : "读取失败" }));
        }).finally(() => signal.removeEventListener("abort", onAbort));
      };
      deadline = setTimeout(() => controller?.abort(), 15000);
      void Promise.allSettled([load(api.devices(signal), setDevices), load(api.diagnostics(signal), setRuntime)])
        .then(() => {
          if (!active || current !== generation) return;
          clearTimeout(deadline);
          setLoading(false);
          if (automatic && !document.hidden) timer = setTimeout(refresh, 10000);
        });
    };
    const visibility = () => {
      if (document.hidden) {
        ++generation; clearTimeout(timer); clearTimeout(deadline); controller?.abort(); setLoading(false);
      } else if (automatic) refresh();
    };
    refresh();
    document.addEventListener("visibilitychange", visibility);
    return () => {
      active = false; ++generation; clearTimeout(timer); clearTimeout(deadline); controller?.abort();
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [automatic, tick]);
  const retention = devices.data?.retention;
  const memory = runtime.data?.memory_worker;
  const stale = (error: string | null, at: number | null) => error &&
    <p className="banner error" role="alert">{error}{at ? ` · 显示上次成功数据（${time(at)}）` : ""}</p>;
  return <div className="runtime-diagnostics">
    <div className="section-head"><h2>设备与后台状态</h2><div>
      <label><input type="checkbox" checked={automatic} onChange={e => setAutomatic(e.target.checked)} /> 自动刷新</label>{" "}
      <button className="text-button" onClick={() => setTick(n => n + 1)}>{loading ? "重新读取" : "刷新状态"}</button>
    </div></div>
    <p className="muted" role="status">{loading ? "正在读取状态…" : "只读诊断"} · 自动刷新每10秒完成后继续，切到后台暂停。</p>
    <section className="card" aria-label="设备连接">
      <h3>设备连接</h3>{stale(devices.error, devices.at)}
      <p className="muted">最近读取：{time(devices.at)}</p>
      {devices.data ? <>
        <p>已登记 {devices.data.devices.length} 台 · 在线 {devices.data.devices.filter(d => d.online && d.status === "active").length} 台</p>
        {!devices.data.devices.length && <p>尚未登记设备。请先在手机端发起配对。</p>}
        <ul>{devices.data.devices.map(device => <li key={device.device_id}>
          <strong>{device.label || "未命名设备"}</strong> · {device.status === "revoked" ? "已撤销" : device.status === "pending" ? "待确认" : device.online === true ? "在线" : device.online === false ? "离线" : "连接状态未知"}
          <div className="muted">{device.device_id} · 已授予 {device.scopes?.length ?? 0} 项能力</div>
        </li>)}</ul>
      </> : <p>{devices.error ? "设备状态暂不可用" : "等待设备状态"}</p>}
    </section>
    <div className="cards">
      <section className="card" aria-label="动作记录清理"><h3>动作记录清理</h3>
        <p>{!devices.data ? devices.error ? "清理状态暂不可用" : "等待清理状态" : !retention ? "当前核心未提供清理诊断" : retention.state === "failed" ? "最近一批清理失败" : retention.state === "not_run" ? "尚未执行首批清理" : retention.state === "ok" ? retention.more ? "仍有后续批次" : "本轮扫描完成" : "清理状态不可用"}</p>
        {retention?.state === "ok" && <dl>
          <dt>本批检查 / 回执检查</dt><dd>{count(retention.scanned)} / {count(retention.receipt_scanned)}</dd>
          <dt>删除 / 标记待核对</dt><dd>{count(retention.deleted)} / {count(retention.marked_unknown)}</dd>
          <dt>本批耗时</dt><dd>{count(retention.duration_ms)} ms</dd>
        </dl>}
        {retention?.state === "failed" && <p role="alert">错误类型：{retention.error || "未知"}。请检查后台日志。</p>}
        {devices.error && devices.data && <p className="muted">清理状态也来自上次成功读取。</p>}
        <p className="muted">清理时间：{time(retention?.at)} · 统计为单批结果，并非累计总量。</p>
      </section>
      <section className="card" aria-label="记忆后台任务"><h3>记忆后台任务</h3>
        {stale(runtime.error, runtime.at)}
        <p>{!runtime.data ? runtime.error ? "记忆任务状态暂不可用" : "等待记忆任务状态" : !memory || memory.state === "unavailable" ? "当前核心未装配记忆任务诊断" : memory.state !== "ok" ? "暂时无法读取记忆任务状态" : memory.model_busy ? "模型调用处理中" : memory.in_flight ? "任务处理中" : memory.queue_depth ? "有任务等待处理" : "当前空闲"}</p>
        {memory?.state === "ok" && <dl><dt>待处理 / 已完成</dt><dd>{count(memory.queue_depth)} / {count(memory.completed)}</dd>
          <dt>重试 / 失败</dt><dd>{count(memory.retried)} / {count(memory.failed)}</dd>
          <dt>模型失败 / 提交拒绝</dt><dd>{count(memory.model_failures)} / {count(memory.commit_rejected)}</dd></dl>}
        <p className="muted">最近读取：{time(runtime.at)}。计数属于当前 worker，重建后会重置。</p>
      </section>
    </div>
  </div>;
}
