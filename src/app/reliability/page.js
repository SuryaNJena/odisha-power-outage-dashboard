'use client';

import { useMemo, useState } from 'react';
import { AnalyticsOnly } from '../platformMode';
import Navigation from '../navigation';
import TrendChart from '../components/TrendChart';
import StatusPanel from '../components/StatusPanel';
import { RELIABILITY_URL } from '../config';
import { useApiData } from '../useApiData';
import { formatDayLabel, formatNumber, toNumber } from '../format';

const RANGES = [
  { days: 7, label: '7 days' },
  { days: 30, label: '30 days' },
  { days: 90, label: '90 days' },
];

/**
 * Defensive normaliser for the /api/reliability contract
 * ({ available, days, districts[], trend[] }). Missing fields become 0 so a
 * partial payload never crashes the table or the SVG chart.
 */
function normalizeReliability(payload) {
  const raw = payload && typeof payload === 'object' ? payload : {};
  const districts = Array.isArray(raw.districts) ? raw.districts.filter((d) => d && d.district) : [];
  const trend = Array.isArray(raw.trend) ? raw.trend.filter((t) => t && t.date) : [];

  const available =
    raw.available === true ||
    (raw.available === undefined && (Array.isArray(raw.districts) || Array.isArray(raw.trend)));

  return {
    available,
    days: toNumber(raw.days, 0) || null,
    districts: districts.map((d) => ({
      district: d.district,
      saidiMinutes: toNumber(d.saidiMinutes),
      saifi: toNumber(d.saifi),
      caidiMinutes: toNumber(d.caidiMinutes),
      asaiPct: toNumber(d.asaiPct),
      outageCount: toNumber(d.outageCount),
      customersAffected: toNumber(d.customersAffected),
    })),
    trend: trend
      .map((t) => ({
        date: String(t.date),
        saidiMinutes: toNumber(t.saidiMinutes),
        saifi: toNumber(t.saifi),
      }))
      .sort((a, b) => a.date.localeCompare(b.date)),
  };
}

export default function ReliabilityPage() {
  return <AnalyticsOnly><ReportPage /></AnalyticsOnly>;
}

