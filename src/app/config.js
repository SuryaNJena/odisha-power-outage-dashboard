/**
 * config.js — shared constants for the static frontend.
 *
 * Local self-hosted mode uses no basePath.
 * Deployment mode can enable a basePath via NEXT_PUBLIC_USE_BASE_PATH=true.
 */

const BASE = process.env.NEXT_PUBLIC_BASE_PATH || '';
// Same-origin by default (one tunnel / single port deployment). For local dev
// (Next on :3000, Go on :3001) set NEXT_PUBLIC_BACKEND_URL in .env.local.
const BACKEND_BASE = process.env.NEXT_PUBLIC_BACKEND_URL || '';

export const MAP_URL = `${BASE}/Odisha.json`;
export const DATA_URL = `${BASE}/outage_data.json`;
export const BACKEND_URL = BACKEND_BASE;
export const BACKEND_MAPDATA_URL = `${BACKEND_URL}/api/mapdata`;
export const BACKEND_REPORT_URL = `${BACKEND_URL}/api/report`;

