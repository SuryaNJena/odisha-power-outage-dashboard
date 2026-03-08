'use client';

import React, { useEffect, useRef, useState } from 'react';
import * as echarts from 'echarts';
import Navigation from '../navigation';
import { MAP_URL, DATA_URL } from '../config';

// OSM district name → backend district name aliases
const OSM_ALIAS = {
  'baleshwar': 'balasore',
  'balangir': 'bolangir',
  'jagatsinghapur': 'jagatsinghpur',
  'nabarangapur': 'nabarangpur',
  'kendujhar': 'keonjhar',
};

const ChloroplethMap = () => {
  const chartRef = useRef(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let chart = null;
    let cancelled = false;

    const init = async () => {
      try {
        const [topoRes, dataRes] = await Promise.all([
          fetch(MAP_URL),
          fetch(DATA_URL),
        ]);
        const topoData = await topoRes.json();
        const rawData = await dataRes.json();

        if (cancelled) return;

        const topojson = await import('topojson-client');

        // Use only the Districts object (not sub-district level)
        const districtKey = Object.keys(topoData.objects).find(k =>
          k.toLowerCase().includes('district')
        ) || Object.keys(topoData.objects)[0];

        const geojsonRaw = topojson.feature(topoData, topoData.objects[districtKey]);
        const features = (geojsonRaw.type === 'FeatureCollection'
          ? geojsonRaw.features
          : [geojsonRaw]
        ).filter(f => {
          const n = (f.properties?.name || '').toLowerCase();
          return n && n !== 'odisha'; // filter the outer boundary
        });

        // Build outage lookup: lowercase name → count
        const outageMap = {};
        (rawData.districts || []).forEach(({ name, value }) => {
          outageMap[name.toLowerCase()] = value;
        });

        // Map features with normalized names
        const geojsonMap = {
          type: 'FeatureCollection',
          features: features.map(f => ({
            ...f,
            properties: {
              ...f.properties,
              name: f.properties?.name || 'Unknown',
            },
          })),
        };

        echarts.registerMap('Odisha', geojsonMap);

        // Build chart data matching OSM names → backend names
        const chartData = features.map(f => {
          const osmName = (f.properties?.name || '').toLowerCase();
          const backendName = OSM_ALIAS[osmName] || osmName;
          const value = outageMap[backendName] ?? 0;
          return { name: f.properties?.name || '', value };
        });

        const maxValue = Math.max(...chartData.map(d => d.value), 1);

        if (cancelled) return;

        chart = echarts.init(chartRef.current, null, { renderer: 'canvas' });

        const option = {
          backgroundColor: '#050510',
          title: {
            text: 'Power Outage Heat Map',
            subtext: 'Odisha — District-level outage distribution',
            left: 'center',
            top: 16,
            textStyle: { color: '#ffffff', fontSize: 18, fontWeight: 700 },
            subtextStyle: { color: 'rgba(255,255,255,0.45)', fontSize: 12 },
          },
          tooltip: {
            trigger: 'item',
            backgroundColor: 'rgba(10,10,20,0.95)',
            borderColor: 'rgba(255,100,50,0.4)',
            borderWidth: 1,
            textStyle: { color: '#fff', fontSize: 13 },
            formatter: params => {
              const v = params.value ?? 0;
              return `<b>${params.name}</b><br/>🔴 ${v} outage${v !== 1 ? 's' : ''}`;
            },
          },
          visualMap: {
            min: 0,
            max: maxValue,
            left: 20,
            bottom: 60,
            text: ['High', 'Low'],
            calculable: true,
            inRange: {
              color: ['#1a2e1a', '#22c55e', '#fbbf24', '#f97316', '#dc2626'],
            },
            textStyle: { color: 'rgba(255,255,255,0.6)', fontSize: 11 },
            handleStyle: { color: '#ff6432' },
          },
          series: [
            {
              name: 'Power Outages',
              type: 'map',
              map: 'Odisha',
              roam: true,
              scaleLimit: { min: 0.5, max: 6 },
              data: chartData,
              emphasis: {
                label: {
                  show: true, color: '#fff', fontSize: 11, fontWeight: 600,
                },
                itemStyle: {
                  areaColor: 'rgba(255,100,50,0.5)',
                  borderColor: '#ff6432', borderWidth: 1.5,
                },
              },
              label: { show: false },
              itemStyle: {
                areaColor: '#1a1a2e',
                borderColor: 'rgba(255,255,255,0.15)',
                borderWidth: 0.8,
              },
            },
          ],
        };

        chart.setOption(option);
        setLoading(false);

        const handleResize = () => chart?.resize();
        window.addEventListener('resize', handleResize);
        return () => window.removeEventListener('resize', handleResize);

      } catch (err) {
        console.error('ChloroplethMap error:', err);
        if (!cancelled) {
          setError(err.message);
          setLoading(false);
        }
      }
    };

    init();

    return () => {
      cancelled = true;
      chart?.dispose();
    };
  }, []);

  return (
    <div style={{
      width: '100vw', height: '100vh', overflow: 'hidden',
      position: 'relative', background: '#050510',
    }}>
      <Navigation />

      <div
        ref={chartRef}
        style={{ width: '100%', height: '100%', position: 'absolute', top: 0, left: 0 }}
      />

      {loading && (
        <div style={{
          position: 'absolute', inset: 0,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          background: 'rgba(5,5,16,0.9)', zIndex: 100,
        }}>
          <div style={{ textAlign: 'center', color: '#ff6432' }}>
            <div style={{ fontSize: 32, marginBottom: 12 }}>🔥</div>
            <div style={{ fontSize: 14, letterSpacing: '0.1em' }}>Building heat map…</div>
          </div>
        </div>
      )}

      {error && (
        <div style={{
          position: 'absolute', bottom: 20, left: '50%', transform: 'translateX(-50%)',
          background: 'rgba(220,38,38,0.15)', border: '1px solid rgba(220,38,38,0.4)',
          color: '#fca5a5', borderRadius: 6, padding: '8px 16px', fontSize: 12, zIndex: 200,
        }}>
          ⚠️ {error}
        </div>
      )}
    </div>
  );
};

export default ChloroplethMap;
