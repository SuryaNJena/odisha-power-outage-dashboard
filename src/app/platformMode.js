'use client';

import { createContext, useContext, useEffect, useState } from 'react';
import Link from 'next/link';
import { BACKEND_URL } from './config';

const PlatformMode = createContext(null);
export const useAnalytics = () => useContext(PlatformMode);

export function PlatformProvider({ children }) {
  const [enabled, setEnabled] = useState(null);
  useEffect(() => {
    const controller = new AbortController();
    fetch(BACKEND_URL + '/api/features', { signal: controller.signal, cache: 'no-store' })
      .then(res => res.ok ? res.json() : {})
      .then(data => setEnabled(data.analytics === true))
      .catch(() => { if (!controller.signal.aborted) setEnabled(false); });
    return () => controller.abort();
  }, []);
  return <PlatformMode.Provider value={enabled}>{children}</PlatformMode.Provider>;
}

export function AnalyticsOnly({ children }) {
  const enabled = useAnalytics();
  if (enabled === null) return null;
  if (enabled) return children;
  return <main style={{ padding: 24 }}><h1>Analytics are paused</h1>
    <p>The outage map shows the most recent manual refresh.</p><Link href="/">Open the map</Link></main>;
}
