'use client';

import React, { useMemo, useState } from 'react';
import { BACKEND_REPORT_URL } from '../config';

function formatReason(reason) {
  if (!reason) return '—';
  return reason.length > 60 ? reason.slice(0, 58) + '…' : reason;
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
  const [reporting, setReporting] = useState(null);
  const [comment, setComment] = useState('');
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
      const res = await fetch(BACKEND_REPORT_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          district: reporting.district,
          gss: reporting.gss,
          feeder: reporting.feeder,
          comment,
        }),
      });
      setToast(res.ok ? 'Report submitted — thank you!' : 'Failed to submit report.');
    } catch (err) {
      console.error(err);
      setToast('Failed to submit report. Is the backend running?');
    }
    setReporting(null);
    setComment('');
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
          <table className="data-table">
            <thead>
              <tr>
                <th>District</th>
                <th>Substation</th>
                <th>Feeder</th>
                <th>Key areas affected</th>
                <th>Reason</th>
                <th>Start</th>
                <th>Restore</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {filtered.slice(0, 200).map((d, i) => (
                <tr key={`${d.district}-${d.gss}-${d.feeder}-${i}`}>
                  <td style={{ fontWeight: 600, whiteSpace: 'nowrap' }}>{d.district}</td>
                  <td>{d.gss || '—'}</td>
                  <td>{d.feeder || '—'}</td>
                  <td><AreasCell areas={d.areas} /></td>
                  <td title={d.reason}>{formatReason(d.reason)}</td>
                  <td style={{ whiteSpace: 'nowrap' }}>{d.start || '—'}</td>
                  <td style={{ whiteSpace: 'nowrap' }}>{d.end || '—'}</td>
                  <td>
                    <button className="btn" style={{ padding: '5px 10px', fontSize: 12 }} onClick={() => setReporting(d)}>
                      ⚠ Report
                    </button>
                  </td>
                </tr>
              ))}
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={8} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}>
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

