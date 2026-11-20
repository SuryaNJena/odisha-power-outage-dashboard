'use client';

import { useMemo, useState } from 'react';
import { AnalyticsOnly } from '../platformMode';
import Navigation from '../navigation';
import TrendChart from '../components/TrendChart';
import StatusPanel from '../components/StatusPanel';
import QuickReport, { FALLBACK_DISTRICTS } from '../components/QuickReport';
import { REALTIME_URL } from '../config';
import { useApiData } from '../useApiData';
import {
  formatClock,
  formatNumber,
  formatRelative,
  humanizeKey,
  parseDateMs,
  pipelineTone,
  riskLevelFromScore,
  riskTone,
  severityTone,
  toNumber,
} from '../format';

const POLL_MS = 15000;

const PIPELINE_ROWS = [
  { key: 'kafka', label: 'Kafka' },
  { key: 'flink', label: 'Flink' },
  { key: 'spark', label: 'Spark' },
];

/**
 * Defensive normaliser for the /api/realtime contract. Everything the page
 * renders goes through here so a partial payload (`{available:false}`, missing
 * arrays, null totals) still renders cleanly instead of crashing.
 */
function normalizeRealtime(payload) {
  const raw = payload && typeof payload === 'object' ? payload : {};
  const totals = raw.totals && typeof raw.totals === 'object' ? raw.totals : {};
  const districts = Array.isArray(raw.districts) ? raw.districts.filter((d) => d && d.district) : [];
  const alerts = Array.isArray(raw.alerts) ? raw.alerts.filter((a) => a && (a.message || a.alertId)) : [];
  const trend = Array.isArray(raw.trend) ? raw.trend.filter((t) => t && t.bucket) : [];
  const pipeline = raw.pipeline && typeof raw.pipeline === 'object' ? raw.pipeline : null;

  const available =
    raw.available === true ||
    (raw.available === undefined && (raw.totals !== undefined || Array.isArray(raw.districts)));

  return {
    available,
    updatedAt: raw.updatedAt || '',
    windowMinutes: toNumber(raw.windowMinutes, 15) || 15,
    totals: {
      reports: toNumber(totals.reports),
      districts: toNumber(totals.districts),
      alerts: toNumber(totals.alerts),
      officialOutages: toNumber(totals.officialOutages),
    },
    districts: districts
      .map((d) => ({
        district: d.district,
        reports: toNumber(d.reports),
        uniqueReporters: toNumber(d.uniqueReporters),
        officialOutages: toNumber(d.officialOutages),
        unconfirmedReports: toNumber(d.unconfirmedReports),
        riskScore: toNumber(d.riskScore),
        riskLevel: d.riskLevel || '',
        updatedAt: d.updatedAt || '',
      }))
      .sort(
        (a, b) =>
          b.reports - a.reports ||
          a.district.localeCompare(b.district)
      ),
    alerts: [...alerts].sort((a, b) => (parseDateMs(b.createdAt) ?? 0) - (parseDateMs(a.createdAt) ?? 0)),
    trend: [...trend].sort((a, b) => (parseDateMs(a.bucket) ?? 0) - (parseDateMs(b.bucket) ?? 0)),
    pipeline,
    detail:
      typeof raw.message === 'string'
        ? raw.message
        : typeof raw.detail === 'string'
          ? raw.detail
          : '',
  };
}

function riskLevelFor(district) {
  const known = String(district.riskLevel || '').toLowerCase();
  if (['low', 'moderate', 'medium', 'high', 'critical', 'severe'].includes(known)) {
    return known;
  }
  return riskLevelFromScore(district.riskScore);
}

export default function LivePage() {
  return <AnalyticsOnly><ReportPage /></AnalyticsOnly>;
}

