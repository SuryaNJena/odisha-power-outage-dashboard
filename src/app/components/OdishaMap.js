'use client';

import React, { useEffect, useRef, useState } from 'react';
import * as topojson from 'topojson-client';
import 'leaflet/dist/leaflet.css';
import { MAP_URL } from '../config';
import { useTheme } from '../theme';

const REPORT_TIME = new Intl.DateTimeFormat('en-IN', {
  dateStyle: 'medium',
  timeStyle: 'short',
  timeZone: 'Asia/Kolkata',
});
const FIT_OPTIONS = { padding: [12, 12], maxZoom: 9 };
const MAX_DOTS = 600; // hard cap: beyond this, aggregate dots by substation

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

// Theme-aware Esri World Gray Canvas basemap — free, no API key. (CARTO's
// public basemap CDN now requires an API key, so we use Esri's canvas tiles:
// a gray base with a transparent reference layer on top for labels.)
function tileUrls(dark) {
  const base = dark ? 'World_Dark_Gray_Base' : 'World_Light_Gray_Base';
  const ref = dark ? 'World_Dark_Gray_Reference' : 'World_Light_Gray_Reference';
  const tpl = (layer) =>
    `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/${layer}/MapServer/tile/{z}/{y}/{x}`;
  return { base: tpl(base), ref: tpl(ref) };
}

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

function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Resolve a CSS custom property to its concrete value (theme-aware).
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// Cap dots: aggregate beyond MAX_DOTS by substation to keep the DOM cheap.
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

function districtTooltip(name, value) {
  const sub = value ? `${value} outage${value !== 1 ? 's' : ''}` : 'No recorded outages';
  return `<div class="tt-title">${escapeHtml(name)}</div><div class="tt-sub">${sub}</div>`;
}

function dotTooltip(d) {
  const title = escapeHtml(d.gss || d.district || 'Outage');
  const sub = `${escapeHtml(d.district || '—')} • ${escapeHtml(d.feeder || d.division || '—')}`;
  const extra = d.reason ? `<div class="tt-sub" style="margin-top:3px">${escapeHtml(d.reason)}</div>` : '';
  const timings = `<div class="tt-sub" style="margin-top:3px">Start: ${escapeHtml(d.start || 'Not available')}<br>Expected restoration: ${escapeHtml(d.end || 'Not available')}</div>`;
  const location = d.locationArea ? `Approximate area: ${d.locationArea}`
    : d.geocoded ? 'Mapped near the named substation' : 'Approximate district location';
  return `<div class="tt-title">${title}</div><div class="tt-sub">${sub}</div>${timings}${extra}<div class="tt-sub">${escapeHtml(location)}</div>`;
}

/**
 * Leaflet map view:
 *  - Theme-aware Esri World Gray Canvas basemap (dark/light, free, no API key)
 *    with a translucent district heat overlay on top.
 *  - District choropleth (GeoJSON) using the same green→red heat ramp.
 *  - Outage dots as static blue markers (capped at MAX_DOTS).
 *  - Native Leaflet pan/zoom + a custom "zoom to fit" control.
 */
