'use client';

import { useCallback, useEffect, useState } from 'react';

/**
 * Thin JSON fetch layer used by the Live NRT and Reliability pages.
 *
 * Every failure mode is surfaced as an ApiError so pages can render a graceful
 * "not available yet" state: non-ok HTTP responses, network failures and
 * invalid JSON all throw (aborted requests keep their AbortError so callers can
 * ignore them).
 */
export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export async function fetchJson(url, options = {}) {
  let res;
  try {
    res = await fetch(url, { cache: 'no-store', ...options });
  } catch (err) {
    if (err && err.name === 'AbortError') throw err;
    throw new ApiError(0, 'could not reach the API');
  }

  if (!res.ok) {
    throw new ApiError(
      res.status,
      res.status === 404 ? 'endpoint not exposed yet (404)' : `API responded ${res.status}`
    );
  }

  try {
    return await res.json();
  } catch (err) {
    if (err && err.name === 'AbortError') throw err;
    throw new ApiError(res.status, 'API returned invalid JSON');
  }
}

/**
 * Fetch a JSON endpoint with optional polling.
 *
 * - `data` keeps the last successful payload during transient poll failures.
 * - `error` is the latest failure (or null).
 * - `loading` is true for the initial load / manual reload.
 * - `reload()` retriggers the request.
 * - polling pauses while the tab is hidden and refreshes on tab focus.
 */
export function useApiData(url, { intervalMs = 0 } = {}) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    if (!url) return;
    let cancelled = false;
    let timer = null;
    let controller = null;

    const load = async () => {
      if (controller) controller.abort();
      controller = new AbortController();
      try {
        const payload = await fetchJson(url, { signal: controller.signal });
        if (cancelled) return;
        setData(payload);
        setError(null);
      } catch (err) {
        if (cancelled || (err && err.name === 'AbortError')) return;
        setError(err);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    setLoading(true);
    load();

    const supportsDocument = typeof document !== 'undefined';
    const onVisibilityChange = () => {
      if (!document.hidden) load();
    };

    if (intervalMs > 0) {
      timer = setInterval(() => {
        if (typeof document !== 'undefined' && document.hidden) return;
        load();
      }, intervalMs);
      if (supportsDocument) document.addEventListener('visibilitychange', onVisibilityChange);
    }

    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
      if (supportsDocument) document.removeEventListener('visibilitychange', onVisibilityChange);
      if (controller) controller.abort();
    };
  }, [url, intervalMs, nonce]);

  return { data, error, loading, reload };
}
