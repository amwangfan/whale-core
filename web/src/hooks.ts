import { useCallback, useEffect, useRef, useState } from "react";

/** Latest request wins; stale/manual reloads cannot overwrite a newer result. */
export function useApi<T>(fn: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const sequence = useRef(0);
  const mounted = useRef(true);
  const reload = useCallback(() => {
    const current = ++sequence.current;
    setLoading(true);
    setError(null);
    fn()
      .then((value) => {
        if (mounted.current && current === sequence.current) setData(value);
      })
      .catch((e) => {
        if (mounted.current && current === sequence.current)
          setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (mounted.current && current === sequence.current) setLoading(false);
      });
  }, [fn]);
  useEffect(() => {
    mounted.current = true;
    reload();
    return () => {
      mounted.current = false;
      sequence.current++;
    };
  }, [reload]);
  return { data, error, loading, reload };
}
