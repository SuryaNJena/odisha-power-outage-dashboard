'use client';

import React, { useEffect, useRef } from 'react';
import * as echarts from 'echarts';
import 'echarts/map/js/china';

const ChloroplethMap = () => {
  const chartRef = useRef(null);
  const mapRef = useRef(null);

  useEffect(() => {
    // Initialize ECharts
    const chart = echarts.init(chartRef.current);

    // Load Odisha map data
    fetch('/Odisha.json')
      .then(response => response.json())
      .then(data => {
        echarts.registerMap('Odisha', data);
        
        // Load data from backend
        fetch('http://localhost:3001/api/mapdata')
          .then(response => response.json())
          .then(data => {
            const option = {
              title: {
                text: 'Odisha Power Outage Distribution',
                subtext: 'Data visualization using ECharts',
                left: 'center'
              },
              tooltip: {
                trigger: 'item',
                formatter: function (params) {
                  return `${params.name}: ${params.value}%`;
                }
              },
              visualMap: {
                min: 0,
                max: 100,
                left: 'left',
                top: 'bottom',
                text: ['High', 'Low'],
                calculable: true,
                inRange: {
                  color: ['#e0f3f8', '#006699']
                }
              },
              series: [
                {
                  name: 'Power Outage Rate',
                  type: 'map',
                  mapType: 'Odisha',
                  roam: true,
                  label: {
                    show: true,
                    color: '#fff'
                  },
                  data: data.districts,
                  itemStyle: {
                    areaColor: '#323c48',
                    borderColor: '#404a59'
                  }
                }
              ]
            };

            chart.setOption(option);
          });
      });

    // Clean up
    return () => {
      chart.dispose();
    };
  }, []);

  return (
    <div
      style={{
        width: '100vw',
        height: '100vh',
        overflow: 'hidden',
        position: 'relative',
        backgroundColor: '#1b1b1b'
      }}
    >
      <div
        ref={chartRef}
        style={{
          width: '100%',
          height: '100%',
          position: 'absolute',
          top: 0,
          left: 0
        }}
      />
    </div>
  );
};

export default ChloroplethMap;
