'use client';

import { useEffect, useRef, useState } from 'react';

const HOUR = 3600000;
const DAY = 24 * HOUR;
const ARC = Math.PI * 0.46;
const steps = [1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 10080, 20160, 43200, 129600, 525600];
const date = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
const day = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short' });
const clock = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit' });
const stamp = ms => date.format(ms) + ' ' + clock.format(ms);
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

function snap(ms) {
  const d = new Date(ms);
  d.setHours(d.getHours() + (d.getMinutes() >= 30 ? 1 : 0), 0, 0, 0);
  return +d;
}

function drumScale(center, span, width, min, max) {
  // Equal time intervals occupy equal angles on the drum. Project their
  // position and apparent width onto the face of the instrument.
  const radius = (width / 2 - 4) / Math.sin(ARC);
  const angle = ms => (ms - center) * 2 * ARC / span;
  const project = ms => width / 2 + radius * Math.sin(clamp(angle(ms), -ARC, ARC));
  const target = span / 60000 / Math.max(8, width / 9);
  const step = (steps.find(minutes => minutes >= target) ?? Math.ceil(target)) * 60000;
  const origin = new Date(center).getTimezoneOffset() * 60000;
  const ticks = [];
  let lastRight = 0;
  const first = Math.ceil((center - span / 2 - origin) / step);
  const last = Math.floor((center + span / 2 - origin) / step);
  for (let i = first; i <= last; i++) {
    const ms = i * step + origin;
    const x = project(ms);
    const face = Math.cos(angle(ms));
    const major = i % 4 === 0;
    const middle = i % 2 === 0;
    const available = ms >= min && ms <= max;
    let label = '';
    if (available && face > 0.32) {
      const d = new Date(ms);
      const midnight = d.getHours() === 0 && d.getMinutes() === 0;
      const candidate = step >= DAY || midnight ? day.format(ms) : clock.format(ms);
      const half = candidate.length * 3.5;
      // Reserve space for the next major graduation before adding a minor label.
      const nextMajor = project(ms + (4 - ((i % 4 + 4) % 4)) * step);
      if (x - half > lastRight + 4 && x + half < width &&
          (major || nextMajor - x > 2 * half + 6)) {
        label = candidate;
        lastRight = x + half;
      }
    }
    ticks.push(<g key={i} opacity={available ? 1 : 0.22}>
      <line x1={x} x2={x} y1={46} y2={major ? 29 : middle ? 34 : 39}
        stroke="currentColor" strokeWidth={major ? 1.6 : 1} />
      {label && <text x={x} y="23" textAnchor="middle">{label}</text>}
    </g>);
  }
  return { ticks, project };
}

