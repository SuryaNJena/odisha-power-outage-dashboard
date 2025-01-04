'use client';

import React, { useRef, useEffect, useState } from 'react';
import * as d3 from 'd3';
import * as topojson from 'topojson-client';

const MapComponent = () => {
  const mapRef = useRef(null);
  const nameBarRef = useRef(null);

  const [topoJsonData, setTopoJsonData] = useState(null);
  const [selectedFeatures, setSelectedFeatures] = useState([]);

  useEffect(() => {
    const loadTopoJson = async () => {
      try {
        const response = await fetch("/Odisha.json");
        if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
        const data = await response.json();
        console.log("Fetched TopoJSON data:", data);
        setTopoJsonData(data);
      } catch (error) {
        console.error("Error fetching or parsing topojson data:", error);
      }
    };
    loadTopoJson();
  }, []);

  useEffect(() => {
    if (!topoJsonData || !mapRef.current) return;

    const width = mapRef.current.clientWidth;
    const height = mapRef.current.clientHeight;

    const svg = d3.select(mapRef.current).selectAll('svg').data([null]).join('svg')
      .attr('width', width)
      .attr('height', height);

    d3.select(mapRef.current).selectAll('.title-bar').data([null]).join('div')
      .attr('class', 'title-bar')
      .style('position', 'absolute')
      .style('top', '0')
      .style('width', '100%')
      .style('display', 'flex')
      .style('justify-content', 'center')
      .style('padding', '10px')
      .style('background-color', 'rgba(0, 0, 0, 0.7)')
      .style('color', 'white')
      .style('text-align', 'center')
      .style('font-size', '24px')
      .style('font-weight', 'bold')
      .text("Odisha Live Power Outage Dashboard");

    const nameBar = d3.select(mapRef.current).selectAll('.name-bar').data([null]).join('div')
      .attr('class', 'name-bar')
      .style('position', 'absolute')
      .style('bottom', '0')
      .style('width', '100%')
      .style('display', 'flex')
      .style('justify-content', 'center')
      .style('padding', '10px')
       .style('overflow-x', 'auto') // Allows horizontal scrolling
      .style('white-space', 'nowrap') // Keeps elements in a single line
      .style('background-color', 'rgba(0, 0, 0, 0.7)')
      .style('color', 'white')
      .style('text-align', 'center')
      .style('font-size', '20px')
      .style('font-weight', 'bold')
      .style('visibility', 'hidden');

    nameBarRef.current = nameBar.node(); // Set the ref

    let overallBounds;
    for (const key in topoJsonData.objects) {
      const geojson = topojson.feature(topoJsonData, topoJsonData.objects[key]);
      const bounds = d3.geoBounds(geojson);
      overallBounds = overallBounds ? [
        [Math.min(overallBounds[0][0], bounds[0][0]), Math.min(overallBounds[0][1], bounds[0][1])],
        [Math.max(overallBounds[1][0], bounds[1][0]), Math.max(overallBounds[1][1], bounds[1][1])]
      ] : bounds;
    }

    if (overallBounds) {
      const [[minLon, minLat], [maxLon, maxLat]] = overallBounds;
      const geoWidth = maxLon - minLon;
      const geoHeight = maxLat - minLat;
      const scaleX = width / geoWidth;
      const scaleY = height / geoHeight;
      const scale = Math.min(scaleX, scaleY) * 0.80;
      const translateX = (width - geoWidth * scale) / 2;
      const translateY = (height - geoHeight * scale) / 2;

      for (const objectName in topoJsonData.objects) {
        const geojsonData = topojson.feature(topoJsonData, topoJsonData.objects[objectName]);
        svg.selectAll(`.layer-${objectName.replace(/[^a-zA-Z0-9]/g, '_')}`)
          .data(geojsonData.features)
          .join('path')
          .attr('class', `layer-${objectName.replace(/[^a-zA-Z0-9]/g, '_')}`)
          .attr('d', (d) => d3.line()(d.geometry.coordinates[0].map(coord => [
            (coord[0] - minLon) * scale + translateX,
            (maxLat - coord[1]) * scale + translateY
          ])))
          .attr('fill', 'black')
          .attr('stroke', 'lightgoldenrodyellow')
          .attr('stroke-width', 1)
          .on('mouseover', handleMouseOver)
          .on('mouseout', handleMouseOut)
          .on('click', handleClick);
      }

      svg.selectAll('path')
        .each(function(d) {
          const isSelected = selectedFeatures.includes(d);
           d3.select(this)
            .attr('stroke', isSelected ? 'red' : 'lightgoldenrodyellow')
            .attr('stroke-width', isSelected ? 3 : 1);
        })
          .sort((a, b) => {
          const aSelected = selectedFeatures.includes(a);
          const bSelected = selectedFeatures.includes(b);
          if (aSelected) return 1;
          if (bSelected) return -1;
          return -1;
        });
    }
  }, [topoJsonData, selectedFeatures]);

  const handleMouseOver = (event, d) => {
    d3.select(event.target)
      .attr('stroke', 'orange')
      .attr('stroke-width', 3);
  };

  const handleMouseOut = (event, d) => {
    const isSelected = selectedFeatures.includes(d);
    d3.select(event.target)
      .attr('stroke', isSelected ? 'red' : 'lightgoldenrodyellow')
      .attr('stroke-width', isSelected ? 3 : 1);
  };

  const handleClick = (event, d) => {
    const isSelected = selectedFeatures.includes(d);
    if (isSelected) {
      setSelectedFeatures(selectedFeatures.filter(feature => feature !== d));
    } else {
      setSelectedFeatures([...selectedFeatures, d]);
    }
    updateNameBar();
  };

  const updateNameBar = () => {
    if (!nameBarRef.current) return;
    if (selectedFeatures.length === 0) {
      nameBarRef.current.textContent = "";
      nameBarRef.current.style.visibility = 'hidden';
      return;
    }
    nameBarRef.current.style.visibility = 'visible';
    const names = selectedFeatures.map(feature => {
      return feature?.properties?.name || feature?.properties?.Name || feature?.properties?.DIST_NAME || feature?.properties?.Dist_Name;
    });
    nameBarRef.current.textContent = names.join(", ");
  };

  return (
    <div
      ref={mapRef}
      style={{
        width: '100vw',
        height: '100vh',
        overflow: 'hidden',
        position: 'relative',
      }}
    />
  );
};

export default MapComponent;