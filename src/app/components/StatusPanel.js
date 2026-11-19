/**
 * StatusPanel — full-width "not available yet" guidance card shared by the
 * Live NRT and Reliability pages. Rendered only inside client pages.
 */
export default function StatusPanel({
  title,
  children,
  endpoint,
  tone = 'warn',
  icon = '🛰️',
  onRetry,
  retrying = false,
  retryLabel = 'Retry now',
}) {
  return (
    <section className={`card status-panel status-${tone}`} role="status">
      <div className="status-icon" aria-hidden="true">{icon}</div>
      <h2 className="status-title">{title}</h2>
      <div className="status-text">{children}</div>
      {endpoint && <code className="status-endpoint">{endpoint}</code>}
      {onRetry && (
        <div className="status-actions">
          <button type="button" className="btn" onClick={onRetry} disabled={retrying}>
            {retrying ? 'Retrying…' : retryLabel}
          </button>
        </div>
      )}
    </section>
  );
}
