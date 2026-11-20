'use client';

import { useEffect, useRef, useState } from 'react';
import { REPORT_URL } from '../config';

export const FALLBACK_DISTRICTS = [
  'Angul', 'Balangir', 'Balasore', 'Bargarh', 'Bhadrak', 'Boudh', 'Cuttack',
  'Deogarh', 'Dhenkanal', 'Gajapati', 'Ganjam', 'Jagatsinghpur', 'Jajpur',
  'Jharsuguda', 'Kalahandi', 'Kandhamal', 'Kendrapara', 'Kendujhar', 'Khordha',
  'Koraput', 'Malkangiri', 'Mayurbhanj', 'Nabarangpur', 'Nayagarh', 'Nuapada',
  'Puri', 'Rayagada', 'Sambalpur', 'Subarnapur', 'Sundargarh',
];

const SEVERITY_OPTIONS = [
  [1, '1 · Minor flicker'],
  [2, '2 · Brief interruption'],
  [3, '3 · Power out (default)'],
  [4, '4 · Long outage'],
  [5, '5 · Extended outage'],
];

export default function QuickReport({ districts = FALLBACK_DISTRICTS, onSaved }) {
  const [district, setDistrict] = useState('');
  const [severity, setSeverity] = useState(3);
  const [comment, setComment] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [toast, setToast] = useState(null);
  const toastTimer = useRef(null);

  useEffect(
    () => () => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
    },
    []
  );

  const notify = (tone, text) => {
    setToast({ tone, text });
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 4500);
  };

  const onSubmit = async (event) => {
    event.preventDefault();
    if (!district) {
      notify('error', 'Pick a district before submitting.');
      return;
    }
    if (submitting) return;

    setSubmitting(true);
    try {
      const res = await fetch(REPORT_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          district,
          kind: 'outage',
          gss: '',
          feeder: '',
          comment: comment.trim(),
          severity: Number(severity),
          reporter: 'web',
        }),
      });
      if (!res.ok) throw new Error(`API responded ${res.status}`);
      const { stored } = await res.json();
      notify('ok', stored ? 'Your outage report was saved.' : 'Your outage report was queued.');
      if (stored) onSaved?.();
      setComment('');
    } catch (err) {
      console.error('Failed to submit outage report:', err);
      notify('error', 'Could not submit report. The report API may be offline.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="card" style={{ padding: 18 }}>
      <div className="card-head">
        <h2 className="card-title">Report an outage</h2>
      </div>
      <form className="form-grid" onSubmit={onSubmit}>
        <div>
          <label className="form-label" htmlFor="outage-report-district">District</label>
          <select
            id="outage-report-district"
            className="control control-block"
            value={district}
            onChange={(e) => setDistrict(e.target.value)}
            required
          >
            <option value="">Select a district…</option>
            {districts.map((name) => (
              <option key={name} value={name}>{name}</option>
            ))}
          </select>
        </div>

        <div>
          <label className="form-label" htmlFor="outage-report-severity">Severity</label>
          <select
            id="outage-report-severity"
            className="control control-block"
            value={severity}
            onChange={(e) => setSeverity(Number(e.target.value))}
          >
            {SEVERITY_OPTIONS.map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </div>

        <div>
          <label className="form-label" htmlFor="outage-report-comment">Comment</label>
          <textarea
            id="outage-report-comment"
            className="control control-block"
            maxLength={1000}
            rows={3}
            style={{ resize: 'vertical', fontFamily: 'inherit' }}
            placeholder="Feeder, area, or what you are seeing…"
            value={comment}
            onChange={(e) => setComment(e.target.value)}
          />
        </div>

        <button
          type="submit"
          className="btn btn-accent"
          disabled={submitting || !district}
          style={{ justifyContent: 'center' }}
        >
          {submitting ? 'Submitting…' : 'Submit report'}
        </button>

      </form>

      {toast && (
        <div className={toast.tone === 'error' ? 'toast toast-error' : 'toast'} role="status">
          {toast.text}
        </div>
      )}
    </div>
  );
}
