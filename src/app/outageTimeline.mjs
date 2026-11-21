export const HOUR = 3600000;
export const DAY = 24 * HOUR;
// The utility feed is in Indian Standard Time, independent of the viewer's timezone.
export const IST_OFFSET = 5.5 * HOUR;
const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];

export const floorHour = (ms) => Math.floor((ms + IST_OFFSET) / HOUR) * HOUR - IST_OFFSET;
export const ceilHour = (ms) => Math.ceil((ms + IST_OFFSET) / HOUR) * HOUR - IST_OFFSET;
export const snapHour = (ms) => floorHour(ms + HOUR / 2);

export function parseOutageTime(value) {
  const match = String(value ?? '').trim()
    .match(/^(\d{1,2})-([a-z]{3}|\d{1,2})-(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$/i);
  if (!match) return null;
  const [, day, month, year, hour = '0', minute = '0', second = '0'] = match;
  const monthIndex = /^\d+$/.test(month) ? Number(month) - 1 : MONTHS.indexOf(month.toUpperCase());
  if (monthIndex < 0 || monthIndex > 11 || +hour > 23 || +minute > 59 || +second > 59) return null;
  const ms = Date.UTC(+year, monthIndex, +day, +hour, +minute, +second);
  const check = new Date(ms);
  return check.getUTCMonth() === monthIndex && check.getUTCDate() === +day ? ms - IST_OFFSET : null;
}

export function outageInterval(dot) {
  const start = parseOutageTime(dot.start);
  if (start === null) return null;
  let end = parseOutageTime(dot.end) ?? Infinity;
  // The emergency feed uses the start date for both times, including overnight outages.
  if (end < start && /^\d{1,2}-\d{1,2}-/.test(dot.start) &&
      Math.floor((start + IST_OFFSET) / DAY) === Math.floor((end + IST_OFFSET) / DAY)) {
    end += DAY;
  }
  return { dot, start, end: Math.max(start, end) };
}

export function centeredSelection(center, duration) {
  return { start: center - duration / 2, end: center + duration / 2 };
}

export function overlaps(interval, selection) {
  return interval.start <= selection.end && interval.end >= selection.start;
}

export function busiestTime(intervals, fallback) {
  const starts = intervals.map(({ start }) => start).sort((a, b) => a - b);
  const ends = intervals.map(({ end }) => end).sort((a, b) => a - b);
  let best = fallback;
  let bestCount = 0;
  let finished = 0;
  for (let i = 0; i < starts.length; i++) {
    while (ends[finished] < starts[i]) finished++;
    const active = i + 1 - finished;
    if (active >= bestCount) {
      best = starts[i];
      bestCount = active;
    }
  }
  return best;
}

export function pointWithinWindow(intervals, selection) {
  const center = (selection.start + selection.end) / 2;
  let nearest = center;
  let distance = Infinity;
  for (const interval of intervals) {
    if (!overlaps(interval, selection)) continue;
    const point = Math.max(interval.start, Math.min(center, interval.end));
    if (Math.abs(point - center) < distance) {
      nearest = point;
      distance = Math.abs(point - center);
    }
  }
  return nearest;
}
