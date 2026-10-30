import { useCallback, useEffect, useState } from 'react';

/** 简单的数据加载 hook：挂载时执行 fn，提供 reload 手动刷新。 */
export function useApi<T>(fn: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(() => {
    setLoading(true);
    setError(null);
    let cancelled = false;
    fn()
      .then(d => {
        if (!cancelled) setData(d);
      })
      .catch(e => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [fn]);

  useEffect(() => {
    const cancel = reload();
    return cancel;
  }, [reload]);

  return { data, error, loading, reload };
}
