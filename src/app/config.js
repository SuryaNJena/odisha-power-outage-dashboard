/**
 * config.js — shared constants for the static frontend.
 *
 * During local dev (`npm run dev`) basePath is empty so all paths are absolute.
 * In production (`npm run build`) basePath = '/odisha-power-outage-dashboard'.
 */

const BASE = process.env.NEXT_PUBLIC_BASE_PATH || '';

export const MAP_URL = `${BASE}/Odisha.json`;
export const DATA_URL = `${BASE}/outage_data.json`;
