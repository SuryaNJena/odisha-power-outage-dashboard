import { readFileSync, writeFileSync } from 'node:fs';
import { geoBounds, geoCentroid, geoContains } from 'd3-geo';
import { feature } from 'topojson-client';

const read = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), 'utf8'));
const normalize = (text) => text.toLowerCase().replace(/[^\p{L}\p{N}]/gu, '');
const topo = read('../public/Odisha.json');
const districts = feature(topo, topo.objects.OSMB_Odisha_Districts_Rewind).features
  .filter((district) => district.properties.name !== 'Odisha');
const byId = new Map(districts.map((district) => [district.properties.osm_id, district]));
const byName = new Map(districts.map((district) => [district.properties.name, district]));
const { districts: aliases } = read('../data/mappings/district_aliases.json');
const names = Object.fromEntries(Object.entries(aliases).flatMap(([name, variants]) =>
  [name, ...variants].map((variant) => [normalize(variant), name])
));

function interiorPoint(area, district) {
  const inside = (point) => geoContains(area, point) && geoContains(district, point);
  const center = geoCentroid(area);
  if (inside(center)) return center;
  // A centroid can be outside a concave or multi-part area. Find an interior
  // point close to the center instead of placing a marker in a neighbor.
  const [[west, south], [east, north]] = geoBounds(area);
  const candidates = [];
  for (let x = 0; x < 12; x++) {
    for (let y = 0; y < 12; y++) {
      const lon = west + (east - west) * (x + 0.5) / 12;
      const lat = south + (north - south) * (y + 0.5) / 12;
      if (inside([lon, lat])) candidates.push([lon, lat]);
    }
  }
  const distance = ([lon, lat]) => (lon - center[0]) ** 2 + (lat - center[1]) ** 2;
  return candidates.sort((a, b) => distance(a) - distance(b))[0];
}

const places = new Map();
const disputed = new Set();
function add(place) {
  const key = place.district + '|' + normalize(place.area);
  if (disputed.has(key)) return;
  const previous = places.get(key);
  if (previous && Math.hypot(previous.lat - place.lat, previous.lon - place.lon) > 0.02) {
    places.delete(key);
    disputed.add(key);
  } else if (!previous) {
    places.set(key, { ...place, lat: +place.lat.toFixed(5), lon: +place.lon.toFixed(5) });
  }
}

for (const area of feature(topo, topo.objects.OSMB_Odisha_Rewind).features) {
  const parent = (area.properties.parents || []).map(id => byId.get(id)).find(Boolean);
  const label = (area.properties.name_en || area.properties.name || '').replace(/\s+\(M\.Corp\.\)$/i, '');
  if (!parent || !label) continue;
  const point = interiorPoint(area, parent);
  if (point) add({ area: label, district: parent.properties.name, lat: point[1], lon: point[0] });
}

for (const [key, point] of Object.entries(read('../data/mappings/geocode_cache.json'))) {
  const [name, districtName] = key.split('|');
  const district = names[normalize(districtName)] || districtName;
  const parent = byName.get(district);
  const cleanName = name.replace(/\s*\(GSS\)$/i, '');
  const display = normalize(point.display || '');
  if (!parent || !Number.isFinite(point.lat) || !Number.isFinite(point.lon) ||
      !display.includes(normalize(cleanName)) || !geoContains(parent, [point.lon, point.lat])) continue;
  const area = cleanName.toLowerCase().replace(/\b\p{L}/gu, (letter) => letter.toUpperCase());
  const placeKey = district + '|' + normalize(area);
  if (!places.has(placeKey)) add({ area, district, lat: point.lat, lon: point.lon });
}

for (const place of read('../data/mappings/bhubaneswar_areas.json')) {
  const parent = byName.get(place.district);
  if (!parent || !geoContains(parent, [place.lon, place.lat])) throw Error('Reference point outside district: ' + place.area);
  places.set(place.district + '|' + normalize(place.area), place);
}
const catalog = [...places.values()].sort((a, b) => a.district.localeCompare(b.district) || a.area.localeCompare(b.area));
const data = '{"districtNames":' + JSON.stringify(names) + ',"areas":[\n' +
  catalog.map(place => JSON.stringify(place)).join(',\n') + '\n]}\n';
writeFileSync(new URL('../backend/report_areas.json', import.meta.url), data);
console.log('Generated area reference for ' + catalog.length + ' places');
