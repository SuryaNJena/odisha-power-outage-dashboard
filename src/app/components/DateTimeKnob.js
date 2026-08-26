'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';

// Date-time dial — a scale printed around a horizontal drum (cylinder), read
// under a static red V pointer (weighing-scale style). Time maps linearly to an
// angle around the drum and the screen x follows sin(θ), so the scale bunches
// toward the edges and spreads near the centre — the classic cylinder look.
// Dragging rotates the drum; the selection is always under the pointer at the
// centre. The selection is either a single point in time (start === end) or a
// 24-hour window, in which case the V pointer expands to bracket the window.
const HOUR_MS = 3600000;
const DAY_MS = 86400000;
const WINDOW_MS = 24 * HOUR_MS;
const W = 640;
const H = 300;
const MARG = 24;
const BAND = 54;
const ER = 20; // cylinder end-cap radius (x direction)
const Y0 = 176; // cylinder axis / band centreline
const VH = 46; // height of the V pointer arms above the band
const TH_MAX = (80 * Math.PI) / 180; // visible half-angle around the drum
const R = (W / 2 - MARG) / Math.sin(TH_MAX); // projection radius
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
function fmtClock(ms) {
  return `${String(new Date(ms).getHours()).padStart(2, '0')}:00`;
}
function formatTick(ms, iv, curYear) {
  const d = new Date(ms);
  if (iv >= 30 * DAY_MS) {
    const opts = d.getFullYear() === curYear ? { month: 'short' } : { month: 'short', year: 'numeric' };
    return d.toLocaleDateString(undefined, opts);
  }
  if (iv >= DAY_MS || d.getHours() === 0) return fmtDate(d, curYear);
  return fmtClock(ms);
}
// Ticks for the currently visible window, drawn at their absolute positions.
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
// Static "pill" silhouette of the cylinder side surface: straight top/bottom
// edges with half-ellipse end caps, spanning the full dial width.
function buildPillPath() {
  const top = Y0 - BAND / 2;
  const bot = Y0 + BAND / 2;
  const L = ER;
  const R = W - ER;
  return (
    `M ${L} ${top} L ${R} ${top} ` +
    `A ${ER} ${BAND / 2} 0 0 1 ${W} ${Y0} ` +
    `A ${ER} ${BAND / 2} 0 0 1 ${R} ${bot} ` +
    `L ${L} ${bot} ` +
    `A ${ER} ${BAND / 2} 0 0 1 0 ${Y0} ` +
    `A ${ER} ${BAND / 2} 0 0 1 ${L} ${top} Z`
  );
}
export default function DateTimeKnob({ minMs, maxMs, selection, onChange }) {
  const [view, setView] = useState({ start: minMs, end: maxMs });
  const svgRef = useRef(null);
  const dragging = useRef(false);
  const lastX = useRef(null);

  const sel =
    selection && Number.isFinite(selection.start)
      ? selection
      : { start: minMs, end: minMs };
  const rangeMs = Math.max(1, maxMs - minMs);
  const windowMode = sel.end > sel.start;
  const windowDur = Math.min(WINDOW_MS, rangeMs);
  const selCenter = (sel.start + sel.end) / 2;

  useEffect(() => {
    setView({ start: minMs, end: maxMs });
  }, [minMs, maxMs]);

  const viewDur = Math.max(1, view.end - view.start);
  // Cylindrical projection: time maps linearly to an angle around the drum and
  // the screen x is R·sin(θ), so the scale bunches toward the edges like a scale
  // printed on a cylinder. The selection is always under the centre pointer.
  // Past ±90° the tape has rotated behind the drum, so it is parked off-screen.
  const tapeX = useCallback(
    (t) => {
      const th = ((t - selCenter) / viewDur) * 2 * TH_MAX;
      if (th < -Math.PI / 2) return -1000;
      if (th > Math.PI / 2) return W + 1000;
      return W / 2 + R * Math.sin(th);
    },
    [selCenter, viewDur]
  );
  // Inverse projection: drum angle at a screen x (drag rotates the drum).
  const thetaAtScreenX = useCallback((x) => Math.asin(clamp((x - W / 2) / R, -1, 1)), []);

  // Emit a selection whose centre is c. With snap (drag release / keyboard), the
  // centre is rounded to the nearest hour; while dragging it stays continuous so
  // the scale follows the pointer smoothly. The zoom window is kept centred on
  // the new centre so the scale always fills the dial.
  const centeredView = useCallback(
    (c, dur) => {
      let s = c - dur / 2;
      let e = c + dur / 2;
      if (s < minMs) {
        s = minMs;
        e = s + dur;
      }
      if (e > maxMs) {
        e = maxMs;
        s = e - dur;
      }
      return { start: Math.max(minMs, s), end: Math.min(maxMs, e) };
    },
    [minMs, maxMs]
  );

  const emitFromCenter = useCallback(
    (c, snap) => {
      const center = snap ? roundToHour(c) : c;
      let next;
      if (windowMode) {
        const start = clamp(center - windowDur / 2, minMs, maxMs - windowDur);
        next = { start, end: start + windowDur };
      } else {
        const ms = clamp(center, minMs, maxMs);
        next = { start: ms, end: ms };
      }
      onChange(next);
      setView(centeredView((next.start + next.end) / 2, viewDur));
    },
    [onChange, windowMode, windowDur, minMs, maxMs, centeredView, viewDur]
  );

  // Safety net for selection changes coming from outside (initial default, data
  // reload, Now button): keep the zoom window centred on the selection.
  const recenter = useCallback(() => {
    setView(centeredView(selCenter, viewDur));
  }, [centeredView, selCenter, viewDur]);

  useEffect(() => {
    recenter();
  }, [recenter]);

  const zoomBy = useCallback(
    (factor) => {
      const dur = clamp(viewDur * factor, MIN_VIEW_MS, maxMs - minMs);
      setView(centeredView(selCenter, dur));
    },
    [viewDur, selCenter, maxMs, minMs, centeredView]
  );

  const toPoint = useCallback(() => {
    const ms = roundToHour(selCenter);
    onChange({ start: ms, end: ms });
  }, [onChange, selCenter]);

  const toWindow = useCallback(() => {
    const start = clamp(selCenter - windowDur / 2, minMs, maxMs - windowDur);
    onChange({ start, end: start + windowDur });
  }, [onChange, selCenter, windowDur, minMs, maxMs]);

  // Drag rotates the cylinder: pulling the scale right brings earlier times
  // under the pointer. Snap to the nearest hour on release.
  const onPointerDown = (e) => {
    dragging.current = true;
    lastX.current = e.clientX;
    e.currentTarget.setPointerCapture?.(e.pointerId);
  };
  const onPointerMove = (e) => {
    if (!dragging.current || lastX.current === null) return;
    const rect = svgRef.current.getBoundingClientRect();
    const toLocal = (cx) => clamp(((cx - rect.left) * W) / rect.width, 0, W);
    const x0 = toLocal(lastX.current);
    const x1 = toLocal(e.clientX);
    lastX.current = e.clientX;
    if (x1 === x0) return;
    // Rotate the drum so the surface point under the cursor follows it exactly.
    const dTh = thetaAtScreenX(x1) - thetaAtScreenX(x0);
    emitFromCenter(selCenter - (dTh / (2 * TH_MAX)) * viewDur, false);
  };
  const onPointerUp = (e) => {
    if (!dragging.current) return;
    dragging.current = false;
    lastX.current = null;
    emitFromCenter(selCenter, true);
    e.currentTarget.releasePointerCapture?.(e.pointerId);
  };

  const onKeyDown = (e) => {
    let next = null;
    if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = selCenter - HOUR_MS;
    else if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = selCenter + HOUR_MS;
    else if (e.key === 'PageUp') next = selCenter - DAY_MS;
    else if (e.key === 'PageDown') next = selCenter + DAY_MS;
    else if (e.key === 'Home') next = minMs + (windowMode ? windowDur / 2 : 0);
    else if (e.key === 'End') next = maxMs - (windowMode ? windowDur / 2 : 0);
    else return;
    e.preventDefault();
    emitFromCenter(next, true);
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

  // ---- Geometry (recomputed each render) ----
  const pillPath = buildPillPath();

  // Soft red highlight along the band marking the selected window.
  const hlX1 = clamp(tapeX(sel.start), 0, W);
  const hlX2 = clamp(tapeX(sel.end), 0, W);
  const highlightPath = windowMode && hlX2 - hlX1 > 1 ? `M ${hlX1.toFixed(1)} ${Y0} L ${hlX2.toFixed(1)} ${Y0}` : null;

  const ticks = buildTicks(view.start, view.end);
  const tipY = Y0;
  // Arms land on the window bounds (a small fixed spread keeps the V visible in
  // point mode, where the window has zero width).
  const armHalf = Math.max(6, (tapeX(sel.end) - tapeX(sel.start)) / 2);
  const armL = clamp(W / 2 - armHalf, 16, W - 16);
  const armR = clamp(W / 2 + armHalf, 16, W - 16);
  const needlePath = `M ${armL.toFixed(1)} ${(tipY - VH).toFixed(1)} L ${(W / 2).toFixed(1)} ${tipY.toFixed(
    1
  )} L ${armR.toFixed(1)} ${(tipY - VH).toFixed(1)}`;

  const curYear = new Date().getFullYear();
  const dateLine = windowMode
    ? `${fmtDate(new Date(sel.start), curYear)} ${fmtClock(sel.start)} – ${fmtDate(
        new Date(sel.end),
        curYear
      )} ${fmtClock(sel.end)}`
    : fmtDate(new Date(sel.start), curYear);
  const timeLine = windowMode ? `${Math.round(windowDur / HOUR_MS)} h window` : fmtClock(sel.start);
  const canZoomIn = viewDur > MIN_VIEW_MS + 1;
  const canZoomOut = viewDur < maxMs - minMs - 1;

  return (
    <div className="dtk">
      <div className="dtk-zoom">
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <div className="dtk-mode" role="group" aria-label="Selection mode">
            <button
              type="button"
              className={windowMode ? 'dtk-mode-btn' : 'dtk-mode-btn active'}
              onClick={toPoint}
              title="Select a single point in time"
            >
              Point
            </button>
            <button
              type="button"
              className={windowMode ? 'dtk-mode-btn active' : 'dtk-mode-btn'}
              onClick={toWindow}
              title="Select a 24-hour window"
            >
              24 h
            </button>
          </div>
          <button
            type="button"
            className="dtk-now"
            onClick={() => emitFromCenter(Date.now(), true)}
            title="Jump to the current time"
          >
            Now
          </button>
        </div>
        <div style={{ display: 'flex', gap: 6 }}>
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
        aria-valuenow={Math.round(selCenter)}
        aria-valuetext={windowMode ? dateLine : `${dateLine} ${timeLine}`}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onKeyDown={onKeyDown}
      >
        <defs>
          {/* Horizontal shading: the surface curves away toward the drum ends,
              so it darkens at the left/right edges and brightens at the centre. */}
          <linearGradient id="dtk-cyl" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="rgba(0,0,0,0.32)" />
            <stop offset="22%" stopColor="rgba(0,0,0,0.03)" />
            <stop offset="50%" stopColor="rgba(255,255,255,0.07)" />
            <stop offset="78%" stopColor="rgba(0,0,0,0.03)" />
            <stop offset="100%" stopColor="rgba(0,0,0,0.32)" />
          </linearGradient>
          <clipPath id="dtk-clip">
            <path d={pillPath} />
          </clipPath>
        </defs>

        {/* Cylinder body */}
        <path d={pillPath} className="dtk-band" />

        {/* Sliding content, clipped to the cylinder surface */}
        <g clipPath="url(#dtk-clip)">
          {highlightPath && <path d={highlightPath} className="dtk-window" />}

          {ticks.map((t) => {
            const x = tapeX(t.ms);
            const inner = BAND / 2 - 3;
            return (
              <line
                key={t.ms}
                x1={x.toFixed(1)}
                y1={(Y0 - inner).toFixed(1)}
                x2={x.toFixed(1)}
                y2={t.major ? (Y0 + inner).toFixed(1) : (Y0 + 3).toFixed(1)}
                className={t.major ? 'dtk-tick major' : 'dtk-tick'}
              />
            );
          })}

          {(() => {
            const els = [];
            let lastLX = -Infinity;
            for (const t of ticks) {
              if (!t.major || !t.label) continue;
              const raw = tapeX(t.ms);
              if (raw < 0 || raw > W) continue; // rotated behind the drum
              const x = clamp(raw, 32, W - 32);
              // Keep labels clear of the V pointer (centre) and of each other
              // where the cylinder projection compresses them near the edges.
              if (Math.abs(x - W / 2) < 22) continue;
              if (Math.abs(x - lastLX) < 22) continue;
              lastLX = x;
              els.push(
                <text
                  key={'l' + t.ms}
                  x={x.toFixed(1)}
                  y={(Y0 - 2).toFixed(1)}
                  textAnchor="middle"
                  className="dtk-label"
                >
                  {t.label}
                </text>
              );
            }
            return els;
          })()}
        </g>

        {/* Cylinder shading (fixed, so the sliding tape reads as rotation) */}
        <path d={pillPath} fill="url(#dtk-cyl)" className="dtk-shade" />

        {/* Static V pointer (red, weighing-scale style); expands to the window bounds */}
        <path d={needlePath} className="dtk-needle" />
        <circle cx={W / 2} cy={tipY} r={3.4} className="dtk-needle-dot" />
      </svg>

      <div className="dtk-value">
        <div className="dtk-date">{dateLine}</div>
        <div className="dtk-time">{timeLine}</div>
      </div>
      <div className="dtk-hint">Drag to rotate the scale • Scroll to zoom</div>
    </div>
  );
}
