"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export interface Resource<T> {
  data: T | undefined;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/** ``load()`` now, again on ``reload()``, and every ``everyMs`` while mounted (0: never). */
export function useResource<T>(load: () => Promise<T>, key: string, everyMs = 0): Resource<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const latest = useRef(load);
  latest.current = load;

  const reload = useCallback(() => {
    latest
      .current()
      .then((value) => {
        setData(value);
        setError(null);
      })
      .catch((caught: unknown) => setError(caught instanceof Error ? caught.message : String(caught)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    setLoading(true);
    reload();
    if (everyMs <= 0) return;
    const timer = window.setInterval(reload, everyMs);
    return () => window.clearInterval(timer);
  }, [key, everyMs, reload]);

  return { data, error, loading, reload };
}
