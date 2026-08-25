'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';

// Date-time dial — exact hourly selection, adaptive tick density, zoomable range.
// The band is a shallow curved "dial" (like a weighing scale). It is rendered as a
// curve for looks, but x maps linearly to time, so the drag maps x → timestamp →
// nearest 1-hour increment. The underlying step is always one hour regardless of how
// many ticks are actually drawn.
const HOUR_MS = 3600000;
const DAY_MS = 86400000;
const W = 640;
const H = 172;
const MARG = 24;
const BAND = 18;
const AMP = 16;
const Y0 = 108;
const ZOOM = 2.2;
const MIN_VIEW_MS = 3 * HOUR_MS;
const MAX_MAJOR = 14;
const MAX_TOTAL = 48;

const LADDER_HOURS = [1, 2, 3, 4, 6, 12, 24, 48, 72, 168, 336, 720, 1440, 2160, 4320, 8760];

function clamp(v, a, b) {
  return Math.max(a, Math.min(b, v));
}
function roundToHour(ms) {
  const d = new Date(ms);
  d.setSeconds(0, 0);
  if (d.getMinutes() >= 30) d.setHours(d.getHours() + 1);
  d.setMinutes(0);
  return d.getTime();
}
function pickInterval(durMs) {
  for (const h of LADDER_HOURS) {
    if (Math.ceil(durMs / (h * HOUR_MS)) <= MAX_MAJOR) return h * HOUR_MS;
  }
  return 8760 * HOUR_MS;
}
function subDivisions(iv) {
  if (iv < 4 * HOUR_MS) return 4;
  if (iv < DAY_MS) return 4;
  if (iv === DAY_MS) return 6;
  if (iv === 2 * DAY_MS) return 8;
  if (iv === 3 * DAY_MS) return 6;
  if (iv === 7 * DAY_MS) return 7;
  if (iv < 30 * DAY_MS) return 4;
  return 1;
}
function fmtDate(d, curYear) {
  const opts =
    d.getFullYear() === curYear
      ? { day: 'numeric', month: 'short' }
      : { day: 'numeric', month: 'short', year: 'numeric' };
  return d.toLocaleDateString(undefined, opts);
}
function formatTick(ms, iv, curYear) {
  const d = new Date(ms);
  if (iv >= 30 * DAY_MS) {
    const opts = d.getFullYear() === curYear ? { month: 'short' } : { month: 'short', year: 'numeric' };
    return d.toLocaleDateString(undefined, opts);
  }
  if (iv >= DAY_MS || d.getHours() === 0) return fmtDate(d, curYear);
  return `${String(d.getHours()).padStart(2, '0')}:00`;
}
function yCenterAt(f) {
  return { x: MARG + f * (W - 2 * MARG), y: Y0 - AMP * Math.sin(Math.PI * f) };
}
function buildBandPath() {
  const N = 80;
  let d = '';
  for (let i = 0; i <= N; i++) {
    const p = yCenterAt(i / N);
    d += (i ? 'L' : 'M') + p.x.toFixed(1) + ' ' + (p.y - BAND / 2).toFixed(1) + ' ';
  }
  for (let i = N; i >= 0; i--) {
    const p = yCenterAt(i / N);
    d += 'L' + p.x.toFixed(1) + ' ' + (p.y + BAND / 2).toFixed(1) + ' ';
  }
  return d + 'Z';
}
function buildTicks(viewStart, viewEnd) {
  const dur = Math.max(1, viewEnd - viewStart);
  const iv = pickInterval(dur);
  let sub = subDivisions(iv);
  let minorStep = iv / sub;
  while (dur / minorStep > MAX_TOTAL && sub > 1) {
    sub = Math.max(1, Math.floor(sub / 2));
    minorStep = iv / sub;
  }
  const curYear = new Date().getFullYear();
  const ticks = [];
  const startTick = Math.floor(viewStart / minorStep) * minorStep;
  for (let t = startTick; t <= viewEnd; t += minorStep) {
    if (t < viewStart) continue;
    const isMajor = Math.abs(t / iv - Math.round(t / iv)) < 1e-6;
    ticks.push({ ms: t, major: isMajor, label: isMajor ? formatTick(t, iv, curYear) : null });
  }
  return ticks;
}
export default function DateTimeKnob({ minMs, maxMs, valueMs, onChange }) {
  const [view, setView] = useState({ start: minMs, end: maxMs });
  const svgRef = useRef(null);
  const dragging = useRef(false);

  useEffect(() => {
    setView({ start: minMs, end: maxMs });
  }, [minMs, maxMs]);

  const viewDur = Math.max(1, view.end - view.start);
  const clampValue = useCallback((ms) => clamp(roundToHour(ms), minMs, maxMs), [minMs, maxMs]);

  const applyClientX = useCallback(
    (clientX) => {
      const rect = svgRef.current.getBoundingClientRect();
      const lx = (clientX - rect.left) * (W / rect.width);
      const f = clamp((lx - MARG) / (W - 2 * MARG), 0, 1);
      onChange(clampValue(view.start + f * viewDur));
    },
    [onChange, clampValue, view.start, viewDur]
  );

  const zoomBy = useCallback(
    (factor) => {
      const sel = clampValue(valueMs);
      const dur = clamp(viewDur * factor, MIN_VIEW_MS, maxMs - minMs);
      let s = sel - dur / 2;
      let e = sel + dur / 2;
      if (s < minMs) {
        s = minMs;
        e = s + dur;
      }
      if (e > maxMs) {
        e = maxMs;
        s = e - dur;
      }
      setView({ start: Math.max(minMs, s), end: Math.min(maxMs, e) });
    },
    [clampValue, valueMs, viewDur, minMs, maxMs]
  );

  const setValueFollow = useCallback(
    (ms) => {
      const v = clampValue(ms);
      onChange(v);
      if (v < view.start || v > view.end) {
        const dur = viewDur;
        let s = v - dur / 2;
        let e = v + dur / 2;
        if (s < minMs) {
          s = minMs;
          e = s + dur;
        }
        if (e > maxMs) {
          e = maxMs;
          s = e - dur;
        }
        setView({ start: Math.max(minMs, s), end: Math.min(maxMs, e) });
      }
    },
    [onChange, clampValue, view.start, view.end, viewDur, minMs, maxMs]
  );

  const onPointerDown = (e) => {
    dragging.current = true;
    e.currentTarget.setPointerCapture?.(e.pointerId);
    applyClientX(e.clientX);
  };
  const onPointerMove = (e) => {
    if (dragging.current) applyClientX(e.clientX);
  };
  const onPointerUp = (e) => {
    dragging.current = false;
    e.currentTarget.releasePointerCapture?.(e.pointerId);
  };

  const onKeyDown = (e) => {
    let next = null;
    if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = valueMs - HOUR_MS;
    else if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = valueMs + HOUR_MS;
    else if (e.key === 'PageUp') next = valueMs - DAY_MS;
    else if (e.key === 'PageDown') next = valueMs + DAY_MS;
    else if (e.key === 'Home') next = minMs;
    else if (e.key === 'End') next = maxMs;
    else return;
    e.preventDefault();
    setValueFollow(next);
  };

  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const onWheel = (e) => {
      e.preventDefault();
      zoomBy(e.deltaY > 0 ? ZOOM : 1 / ZOOM);
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [zoomBy]);

  const ticks = buildTicks(view.start, view.end);
  const fSel = clamp((valueMs - view.start) / viewDur, 0, 1);
  const selPoint = yCenterAt(fSel);
  const bandPath = buildBandPath();
  const dt = new Date(valueMs);
  const dateLine = fmtDate(dt, new Date().getFullYear());
  const timeLine = `${String(dt.getHours()).padStart(2, '0')}:00`;
  const canZoomIn = viewDur > MIN_VIEW_MS + 1;
  const canZoomOut = viewDur < maxMs - minMs - 1;

  return (
    <div className="dtk">
      <div className="dtk-zoom">
        <button
          type="button"
          className="dtk-btn"
          onClick={() => zoomBy(1 / ZOOM)}
          disabled={!canZoomIn}
          aria-label="Zoom in to finer time"
          title="Zoom in"
        >
          +
        </button>
        <button
          type="button"
          className="dtk-btn"
          onClick={() => zoomBy(ZOOM)}
          disabled={!canZoomOut}
          aria-label="Zoom out to coarser time"
          title="Zoom out"
        >
          −
        </button>
      </div>

      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        className="dtk-svg"
        role="slider"
        tabIndex={0}
        aria-label="Select date and time"
        aria-valuemin={minMs}
        aria-valuemax={maxMs}
        aria-valuenow={valueMs}
        aria-valuetext={`${dateLine} ${timeLine}`}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onKeyDown={onKeyDown}
      >
        <path d={bandPath} className="dtk-band" />

        {ticks.map((t) => {
          const p = yCenterAt((t.ms - view.start) / viewDur);
          const len = t.major ? 15 : 9;
          return (
            <line
              key={t.ms}
              x1={p.x}
              y1={p.y}
              x2={p.x}
              y2={p.y - len}
              className={t.major ? 'dtk-tick major' : 'dtk-tick'}
            />
          );
        })}

        {ticks
          .filter((t) => t.major && t.label)
          .map((t) => {
            const p = yCenterAt((t.ms - view.start) / viewDur);
            return (
              <text
                key={'l' + t.ms}
                x={clamp(p.x, 32, W - 32)}
                y={p.y - 20}
                textAnchor="middle"
                className="dtk-label"
              >
                {t.label}
              </text>
            );
          })}

        {/* Needle */}
        <line x1={selPoint.x} y1={selPoint.y + 8} x2={selPoint.x} y2={selPoint.y - 20} className="dtk-needle" />
        <circle cx={selPoint.x} cy={selPoint.y} r={4.5} className="dtk-needle-dot" />
      </svg>

      <div className="dtk-value">
        <div className="dtk-date">{dateLine}</div>
        <div className="dtk-time">{timeLine}</div>
      </div>
    </div>
  );
}