function ReportPage() {
  const [days, setDays] = useState(30);

  return (
    <main style={{ minHeight: '100vh', background: 'var(--bg)' }}>
      <Navigation />

      <div style={{ maxWidth: 1200, margin: '0 auto', padding: '88px 20px 48px' }}>
        {/* Header + range selector */}
        <div className="page-head">
          <div className="page-head-main">
            <h1 style={{ fontSize: 26, fontWeight: 800, letterSpacing: '-0.02em', color: 'var(--text)' }}>
              📊 Reliability indices
            </h1>
            <p style={{ color: 'var(--text-muted)', fontSize: 14, marginTop: 4 }}>
              SAIDI · SAIFI · CAIDI · ASAI by district over the last {days} days
            </p>
          </div>
          <div className="page-head-actions">
            <div className="seg" role="group" aria-label="Reliability time range">
              {RANGES.map((range) => (
                <button
                  key={range.days}
                  type="button"
                  className={`seg-btn${range.days === days ? ' active' : ''}`}
                  aria-pressed={range.days === days}
                  onClick={() => setDays(range.days)}
                >
                  {range.label}
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* key={days} gives each range a fresh load state (no stale numbers) */}
        <ReliabilityReport key={days} days={days} />
      </div>
    </main>
  );
}

function ReliabilityReport({ days }) {
  const url = `${RELIABILITY_URL}?days=${days}`;
  const { data: raw, error, loading, reload } = useApiData(url);
  const rel = useMemo(() => (raw ? normalizeReliability(raw) : null), [raw]);

  const ready = Boolean(rel && rel.available);
  const unavailable = !loading && !ready;

  const districts = useMemo(() => {
    if (!ready) return [];
    return [...rel.districts].sort(
      (a, b) => b.saidiMinutes - a.saidiMinutes || a.district.localeCompare(b.district)
    );
  }, [ready, rel]);

  const metrics = useMemo(() => {
    const count = districts.length;
    const totalOutages = districts.reduce((sum, d) => sum + d.outageCount, 0);
    const totalCustomers = districts.reduce((sum, d) => sum + d.customersAffected, 0);
    const avgAsai = count ? districts.reduce((sum, d) => sum + d.asaiPct, 0) / count : 0;
    const avgSaifi = count ? districts.reduce((sum, d) => sum + d.saifi, 0) / count : 0;
    const maxSaidi = count ? Math.max(...districts.map((d) => d.saidiMinutes)) : 0;
    return {
      count,
      totalOutages,
      totalCustomers,
      avgAsai,
      avgSaifi,
      maxSaidi,
      worst: districts[0] || null,
    };
  }, [districts]);

  const trend = useMemo(() => {
    const points = ready ? rel.trend : [];
    return {
      labels: points.map((t) => formatDayLabel(t.date)),
      saidi: points.map((t) => t.saidiMinutes),
      saifi: points.map((t) => t.saifi),
    };
  }, [ready, rel]);

  // '…' while the first fetch is in flight, '—' when the API is unavailable.
  const kpi = (value) => (ready ? value : loading ? '…' : '—');

  return (
    <>
      {unavailable && (
        <StatusPanel
          title="Reliability indices are not available yet"
          endpoint={url}
          tone="warn"
          icon="📊"
          onRetry={reload}
          retrying={loading}
        >
          <p>
            The page is ready, but the reliability API has not returned data
            {error ? ` (${error.message})` : ''}.
          </p>
          <p>
            dbt publishes <code className="inline-code">analytics.reliability_daily</code>; the Go
            API aggregates it into district SAIDI / SAIFI / CAIDI / ASAI. Run the dbt daily DAG,
            then this page populates on the next retry.
          </p>
        </StatusPanel>
      )}

      {/* KPI cards */}
      <section className="kpi-grid" aria-label="Reliability summary">
        <div className="card card-hover kpi-card">
          <div className="stat-label">Worst SAIDI</div>
          <div className="stat-value" style={{ color: 'var(--red)' }}>
            {kpi(metrics.worst ? formatNumber(metrics.worst.saidiMinutes, 1) : '—')}
            <span style={{ fontSize: 14, fontWeight: 600, color: 'var(--text-muted)', marginLeft: 4 }}>
              min
            </span>
          </div>
          <div className="stat-sub">
            {ready ? metrics.worst?.district || 'no districts' : 'worst-affected district'}
          </div>
        </div>
        <div className="card card-hover kpi-card">
          <div className="stat-label">Outages</div>
          <div className="stat-value">{kpi(formatNumber(metrics.totalOutages))}</div>
          <div className="stat-sub">
            {ready ? `${metrics.count} districts · ${days}-day window` : `${days}-day window`}
          </div>
        </div>
        <div className="card card-hover kpi-card">
          <div className="stat-label">Avg ASAI</div>
          <div className="stat-value" style={{ color: 'var(--green)' }}>
            {kpi(`${formatNumber(metrics.avgAsai, 2)}%`)}
          </div>
          <div className="stat-sub">average service availability</div>
        </div>
        <div className="card card-hover kpi-card">
          <div className="stat-label">Avg SAIFI</div>
          <div className="stat-value">{kpi(formatNumber(metrics.avgSaifi, 2))}</div>
          <div className="stat-sub">
            {ready
              ? `${formatNumber(metrics.totalCustomers)} customers affected`
              : 'interruptions per customer'}
          </div>
        </div>
      </section>

      {/* SAIDI bars + daily trend */}
      <div className="live-grid">
        <div className="card" style={{ padding: 18 }}>
          <div className="card-head">
            <h2 className="card-title">SAIDI trend</h2>
            <span className="card-sub">daily · last {days} days</span>
          </div>
          <TrendChart
            labels={trend.labels}
            series={[
              {
                name: 'SAIDI (min)',
                color: 'var(--accent)',
                values: trend.saidi,
                format: (v) => formatNumber(v, 1),
              },
              {
                name: 'SAIFI',
                color: 'var(--yellow)',
                values: trend.saifi,
                format: (v) => formatNumber(v, 2),
              },
            ]}
            height={230}
            area={false}
            ariaLabel={`SAIDI and SAIFI daily trend over the last ${days} days`}
            emptyLabel={
              loading && !rel ? 'Loading reliability trend…' : 'No daily reliability trend for this range yet.'
            }
          />
        </div>

        <div className="card" style={{ padding: 18 }}>
          <div className="card-head">
            <h2 className="card-title">SAIDI by district</h2>
            <span className="card-sub">minutes lost · {days} days</span>
          </div>
          {districts.length === 0 ? (
            <div className="empty-note">
              {loading && !rel ? 'Loading district reliability…' : 'No district reliability data for this range yet.'}
            </div>
          ) : (
            <div className="bar-list">
              {districts.map((d) => {
                const width = metrics.maxSaidi > 0 ? Math.max(2, (d.saidiMinutes / metrics.maxSaidi) * 100) : 0;
                return (
                  <div className="bar-row" key={d.district}>
                    <span className="bar-name" title={d.district}>{d.district}</span>
                    <span className="bar-track">
                      <span className="bar-fill" style={{ width: `${width}%` }} />
                    </span>
                    <span className="bar-value">{formatNumber(d.saidiMinutes, 1)} min</span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {/* Full table */}
      <div className="card" style={{ padding: 6, marginTop: 16 }}>
        <div className="card-head" style={{ padding: '12px 12px 0', marginBottom: 6 }}>
          <h2 className="card-title">Reliability by district</h2>
          <span className="card-sub">
            {ready ? `${metrics.count} districts · ${days}-day window` : `${days}-day window`}
          </span>
        </div>
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th className="num">#</th>
                <th>District</th>
                <th className="num">SAIDI (min)</th>
                <th className="num">SAIFI</th>
                <th className="num">CAIDI (min)</th>
                <th className="num">ASAI (%)</th>
                <th className="num">Outages</th>
                <th className="num">Customers affected</th>
              </tr>
            </thead>
            <tbody>
              {districts.map((d, index) => (
                <tr key={d.district}>
                  <td className="num" style={{ color: 'var(--text-muted)' }}>{index + 1}</td>
                  <td style={{ fontWeight: 600, whiteSpace: 'nowrap' }}>{d.district}</td>
                  <td className="num">{formatNumber(d.saidiMinutes, 1)}</td>
                  <td className="num">{formatNumber(d.saifi, 2)}</td>
                  <td className="num">{formatNumber(d.caidiMinutes, 1)}</td>
                  <td className="num">{formatNumber(d.asaiPct, 2)}</td>
                  <td className="num">{formatNumber(d.outageCount)}</td>
                  <td className="num">{formatNumber(d.customersAffected)}</td>
                </tr>
              ))}
              {districts.length === 0 && (
                <tr>
                  <td colSpan={8} style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}>
                    {loading && !rel
                      ? 'Loading reliability data…'
                      : 'No reliability data for this range yet.'}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <p className="page-foot">
        Metrics: SAIDI = sustained average interruption duration (minutes lost / customer), SAIFI =
        interruptions per customer, CAIDI = average interruption duration, ASAI = service availability
        %. Source: dbt <code className="inline-code">analytics.reliability_daily</code> served by{' '}
        <code className="inline-code">GET {url}</code>.
      </p>
    </>
  );
}
