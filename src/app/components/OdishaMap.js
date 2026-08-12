'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';
import * as d3 from 'd3';
import * as topojson from 'topojson-client';
import { MAP_URL } from '../config';
import { useTheme } from '../theme';

const MAX_DOTS = 600; // hard cap: beyond this, aggregate dots by substation
const PULSE_RINGS = 2; // fewer rings = cheaper frames
const MIN_ZOOM = 1;
const MAX_ZOOM = 8;
const ZOOM_STEP = 1.35;

// Heat scale: green (few) → yellow → orange → red (many).
// Dark mode uses deeper tones so districts stay dark and the bright blue
// dots keep strong contrast against them.
const LIGHT_RAMP = [
  [0x22, 0xc5, 0x5e],
  [0xfb, 0xbf, 0x24],
  [0xf9, 0x73, 0x16],
  [0xdc, 0x26, 0x26],
];

const DARK_RAMP = [
  [0x16, 0x65, 0x34], // green-800
  [0xa1, 0x62, 0x07], // yellow-700
  [0x9a, 0x34, 0x12], // orange-800
  [0x7f, 0x1d, 0x1d], // red-900
];

export const LEGEND_LIGHT = 'linear-gradient(90deg, #22c55e, #fbbf24, #f97316, #dc2626)';
export const LEGEND_DARK = 'linear-gradient(90deg, #166534, #a16207, #9a3412, #7f1d1d)';

function heatColor(t, ramp) {
  const seg = t * (ramp.length - 1);
  const i = Math.min(Math.floor(seg), ramp.length - 2);
  const f = seg - i;
  const a = ramp[i];
  const b = ramp[i + 1];
  const r = Math.round(a[0] + (b[0] - a[0]) * f);
  const g = Math.round(a[1] + (b[1] - a[1]) * f);
  const bl = Math.round(a[2] + (b[2] - a[2]) * f);
  return `rgb(${r},${g},${bl})`;
}

function getColor(value, min, max, dark) {
  if (max === min) return dark ? 'rgb(22,101,52)' : '#22c55e';
  const t = Math.min(1, Math.max(0, (value - min) / (max - min)));
  return heatColor(t, dark ? DARK_RAMP : LIGHT_RAMP);
}

/**
 * Merged map view:
 *  - District choropleth fills (heat map) rendered once as a static SVG layer.
 *  - Outage dots drawn on an animated <canvas> overlay (cheap 60fps pulse).
 *  - Plus/minus zoom buttons re-project the whole map.
 *  - All hover hit-testing happens on the canvas so tooltips work with dots on OR off.
 */
