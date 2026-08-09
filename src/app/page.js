'use client';

import React, { useEffect, useMemo, useState } from 'react';
import Navigation from './navigation';
import OdishaMap from './components/OdishaMap';
import Dashboard from './components/Dashboard';
import { BACKEND_MAPDATA_URL } from './config';

export default function HomePage() {
  const [data, setData] = useState({ districts: [], dots: [], total: 0, updatedAt: '' });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [showDots, setShowDots] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(BACKEND_MAPDATA_URL, { cache: 'no-store' });
        if (!res.ok) throw new Error(`backend responded ${res.status}`);
        const payload = await res.json();
        if (!cancelled) setData(payload);
      } catch (err) {
        console.error(err);
        if (!cancelled)
          setError('Backend unavailable. Start the Go backend (go run ./backend) then refresh.');
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
    <main style={{ minHeight: '100vh', background: 'var(--bg)' }}>
      <Navigation />

      <div style={{ maxWidth: 1200, margin: '0 auto', padding: '88px 20px 48px' }}>
        {/* Header */}
        <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'flex-end', gap: 10, marginBottom: 20 }}>
          <div>
            <h1 style={{ fontSize: 26, fontWeight: 800, letterSpacing: '-0.02em', color: 'var(--text)' }}>
              ⚡ Odisha Live Power Outage Dashboard
            </h1>
            <p style={{ color: 'var(--text-muted)', fontSize: 14, marginTop: 4 }}>
              Planned &amp; ongoing outages by district • Data refreshed {loading ? '…' : lastUpdated}
            </p>
          </div>
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 10 }}>
            <div className="card" style={{ padding: '10px 18px', textAlign: 'center' }}>
              <div className="stat-value" style={{ fontSize: 20, color: 'var(--accent)' }}>
                {loading ? '…' : data.total}
              </div>
              <div className="stat-label">Total outages</div>
            </div>
            <div className="card" style={{ padding: '10px 18px', textAlign: 'center' }}>
              <div className="stat-value" style={{ fontSize: 20 }}>
                {loading ? '…' : data.districts.length}
              </div>
              <div className="stat-label">Districts</div>
            </div>
          </div>
        </div>

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
            ⚠️ {error}
          </div>
        )}

        {/* Map controls */}
        <div className="card" style={{ padding: 12, marginBottom: 14, display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
          <span style={{ color: 'var(--text-muted)', fontSize: 12, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            View
          </span>
          <button
            className={showDots ? 'btn btn-accent' : 'btn'}
            onClick={() => setShowDots((v) => !v)}
            title="Overlay outage dots on the heat map"
          >
            ⚡ Dots {showDots ? 'on' : 'off'}
          </button>
          <span style={{ color: 'var(--text-muted)', fontSize: 12, marginLeft: 'auto' }}>
            {loading ? '…' : `${data.dots.length} outage dots • canvas-rendered for smooth animation`}
          </span>
        </div>

        {/* Merged map: choropleth base + optional dots overlay */}
        <OdishaMap dots={data.dots} districts={data.districts} showDots={showDots} />

        {/* Dashboard with filters + report */}
        <div style={{ marginTop: 22 }}>
          <h2 style={{ fontSize: 19, fontWeight: 700, marginBottom: 14, color: 'var(--text)' }}>
            Outage details
          </h2>
          <Dashboard dots={data.dots} />
        </div>
      </div>
    </main>
  );
}

