'use client';

import React, { useEffect, useMemo, useState } from 'react';
import dynamic from 'next/dynamic';
import Navigation from './navigation';
import Dashboard from './components/Dashboard';
import DateTimeKnob from './components/DateTimeKnob';
import { BACKEND_MAPDATA_URL, DATA_URL } from './config';

const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
const HOUR_MS = 3600000;
const DAY_MS = 86400000;

// Outage dates look like "10-AUG-2026 10:00" → Date (minutes captured too).
function parseDateTime(str) {
  if (!str) return null;
  const m = String(str).trim().match(/^(\d{1,2})-([A-Za-z]{3})-(\d{4})(?:\s+(\d{1,2}):(\d{2}))?/);
  if (!m) return null;
  const month = MONTHS.indexOf(m[2].toUpperCase());
  if (month === -1) return null;
  const dt = new Date(Number(m[3]), month, Number(m[1]), m[4] ? Number(m[4]) : 0, m[5] ? Number(m[5]) : 0);
  return Number.isNaN(dt.getTime()) ? null : dt;
}

// Snap to the nearest local hour (the minimum step). Uses local time so it works
// in any timezone (e.g. IST is +05:30, so rounding epoch ms would break).
function roundHour(ms) {
  const d = new Date(ms);
  d.setSeconds(0, 0);
  if (d.getMinutes() >= 30) d.setHours(d.getHours() + 1);
  d.setMinutes(0);
  return d.getTime();
}
// Truncate to the start of the current local hour.
function floorHour(ms) {
  const d = new Date(ms);
  d.setMinutes(0, 0, 0);
  return d.getTime();
}
// Truncate to the next local hour boundary.
function ceilHour(ms) {
  const d = new Date(ms);
  d.setMinutes(0, 0, 0);
  if (d.getTime() < ms) d.setHours(d.getHours() + 1);
  return d.getTime();
}

// An outage is "live" at time ms if it started at/before ms and hasn't ended yet.
function isActiveAt(dot, ms) {
  const s = parseDateTime(dot.start);
  if (!s || s.getTime() > ms) return false;
  if (dot.end) {
    const e = parseDateTime(dot.end);
    if (e && e.getTime() < ms) return false;
  }
  return true;
}

function formatSelected(ms) {
  const d = new Date(ms);
  const date = d.toLocaleDateString(undefined, {
    day: 'numeric',
    month: 'short',
    year: d.getFullYear() === new Date().getFullYear() ? undefined : 'numeric',
  });
  const time = `${String(d.getHours()).padStart(2, '0')}:00`;
  return `${date} ${time}`;
}

const OdishaMap = dynamic(() => import('./components/OdishaMap'), {
  ssr: false,
  loading: () => (
    <div className="card" style={{ padding: 8 }}>
      <div
        style={{
          height: 'clamp(420px, 60vh, 640px)',
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
  const [data, setData] = useState({ districts: [], dots: [], total: 0, updatedAt: '' });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [showDots, setShowDots] = useState(true);
  const [showHeatmap, setShowHeatmap] = useState(true);
  const [selectedMs, setSelectedMs] = useState(null);

  // Selectable range spans every outage start/end time, floored/ceiled to the hour.
  const timeRange = useMemo(() => {
    let min = Infinity;
    let max = -Infinity;
    for (const d of data.dots) {
      const s = parseDateTime(d.start);
      if (s) {
        min = Math.min(min, s.getTime());
        max = Math.max(max, s.getTime());
      }
      if (d.end) {
        const e = parseDateTime(d.end);
        if (e) {
          min = Math.min(min, e.getTime());
          max = Math.max(max, e.getTime());
        }
      }
    }
    if (!Number.isFinite(min)) {
      const now = Date.now();
      return { min: now, max: now + DAY_MS };
    }
    return {
      min: floorHour(min),
      max: ceilHour(max),
    };
  }, [data.dots]);

  // Default to the hour with the most outages live (fallback: the latest hour).
  const defaultMs = useMemo(() => {
    let best = null;
    let bestCount = -1;
    for (let t = timeRange.min; t <= timeRange.max; t += HOUR_MS) {
      const c = data.dots.filter((d) => isActiveAt(d, t)).length;
      if (c >= bestCount) {
        bestCount = c;
        best = t;
      }
    }
    return best === null ? timeRange.max : best;
  }, [data.dots, timeRange]);

  // Keep the selection within range; default to the busiest hour.
  const effectiveSelectedMs = useMemo(() => {
    if (timeRange.max <= timeRange.min) return timeRange.max;
    const candidate = selectedMs === null ? defaultMs : roundHour(selectedMs);
    return Math.max(timeRange.min, Math.min(timeRange.max, candidate));
  }, [timeRange, selectedMs, defaultMs]);

  // Outages that are live at the selected hour.
  const visibleDots = useMemo(
    () => data.dots.filter((d) => isActiveAt(d, effectiveSelectedMs)),
    [data.dots, effectiveSelectedMs]
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
                {loading ? '…' : visibleDots.length}
              </div>
              <div className="stat-label">Outages (at time)</div>
            </div>
            <div className="card" style={{ padding: '10px 18px', textAlign: 'center' }}>
              <div className="stat-value" style={{ fontSize: 20 }}>
                {loading ? '…' : districtCounts.length}
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
            title="Overlay outage dots"
          >
            ⚡ Dots {showDots ? 'on' : 'off'}
          </button>
          <button
            className={showHeatmap ? 'btn btn-accent' : 'btn'}
            onClick={() => setShowHeatmap((v) => !v)}
            title="Toggle the district heat map layer"
          >
            🌡 Heatmap {showHeatmap ? 'on' : 'off'}
          </button>
          <span style={{ color: 'var(--text-muted)', fontSize: 12, marginLeft: 'auto' }}>
            {loading ? '…' : `${visibleDots.length} outages • ${formatSelected(effectiveSelectedMs)}`}
          </span>
        </div>

        {/* Leaflet map: OSM base + translucent district heat + static dots on top */}
        <OdishaMap dots={visibleDots} districts={districtCounts} showDots={showDots} showHeatmap={showHeatmap} />

        {/* Date-time dial: exact hourly selection, adaptive tick density, zoomable */}
        {!loading && Number.isFinite(timeRange.min) && (
          <div className="card" style={{ padding: 24, marginTop: 14 }}>
            <div className="stat-label" style={{ textAlign: 'center', marginBottom: 6 }}>
              Select date &amp; time
            </div>
            <DateTimeKnob
              minMs={timeRange.min}
              maxMs={timeRange.max}
              valueMs={effectiveSelectedMs}
              onChange={setSelectedMs}
            />
          </div>
        )}

        {/* Dashboard with filters + report */}
        <div style={{ marginTop: 22 }}>
          <h2 style={{ fontSize: 19, fontWeight: 700, marginBottom: 14, color: 'var(--text)' }}>
            Outage details
          </h2>
          <Dashboard dots={visibleDots} />
        </div>
      </div>
    </main>
  );
}

