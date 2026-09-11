"use client";

/**
 * One hook for every screen's data.
 *
 * Loading, empty, error, and "the backend is unreachable" are four different
 * things, and a screen that collapses them renders a blank page for all four.
 * This keeps them distinct so `04_FRONTEND_SPEC.md`'s state discipline is
 * something a screen inherits rather than re-implements.
 */

import { useCallback, useEffect, useState } from "react";

export interface Resource<T> {
  data: T | null;
  loading: boolean;
  error: unknown;
  /** Re-fetch. Used after an action that changes backend state. */
  refresh: () => void;
}

/**
 * What one completed request produced, tagged with the request it answered.
 *
 * Keyed rather than a bare value so `loading` is *derived* — the alternative is
 * setting state in the effect body, which React 19 flags because it causes an
 * extra render pass on every dependency change.
 */
interface Settled<T> {
  key: number;
  data: T | null;
  error: unknown;
}

export function useApi<T>(
  load: () => Promise<T>,
  deps: unknown[] = [],
): Resource<T> {
  const [nonce, setNonce] = useState(0);
  const [settled, setSettled] = useState<Settled<T>>({
    key: -1,
    data: null,
    error: null,
  });

  // Identifies the request the current dependencies ask for. A settled result
  // for a different key is stale, and reads as loading.
  //
  // Computed on every render rather than memoised: hashing a short dependency
  // list is cheaper than the comparison a `useMemo` would do, and a spread
  // dependency array is not something the hooks linter can verify.
  const key = hash([...deps, nonce]);

  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    let cancelled = false;

    load()
      .then((value) => {
        // A response that arrives after the component moved on must not write
        // into unmounted state, or overwrite a newer request's result.
        if (!cancelled) setSettled({ key, data: value, error: null });
      })
      .catch((cause) => {
        if (!cancelled) setSettled({ key, data: null, error: cause });
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const fresh = settled.key === key;
  return {
    // Stale data is withheld rather than shown under a spinner: a screen
    // displaying one project's numbers while loading another's is worse than
    // one showing nothing.
    data: fresh ? settled.data : null,
    loading: !fresh,
    error: fresh ? settled.error : null,
    refresh,
  };
}

function hash(values: unknown[]): number {
  const text = JSON.stringify(values);
  let result = 0;
  for (let i = 0; i < text.length; i += 1) {
    result = (result * 31 + text.charCodeAt(i)) | 0;
  }
  return result;
}

/**
 * Poll while a run is in flight.
 *
 * The pipeline takes minutes, and a page that never updates makes a working
 * system look hung. Polling stops when the tab is hidden, because a background
 * tab hammering the API helps nobody.
 */
export function usePolling(refresh: () => void, intervalMs = 5000): void {
  useEffect(() => {
    const tick = () => {
      if (typeof document !== "undefined" && document.hidden) return;
      refresh();
    };
    const id = setInterval(tick, intervalMs);
    return () => clearInterval(id);
  }, [refresh, intervalMs]);
}
