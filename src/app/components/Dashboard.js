'use client';

import { useFeatures } from '../platformMode';
import React, { useMemo, useState } from 'react';
import { REPORT_URL } from '../config';

function formatReason(reason) {
  if (!reason) return '—';
  return reason.length > 60 ? reason.slice(0, 58) + '…' : reason;
}

const MONTH_NAMES = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];

/**
 * Split the feed's two date formats into a compact date and a time so the
 * Start/Restore columns can stack them instead of forcing a ~150px nowrap cell
 * that pushed the table past the card edge.
 *
 * Accepted: "19-NOV-2026 10:00" and "19-11-2026 15:30".
 */
function parseFeedParts(value) {
  const raw = String(value || '').trim();
  if (!raw) return null;
  let match = raw.match(/^(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+(\d{1,2}:\d{2})/);
  if (match) {
    const month = MONTH_NAMES.findIndex(
      (name) => name.toUpperCase() === match[2].toUpperCase()
    );
    if (month >= 0) {
      return { date: `${Number(match[1])} ${MONTH_NAMES[month]} ${match[3]}`, time: match[4] };
    }
  }
  match = raw.match(/^(\d{1,2})-(\d{1,2})-(\d{4})\s+(\d{1,2}:\d{2})/);
  if (match) {
    const month = Number(match[2]) - 1;
    if (month >= 0 && month < 12) {
      return { date: `${Number(match[1])} ${MONTH_NAMES[month]} ${match[3]}`, time: match[4] };
    }
  }
  return null;
}

function FeedTime({ value }) {
  const parts = parseFeedParts(value);
  if (!parts) return <span>{value || '—'}</span>;
  return (
    <span className="dt" title={value}>
      <span className="dt-date">{parts.date}</span>
      <span className="dt-time">{parts.time}</span>
    </span>
  );
}

function splitAreas(areas) {
  if (!areas) return [];
  return areas
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

function AreasCell({ areas }) {
  const [expanded, setExpanded] = useState(false);
  const list = splitAreas(areas);

  if (list.length === 0) return '—';
  if (list.length <= 3) return <span>{list.join(', ')}</span>;

  const shown = expanded ? list : list.slice(0, 3);

  return (
    <button
      type="button"
      className="areas-cell"
      onClick={() => setExpanded((v) => !v)}
      aria-expanded={expanded}
      title={expanded ? 'Click to collapse' : list.join(', ')}
    >
      {shown.join(', ')}
      {expanded ? (
        <span className="areas-toggle">▴ less</span>
      ) : (
        <span className="areas-toggle">▾ +{list.length - 3} more</span>
      )}
    </button>
  );
}

export default function Dashboard({ dots = [] }) {
  const [district, setDistrict] = useState('all');
  const [reason, setReason] = useState('all');
  const [search, setSearch] = useState('');
  const reportingEnabled = useFeatures()?.reporting;
  const [reporting, setReporting] = useState(null);
  const [comment, setComment] = useState('');
  const [severity, setSeverity] = useState(3);
  const [toast, setToast] = useState('');

  const districts = useMemo(
    () => [...new Set(dots.map((d) => d.district))].sort(),
    [dots]
  );
  const reasons = useMemo(
    () => [...new Set(dots.map((d) => d.reason).filter(Boolean))].sort(),
    [dots]
  );

  const filtered = useMemo(() => {
    return dots.filter((d) => {
      if (district !== 'all' && d.district !== district) return false;
      if (reason !== 'all' && d.reason !== reason) return false;
      if (search) {
        const q = search.toLowerCase();
        const hay = `${d.gss} ${d.feeder} ${d.division} ${d.areas} ${d.district} ${d.circle}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [dots, district, reason, search]);

  const topReason = useMemo(() => {
    const counts = {};
    filtered.forEach((d) => {
      if (d.reason) counts[d.reason] = (counts[d.reason] || 0) + 1;
    });
    const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    return entries[0] ? entries[0][0] : '—';
  }, [filtered]);

  async function submitReport() {
    if (!reporting) return;
    try {
      const res = await fetch(REPORT_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          district: reporting.district,
          kind: 'correction',
          gss: reporting.gss,
          feeder: reporting.feeder,
          comment,
          severity: Number(severity),
          reporter: 'web',
        }),
      });
      setToast(res.ok ? 'Report submitted — thank you!' : 'Failed to submit report.');
    } catch (err) {
      console.error(err);
      setToast('Failed to submit report. Is the backend running?');
    }
    setReporting(null);
    setComment('');
    setSeverity(3);
    setTimeout(() => setToast(''), 4000);
  }

  return (
    <section>
      {/* Summary cards */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
          gap: 14,
          marginBottom: 18,
        }}
      >
        <div className="card card-hover" style={{ padding: 18 }}>
          <div className="stat-label">Outages</div>
          <div className="stat-value" style={{ color: 'var(--accent)' }}>{filtered.length}</div>
        </div>
        <div className="card card-hover" style={{ padding: 18 }}>
          <div className="stat-label">Districts</div>
          <div className="stat-value">{new Set(filtered.map((d) => d.district)).size}</div>
        </div>
        <div className="card card-hover" style={{ padding: 18 }}>
          <div className="stat-label">Top reason</div>
          <div className="stat-value" style={{ fontSize: 16, marginTop: 8 }}>{formatReason(topReason)}</div>
        </div>
        <div className="card card-hover" style={{ padding: 18 }}>
          <div className="stat-label">Geocoded dots</div>
          <div className="stat-value">{filtered.filter((d) => d.geocoded).length}</div>
        </div>
      </div>

      {/* Filters */}
      <div className="card" style={{ padding: 16, marginBottom: 18 }}>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'center' }}>
          <input
            className="control"
            style={{ flex: '1 1 200px', minWidth: 180 }}
            type="search"
            placeholder="Search station, feeder, division, area…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <select className="control" value={district} onChange={(e) => setDistrict(e.target.value)}>
            <option value="all">All districts</option>
            {districts.map((d) => (
              <option key={d} value={d}>{d}</option>
            ))}
          </select>
          <select className="control" value={reason} onChange={(e) => setReason(e.target.value)}>
            <option value="all">All reasons</option>
            {reasons.map((r) => (
              <option key={r} value={r}>{r}</option>
            ))}
          </select>
          {(district !== 'all' || reason !== 'all' || search) && (
            <button
              className="btn"
              onClick={() => {
                setDistrict('all');
                setReason('all');
                setSearch('');
              }}
            >
              Reset
            </button>
          )}
        </div>
      </div>

      {/* Table */}
      <div className="card" style={{ padding: 6 }}>
        <div className="table-wrap">
          <table className="data-table outage-table">
            <colgroup>
              <col style={{ width: '9%' }} />
              <col style={{ width: '12%' }} />
              <col style={{ width: '12%' }} />
              <col style={{ width: '23%' }} />
              <col style={{ width: '14%' }} />
              <col style={{ width: '10.5%' }} />
              <col style={{ width: '10.5%' }} />
              {reportingEnabled && <col style={{ width: '9%' }} />}
            </colgroup>
            <thead>
              <tr>
                <th scope="col">District</th>
                <th scope="col">Substation</th>
                <th scope="col">Feeder</th>
                <th scope="col">Key areas affected</th>
                <th scope="col">Reason</th>
                <th scope="col">Start</th>
                <th scope="col">Restore</th>
                {reportingEnabled && <th scope="col" aria-label="Actions"></th>}
              </tr>
            </thead>
            <tbody>
              {filtered.slice(0, 200).map((d, i) => (
                <tr key={`${d.district}-${d.gss}-${d.feeder}-${i}`}>
                  <td className="district-cell" style={{ fontWeight: 600 }}>{d.district}</td>
                  <td>{d.gss || '—'}</td>
                  <td>{d.feeder || '—'}</td>
                  <td><AreasCell areas={d.areas} /></td>
                  <td title={d.reason}>{formatReason(d.reason)}</td>
                  <td className="dt-cell"><FeedTime value={d.start} /></td>
                  <td className="dt-cell"><FeedTime value={d.end} /></td>
                  {reportingEnabled && <td className="action-cell">
                    <button
                      className="btn btn-compact"
                      type="button"
                      title="Report incorrect data"
                      aria-label={`Report incorrect data for ${d.district}`}
                      onClick={() => { setSeverity(3); setReporting(d); }}
                    >
                      Report
                    </button>
                  </td>}
                </tr>
              ))}
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={reportingEnabled ? 8 : 7} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}>
                    No outages match the current filters.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        {filtered.length > 200 && (
          <div style={{ padding: 10, textAlign: 'center', color: 'var(--text-muted)', fontSize: 12 }}>
            Showing first 200 of {filtered.length} — refine filters to narrow down.
          </div>
        )}
      </div>

      {/* Report modal */}
      {reporting && (
        <div className="modal-backdrop" onClick={() => setReporting(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3 style={{ marginBottom: 6 }}>Report incorrect data</h3>
            <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 14 }}>
              <strong>{reporting.district}</strong> — {reporting.gss || reporting.feeder || 'unknown substation'}
            </p>
            <div style={{ marginBottom: 12 }}>
              <label className="form-label" htmlFor="report-severity">
                Severity (1 = minor, 5 = extended outage)
              </label>
              <select
                id="report-severity"
                className="control"
                style={{ width: '100%' }}
                value={severity}
                onChange={(e) => setSeverity(Number(e.target.value))}
              >
                <option value={1}>1 · Minor flicker</option>
                <option value={2}>2 · Brief interruption</option>
                <option value={3}>3 · Power out (default)</option>
                <option value={4}>4 · Long outage</option>
                <option value={5}>5 · Extended outage</option>
              </select>
            </div>
            <textarea
              className="control"
              rows={4}
              style={{ width: '100%', resize: 'vertical', fontFamily: 'inherit' }}
              placeholder="What's wrong? (wrong timing, cancelled, location, etc.)"
              value={comment}
              onChange={(e) => setComment(e.target.value)}
            />
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 16 }}>
              <button className="btn" onClick={() => setReporting(null)}>Cancel</button>
              <button className="btn btn-accent" onClick={submitReport}>Submit</button>
            </div>
          </div>
        </div>
      )}

      {toast && <div className="toast">{toast}</div>}
    </section>
  );
}

