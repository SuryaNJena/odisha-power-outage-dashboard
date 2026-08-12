/**
 * dateUtils.js — parse outage date strings used by the Kavach feed.
 *
 * Scheduled outages look like  "13-AUG-2026 10:00"
 * Emergency outages look like  "12-08-2026 21:58"
 * Enriched dots also carry a `date` field ("YYYY-MM-DD") so we can skip parsing.
 */

const MONTHS = {
  JAN: 0, FEB: 1, MAR: 2, APR: 3, MAY: 4, JUN: 5,
  JUL: 6, AUG: 7, SEP: 8, OCT: 9, NOV: 10, DEC: 11,
};

/**
 * Parse an outage `start`/`end` string into a Date, or null when unparseable.
 */
export function parseOutageDate(value) {
  if (!value) return null;
  const s = String(value).trim();
  if (!s) return null;

  // Already a YYYY-MM-DD date key.
  const iso = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (iso) {
    const d = new Date(+iso[1], +iso[2] - 1, +iso[3]);
    return isNaN(d.getTime()) ? null : d;
  }

  // DD-MMM-YYYY [HH:MM]
  const dmy = s.match(/^(\d{1,2})-([A-Za-z]{3})-(\d{4})/);
  if (dmy) {
    const mon = MONTHS[dmy[2].toUpperCase()];
    if (mon === undefined) return null;
    const d = new Date(+dmy[3], mon, +dmy[1]);
    return isNaN(d.getTime()) ? null : d;
  }

  // DD-MM-YYYY [HH:MM]
  const ddmmyyyy = s.match(/^(\d{1,2})-(\d{1,2})-(\d{4})/);
  if (ddmmyyyy) {
    const d = new Date(+ddmmyyyy[3], +ddmmyyyy[2] - 1, +ddmmyyyy[1]);
    return isNaN(d.getTime()) ? null : d;
  }

  return null;
}

/**
 * Local-timezone YYYY-MM-DD key for a Date (or null).
 */
export function toDateKey(date) {
  if (!date || isNaN(date.getTime())) return null;
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

/**
 * Date key (YYYY-MM-DD) for an outage dot. Prefers the precomputed `date`
 * field, otherwise parses `start`.
 */
export function outageDateKey(dot) {
  if (dot && dot.date) return dot.date;
  const d = parseOutageDate(dot && dot.start);
  return d ? toDateKey(d) : null;
}

/**
 * True when the dot's date falls inside the inclusive [from, to] range.
 * A missing date (unparseable) is included only when no range is set.
 */
export function inDateRange(dot, from, to) {
  if (!from && !to) return true;
  const key = outageDateKey(dot);
  if (!key) return false;
  if (from && key < from) return false;
  if (to && key > to) return false;
  return true;
}