function OdishaMap({ dots = [], districts = [], showDots = true }) {
  const wrapRef = useRef(null);
  const svgRef = useRef(null);
  const canvasRef = useRef(null);
  const [topo, setTopo] = useState(null);
  const [tooltip, setTooltip] = useState(null);
  const [zoom, setZoom] = useState(1);
  const { theme } = useTheme();

  // Refs let the animation loop read live props without restarting.
  const dotsRef = useRef(dots);
  const showDotsRef = useRef(showDots);
  const zoomRef = useRef(1);
  const panRef = useRef({ dx: 0, dy: 0 }); // accumulated drag offset
  const layoutRef = useRef(null);
  const projectedRef = useRef([]); // [{d, x, y, seed}]
  // District heat data is kept in a ref so the animation loop + hover handlers
  // always read the latest values while re-projection stays cheap.
  const districtsRef = useRef(districts);
  const countsRef = useRef({ counts: {}, min: 0, max: 1 });

  useEffect(() => {
    dotsRef.current = dots;
  }, [dots]);
  useEffect(() => {
    showDotsRef.current = showDots;
  }, [showDots]);

  // When the filtered data slice changes, refresh the district heat scale and
  // re-project dots without restarting the canvas animation loop.
  useEffect(() => {
    districtsRef.current = districts;
    const counts = Object.fromEntries(
      districts.map((d) => [d.name.toLowerCase(), d.value])
    );
    const values = districts.map((d) => d.value);
    countsRef.current = {
      counts,
      min: values.length ? Math.min(...values) : 0,
      max: values.length ? Math.max(...values) : 1,
    };
    layoutRef.current?.();
  }, [districts]);

  useEffect(() => {
    layoutRef.current?.();
  }, [dots]);

  useEffect(() => {
    let cancelled = false;
    fetch(MAP_URL)
      .then((r) => r.json())
      .then((data) => {
        if (!cancelled) setTopo(data);
      })
      .catch((err) => console.error('Failed to load map topology:', err));
    return () => {
      cancelled = true;
    };
  }, []);

  const zoomBy = useCallback((dir) => {
    const next = Math.min(
      MAX_ZOOM,
      Math.max(MIN_ZOOM, zoomRef.current * (dir > 0 ? ZOOM_STEP : 1 / ZOOM_STEP))
    );
    if (next === zoomRef.current) return;
    zoomRef.current = next;
    setZoom(next);
    layoutRef.current?.();
  }, []);

  // Reset zoom + pan back to the fitted default view.
  const resetView = useCallback(() => {
    zoomRef.current = MIN_ZOOM;
    panRef.current = { dx: 0, dy: 0 };
    setZoom(MIN_ZOOM);
    layoutRef.current?.();
  }, []);

  // Main render effect: projection + static SVG districts + canvas dot loop + hover.
  useEffect(() => {
    if (!topo) return;
    const wrap = wrapRef.current;
    const svgEl = svgRef.current;
    const canvas = canvasRef.current;
    if (!wrap || !svgEl || !canvas) return;
    const ctx = canvas.getContext('2d');

    let w, h, dpr, projection, features;
    let districtSelection = null;
    let raf = null;
    let resizeTimer = null;
    const animStart = performance.now();

    function fillFor(f) {
      const { counts, min, max } = countsRef.current;
      const v = counts[(f.properties?.name || '').toLowerCase()];
      return v ? getColor(v, min, max, theme === 'dark') : 'var(--map-path)';
    }

    // Cap dots: aggregate beyond MAX_DOTS by substation to keep frames cheap.
    function prepareDots(rawDots) {
      if (rawDots.length <= MAX_DOTS) return rawDots;
      const byGss = new Map();
      rawDots.forEach((d) => {
        const key = `${d.gss || ''}|${d.district}`;
        const entry = byGss.get(key) || { ...d, count: 0 };
        entry.count += 1;
        byGss.set(key, entry);
      });
      return [...byGss.values()].slice(0, MAX_DOTS);
    }

    function layout() {
      w = Math.max(wrap.clientWidth, 320);
      h = Math.max(wrap.clientHeight, 420);
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      const key =
        Object.keys(topo.objects).find((k) => k.toLowerCase().includes('district')) ||
        Object.keys(topo.objects)[0];
      const raw = topojson.feature(topo, topo.objects[key]);
      features = (raw.type === 'FeatureCollection' ? raw.features : [raw]).filter(
        (f) => (f.properties?.name || '').toLowerCase() !== 'odisha'
      );
      const collection = { type: 'FeatureCollection', features };

      projection = buildProjection(collection, zoomRef.current, panRef.current);
      renderLayers();
    }

    // Single source of truth for the projection: fitExtent → scale by z about the
    // viewport centre → apply accumulated pan. Both button zoom, mouse-wheel,
    // drag-pan and touch pinch all go through zoomRef/panRef, so the view survives
    // resize / theme changes.
    function buildProjection(collection, z, pan) {
      const p = d3.geoMercator().fitExtent([[12, 12], [w - 12, h - 12]], collection);
      if (z !== 1) {
        // Zoom toward the current viewport center so the map never drifts off-screen.
        const cx = w / 2;
        const cy = h / 2;
        const center = p.invert([cx, cy]);
        p.scale(p.scale() * z);
        const pp = p(center);
        const t = p.translate();
        p.translate([t[0] + (cx - pp[0]), t[1] + (cy - pp[1])]);
      }
      // Apply accumulated drag offset so panning survives zoom/resize/theme changes.
      const t = p.translate();
      p.translate([t[0] + pan.dx, t[1] + pan.dy]);
      return p;
    }

    // Coalesce rebuilds to one per animation frame (keeps pinch/drag smooth).
    let layoutRaf = null;
    function commitLayout() {
      if (layoutRaf) return;
      layoutRaf = requestAnimationFrame(() => {
        layoutRaf = null;
        layout();
      });
    }

    // Zoom by `factor` keeping the geographic point currently under (cx, cy) fixed.
    // Used by two-finger pinch and double-tap zoom.
    function zoomAt(factor, cx, cy) {
      const next = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoomRef.current * factor));
      if (next === zoomRef.current) return;
      const g = projection.invert([cx, cy]); // point under the fingers right now
      const collection = { type: 'FeatureCollection', features };
      const q = buildProjection(collection, next, { dx: 0, dy: 0 })(g);
      zoomRef.current = next;
      panRef.current = { dx: cx - q[0], dy: cy - q[1] };
      setZoom(next);
      commitLayout();
    }

    // Re-render districts (SVG) and recompute projected dots from the current projection.
    function renderLayers() {
      const path = d3.geoPath(projection);
      const svg = d3.select(svgEl);
      svg.selectAll('*').remove();
      svg.attr('width', w).attr('height', h).style('background', 'var(--map-bg)');
      const g = svg.append('g');
      districtSelection = g
        .selectAll('path.district')
        .data(features)
        .join('path')
        .attr('d', path)
        .attr('fill', fillFor)
        .attr('stroke', 'var(--map-stroke)')
        .attr('stroke-width', 0.7);

      // Precompute projected dot positions (also used by canvas hit-testing).
      const prepared = prepareDots(dotsRef.current);
      projectedRef.current = prepared.map((d, i) => {
        const p = projection([d.lon, d.lat]);
        return { d, x: p[0], y: p[1], seed: ((i * 0.37) % 1) };
      });
    }

    layout();
    layoutRef.current = layout;

    function drawDot(pt, elapsed, color, rBase) {
      const { x, y, seed } = pt;
      const phase = ((elapsed / 2600) + seed) % 1;
      for (let i = 0; i < PULSE_RINGS; i++) {
        const pp = (phase + i / PULSE_RINGS) % 1;
        const r = rBase + pp * (rBase * 4);
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.globalAlpha = (1 - pp) * 0.7;
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.4;
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
      ctx.beginPath();
      ctx.arc(x, y, rBase, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
    }

    function frame(now) {
      ctx.clearRect(0, 0, w, h);
      if (showDotsRef.current) {
        // Blue dots contrast against the green→red heat scale.
        const color = theme === 'dark' ? '#60a5fa' : '#2563eb';
        const elapsed = now - animStart;
        // Dots scale with zoom so they stay proportional to the map.
        const rBase = 3.5 + (zoomRef.current - 1) * 1.1;
        for (const pt of projectedRef.current) drawDot(pt, elapsed, color, rBase);
      }
      raf = requestAnimationFrame(frame);
    }
    raf = requestAnimationFrame(frame);


    // --- Input handling (mouse + touch + pen via Pointer Events) ---
    let dragging = false;
    let lastX = 0;
    let lastY = 0;
    const pointers = new Map(); // pointerId -> {x, y} (client coords)
    let pinchState = null; // { dist, midX, midY }
    let tapState = null; // { x, y, t, moved } — tracks a possible tap
    let lastTapAt = 0; // for double-tap detection
    let lastTapPt = null;

    function panBy(dx, dy) {
      panRef.current.dx += dx;
      panRef.current.dy += dy;
      commitLayout();
    }

    function canvasPoint(e) {
      const rect = canvas.getBoundingClientRect();
      return { x: e.clientX - rect.left, y: e.clientY - rect.top };
    }

    // Unified hover / tap hit-test: dots (if enabled) then districts via geoContains.
    function updateHover(x, y, clientX, clientY) {
      let hitDot = null;
      if (showDotsRef.current) {
        let best = 10;
        for (const pt of projectedRef.current) {
          const dx = pt.x - x;
          const dy = pt.y - y;
          const dist = Math.hypot(dx, dy);
          if (dist < best) {
            best = dist;
            hitDot = pt;
          }
        }
      }

      const lonlat = projection.invert([x, y]);
      let hitFeature = null;
      if (!hitDot && isFinite(lonlat[0]) && isFinite(lonlat[1])) {
        hitFeature = features.find((f) => d3.geoContains(f, lonlat)) || null;
      }

      // Highlight hovered district, restore others to their base color.
      if (districtSelection) {
        districtSelection.attr('fill', (f) =>
          hitFeature && f === hitFeature ? 'var(--map-path-hover)' : fillFor(f)
        );
      }
      canvas.style.cursor = hitDot ? 'pointer' : 'grab';

      // Keep the tooltip on-screen on small phones.
      const vw = typeof window !== 'undefined' ? window.innerWidth : 1200;
      const left = Math.max(8, Math.min(clientX + 14, vw - 224));

      if (hitDot) {
        setTooltip({
          x: left,
          y: clientY,
          title: hitDot.d.gss || hitDot.d.district,
          sub: `${hitDot.d.district} • ${hitDot.d.feeder || hitDot.d.division || '—'}`,
          extra: hitDot.d.reason || null,
        });
      } else if (hitFeature) {
        const name = hitFeature.properties?.name || 'Unknown district';
        const v = countsRef.current.counts[name.toLowerCase()];
        setTooltip({
          x: left,
          y: clientY,
          title: name,
          sub: v ? `${v} outage${v !== 1 ? 's' : ''}` : 'No recorded outages',
        });
      } else {
        setTooltip(null);
      }
    }

    const onPointerDown = (e) => {
      if (e.pointerType === 'mouse' && e.button !== 0) return; // ignore right/middle
      try {
        canvas.setPointerCapture(e.pointerId);
      } catch (_) {}
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      setTooltip(null);

      if (pointers.size === 1) {
        dragging = true;
        lastX = e.clientX;
        lastY = e.clientY;
        tapState = { x: e.clientX, y: e.clientY, t: performance.now(), moved: false };
        canvas.style.cursor = 'grabbing';
      } else if (pointers.size === 2) {
        dragging = false;
        tapState = null;
        const [a, b] = [...pointers.values()];
        pinchState = {
          dist: Math.hypot(a.x - b.x, a.y - b.y),
          midX: (a.x + b.x) / 2,
          midY: (a.y + b.y) / 2,
        };
        canvas.style.cursor = 'grabbing';
      }
      if (e.cancelable) e.preventDefault();
    };

    const onPointerMove = (e) => {
      if (pointers.has(e.pointerId)) {
        pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      }

      // Two-finger pinch → zoom around the pinch midpoint.
      if (pointers.size === 2 && pinchState) {
        const [a, b] = [...pointers.values()];
        const dist = Math.hypot(a.x - b.x, a.y - b.y);
        if (pinchState.dist > 0 && dist > 0) {
          const rect = canvas.getBoundingClientRect();
          const midX = (a.x + b.x) / 2 - rect.left;
          const midY = (a.y + b.y) / 2 - rect.top;
          zoomAt(dist / pinchState.dist, midX, midY);
        }
        pinchState.dist = dist;
        if (e.cancelable) e.preventDefault();
        return;
      }

      if (pointers.size === 1) {
        if (dragging) {
          // Drag to pan (mouse or touch).
          const dx = e.clientX - lastX;
          const dy = e.clientY - lastY;
          lastX = e.clientX;
          lastY = e.clientY;
          if (tapState) tapState.moved = tapState.moved || Math.hypot(dx, dy) > 5;
          if (dx || dy) panBy(dx, dy);
          if (e.cancelable) e.preventDefault();
          return;
        }
      }

      // Mouse hover (no button pressed → no active pointers). Touch has no hover;
      // taps show tooltips on pointerup instead.
      if (pointers.size === 0 && e.pointerType === 'mouse' && !dragging) {
        const pt = canvasPoint(e);
        updateHover(pt.x, pt.y, e.clientX, e.clientY);
      }
    };

    const onPointerUp = (e) => {
      const isTouch = e.pointerType !== 'mouse';

      // A quick tap (no drag) shows the tooltip; a second tap zooms in.
      if (isTouch && tapState && !tapState.moved && pointers.size === 1) {
        const dur = performance.now() - tapState.t;
        if (dur < 600) {
          const pt = canvasPoint(e);
          const now = performance.now();
          const isDoubleTap =
            lastTapPt &&
            now - lastTapAt < 350 &&
            Math.hypot(pt.x - lastTapPt.x, pt.y - lastTapPt.y) < 44;
          if (isDoubleTap) {
            setTooltip(null);
            zoomAt(ZOOM_STEP, pt.x, pt.y);
          } else {
            updateHover(pt.x, pt.y, e.clientX, e.clientY);
          }
          lastTapAt = now;
          lastTapPt = pt;
        }
      }

      pointers.delete(e.pointerId);
      if (pointers.size < 2) pinchState = null;
      if (pointers.size === 0) {
        dragging = false;
        tapState = null;
        canvas.style.cursor = 'grab';
      }
      try {
        canvas.releasePointerCapture(e.pointerId);
      } catch (_) {}
    };

    const onPointerCancel = (e) => {
      pointers.delete(e.pointerId);
      pinchState = null;
      if (pointers.size === 0) {
        dragging = false;
        tapState = null;
        canvas.style.cursor = 'grab';
      }
      try {
        canvas.releasePointerCapture(e.pointerId);
      } catch (_) {}
    };

    const onPointerLeave = () => {
      if (districtSelection) districtSelection.attr('fill', fillFor);
      if (!dragging) canvas.style.cursor = 'grab';
      setTooltip(null);
    };

    canvas.addEventListener('pointerdown', onPointerDown);
    canvas.addEventListener('pointermove', onPointerMove);
    canvas.addEventListener('pointerup', onPointerUp);
    canvas.addEventListener('pointercancel', onPointerCancel);
    canvas.addEventListener('pointerleave', onPointerLeave);
    canvas.style.cursor = 'grab';
    canvas.style.touchAction = 'none';

    // Debounced resize — avoids hammering the projection/layout code.
    const ro = new ResizeObserver(() => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(layout, 120);
    });
    ro.observe(wrap);

    // Mouse-wheel zoom, throttled so it stays smooth and cheap.
    let lastWheelAt = 0;
    const onWheel = (e) => {
      e.preventDefault();
      const now = performance.now();
      if (now - lastWheelAt < 80) return;
      lastWheelAt = now;
      zoomBy(e.deltaY < 0 ? 1 : -1);
    };
    canvas.addEventListener('wheel', onWheel, { passive: false });

    return () => {
      cancelAnimationFrame(raf);
      if (layoutRaf) cancelAnimationFrame(layoutRaf);
      ro.disconnect();
      clearTimeout(resizeTimer);
      canvas.removeEventListener('pointerdown', onPointerDown);
      canvas.removeEventListener('pointermove', onPointerMove);
      canvas.removeEventListener('pointerup', onPointerUp);
      canvas.removeEventListener('pointercancel', onPointerCancel);
      canvas.removeEventListener('pointerleave', onPointerLeave);
      canvas.removeEventListener('wheel', onWheel);
      layoutRef.current = null;
    };
  }, [topo, theme]);


  return (
    <div className="card" style={{ padding: 8, position: 'relative' }}>
      <div
        ref={wrapRef}
        style={{
          position: 'relative',
          width: '100%',
          height: 'clamp(420px, 60vh, 640px)',
          overflow: 'hidden', // clip zoomed content cleanly inside the card
          borderRadius: 'var(--radius-sm)',
        }}
      >
        <svg
          ref={svgRef}
          style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }}
        />
        <canvas
          ref={canvasRef}
          style={{
            position: 'absolute',
            inset: 0,
            touchAction: 'none', // let pan/pinch reach us instead of scrolling the page
            userSelect: 'none',
            WebkitUserSelect: 'none',
          }}
        />
      </div>

      {/* Zoom controls */}
      <div className="map-controls">
        <button
          className="zoom-btn"
          onClick={resetView}
          title="Zoom to fit (reset view)"
          aria-label="Zoom to fit"
        >
          ⤢
        </button>
        <button
          className="zoom-btn"
          onClick={() => zoomBy(1)}
          disabled={zoom >= MAX_ZOOM}
          title="Zoom in"
          aria-label="Zoom in"
        >
          +
        </button>
        <button
          className="zoom-btn"
          onClick={() => zoomBy(-1)}
          disabled={zoom <= MIN_ZOOM}
          title="Zoom out"
          aria-label="Zoom out"
        >
          −
        </button>
      </div>

      {/* Heat-map legend */}
      <div className="map-legend">
        <div className="stat-label" style={{ marginBottom: 5 }}>Outages</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>Few</span>
          <div
            style={{
              width: 110,
              height: 10,
              borderRadius: 6,
              background: theme === 'dark' ? LEGEND_DARK : LEGEND_LIGHT,
            }}
          />
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>Many</span>
        </div>
      </div>

      {tooltip && (
        <div className="map-tooltip" style={{ left: tooltip.x, top: tooltip.y - 12 }}>
          <div className="tt-title">{tooltip.title}</div>
          <div className="tt-sub">{tooltip.sub}</div>
          {tooltip.extra && (
            <div className="tt-sub" style={{ marginTop: 3 }}>{tooltip.extra}</div>
          )}
        </div>
      )}
    </div>
  );
}

export default React.memo(OdishaMap);