export default function DateTimeKnob({ minMs, maxMs, selection, onChange }) {
  const range = Math.max(1, maxMs - minMs);
  const duration = selection.end - selection.start;
  const selected = (selection.start + selection.end) / 2;
  const lo = minMs + duration / 2;
  const hi = maxMs - duration / 2;
  const [span, setSpan] = useState(range);
  const [width, setWidth] = useState(640);
  const [preview, setPreview] = useState(null);
  const center = clamp(preview ?? selected, lo, hi);
  const minimum = Math.min(range, 3 * HOUR);
  const svg = useRef(null);
  const drag = useRef(null);
  const frame = useRef(0);

  useEffect(() => { setSpan(range); }, [minMs, maxMs, range]);
  useEffect(() => {
    const el = svg.current;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(12, entry.contentRect.width)));
    observer.observe(el);
    let wheelFrame = 0;
    let delta = 0;
    function wheel(e) {
      e.preventDefault();
      if (drag.current) return;
      delta += e.deltaY * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 300 : 1);
      if (!wheelFrame) wheelFrame = requestAnimationFrame(() => {
        const factor = Math.exp(clamp(delta, -300, 300) * 0.004);
        setSpan(v => clamp(v * factor, minimum, range));
        delta = wheelFrame = 0;
      });
    }
    el.addEventListener('wheel', wheel, { passive: false });
    return () => {
      observer.disconnect();
      el.removeEventListener('wheel', wheel);
      cancelAnimationFrame(wheelFrame);
      cancelAnimationFrame(frame.current);
    };
  }, [minimum, range]);

  function select(c, length = duration) {
    const start = clamp(c - length / 2, minMs, maxMs - length);
    onChange({ start, end: start + length });
  }
  function startDrag(e) {
    if (drag.current || e.button !== 0 || !e.isPrimary) return;
    const rect = svg.current.getBoundingClientRect();
    const radius = (rect.width / 2 - 4) / Math.sin(ARC);
    const angle = x => Math.asin(clamp((x - rect.left - rect.width / 2) / radius, -1, 1));
    drag.current = { id: e.pointerId, angle, previous: angle(e.clientX), center, lastPublish: performance.now() };
    e.currentTarget.setPointerCapture(e.pointerId);
  }
  function advance(x) {
    const d = drag.current;
    const angle = d.angle(x);
    d.center = clamp(d.center + (d.previous - angle) * span / (2 * ARC), lo, hi);
    d.previous = angle;
    return d.center;
  }
  function move(e) {
    if (drag.current?.id !== e.pointerId) return;
    advance(e.clientX);
    if (!frame.current) frame.current = requestAnimationFrame(now => {
      frame.current = 0;
      const d = drag.current;
      if (!d) return;
      setPreview(d.center);
      if (now - d.lastPublish >= 120) {
        select(d.center);
        d.lastPublish = now;
      }
    });
  }
  function finish(e) {
    if (drag.current?.id !== e.pointerId) return;
    const c = e.type === 'pointerup' ? advance(e.clientX) : drag.current.center;
    cancelAnimationFrame(frame.current);
    frame.current = 0;
    drag.current = null;
    setPreview(null);
    select(snap(c));
  }
  function keyDown(e) {
    const values = { ArrowLeft: center - HOUR, ArrowUp: center - HOUR,
      ArrowRight: center + HOUR, ArrowDown: center + HOUR, PageUp: center - DAY,
      PageDown: center + DAY, Home: lo, End: hi };
    if (!Object.hasOwn(values, e.key)) return;
    e.preventDefault();
    select(e.key === 'Home' || e.key === 'End' ? values[e.key] : snap(values[e.key]));
  }

  const { ticks, project } = drumScale(center, span, width, minMs, maxMs);
  const left = project(center - duration / 2);
  const right = project(center + duration / 2);
  const readout = duration ? stamp(center - duration / 2) + ' – ' + stamp(center + duration / 2) : stamp(center);
  return <section className="card dtk" aria-label="Outage timeline">
    <div className="dtk-bar">
      <div className="dtk-actions" role="group" aria-label="Time selection mode">
        <button aria-pressed={!duration} onClick={() => select(snap(center), 0)}>Point</button>
        <button aria-pressed={!!duration} onClick={() => select(center, Math.min(DAY, maxMs - minMs))}>24 h</button>
        <button onClick={() => select(snap(Date.now()))}>Now</button>
      </div>
      <strong className="dtk-readout">{readout}</strong>
      <div className="dtk-actions">
        <button aria-label="Zoom out" disabled={span >= range} onClick={() => setSpan(v => Math.min(range, v * 2.2))}>−</button>
        <button aria-label="Zoom in" disabled={span <= minimum} onClick={() => setSpan(v => Math.max(minimum, v / 2.2))}>+</button>
      </div>
    </div>
    <svg ref={svg} className="dtk-drum" viewBox={`0 0 ${width} 50`} role="slider" tabIndex={0}
      aria-label="Outage date and time" aria-valuemin={lo} aria-valuemax={hi} aria-valuenow={center}
      aria-valuetext={readout} aria-describedby="timeline-help" onPointerDown={startDrag} onPointerMove={move}
      onPointerUp={finish} onPointerCancel={finish} onLostPointerCapture={finish} onKeyDown={keyDown}>
      <title>Drag to rotate · Scroll to zoom</title>
      {duration > 0 && <rect x={left} width={right-left} height="48" fill="var(--accent-soft)" />}
      {ticks}
      <path d={`M${width/2} 8 V48`} stroke="var(--accent)" strokeWidth="2" />
      <path d={`M${width/2-5} 0 h10 l-5 9 Z`} fill="var(--accent)" />
    </svg>
    <span id="timeline-help" className="dtk-help">Drag to rotate or scroll to zoom. Arrow keys step one hour, Page Up and Page Down one day. Home and End select the limits.</span>
  </section>;
}
