'use client';

import { useEffect, useRef, useState } from 'react';
import { BACKEND_URL, BACKEND_MAPDATA_URL } from '../config';

export default function RefreshButton({ onData }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const request = useRef(null);
  useEffect(() => () => request.current?.abort(), []);

  async function refresh() {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setMessage('');
    try {
      let method = 'POST';
      const deadline = Date.now() + 6 * 60_000;
      while (!controller.signal.aborted) {
        const response = await fetch(BACKEND_URL + '/api/refresh', {
          method, signal: controller.signal, cache: 'no-store',
        });
        if (!response.ok) throw new Error('Refresh unavailable. Your saved map is still visible.');
        const { status, nextAllowedAt } = await response.json();
        const next = new Date(nextAllowedAt * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
        if (status === 'failed') throw new Error('Refresh failed. Showing the last saved map. Try again after ' + next + '.');
        if (status !== 'running') {
          const map = await fetch(BACKEND_MAPDATA_URL, {signal: controller.signal, cache: 'no-store'});
          if (!map.ok) throw new Error('Could not load the saved map.');
          onData(await map.json());
          setMessage((method === 'POST' ? 'Using saved map.' : 'Map updated.') + ' Next refresh after ' + next + '.');
          break;
        }
        if (Date.now() >= deadline) throw new Error('Still processing. Check again shortly.');
        setMessage('Fetching the latest utility data…');
        await new Promise(resolve => setTimeout(resolve, 3000));
        method = 'GET';
      }
    } catch (error) {
      if (!controller.signal.aborted) setMessage(error.message);
    } finally {
      request.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  return <>
    <button className="btn btn-compact" type="button" onClick={refresh} disabled={busy}
      title="Fetch new utility data at most once every three hours">{busy ? 'Refreshing…' : 'Refresh'}</button>
    {message && <span className="refresh-feedback" role="status">{message}</span>}
  </>;
}
