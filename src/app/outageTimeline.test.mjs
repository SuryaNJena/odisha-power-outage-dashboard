import test from 'node:test';
import assert from 'node:assert/strict';
import {
  DAY, busiestTime, centeredSelection, outageInterval, overlaps, parseOutageTime, pointWithinWindow,
} from './outageTimeline.mjs';

test('scheduled and emergency feed dates use IST, including overnight emergency outages', () => {
  assert.equal(parseOutageTime('21-NOV-2026 09:15'), Date.parse('2026-11-21T03:45:00Z'));
  assert.equal(parseOutageTime('21-11-2026 09:15'), Date.parse('2026-11-21T03:45:00Z'));
  assert.equal(parseOutageTime('31-11-2026 09:15'), null);
  const interval = outageInterval({ start: '21-11-2026 23:40', end: '21-11-2026 00:10' });
  assert.equal(interval.end - interval.start, 30 * 60000);
});

test('Point keeps a nearby outage from the selected 24-hour window', () => {
  const center = parseOutageTime('21-NOV-2026 12:00');
  const window = centeredSelection(center, DAY);
  const older = outageInterval({ start: '21-NOV-2026 08:30', end: '21-NOV-2026 09:00' });
  const closer = outageInterval({ start: '21-NOV-2026 13:35', end: '21-NOV-2026 14:00' });
  const outside = outageInterval({ start: '22-NOV-2026 08:00', end: '22-NOV-2026 09:00' });
  assert.equal(pointWithinWindow([older, closer, outside], window), closer.start);
  assert.equal(pointWithinWindow([outside], window), center);
  assert(overlaps(closer, centeredSelection(pointWithinWindow([older, closer], window), 0)));
});

test('short outages can provide the initial anchor even if they fall between hour ticks', () => {
  const older = outageInterval({ start: '21-11-2026 09:05', end: '21-11-2026 09:20' });
  const later = outageInterval({ start: '21-11-2026 09:10', end: '21-11-2026 09:25' });
  assert.equal(busiestTime([older, later], 0), later.start);
});
