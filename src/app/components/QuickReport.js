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
  [1, '1 · Minor flicker', '15 min'],
  [2, '2 · Brief interruption', '1 h'],
  [3, '3 · Power out (default)', '4 h'],
  [4, '4 · Long outage', '12 h'],
  [5, '5 · Extended outage', '24 h'],
];

export default function QuickReport({ districts = FALLBACK_DISTRICTS, onSaved }) {
  const [district, setDistrict] = useState('');
  const [area, setArea] = useState('');
  const [matches, setMatches] = useState([]);
  const [locationMessage, setLocationMessage] = useState('');
  const [locating, setLocating] = useState(false);
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

  function selectArea(place, matchDistrict) {
    const matched = matchDistrict(districts, place.district);
    setDistrict(matched);
    setArea(place.area.slice(0, 120));
    setMatches([]);
    setLocationMessage(matched
      ? place.area ? 'Area and district selected. You can edit them.' : 'District selected. You can type a nearby area.'
      : 'Area selected. Choose its district before submitting.');
  }

  async function lookup(useLocation) {
    if (locating || submitting) return;
    setLocating(true);
    setMatches([]);
    setLocationMessage(useLocation ? 'Finding your approximate area…' : 'Looking for matching areas…');
    try {
      const location = await import('../reportLocation');
      if (useLocation) {
        if (!navigator.geolocation) {
          setLocationMessage('Location is unavailable here. Type your area instead.');
          return;
        }
        const { coords } = await new Promise((resolve, reject) => navigator.geolocation
          .getCurrentPosition(resolve, reject, { timeout: 8000, maximumAge: 60_000 }));
        const place = await location.areaAtLocation(coords.latitude, coords.longitude);
        if (place) selectArea(place, location.matchDistrict);
        else setLocationMessage('Could not match your location to Odisha. Choose a district and type your area.');
      } else {
        const places = await location.findAreas(area, district);
        setMatches(places);
        setLocationMessage(places.length ? 'Choose a match or keep your typed area.' : 'No match found. You can submit the area as typed.');
      }
    } catch {
      setLocationMessage(useLocation
        ? 'Could not get your location. Type your area instead.'
        : 'Area lookup is unavailable. You can submit the area as typed.');
    } finally {
      setLocating(false);
    }
  }

  const onSubmit = async (event) => {
    event.preventDefault();
    if (!district) {
      notify('error', 'Pick a district before submitting.');
      return;
    }
    if (submitting || locating) return;

    setSubmitting(true);
    try {
      const res = await fetch(REPORT_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          district,
          kind: 'outage',
          area: area.trim(),
          gss: '',
          feeder: '',
          comment: comment.trim(),
          severity: Number(severity),
          reporter: 'web',
        }),
      });
      if (!res.ok) throw new Error(`API responded ${res.status}`);
      const { stored, mapArea } = await res.json();
      if (stored) {
        notify('ok', mapArea
          ? `Report saved. Map shows the approximate area near ${mapArea}.`
          : 'Report saved. Map shows the district location.');
      } else {
        notify('ok', 'Your outage report was queued.');
      }
      if (stored) onSaved?.();
      setComment('');
      setArea('');
      setMatches([]);
      setLocationMessage('');
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
            onChange={(e) => {
              setDistrict(e.target.value);
              setMatches([]);
              setLocationMessage('');
            }}
            disabled={locating || submitting}
            required
          >
            <option value="">Select a district…</option>
            {districts.map((name) => (
              <option key={name} value={name}>{name}</option>
            ))}
          </select>
        </div>

        <div>
          <label className="form-label" htmlFor="outage-report-area">Area (optional)</label>
          <input
            id="outage-report-area"
            className="control control-block"
            placeholder="e.g. Nayapalli or Patia"
            maxLength={120}
            value={area}
            disabled={locating || submitting}
            onChange={(e) => {
              setArea(e.target.value);
              setMatches([]);
              setLocationMessage('');
            }}
          />
          <div className="report-location-actions">
            <button type="button" className="btn btn-compact" disabled={locating || submitting}
              onClick={() => lookup(true)}>Use my location</button>
            <button type="button" className="btn btn-compact" disabled={locating || submitting || area.trim().length < 2}
              onClick={() => lookup(false)}>Find area</button>
          </div>
          <p className="form-hint">Enter a locality or landmark. GPS stays on your device.</p>
          {locationMessage && <p className="form-hint" role="status">{locationMessage}</p>}
          {matches.length > 0 && (
            <ul className="report-area-matches" aria-label="Matching areas">
              {matches.map((place) => (
                <li key={place.area + '|' + place.district}>
                  <button type="button" className="btn btn-compact" disabled={submitting}
                    onClick={async () => {
                      const { matchDistrict } = await import('../reportLocation');
                      selectArea(place, matchDistrict);
                    }}>
                    {place.area === place.district ? place.area : `${place.area}, ${place.district}`}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div>
          <label className="form-label" htmlFor="outage-report-severity">Severity</label>
          <select
            id="outage-report-severity"
            className="control control-block"
            value={severity}
            onChange={(e) => setSeverity(Number(e.target.value))}
          >
            {SEVERITY_OPTIONS.map(([value, label, visibility]) => (
              <option key={value} value={value}>{label} · {visibility} on map</option>
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
            placeholder="Feeder or what you are seeing…"
            value={comment}
            onChange={(e) => setComment(e.target.value)}
          />
        </div>

        <button
          type="submit"
          className="btn btn-accent"
          disabled={submitting || locating || !district}
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