function OdishaMap({ reports, dots = [], districts = [], showDots = true, showHeatmap = true }) {
  const wrapRef = useRef(null);
  const leafRef = useRef(null);
  const mapRef = useRef(null);
  const tileLayerRef = useRef(null);
  const districtLayerRef = useRef(null);
  const dotLayerRef = useRef(null);
  const resizeObserverRef = useRef(null);
  const fitButtonRef = useRef(null);
  const hasFittedRef = useRef(false);
  const [topo, setTopo] = useState(null);
  const [ready, setReady] = useState(false);
  const { theme } = useTheme();
  const dark = theme === 'dark';

  // Load the TopoJSON boundary file once.
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

  // Create the Leaflet map once (Leaflet is imported lazily so server-side
  // prerendering never evaluates its browser globals).
  useEffect(() => {
    if (mapRef.current) return;
    let cancelled = false;
    import('leaflet').then((mod) => {
      if (cancelled) return;
      const leaf = mod.default || mod;
      leafRef.current = leaf;
      const map = leaf.map(wrapRef.current, {
        zoomControl: false,
        scrollWheelZoom: true,
        minZoom: 6,
        maxZoom: 19,
      });

      // Dedicated pane for outage dots, above the choropleth overlay pane (400).
      // Leaflet's `bringToFront()` on a hovered district moves that polygon to the
      // end of the overlay SVG, which otherwise paints it over the dots; a separate
      // pane keeps the dots on top regardless of hover or layer re-adds.
      const dotPane = map.createPane('odishaDots');
      dotPane.style.zIndex = 450;
      dotPane.style.pointerEvents = 'auto';
      map.createPane('odishaReports').style.zIndex = 460;

      const zoomIcon = (path) => `<svg viewBox="0 0 20 20" width="18" height="18"
        aria-hidden="true" focusable="false" fill="none" stroke="currentColor"
        stroke-width="1.7" stroke-linecap="round"><path d="${path}"/></svg>`;
      leaf.control.zoom({
        position: 'topright',
        zoomInText: zoomIcon('M5 10h10M10 5v10'),
        zoomOutText: zoomIcon('M5 10h10'),
      }).addTo(map);

      const FitControl = leaf.Control.extend({
        options: { position: 'topright' },
        onAdd() {
          const container = leaf.DomUtil.create('div', 'odisha-fit-control');
          const button = leaf.DomUtil.create('button', 'odisha-fit-button', container);
          button.type = 'button';
          button.disabled = true;
          button.setAttribute('aria-label', 'Fit Odisha');
          button.title = 'Fit Odisha';
          button.innerHTML = `<svg viewBox="0 0 20 20" width="18" height="18"
            aria-hidden="true" focusable="false" fill="none" stroke="currentColor" stroke-width="1.7"
            stroke-linecap="round" stroke-linejoin="round">
            <path d="M7 3H3v4m10-4h4v4M3 13v4h4m10-4v4h-4"/>
            <circle cx="10" cy="10" r="2.1" fill="currentColor" stroke="none"/>
          </svg>`;
          fitButtonRef.current = button;
          leaf.DomEvent.disableClickPropagation(container);
          leaf.DomEvent.on(button, 'click', () => {
            const bounds = districtLayerRef.current?.getBounds();
            if (!bounds?.isValid()) return;
            const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
            map.fitBounds(bounds, { ...FIT_OPTIONS, ...(reduceMotion && { animate: false }) });
          });
          return container;
        },
      });
      new FitControl().addTo(map);

      // Keep the map sized to its container (card is responsive).
      if (typeof ResizeObserver !== 'undefined') {
        const ro = new ResizeObserver(() => map.invalidateSize());
        ro.observe(wrapRef.current);
        resizeObserverRef.current = ro;
      }

      mapRef.current = map;
      setReady(true);
    });
    return () => {
      cancelled = true;
      if (resizeObserverRef.current) {
        resizeObserverRef.current.disconnect();
        resizeObserverRef.current = null;
      }
      if (mapRef.current) {
        mapRef.current.remove();
        mapRef.current = null;
      }
    };
  }, []);

  // Theme-aware Esri canvas basemap; swap the tile layers when the theme changes.
  useEffect(() => {
    if (!ready) return;
    const map = mapRef.current;
    const leaf = leafRef.current;
    if (!map || !leaf) return;
    if (tileLayerRef.current) {
      map.removeLayer(tileLayerRef.current);
      tileLayerRef.current = null;
    }
    const urls = tileUrls(dark);
    const opts = {
      attribution:
        '&copy; Esri, HERE, Garmin, &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, and the GIS User Community',
      // Esri canvas services cap out at z16; keep the map zoomable by letting
      // Leaflet over-zoom the top tiles instead of showing blank ones.
      maxNativeZoom: 16,
      maxZoom: 16,
    };
    const group = leaf.layerGroup([
      leaf.tileLayer(urls.base, opts),
      leaf.tileLayer(urls.ref, { ...opts, attribution: '' }),
    ]);
    group.addTo(map);
    tileLayerRef.current = group;
  }, [ready, dark]);

  // District choropleth: same heat ramp as the old SVG renderer, but as a
  // Leaflet GeoJSON layer with hover highlight + tooltips.
  useEffect(() => {
    if (!ready || !topo) return;
    const map = mapRef.current;
    const leaf = leafRef.current;
    if (!map || !leaf) return;

    const counts = Object.fromEntries(districts.map((d) => [d.name.toLowerCase(), d.value]));
    const values = districts.map((d) => d.value);
    const min = values.length ? Math.min(...values) : 0;
    const max = values.length ? Math.max(...values) : 1;
    const mapPath = cssVar('--map-path');
    const mapPathHover = cssVar('--map-path-hover');
    const mapStroke = cssVar('--map-stroke');

    const key =
      Object.keys(topo.objects).find((k) => k.toLowerCase().includes('district')) ||
      Object.keys(topo.objects)[0];
    const raw = topojson.feature(topo, topo.objects[key]);
    const features = (raw.type === 'FeatureCollection' ? raw.features : [raw]).filter(
      (f) => (f.properties?.name || '').toLowerCase() !== 'odisha'
    );
    const collection = { type: 'FeatureCollection', features };

    const districtStyle = (feature) => {
      const v = counts[(feature.properties?.name || '').toLowerCase()];
      return {
        fillColor: v ? getColor(v, min, max, dark) : mapPath,
        fillOpacity: dark ? 0.4 : 0.45,
        color: mapStroke,
        weight: 0.6,
      };
    };

    if (districtLayerRef.current) map.removeLayer(districtLayerRef.current);

    const layer = leaf.geoJSON(collection, {
      style: districtStyle,
      onEachFeature(feature, lyr) {
        const name = feature.properties?.name || 'Unknown district';
        const v = counts[name.toLowerCase()];
        lyr.bindTooltip(districtTooltip(name, v), {
          direction: 'top',
          offset: [0, -6],
          className: 'odisha-tooltip',
        });
        lyr.on('mouseover', () => {
          lyr.setStyle({ fillColor: mapPathHover, fillOpacity: 0.9 });
          lyr.bringToFront();
        });
        lyr.on('mouseout', () => lyr.setStyle(districtStyle(feature)));
      },
    });

    const bounds = layer.getBounds();
    if (!hasFittedRef.current && bounds.isValid()) {
      hasFittedRef.current = true;
      map.fitBounds(bounds, { ...FIT_OPTIONS, animate: false });
    }
    if (showHeatmap) layer.addTo(map);
    districtLayerRef.current = layer;
    if (fitButtonRef.current) fitButtonRef.current.disabled = !bounds.isValid();
  }, [ready, topo, dark, districts]);

  // Toggle the district heat layer visibility. Dots live in a higher pane, so they
  // always render on top of the heat map regardless.
  useEffect(() => {
    const map = mapRef.current;
    const layer = districtLayerRef.current;
    if (!map || !layer) return;
    if (showHeatmap) {
      if (!map.hasLayer(layer)) map.addLayer(layer);
    } else if (map.hasLayer(layer)) {
      map.removeLayer(layer);
    }
  }, [ready, showHeatmap, dark, districts, topo]);

  // Outage dots: static blue markers (no animation), same data + tooltip
  // content as the old canvas renderer.
  useEffect(() => {
    if (!ready) return;
    const map = mapRef.current;
    const leaf = leafRef.current;
    if (!map || !leaf) return;

    if (dotLayerRef.current) {
      map.removeLayer(dotLayerRef.current);
      dotLayerRef.current = null;
    }
    if (!showDots) return;

    const dotColor = cssVar('--official-outage-dot');
    const group = leaf.layerGroup();
    for (const d of prepareDots(dots)) {
      if (!Number.isFinite(d.lat) || !Number.isFinite(d.lon)) continue;
      const m = leaf.circleMarker([d.lat, d.lon], {
        radius: 4.5,
        color: dotColor,
        weight: 1.2,
        fillColor: dotColor,
        fillOpacity: 1,
        pane: 'odishaDots',
      });
      m.bindTooltip(dotTooltip(d), {
        direction: 'top',
        offset: [0, -6],
        className: 'odisha-tooltip',
      });
      group.addLayer(m);
    }
    group.addTo(map);
    dotLayerRef.current = group;
  }, [ready, dots, dark, showDots]);

  useEffect(() => {
    const map = mapRef.current;
    const leaf = leafRef.current;
    if (!ready || !map || !leaf || !showDots || !reports?.length) return;

    const group = leaf.layerGroup();
    const fillColor = cssVar('--community-report-dot');
    const borderColor = cssVar('--bg-elev');
    for (const report of reports) {
      const { district, area, lat, lon, count, latestAt } = report;
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
      const latest = REPORT_TIME.format(new Date(latestAt));
      leaf.circleMarker([lat, lon], {
        radius: 6,
        color: borderColor,
        weight: 1.5,
        fillColor,
        fillOpacity: 1,
        pane: 'odishaReports',
        className: 'community-report-marker',
      }).bindTooltip(
        `<div class="tt-title">${escapeHtml(area ? area + ' · ' + district : district)}</div>
         <div class="tt-sub">${count} unverified community report${count === 1 ? '' : 's'} (last 24h)</div>
         <div class="tt-sub">Latest report: ${escapeHtml(latest)} IST</div>
         <div class="tt-sub">${area ? 'Approximate area location' : 'Approximate district location'}</div>`,
        { direction: 'auto', className: 'odisha-tooltip' }
      ).addTo(group);
    }
    group.addTo(map);
    return () => map.removeLayer(group);
  }, [ready, reports, dark, showDots]);

  return (
    <div className="map-frame">
      <div
        ref={wrapRef}
        style={{
          position: 'relative',
          zIndex: 0,
          width: '100%',
          height: 'var(--landing-map-height)',
          overflow: 'hidden',
          borderRadius: 'var(--radius-sm)',
          background: 'var(--map-bg)',
        }}
      />

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
              background: dark ? LEGEND_DARK : LEGEND_LIGHT,
            }}
          />
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>Many</span>
        </div>
        {Array.isArray(reports) && showDots && (
          <div className="map-legend-dots">
            <span><i className="map-dot-key official" aria-hidden="true" /> Official outages</span>
            <span><i className="map-dot-key community" aria-hidden="true" /> Community reports · 24h</span>
          </div>
        )}
      </div>
    </div>
  );
}

export default React.memo(OdishaMap);



