'use client';

import React, { useEffect, useMemo, useState } from 'react';
import Navigation from './navigation';
import OdishaMap from './components/OdishaMap';
import Dashboard from './components/Dashboard';
import { BACKEND_MAPDATA_URL, DATA_URL } from './config';
import { inDateRange, toDateKey } from './dateUtils';

function keyOf(daysFromNow) {
  const d = new Date();
  d.setDate(d.getDate() + daysFromNow);
  return toDateKey(d);
}

export default function HomePage() {
  const [data, setData] = useState({ districts: [], dots: [], total: 0, updatedAt: '' });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [showDots, setShowDots] = useState(true);
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');

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
            setError('Data unavailable. Start the Go backend (go run ./backend) then refresh.');
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

  // Dots filtered by the date range near the map. Everything below — the map
  // dots, district heat values and the details table — reacts to this slice.
  const filteredDots = useMemo(
    () => data.dots.filter((d) => inDateRange(d, dateFrom || null, dateTo || null)),
    [data.dots, dateFrom, dateTo]
  );

  const filteredDistricts = useMemo(() => {
    const counts = {};
    filteredDots.forEach((d) => {
      counts[d.district] = (counts[d.district] || 0) + 1;
    });
    return Object.entries(counts)
      .map(([name, value]) => ({ name, value }))
      .sort((a, b) => b.value - a.value);
  }, [filteredDots]);

  function applyPreset(daysFromNow, span) {
    if (span) {
      setDateFrom(keyOf(daysFromNow));
      setDateTo(keyOf(daysFromNow + span - 1));
    } else {
      setDateFrom('');
      setDateTo('');
    }
  }

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
                {loading ? '…' : filteredDots.length}
              </div>
              <div className="stat-label">Outages{dateFrom || dateTo ? ' (filtered)' : ''}</div>
            </div>
            <div className="card" style={{ padding: '10px 18px', textAlign: 'center' }}>
              <div className="stat-value" style={{ fontSize: 20 }}>
                {loading ? '…' : filteredDistricts.length}
              </div>
              <div className="stat-label">Districts{dateFrom || dateTo ? ' (filtered)' : ''}</div>
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
        <div className="card control-row" style={{ padding: 12, marginBottom: 14, display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
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
            {loading ? '…' : `${filteredDots.length} outage dots in range • canvas-rendered for smooth animation`}
          </span>
        </div>


        {/* Date range filter — drives the map dots + heat map + details table */}
        <div className="card control-row" style={{ padding: 12, marginBottom: 14, display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
          <span style={{ color: 'var(--text-muted)', fontSize: 12, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            Date
          </span>
          <input
            type="date"
            className="control"
            value={dateFrom}
            aria-label="From date"
            onChange={(e) => setDateFrom(e.target.value)}
          />
          <span style={{ color: 'var(--text-muted)', fontSize: 13 }}>→</span>
          <input
            type="date"
            className="control"
            value={dateTo}
            aria-label="To date"
            onChange={(e) => setDateTo(e.target.value)}
          />
          <button
            className={dateFrom === keyOf(0) && dateTo === keyOf(0) ? 'btn btn-accent' : 'btn'}
            onClick={() => applyPreset(0, 1)}
            title="Only today's outages"
          >
            Today
          </button>
          <button
            className={dateFrom === keyOf(0) && dateTo === keyOf(6) ? 'btn btn-accent' : 'btn'}
            onClick={() => applyPreset(0, 7)}
            title="Next 7 days including today"
          >
            Next 7 days
          </button>
          <button
            className={dateFrom === keyOf(0) && dateTo === keyOf(29) ? 'btn btn-accent' : 'btn'}
            onClick={() => applyPreset(0, 30)}
            title="Next 30 days including today"
          >
            Next 30 days
          </button>
          <button className={!dateFrom && !dateTo ? 'btn btn-accent' : 'btn'} onClick={() => applyPreset(0, 0)}>
            All
          </button>
          <span style={{ marginLeft: 'auto', color: 'var(--text-muted)', fontSize: 12 }}>
            {loading ? '…' : `${filteredDots.length} of ${data.dots.length} outages in range`}
          </span>
        </div>

        {/* Merged map: choropleth base + optional dots overlay */}
        <OdishaMap dots={filteredDots} districts={filteredDistricts} showDots={showDots} />

        {/* Dashboard with filters + report */}
        <div style={{ marginTop: 22 }}>
          <h2 style={{ fontSize: 19, fontWeight: 700, marginBottom: 14, color: 'var(--text)' }}>
            Outage details
          </h2>
          <Dashboard dots={filteredDots} />
        </div>
      </div>
    </main>
  );
}

