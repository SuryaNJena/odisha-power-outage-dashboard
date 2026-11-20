import { geoArea, geoContains } from 'd3-geo';
import { feature } from 'topojson-client';
import { MAP_URL } from './config';
import aliases from '../../data/mappings/district_aliases.json';

const normalize = (value) => value.toLowerCase().replace(/[^\p{L}\p{N}]/gu, '');
const districtNames = new Map(
  Object.entries(aliases.districts).flatMap(([name, variants]) =>
    [name, ...variants].map((variant) => [normalize(variant), name])
  )
);
const canonical = (name) => districtNames.get(normalize(name)) || name;
const areaName = ({ properties }) => (properties.name_en || properties.name || '').replace(/\s+\(M\.Corp\.\)$/i, '');

export function matchDistrict(names, district) {
  return names.find((name) => name === district) ||
    names.find((name) => canonical(name) === canonical(district)) || '';
}

let boundaryRequest;

function boundaries() {
  boundaryRequest ??= fetch(MAP_URL).then(async (res) => {
    if (!res.ok) throw new Error('Could not load areas');
    const { objects, ...topology } = await res.json();
    const districts = feature(topology, objects.OSMB_Odisha_Districts_Rewind).features
      .filter((district) => district.properties.name !== 'Odisha');
    const districtById = new Map(districts.map((district) => [district.properties.osm_id, district.properties.name]));
    const areas = feature(topology, objects.OSMB_Odisha_Rewind).features
      .map((geometry) => ({
        geometry,
        area: areaName(geometry),
        district: (geometry.properties.parents || []).map((id) => districtById.get(id)).find(Boolean),
        size: geoArea(geometry),
      }))
      .filter(({ area }) => area)
      .sort((a, b) => a.size - b.size);
    return { districts, areas };
  }).catch((err) => {
    boundaryRequest = undefined;
    throw err;
  });
  return boundaryRequest;
}

export async function areaAtLocation(latitude, longitude) {
  const { districts, areas } = await boundaries();
  const point = [longitude, latitude];
  const district = districts.find((candidate) => geoContains(candidate, point))?.properties.name;
  if (!district) return null;
  const nearestArea = areas.find(({ geometry }) => geoContains(geometry, point));
  return { district, area: nearestArea?.area || '' };
}

export async function findAreas(text, preferredDistrict) {
  const [{ areas, districts }, { default: landmarks }] = await Promise.all([
    boundaries(),
    import('../../data/mappings/geocode_cache.json'),
  ]);
  const query = normalize(text);
  if (!query) return [];

  const places = [
    ...areas.filter(({ district }) => district).map(({ area, district }) => ({ area, district })),
    ...districts.map(({ properties: { name } }) => ({ area: name, district: name })),
    ...Object.entries(landmarks).filter(([, point]) => Number.isFinite(point.lat)).map(([key]) => {
      const [name, district] = key.split('|');
      const area = name.replace(/\s*\(GSS\)$/i, '').toLowerCase().replace(/\b\p{L}/gu, (letter) => letter.toUpperCase());
      return { area, district: canonical(district) };
    }),
  ];

  const scored = places.flatMap((place) => {
    const key = normalize(place.area);
    const aliasMatch = place.area === place.district && districtNames.get(query) === place.district;
    const score = key === query || aliasMatch ? 0 : key.startsWith(query) ? 1 : key.includes(query) ? 2 : -1;
    return score < 0 ? [] : [{ ...place, score }];
  });
  scored.sort((a, b) => a.score - b.score ||
    Number(canonical(b.district) === canonical(preferredDistrict)) - Number(canonical(a.district) === canonical(preferredDistrict)) ||
    a.area.localeCompare(b.area));
  const distinct = new Map();
  for (const { area, district } of scored) {
    const key = normalize(area) + '|' + district;
    if (!distinct.has(key)) distinct.set(key, { area, district });
  }
  return [...distinct.values()].slice(0, 5);
}
