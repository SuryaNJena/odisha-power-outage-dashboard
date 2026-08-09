/**
 * compute_centroids.mjs
 * Extracts per-district centroids from the TopoJSON map and writes them to
 * data/mappings/district_centroids.json. Used as a fallback location when a
 * substation cannot be geocoded via the OSM Nominatim API.
 *
 * Run from repo root:  node scripts/compute_centroids.mjs
 */
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { feature } from 'topojson-client';
import { geoCentroid } from 'd3-geo';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '..');

const topo = JSON.parse(readFileSync(path.join(root, 'public', 'Odisha.json'), 'utf8'));
const key = Object.keys(topo.objects).find((k) => k.toLowerCase().includes('district'));
if (!key) throw new Error('No district object found in Odisha.json');

const coll = feature(topo, topo.objects[key]);
const out = {};

for (const f of coll.features) {
  const name = f.properties?.name;
  if (!name || name.toLowerCase() === 'odisha') continue;
  const [lon, lat] = geoCentroid(f);
  out[name] = { lat, lon };
}

const dir = path.join(root, 'data', 'mappings');
mkdirSync(dir, { recursive: true });
const dest = path.join(dir, 'district_centroids.json');
writeFileSync(dest, JSON.stringify(out, null, 2));
console.log(`Wrote ${Object.keys(out).length} district centroids -> ${dest}`);
