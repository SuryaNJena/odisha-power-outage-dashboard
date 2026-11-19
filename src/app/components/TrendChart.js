/**
 * TrendChart — dependency-free SVG line chart used by the Live NRT and
 * Reliability pages.
 *
 * Each series is normalised to its own range because the dashboards plot values
 * with very different magnitudes (reports per minute vs SAIFI). The legend shows
 * each series' latest value, so a shared Y axis is not needed and no value is
 * misleadingly compared across series.
 */

export default function TrendChart({
  labels = [],
  series = [],
  height = 190,
  area = true,
  emptyLabel = 'No trend data yet.',
  ariaLabel = 'Trend chart',
}) {
  const width = 760;
  const padTop = 14;
  const padBottom = 26;
  const padLeft = 10;
  const padRight = 10;
  const innerW = width - padLeft - padRight;
  const innerH = Math.max(40, height - padTop - padBottom);
  const safeLabels = Array.isArray(labels) ? labels : [];

  const prepared = (Array.isArray(series) ? series : [])
    .filter((s) => s && Array.isArray(s.values))
    .map((s) => {
      const values = s.values.map((v) => {
        if (v === null || v === undefined || v === '') return null;
        const n = Number(v);
        return Number.isFinite(n) ? n : null;
      });
      const finite = values.filter((v) => v !== null);
      const max = finite.length ? Math.max(...finite) : 0;
      return {
        name: s.name || 'Value',
        color: s.color || 'var(--accent)',
        format: s.format,
        values,
        finite,
        max: max > 0 ? max : 1,
      };
    });

  const count = Math.max(safeLabels.length, ...prepared.map((s) => s.values.length), 0);
  const hasData = count > 0 && prepared.some((s) => s.finite.length > 0);
  const baseY = padTop + innerH;

  const x = (i) => (count <= 1 ? padLeft + innerW / 2 : padLeft + (i / (count - 1)) * innerW);
  const y = (s, v) => padTop + innerH - (v / s.max) * innerH;

  const linePath = (s) => {
    let d = '';
    let open = false;
    s.values.forEach((v, i) => {
      if (v === null) {
        open = false;
        return;
      }
      d += `${open ? 'L' : 'M'}${x(i).toFixed(2)},${y(s, v).toFixed(2)} `;
      open = true;
    });
    return d.trim();
  };

  const areaPath = (s) => {
    const points = s.values.map((v, i) => `L${x(i).toFixed(2)},${y(s, v).toFixed(2)}`).join(' ');
    return `M${x(0).toFixed(2)},${baseY.toFixed(2)} ${points} L${x(count - 1).toFixed(2)},${baseY.toFixed(2)} Z`;
  };

  if (!hasData) {
    return <div className="chart-empty">{emptyLabel}</div>;
  }

  const tickIndexes = count <= 1 ? [0] : [...new Set([0, Math.floor((count - 1) / 2), count - 1])];
  const showArea = area && prepared.length === 1 && prepared[0].values.every((v) => v !== null);

  return (
    <div>
      <div className="chart-legend">
        {prepared.map((s) => {
          const latest = [...s.values].reverse().find((v) => v !== null);
          const text = typeof s.format === 'function' ? s.format(latest) : String(latest ?? '');
          return (
            <span className="chart-legend-item" key={s.name}>
              <span className="chart-swatch" style={{ background: s.color }} aria-hidden="true" />
              <span>{s.name}</span>
              {latest !== undefined && latest !== null && <strong>{text}</strong>}
            </span>
          );
        })}
      </div>
      <svg className="trend-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={ariaLabel}>
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <line
            key={f}
            x1={padLeft}
            x2={width - padRight}
            y1={padTop + f * innerH}
            y2={padTop + f * innerH}
            className="chart-grid-line"
          />
        ))}
        {showArea && (
          <path
            d={areaPath(prepared[0])}
            style={{ fill: prepared[0].color, fillOpacity: 0.14, stroke: 'none' }}
          />
        )}
        {prepared.map((s) => (
          <path
            key={`${s.name}-line`}
            d={linePath(s)}
            style={{
              fill: 'none',
              stroke: s.color,
              strokeWidth: 2.25,
              strokeLinecap: 'round',
              strokeLinejoin: 'round',
            }}
          />
        ))}
        {prepared.map((s) => {
          const lastIndex = s.values.reduce((acc, v, i) => (v !== null ? i : acc), -1);
          if (lastIndex < 0) return null;
          return (
            <circle
              key={`${s.name}-dot`}
              cx={x(lastIndex)}
              cy={y(s, s.values[lastIndex])}
              r={3.2}
              style={{ fill: s.color }}
            />
          );
        })}
        {tickIndexes.map((i) => {
          const anchor = count <= 1 ? 'middle' : i === 0 ? 'start' : i === count - 1 ? 'end' : 'middle';
          return (
            <text key={`tick-${i}`} x={x(i)} y={height - 8} textAnchor={anchor} className="chart-axis-label">
              {safeLabels[i] ?? ''}
            </text>
          );
        })}
      </svg>
    </div>
  );
}
