'use client';

import React, { useEffect, useMemo, useState } from 'react';
import dynamic from 'next/dynamic';
import { useFeatures } from './platformMode';
import QuickReport from './components/QuickReport';
import RefreshButton from './components/RefreshButton';
import Navigation from './navigation';
import Dashboard from './components/Dashboard';
import DateTimeKnob from './components/DateTimeKnob';
import { useApiData } from './useApiData';
import { BACKEND_MAPDATA_URL, DATA_URL, MAP_REPORTS_URL } from './config';

import {
  DAY, busiestTime, ceilHour, centeredSelection, floorHour, outageInterval, overlaps, pointWithinWindow,
} from './outageTimeline.mjs';

const OdishaMap = dynamic(() => import('./components/OdishaMap'), {
  ssr: false,
  loading: () => (
    <div className="map-frame">
      <div
        style={{
          height: 'var(--landing-map-height)',
          display: 'grid',
          placeItems: 'center',
          color: 'var(--text-muted)',
          fontSize: 14,
        }}
      >
        Loading map…
      </div>
    </div>
  ),
});

export default function HomePage() {
  const features = useFeatures();
  const { data: reports, reload: reloadReports } = useApiData(
    features?.reporting ? MAP_REPORTS_URL : null,
    { intervalMs: 60_000 }
  );
  const [data, setData] = useState({ districts: [], dots: [], total: 0, updatedAt: '' });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [showDots, setShowDots] = useState(true);
  const [showHeatmap, setShowHeatmap] = useState(true);
  // Selection window { start, end }; start === end means a single point in time.
  const [selection, setSelection] = useState(null);

  const intervals = useMemo(() => data.dots.map(outageInterval).filter(Boolean), [data.dots]);
  const initialTime = useMemo(() => {
    const now = Date.now();
    let min = now;
    let max = now + DAY;
    for (const { start, end } of intervals) {
      min = Math.min(min, start);
      max = Math.max(max, start, Number.isFinite(end) ? end : start);
    }
    return { min, max, center: busiestTime(intervals, now) };
  }, [intervals]);

  const effectiveSelection = useMemo(
    () => selection ?? centeredSelection(initialTime.center, DAY),
    [selection, initialTime]
  );
  const center = (effectiveSelection.start + effectiveSelection.end) / 2;
  // Include the current instant when Now is pressed, even with a stale feed.
  const minMs = floorHour(Math.min(initialTime.min, center));
  const maxMs = ceilHour(Math.max(initialTime.max, center));

  const visibleDots = useMemo(
    () => intervals.filter((interval) => overlaps(interval, effectiveSelection)).map(({ dot }) => dot),
    [intervals, effectiveSelection]
  );

  // Recompute district heat counts for the selected hour.
  const districtCounts = useMemo(() => {
    const counts = {};
    for (const d of visibleDots) {
      if (d.district) counts[d.district] = (counts[d.district] || 0) + 1;
    }
    return Object.entries(counts)
      .map(([name, value]) => ({ name, value }))
      .sort((a, b) => b.value - a.value);
  }, [visibleDots]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(BACKEND_MAPDATA_URL, { cache: 'no-store' });
        if (!res.ok) throw new Error(`backend responded ${res.status}`);
        const payload = await res.json();
        if (!cancelled) setData(payload);
      } catch (err) {
        // No backend (e.g. static GitHub Pages build) — fall back to the
        // committed public/outage_data.json snapshot so the map still works.
        console.error('Backend fetch failed, trying static snapshot:', err);
        try {
          const res = await fetch(DATA_URL, { cache: 'no-store' });
          if (!res.ok) throw new Error(`static data responded ${res.status}`);
          const payload = await res.json();
          if (payload && Array.isArray(payload.dots)) {
            if (!cancelled) setData(payload);
          } else {
            throw new Error('static snapshot has no dots');
          }
        } catch (err2) {
          console.error(err2);
          if (!cancelled)
            setError('Backend unavailable. Start the Go backend (go run ./backend) then refresh.');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  const lastUpdated = useMemo(() => {
    if (!data.updatedAt) return 'N/A';
    return new Date(data.updatedAt).toLocaleString(undefined, {
      dateStyle: 'medium',
      timeStyle: 'short',
    });
  }, [data.updatedAt]);

  return (
    <main className="landing" style={{ minHeight: '100vh', background: 'var(--bg)' }}>
      <Navigation />

      <div className="landing-body">
        {error && (
          <div
            className="card"
            style={{
              padding: 16,
              marginBottom: 18,
              borderColor: 'var(--red)',
              color: 'var(--red)',
              fontSize: 14,
            }}
          >
            {error}
          </div>
        )}

        <section className="card map-panel" aria-labelledby="map-heading">
          <h1 id="map-heading" className="visually-hidden">Odisha outage map</h1>
          <div className="map-toolbar">
            <p className="map-summary">
              {!loading && visibleDots.length === 0 ? 'No official outages for this selection' : (
                <>
                  <strong>{loading ? '…' : visibleDots.length}</strong> {visibleDots.length === 1 ? 'outage' : 'outages'} in{' '}
                  <strong>{loading ? '…' : districtCounts.length}</strong> {districtCounts.length === 1 ? 'district' : 'districts'}
                </>
              )}
            </p>
            <div className="map-layers" role="group" aria-label="Map layers">
              <label><input type="checkbox" checked={showDots} onChange={e => setShowDots(e.target.checked)} />Locations</label>
              <label><input type="checkbox" checked={showHeatmap} onChange={e => setShowHeatmap(e.target.checked)} />District heat</label>
            </div>
            <div className="map-updated">
              <span>Updated {loading ? '…' : lastUpdated}</span>
              {features?.manualRefresh && <RefreshButton onData={setData} />}
            </div>
          </div>
          <OdishaMap reports={reports} dots={visibleDots} districts={districtCounts} showDots={showDots} showHeatmap={showHeatmap} />
        </section>

        {/* Date-time dial: exact hourly selection, adaptive tick density, zoomable */}
        {!loading && (
          <DateTimeKnob
              minMs={minMs}
              maxMs={maxMs}
              selection={effectiveSelection}
              onChange={setSelection}
              onPoint={() => setSelection(centeredSelection(pointWithinWindow(intervals, effectiveSelection), 0))}
          />
        )}

        {/* Dashboard with filters + report */}
        <div style={{ marginTop: 22 }}>
          <h2 style={{ fontSize: 19, fontWeight: 700, marginBottom: 14, color: 'var(--text)' }}>
            Outage details
          </h2>
          {features?.reporting && <details className="landing-report">
            <summary>Report an outage</summary>
            <QuickReport onSaved={reloadReports} />
          </details>}
          <Dashboard dots={visibleDots} />
        </div>
      </div>
    </main>
  );
}

