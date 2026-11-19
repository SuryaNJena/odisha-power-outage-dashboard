/**
 * format.js — small dependency-free formatting helpers shared by the Live NRT
 * and Reliability pages.
 *
 * The platform contract stores timestamps as UTC and displays them in
 * Asia/Kolkata, so wall-clock formatting pins the IANA timezone instead of
 * relying on the viewer's machine.
 */

export const DISPLAY_TIME_ZONE = 'Asia/Kolkata';

const MONTHS_SHORT = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];

const OK_STATUS = new Set(['ok', 'up', 'healthy', 'running', 'connected', 'ready', 'active', 'live']);
const WARN_STATUS = new Set(['warn', 'warning', 'degraded', 'stale', 'idle', 'starting', 'pending', 'lagging']);
const BAD_STATUS = new Set(['down', 'error', 'failed', 'fail', 'stopped', 'missing', 'offline', 'unavailable']);

export function toNumber(value, fallback = 0) {
  const n = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(n) ? n : fallback;
}

export function parseDateMs(value) {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  const ms = Date.parse(String(value));
  return Number.isNaN(ms) ? null : ms;
}

/** "1,23,456" style grouping (en-IN) with a fixed number of decimals. */
export function formatNumber(value, digits = 0) {
  const n = toNumber(value);
  const safeDigits = Math.max(0, Math.min(4, digits));
  return n.toLocaleString('en-IN', {
    minimumFractionDigits: safeDigits,
    maximumFractionDigits: safeDigits,
  });
}

export function formatMinutes(value, digits = 1) {
  if (value === null || value === undefined || value === '') return '—';
  return `${formatNumber(value, digits)} min`;
}

/** Local (Asia/Kolkata) clock, e.g. "17:42" or "17:42:07". */
export function formatClock(value, { seconds = false } = {}) {
  const ms = parseDateMs(value);
  if (ms === null) return '—';
  try {
    return new Date(ms).toLocaleTimeString('en-IN', {
      timeZone: DISPLAY_TIME_ZONE,
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
      ...(seconds ? { second: '2-digit' } : {}),
    });
  } catch (err) {
    return new Date(ms).toLocaleTimeString();
  }
}

/** "19 Nov, 17:42 IST". */
export function formatDateTime(value) {
  const ms = parseDateMs(value);
  if (ms === null) return '—';
  try {
    const text = new Date(ms).toLocaleString('en-IN', {
      timeZone: DISPLAY_TIME_ZONE,
      day: '2-digit',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });
    return `${text} IST`;
  } catch (err) {
    return new Date(ms).toLocaleString();
  }
}

/** "2026-11-19" → "19 Nov" without timezone drift (string, not Date parse). */
export function formatDayLabel(value) {
  if (value === null || value === undefined || value === '') return '';
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (match) {
    const month = MONTHS_SHORT[Number(match[2]) - 1];
    if (month) return `${Number(match[3])} ${month}`;
  }
  return String(value);
}

/** "4m ago" / "2h ago" / falls back to a full timestamp after a week. */
export function formatRelative(value, now = Date.now()) {
  const ms = parseDateMs(value);
  if (ms === null) return '—';
  const diff = now - ms;
  if (diff < 0) return 'just now';
  const secs = Math.round(diff / 1000);
  if (secs < 10) return 'just now';
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return formatDateTime(value);
}

export function isOlderThan(value, maxAgeMs, now = Date.now()) {
  const ms = parseDateMs(value);
  if (ms === null) return false;
  return now - ms > maxAgeMs;
}

/** "report_spike" → "Report spike". */
export function humanizeKey(value) {
  if (value === null || value === undefined || value === '') return '—';
  const text = String(value).replace(/[_-]+/g, ' ').trim();
  if (!text) return '—';
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function riskLevelFromScore(score) {
  const n = toNumber(score);
  if (n >= 0.75) return 'critical';
  if (n >= 0.5) return 'high';
  if (n >= 0.25) return 'moderate';
  return 'low';
}

/** Maps a risk level to a badge modifier: ok | warn | bad | muted. */
export function riskTone(level) {
  const text = String(level || '').toLowerCase();
  if (text === 'low') return 'ok';
  if (text === 'moderate' || text === 'medium') return 'warn';
  if (text === 'high' || text === 'severe' || text === 'critical') return 'bad';
  return 'muted';
}

/** Maps an alert severity to an alert-item modifier: info | warning | critical. */
export function severityTone(severity) {
  const text = String(severity || '').toLowerCase();
  if (text === 'critical' || text === 'severe' || text === 'high' || text === 'error') return 'critical';
  if (text === 'warning' || text === 'warn' || text === 'moderate') return 'warning';
  return 'info';
}

/** Maps a pipeline heartbeat status to a chip modifier: ok | warn | bad | muted. */
export function pipelineTone(status) {
  const text = String(status || '').toLowerCase();
  if (OK_STATUS.has(text)) return 'ok';
  if (WARN_STATUS.has(text)) return 'warn';
  if (BAD_STATUS.has(text)) return 'bad';
  return 'muted';
}
