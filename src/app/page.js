'use client';

import React, { useRef, useEffect, useState, useCallback } from 'react';
import * as d3 from 'd3';
import * as topojson from 'topojson-client';
import Navigation from './navigation';
import { MAP_URL, DATA_URL } from './config';

// Color scale: green (few) → orange → red (many)
function getColor(value, min, max) {
  if (max === min) return '#22c55e';
  const t = (value - min) / (max - min);
  if (t < 0.33) {
    // green → yellow
    const s = t / 0.33;
    const r = Math.round(34 + s * (251 - 34));
    const g = Math.round(197 - s * (197 - 191));
    const b = Math.round(94 - s * 94);
    return `rgb(${r},${g},${b})`;
  } else if (t < 0.66) {
    // yellow → orange
    const s = (t - 0.33) / 0.33;
    const r = Math.round(251 + s * (249 - 251));
    const g = Math.round(191 - s * (191 - 115));
    const b = 0;
    return `rgb(${r},${g},${b})`;
  } else {
    // orange → red
    const s = (t - 0.66) / 0.34;
    const r = Math.round(249 - s * (249 - 220));
    const g = Math.round(115 - s * 115);
    const b = 0;
    return `rgb(${r},${g},${b})`;
  }
}

const MapComponent = () => {
  const mapRef = useRef(null);
  const [topoJsonData, setTopoJsonData] = useState(null);
  const [districtData, setDistrictData] = useState({}); // { districtName: count }
  const [stats, setStats] = useState({ total: 0, max: '', maxVal: 0 });
  const [tooltip, setTooltip] = useState({ visible: false, name: '', value: 0, x: 0, y: 0 });
  const [loading, setLoading] = useState(true);

  // Load TopoJSON and backend data in parallel
  useEffect(() => {
    const loadAll = async () => {
      try {
        const [topoRes, dataRes] = await Promise.all([
          fetch(MAP_URL),
          fetch(DATA_URL),
        ]);
        const topo = await topoRes.json();
        const data = await dataRes.json();

        const distMap = {};
        let total = 0;
        let maxVal = 0;
        let maxName = '';
        (data.districts || []).forEach(({ name, value }) => {
          distMap[name.toLowerCase()] = value;
          total += value;
          if (value > maxVal) { maxVal = value; maxName = name; }
        });

        setDistrictData(distMap);
        setStats({ total, max: maxName, maxVal });
        setTopoJsonData(topo);
        setLoading(false);
      } catch (err) {
        console.error('Error loading data:', err);
        setLoading(false);
      }
    };
    loadAll();
  }, []);

  useEffect(() => {
    if (!topoJsonData || !mapRef.current) return;

    const container = mapRef.current;
    const width = container.clientWidth;
    const height = container.clientHeight;

    // Clear previous SVG
    d3.select(container).selectAll('svg').remove();

    const svg = d3.select(container)
      .append('svg')
      .attr('width', width)
      .attr('height', height)
      .style('background-color', '#050510');

    // Defs: dots pattern + glow
    const defs = svg.append('defs');

    const makeDots = (id, r, spacing) => {
      const p = defs.append('pattern')
        .attr('id', id)
        .attr('patternUnits', 'userSpaceOnUse')
        .attr('width', spacing)
        .attr('height', spacing);
      p.append('rect').attr('width', spacing).attr('height', spacing).attr('fill', 'transparent');
      p.append('circle').attr('cx', spacing / 2).attr('cy', spacing / 2).attr('r', r)
        .attr('fill', 'rgba(255,255,255,0.7)').attr('filter', 'url(#glow)');
      return p;
    };

    makeDots('dots', 1.2, 5);
    makeDots('hover-dots', 1.8, 4);

    const filter = defs.append('filter').attr('id', 'glow');
    filter.append('feGaussianBlur').attr('stdDeviation', '1').attr('result', 'coloredBlur');
    const feMerge = filter.append('feMerge');
    feMerge.append('feMergeNode').attr('in', 'coloredBlur');
    feMerge.append('feMergeNode').attr('in', 'SourceGraphic');

    // Collect all features
    const allFeatures = [];
    for (const key in topoJsonData.objects) {
      const geojson = topojson.feature(topoJsonData, topoJsonData.objects[key]);
      geojson.features.forEach(f => allFeatures.push(f));
    }

    // Fit projection
    const projection = d3.geoMercator().fitSize([width, height - 52], {
      type: 'FeatureCollection',
      features: allFeatures,
    });
    const path = d3.geoPath().projection(projection);

    // Outage values for color scale
    const values = Object.values(districtData);
    const minVal = values.length ? Math.min(...values) : 0;
    const maxValNum = values.length ? Math.max(...values) : 1;

    function getDistrictName(d) {
      const p = d.properties || {};
      return p.NAME_2 || p.name || p.Name || p.DIST_NAME || p.Dist_Name || p.district || '';
    }

    function getOutages(d) {
      const name = getDistrictName(d).toLowerCase();
      return districtData[name] || 0;
    }

    // Draw districts
    const paths = svg.selectAll('path.district')
      .data(allFeatures)
      .join('path')
      .attr('class', 'district')
      .attr('d', path)
      .attr('fill', d => {
        const v = getOutages(d);
        if (v === 0) return 'url(#dots)';
        return getColor(v, minVal, maxValNum);
      })
      .attr('opacity', 0.85)
      .attr('stroke', 'rgba(255,255,255,0.15)')
      .attr('stroke-width', 0.5)
      .style('cursor', 'pointer');

    paths
      .on('mouseover', function (event, d) {
        const name = getDistrictName(d);
        const value = getOutages(d);
        d3.select(this)
          .attr('stroke', 'rgba(255,255,255,0.8)')
          .attr('stroke-width', 1.5)
          .attr('opacity', 1);
        setTooltip({ visible: true, name, value, x: event.clientX, y: event.clientY });
      })
      .on('mousemove', function (event) {
        setTooltip(prev => ({ ...prev, x: event.clientX, y: event.clientY }));
      })
      .on('mouseout', function (event, d) {
        d3.select(this)
          .attr('stroke', 'rgba(255,255,255,0.15)')
          .attr('stroke-width', 0.5)
          .attr('opacity', 0.85);
        setTooltip(prev => ({ ...prev, visible: false }));
      });

  }, [topoJsonData, districtData]);

  return (
    <div style={{ width: '100vw', height: '100vh', overflow: 'hidden', position: 'relative', background: '#050510' }}>
      <Navigation />

      {/* Map area */}
      <div
        ref={mapRef}
        style={{ width: '100%', height: '100%', position: 'absolute', top: 0, left: 0 }}
      />

      {/* Loading overlay */}
      {loading && (
        <div style={{
          position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
          background: 'rgba(5,5,16,0.9)', zIndex: 100,
        }}>
          <div style={{ textAlign: 'center', color: '#ff6432' }}>
            <div style={{ fontSize: 32, marginBottom: 12 }}>⚡</div>
            <div style={{ fontSize: 14, letterSpacing: '0.1em' }}>Loading outage data…</div>
          </div>
        </div>
      )}

      {/* Title */}
      <div style={{
        position: 'absolute', top: 52, left: 0, right: 0,
        display: 'flex', justifyContent: 'center', alignItems: 'center',
        padding: '8px 16px',
        background: 'linear-gradient(180deg,rgba(5,5,16,0.95) 0%,transparent 100%)',
        pointerEvents: 'none',
        zIndex: 500,
      }}>
        <h1 style={{
          margin: 0, color: '#fff', fontSize: '18px', fontWeight: 700,
          letterSpacing: '0.06em', textShadow: '0 0 20px rgba(255,100,50,0.5)',
        }}>
          Odisha Live Power Outage Dashboard
        </h1>
      </div>

      {/* Stats bar */}
      {!loading && stats.total > 0 && (
        <div style={{
          position: 'absolute', bottom: 0, left: 0, right: 0,
          display: 'flex', justifyContent: 'center', gap: '3rem',
          padding: '10px 20px',
          background: 'linear-gradient(0deg,rgba(5,5,16,0.97) 0%,transparent 100%)',
          zIndex: 500,
        }}>
          <div style={{ textAlign: 'center' }}>
            <div style={{ color: '#ff6432', fontSize: '20px', fontWeight: 700 }}>{stats.total}</div>
            <div style={{ color: 'rgba(255,255,255,0.5)', fontSize: '11px', letterSpacing: '0.1em' }}>TOTAL OUTAGES</div>
          </div>
          <div style={{ textAlign: 'center' }}>
            <div style={{ color: '#ff6432', fontSize: '20px', fontWeight: 700 }}>{stats.max}</div>
            <div style={{ color: 'rgba(255,255,255,0.5)', fontSize: '11px', letterSpacing: '0.1em' }}>MOST AFFECTED</div>
          </div>
          <div style={{ textAlign: 'center' }}>
            <div style={{ color: '#ff6432', fontSize: '20px', fontWeight: 700 }}>{stats.maxVal}</div>
            <div style={{ color: 'rgba(255,255,255,0.5)', fontSize: '11px', letterSpacing: '0.1em' }}>OUTAGES IN {stats.max?.toUpperCase()}</div>
          </div>
        </div>
      )}

      {/* Legend */}
      {!loading && (
        <div style={{
          position: 'absolute', bottom: 60, right: 20,
          background: 'rgba(10,10,20,0.85)', border: '1px solid rgba(255,255,255,0.1)',
          borderRadius: 8, padding: '10px 14px', zIndex: 500,
        }}>
          <div style={{ color: 'rgba(255,255,255,0.6)', fontSize: '10px', letterSpacing: '0.1em', marginBottom: 6 }}>OUTAGES</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ color: 'rgba(255,255,255,0.5)', fontSize: '10px' }}>Few</span>
            <div style={{
              width: 80, height: 10, borderRadius: 4,
              background: 'linear-gradient(90deg,#22c55e,#fbbf24,#dc2626)',
            }} />
            <span style={{ color: 'rgba(255,255,255,0.5)', fontSize: '10px' }}>Many</span>
          </div>
        </div>
      )}

      {/* Tooltip */}
      {tooltip.visible && (
        <div style={{
          position: 'fixed',
          left: tooltip.x + 12,
          top: tooltip.y - 10,
          background: 'rgba(10,10,20,0.95)',
          border: '1px solid rgba(255,100,50,0.4)',
          borderRadius: 6,
          padding: '6px 12px',
          pointerEvents: 'none',
          zIndex: 3000,
          color: '#fff',
          fontSize: '13px',
          boxShadow: '0 4px 16px rgba(0,0,0,0.5)',
        }}>
          <div style={{ fontWeight: 600 }}>{tooltip.name || '(Unknown district)'}</div>
          <div style={{ color: '#ff6432', fontSize: '12px', marginTop: 2 }}>
            {tooltip.value} outage{tooltip.value !== 1 ? 's' : ''}
          </div>
        </div>
      )}
    </div>
  );
};

export default MapComponent;