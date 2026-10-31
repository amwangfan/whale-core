import { useCallback, useEffect, useRef, useState } from "react";

/** Only the latest request may update a mounted page. */
export function useApi<T>(fn: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const generation = useRef(0);
  const reload = useCallback(() => {
    const current = ++generation.current;
    setLoading(true);
    setError(null);
    void fn()
      .then((d) => {
        if (current === generation.current) setData(d);
      })
      .catch((e) => {
        if (current === generation.current)
          setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (current === generation.current) setLoading(false);
      });
  }, [fn]);
  useEffect(() => {
    reload();
    return () => {
      ++generation.current;
    };
  }, [reload]);
  return { data, error, loading, reload };
}
