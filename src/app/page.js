'use client';

import React, { useRef, useEffect, useState } from 'react';
import * as d3 from 'd3';
import * as topojson from 'topojson-client';
import Navigation from './navigation';

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
    // don't render without data or ref
    if (!topoJsonData || !mapRef.current) return;

    // occupy the full screen
    const width = mapRef.current.clientWidth;
    const height = mapRef.current.clientHeight;

    // Create a black background
    const svg = d3.select(mapRef.current).selectAll('svg').data([null]).join('svg')
      .attr('width', width)
      .attr('height', height)
      .style('background-color', 'black');
    
    // Create white dots pattern with higher density
    var pattern = svg.append('defs')
      .append('pattern')
        .attr('id', 'dots')
        .attr('patternUnits', 'userSpaceOnUse')
        .attr('width', 6)  
        .attr('height', 6);
    
    // Black background for the pattern
    pattern.append('rect')
      .attr('x', 0)
      .attr('y', 0)
      .attr('width', 12)
      .attr('height', 12)
      .attr('fill', 'black');
    
    // White dots with glow effect
    pattern.append('circle')
      .attr('cx', 6)
      .attr('cy', 6)
      .attr('r', 1.5)  
      .attr('fill', 'white')
      .attr('filter', 'url(#glow)');

    // Create hover pattern with higher density
    var hoverPattern = svg.append('defs')
      .append('pattern')
        .attr('id', 'hover-dots')
        .attr('patternUnits', 'userSpaceOnUse')
        .attr('width', 6)  
        .attr('height', 6);

    hoverPattern.append('rect')
      .attr('x', 0)
      .attr('y', 0)
      .attr('width', 12)
      .attr('height', 12)
      .attr('fill', 'black');

    hoverPattern.append('circle')
      .attr('cx', 6)
      .attr('cy', 6)
      .attr('r', 2.5)  
      .attr('fill', 'white')
      .attr('filter', 'url(#glow)');
    
    // Add glow filter
    svg.append('defs')
      .append('filter')
      .attr('id', 'glow')
      .append('feGaussianBlur')
      .attr('stdDeviation', '1')
      .attr('result', 'coloredBlur');
    
    svg.select('#glow')
      .append('feMerge')
      .selectAll('feMergeNode')
      .data(['coloredBlur', 'SourceGraphic'])
      .enter()
      .append('feMergeNode')
      .attr('in', function(d) { return d; });

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
      .style('overflow-x', 'auto') 
      .style('white-space', 'nowrap') 
      .style('background-color', 'rgba(0, 0, 0, 0.7)')
      .style('color', 'white')
      .style('text-align', 'center')
      .style('font-size', '20px')
      .style('font-weight', 'bold')
      .style('z-index', 1000);

    nameBarRef.current = nameBar.node(); 

    // scale the map to fit screen so find the min and max boundary
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
          .attr('fill', 'url(#dots)')
          .attr('opacity', 0.8)
          .on('mouseover', handleMouseOver)
          .on('mouseout', handleMouseOut)
          .on('click', handleClick);
      }

      svg.selectAll('path')
        .each(function(d) {
          const isSelected = selectedFeatures.includes(d);
           d3.select(this)
            .attr('fill', isSelected ? 'red' : 'url(#dots)');
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

  // Update name bar whenever selectedFeatures changes
  useEffect(() => {
    updateNameBar();
  }, [selectedFeatures]);

  const handleMouseOver = (event, d) => {
    d3.select(event.target)
      .transition()
      .duration(300)
      .attr('fill', 'url(#hover-dots)')
      .attr('stroke', 'white')
      .attr('stroke-width', 2)
      .attr('stroke-opacity', 0.8)
      .attr('opacity', 1);
    
    // Update name bar with hover district name
    if (!nameBarRef.current) return;
    nameBarRef.current.style.visibility = 'visible';
    const name = d?.properties?.name || d?.properties?.Name || d?.properties?.DIST_NAME || d?.properties?.Dist_Name;
    nameBarRef.current.textContent = name;
    console.log("Hovering over:", name);
  };

  const handleMouseOut = (event, d) => {
    const isSelected = selectedFeatures.includes(d);
    d3.select(event.target)
      .transition()
      .duration(300)
      .attr('fill', 'url(#dots)')
      .attr('stroke', isSelected ? 'red' : 'none')
      .attr('stroke-width', isSelected ? 3 : 0)
      .attr('opacity', isSelected ? 1 : 0.8);
    
    // If no district is selected, clear the name bar
    if (!isSelected && selectedFeatures.length === 0) {
      nameBarRef.current.textContent = "";
      nameBarRef.current.style.visibility = 'hidden';
    }
    console.log("Mouse out from:", d?.properties?.name || d?.properties?.Name || d?.properties?.DIST_NAME || d?.properties?.Dist_Name);
  };

  const handleClick = (event, d) => {
    const isSelected = selectedFeatures.includes(d);
    if (isSelected) {
      setSelectedFeatures(selectedFeatures.filter(feature => feature !== d));
    } else {
      setSelectedFeatures([d]);
    }
  };

  const updateNameBar = () => {
    if (!nameBarRef.current) return;
    if (selectedFeatures.length === 0) {
      nameBarRef.current.textContent = "";
      return;
    }
    nameBarRef.current.style.visibility = 'visible';
    const names = selectedFeatures.map(feature => {
      return feature?.properties?.name || feature?.properties?.Name || feature?.properties?.DIST_NAME || feature?.properties?.Dist_Name;
    });
    nameBarRef.current.textContent = names.join(", ");
    console.log("Selected features:", selectedFeatures);
    console.log(names.join(", "));
  };

  return (
    <div
      style={{
        width: '100vw',
        height: '100vh',
        overflow: 'hidden',
        position: 'relative',
      }}
    >
      <div ref={mapRef} style={{ width: '100%', height: '100%'}} />
      <div ref={nameBarRef} />
    </div>
  );
};

export default MapComponent;

export { Navigation };