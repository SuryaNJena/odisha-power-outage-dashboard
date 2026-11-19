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

// An outage is visible if it overlaps the selection window { start, end }.
// For a point selection start === end, so this is the same as "live at ms".
function inSelection(dot, sel) {
  const s = parseDateTime(dot.start);
  if (!s || s.getTime() > sel.end) return false;
  if (dot.end) {
    const e = parseDateTime(dot.end);
    if (e && e.getTime() < sel.start) return false;
  }
  return true;
}

function formatSelection(sel) {
  const d = new Date(sel.start);
  const date = d.toLocaleDateString(undefined, {
    day: 'numeric',
    month: 'short',
    year: d.getFullYear() === new Date().getFullYear() ? undefined : 'numeric',
  });
  const time = `${String(d.getHours()).padStart(2, '0')}:00`;
  if (sel.end > sel.start) {
    const e = new Date(sel.end);
    const edate = e.toLocaleDateString(undefined, {
      day: 'numeric',
      month: 'short',
      year: e.getFullYear() === new Date().getFullYear() ? undefined : 'numeric',
    });
    const etime = `${String(e.getHours()).padStart(2, '0')}:00`;
    return `${date} ${time} – ${edate} ${etime}`;
  }
  return `${date} ${time}`;
}

const OdishaMap = dynamic(() => import('./components/OdishaMap'), {
  ssr: false,
  loading: () => (
    <div className="card" style={{ padding: 8 }}>
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
  const [data, setData] = useState({ districts: [], dots: [], total: 0, updatedAt: '' });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [showDots, setShowDots] = useState(true);
  const [showHeatmap, setShowHeatmap] = useState(true);
  // Selection window { start, end }; start === end means a single point in time.
  const [selection, setSelection] = useState(null);

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
      const c = data.dots.filter((d) => inSelection(d, { start: t, end: t })).length;
      if (c >= bestCount) {
        bestCount = c;
        best = t;
      }
    }
    return best === null ? timeRange.max : best;
  }, [data.dots, timeRange]);

  // Keep the selection within range; default to the busiest hour (point mode).
  // A window is at most 24 h and shrinks to the full range when it is shorter.
  const effectiveSelection = useMemo(() => {
    if (timeRange.max <= timeRange.min) return { start: timeRange.max, end: timeRange.max };
    const raw = selection ?? { start: defaultMs, end: defaultMs };
    if (raw.end > raw.start) {
      const dur = Math.min(DAY_MS, timeRange.max - timeRange.min);
      const start = Math.max(timeRange.min, Math.min(raw.start, timeRange.max - dur));
      return { start, end: start + dur };
    }
    const ms = Math.max(timeRange.min, Math.min(timeRange.max, raw.start));
    return { start: ms, end: ms };
  }, [timeRange, selection, defaultMs]);

  // Outages that overlap the selected window (a point when start === end).
  const visibleDots = useMemo(
    () => data.dots.filter((d) => inSelection(d, effectiveSelection)),
    [data.dots, effectiveSelection]
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
        {/* Header */}
        <div className="landing-head">
          <div>
            <h1>
              Odisha Live Power Outage Dashboard
            </h1>
            <p style={{ color: 'var(--text-muted)', fontSize: 14, marginTop: 4 }}>
              Planned &amp; ongoing outages by district • Data refreshed {loading ? '…' : lastUpdated}
            </p>
          </div>
          <div className="landing-stats">
            <div className="landing-stat">
              <div className="stat-value" style={{ fontSize: 20, color: 'var(--accent)' }}>
                {loading ? '…' : visibleDots.length}
              </div>
              <div className="stat-label">Outages (selection)</div>
            </div>
            <div className="landing-stat">
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
            {error}
          </div>
        )}

        {/* Map controls */}
        <div className="card landing-filters">
          <span style={{ color: 'var(--text-muted)', fontSize: 12, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            View
          </span>
          <button
            className={showDots ? 'btn btn-accent' : 'btn'}
            onClick={() => setShowDots((v) => !v)}
            aria-pressed={showDots}
            title="Overlay outage dots"
          >
            Dots {showDots ? 'on' : 'off'}
          </button>
          <button
            className={showHeatmap ? 'btn btn-accent' : 'btn'}
            onClick={() => setShowHeatmap((v) => !v)}
            aria-pressed={showHeatmap}
            title="Toggle the district heat map layer"
          >
            Heatmap {showHeatmap ? 'on' : 'off'}
          </button>
          <span style={{ color: 'var(--text-muted)', fontSize: 12, marginLeft: 'auto' }}>
            {loading ? '…' : `${visibleDots.length} outages • ${formatSelection(effectiveSelection)}`}
          </span>
        </div>

        {/* Leaflet map: OSM base + translucent district heat + static dots on top */}
        <OdishaMap dots={visibleDots} districts={districtCounts} showDots={showDots} showHeatmap={showHeatmap} />

        {/* Date-time dial: exact hourly selection, adaptive tick density, zoomable */}
        {!loading && Number.isFinite(timeRange.min) && (
          <DateTimeKnob
              minMs={timeRange.min}
              maxMs={timeRange.max}
              selection={effectiveSelection}
              onChange={setSelection}
          />
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