function ReportPage() {
  const { data: raw, error, loading, reload } = useApiData(REALTIME_URL, { intervalMs: POLL_MS });
  const live = useMemo(() => (raw ? normalizeRealtime(raw) : null), [raw]);

  const ready = Boolean(live && live.available);
  const unavailable = !loading && !ready;
  const showStale = Boolean(error && ready);

  const windowMinutes = live?.windowMinutes || 15;
  const updatedLabel = ready ? formatRelative(live.updatedAt) : '—';

  const districtRows = useMemo(() => (ready ? live.districts : []), [ready, live]);

  const trend = useMemo(() => {
    const buckets = ready ? live.trend : [];
    return {
      buckets,
      labels: buckets.map((t) => formatClock(t.bucket)),
      values: buckets.map((t) => toNumber(t.reports)),
      total: buckets.reduce((sum, t) => sum + toNumber(t.reports), 0),
    };
  }, [ready, live]);

  const alerts = ready ? live.alerts : [];

  const districtOptions = useMemo(() => {
    const names = new Set(FALLBACK_DISTRICTS);
    for (const d of live?.districts || []) if (d.district) names.add(d.district);
    return [...names].sort((a, b) => a.localeCompare(b));
  }, [live]);

  const pipeline = ready ? live.pipeline : null;

  // '…' while the first fetch is in flight, '—' when the pipeline is absent.
  const kpi = (value) => (ready ? value : loading ? '…' : '—');

  return (
    <main style={{ minHeight: '100vh', background: 'var(--bg)' }}>
      <Navigation />

      <div style={{ maxWidth: 1200, margin: '0 auto', padding: '88px 20px 48px' }}>
        {/* Header */}
        <div className="page-head">
          <div className="page-head-main">
            <h1 style={{ fontSize: 26, fontWeight: 800, letterSpacing: '-0.02em', color: 'var(--text)' }}>
              📡 Live NRT dashboard
            </h1>
            <p style={{ color: 'var(--text-muted)', fontSize: 14, marginTop: 4 }}>
              Near-real-time reports, alerts &amp; pipeline health · refreshes every{' '}
              {POLL_MS / 1000}s
              {ready ? ` · data ${updatedLabel}` : ''}
            </p>
          </div>
          <div className="page-head-actions">
            <button type="button" className="btn" onClick={reload} disabled={loading}>
              {loading ? 'Refreshing…' : '↻ Refresh now'}
            </button>
          </div>
        </div>

        {/* Graceful "not available yet" state */}
        {unavailable && (
          <StatusPanel
            title="Live NRT pipeline is not available yet"
            endpoint={REALTIME_URL || '/api/realtime'}
            tone="warn"
            icon="🛰️"
            onRetry={reload}
            retrying={loading}
          >
            <p>
              The page is ready, but the realtime API has not returned live data
              {error ? ` (${error.message})` : ''}.
            </p>
            <p>
              This endpoint is fed by Spark Structured Streaming into Postgres{' '}
              <code className="inline-code">realtime.*</code> tables and served by the Go API. Once
              Kafka → Flink → Spark → API are running, this dashboard fills in automatically; it
              retries every {POLL_MS / 1000}s.
            </p>
          </StatusPanel>
        )}

        {/* A poll failed but we still have a usable snapshot cached in state */}
        {showStale && (
          <div
            className="card"
            style={{
              padding: '10px 14px',
              marginBottom: 16,
              borderColor: 'var(--yellow)',
              color: 'var(--yellow)',
              fontSize: 13,
            }}
            role="status"
          >
            ⚠️ Live refresh failed ({error.message}). Showing the last successful snapshot from{' '}
            {updatedLabel}; retrying every {POLL_MS / 1000}s.
          </div>
        )}

        {/* KPI cards */}
        <section className="kpi-grid" aria-label="Live totals">
          <div className="card card-hover kpi-card">
            <div className="stat-label">Reports · last {windowMinutes} min</div>
            <div className="stat-value" style={{ color: 'var(--accent)' }}>
              {kpi(live ? formatNumber(live.totals.reports) : '—')}
            </div>
            <div className="stat-sub">{ready ? 'user-submitted outage reports' : 'waiting for the live pipeline'}</div>
          </div>
          <div className="card card-hover kpi-card">
            <div className="stat-label">Districts reporting</div>
            <div className="stat-value">
              {kpi(live ? formatNumber(live.totals.districts) : '—')}
            </div>
            <div className="stat-sub">of {FALLBACK_DISTRICTS.length} Odisha districts</div>
          </div>
          <div className="card card-hover kpi-card">
            <div className="stat-label">Active alerts</div>
            <div className="stat-value" style={{ color: ready && live.totals.alerts > 0 ? 'var(--red)' : undefined }}>
              {kpi(live ? formatNumber(live.totals.alerts) : '—')}
            </div>
            <div className="stat-sub">{ready ? 'raised by Flink in the window' : 'no pipeline connection'}</div>
          </div>
          <div className="card card-hover kpi-card">
            <div className="stat-label">Official outages</div>
            <div className="stat-value">
              {kpi(live ? formatNumber(live.totals.officialOutages) : '—')}
            </div>
            <div className="stat-sub">from the latest scrape batch</div>
          </div>
        </section>

        {/* Main two-column dashboard */}
        <div className="live-grid">
          <div className="live-col">
            {/* Reports-per-minute trend */}
            <div className="card" style={{ padding: 18 }}>
              <div className="card-head">
                <h2 className="card-title">Reports trend</h2>
                <span className="card-sub">per minute · last {windowMinutes} min</span>
              </div>
              <TrendChart
                labels={trend.labels}
                series={[{ name: 'Reports', color: 'var(--accent)', values: trend.values }]}
                height={190}
                ariaLabel={`Reported outages per minute over the last ${windowMinutes} minutes`}
                emptyLabel={
                  loading && !live
                    ? 'Loading live trend…'
                    : 'No report trend for the current window yet.'
                }
              />
            </div>

            {/* District table */}
            <div className="card" style={{ padding: 6 }}>
              <div className="card-head" style={{ padding: '12px 12px 0', marginBottom: 6 }}>
                <h2 className="card-title">District live metrics</h2>
                <span className="card-sub">sorted by reports · last {windowMinutes} min</span>
              </div>
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>District</th>
                      <th className="num">Reports</th>
                      <th className="num">Reporters</th>
                      <th>Unconfirmed</th>
                      <th className="num">Official</th>
                      <th>Risk</th>
                    </tr>
                  </thead>
                  <tbody>
                    {districtRows.map((d) => {
                      const level = riskLevelFor(d);
                      return (
                        <tr key={d.district}>
                          <td style={{ fontWeight: 600, whiteSpace: 'nowrap' }}>{d.district}</td>
                          <td className="num">{formatNumber(d.reports)}</td>
                          <td className="num">{formatNumber(d.uniqueReporters)}</td>
                          <td>
                            {d.unconfirmedReports > 0 ? (
                              <span className="badge badge-warn">
                                {formatNumber(d.unconfirmedReports)} unconfirmed
                              </span>
                            ) : (
                              <span style={{ color: 'var(--text-muted)' }}>0</span>
                            )}
                          </td>
                          <td className="num">{formatNumber(d.officialOutages)}</td>
                          <td style={{ whiteSpace: 'nowrap' }}>
                            <span className={`badge badge-${riskTone(level)}`}>{humanizeKey(level)}</span>
                            <span className="card-sub" style={{ marginLeft: 6 }}>
                              {formatNumber(d.riskScore, 2)}
                            </span>
                          </td>
                        </tr>
                      );
                    })}
                    {districtRows.length === 0 && (
                      <tr>
                        <td
                          colSpan={6}
                          style={{ textAlign: 'center', color: 'var(--text-muted)', padding: 24 }}
                        >
                          {loading && !live
                            ? 'Loading district metrics…'
                            : 'No district-level reports in the current window yet.'}
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          <div className="live-col">
            {/* Pipeline health */}
            <div className="card" style={{ padding: 18 }}>
              <div className="card-head">
                <h2 className="card-title">Pipeline health</h2>
                <span className="card-sub">{windowMinutes} min window</span>
              </div>
              {PIPELINE_ROWS.map((row) => {
                const status = pipeline ? pipeline[row.key] : null;
                return (
                  <div className="pipeline-row" key={row.key}>
                    <span className="pipeline-name">{row.label}</span>
                    <span className={`chip chip-${pipelineTone(status)}`}>
                      <span className="chip-dot" aria-hidden="true" />
                      {status ? humanizeKey(status) : 'unknown'}
                    </span>
                  </div>
                );
              })}
              {pipeline && pipeline.lastSparkUpdate ? (
                <div className="pipeline-meta">
                  Spark last wrote metrics {formatRelative(pipeline.lastSparkUpdate)}
                </div>
              ) : (
                <div className="pipeline-meta">
                  {loading && !live
                    ? 'Loading pipeline heartbeats…'
                    : 'Heartbeats appear here once Spark writes realtime metrics.'}
                </div>
              )}
            </div>

            {/* Alert feed */}
            <div className="card" style={{ padding: 18 }}>
              <div className="card-head">
                <h2 className="card-title">Alert feed</h2>
                <span className="card-sub">{alerts.length} active</span>
              </div>
              <div className="alert-list">
                {alerts.length === 0 ? (
                  <div className="empty-note">
                    {loading && !live ? 'Loading alerts…' : 'No active alerts in the current window.'}
                  </div>
                ) : (
                  alerts.slice(0, 30).map((alert, index) => {
                    const tone = severityTone(alert.severity);
                    return (
                      <div
                        key={alert.alertId || `${alert.district || 'alert'}-${index}`}
                        className={`alert-item ${tone}`}
                      >
                        <div className="alert-message">
                          {alert.message || humanizeKey(alert.alertType)}
                        </div>
                        <div className="alert-meta">
                          <strong style={{ color: 'var(--text)' }}>
                            {alert.district || 'Unknown district'}
                          </strong>
                          <span className="sep">·</span>
                          <span>{humanizeKey(alert.alertType || 'alert')}</span>
                          <span className="sep">·</span>
                          <span>{formatRelative(alert.createdAt)}</span>
                          {toNumber(alert.reportCount) > 0 && (
                            <>
                              <span className="sep">·</span>
                              <span>{formatNumber(alert.reportCount)} reports</span>
                            </>
                          )}
                        </div>
                      </div>
                    );
                  })
                )}
              </div>
            </div>

            {/* Quick report form */}
            <QuickReport districts={districtOptions} />
          </div>
        </div>

        <p className="page-foot">
          Live source: <code className="inline-code">GET {REALTIME_URL || '/api/realtime'}</code>{' '}
          (Flink alerts + Spark streaming windows via the Go API). User reports are POSTed to{' '}
          <code className="inline-code">/api/report</code> and flow through Kafka{' '}
          <code className="inline-code">user-reports</code>.
        </p>
      </div>
    </main>
  );
}
