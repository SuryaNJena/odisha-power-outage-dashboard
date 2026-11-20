'use client';

import { createContext, useContext, useEffect, useState } from 'react';
import Link from 'next/link';
import { BACKEND_URL } from './config';

const PlatformMode = createContext(null);
export const useFeatures = () => useContext(PlatformMode);
export const useAnalytics = () => useFeatures()?.analytics;

export function PlatformProvider({ children }) {
  const [features, setFeatures] = useState(null);
  useEffect(() => {
    const controller = new AbortController();
    fetch(BACKEND_URL + '/api/features', { signal: controller.signal, cache: 'no-store' })
      .then(res => res.ok ? res.json() : {})
      .then(data => setFeatures({ ...data, analytics: data.analytics === true }))
      .catch(() => { if (!controller.signal.aborted) setFeatures({ analytics: false }); });
    return () => controller.abort();
  }, []);
  return <PlatformMode.Provider value={features}>{children}</PlatformMode.Provider>;
}

export function AnalyticsOnly({ children }) {
  const enabled = useAnalytics();
  if (enabled == null) return null;
  if (enabled) return children;
  return <main style={{ padding: 24 }}><h1>Analytics are paused</h1>
    <p>The outage map shows the most recent manual refresh.</p><Link href="/">Open the map</Link></main>;
}
