import { useCallback, useEffect, useRef, useState } from "react";

export const API_READ_TIMEOUT_MS = 15_000;

/** Cancel obsolete reads and bound loading even if a transport ignores abort. */
export function useApi<T>(fn: (signal: AbortSignal) => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const generation = useRef(0);
  const mounted = useRef(false);
  const cancel = useRef<(() => void) | null>(null);
  const reload = useCallback(() => {
    if (!mounted.current) return;
    const current = ++generation.current;
    cancel.current?.();
    const controller = new AbortController();
    let finished = false;
    const isCurrent = () => !finished && current === generation.current && mounted.current;
    const timer = setTimeout(() => {
      if (!isCurrent()) return;
      finished = true;
      setError("请求超时，请重试");
      setLoading(false);
      controller.abort();
    }, API_READ_TIMEOUT_MS);
    const stop = () => {
      finished = true;
      clearTimeout(timer);
      controller.abort();
    };
    cancel.current = stop;
    setLoading(true);
    setError(null);
    void Promise.resolve().then(() => {
      if (!controller.signal.aborted) return fn(controller.signal);
      throw new DOMException("Cancelled", "AbortError");
    })
      .then((d) => {
        if (isCurrent()) setData(d);
      })
      .catch((e) => {
        if (isCurrent())
          setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (isCurrent()) setLoading(false);
        finished = true;
        clearTimeout(timer);
        if (cancel.current === stop) cancel.current = null;
      });
  }, [fn]);
  useEffect(() => {
    mounted.current = true;
    reload();
    return () => {
      mounted.current = false;
      ++generation.current;
      cancel.current?.();
      cancel.current = null;
    };
  }, [reload]);
  return { data, error, loading, reload };
}
