import { useId } from 'react';

const numbers = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
const dates = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });

export const count = (value, unit = '') => value == null ? '—' : `${numbers.format(value)}${unit}`;
export const shortDay = (day) => dates.format(new Date(`${day}T00:00:00Z`));

export function MetricSummary({ metrics, insights }) {
  return <section className="detail metric-section" aria-label="Summary and insights">
    <dl className="metric-summary">
      {metrics.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
    </dl>
    <h3>What stands out</h3>
    <ul className="metric-insights">{insights.map((insight) => <li key={insight}>{insight}</li>)}</ul>
  </section>;
}

export function MetricChart({ title, description, rows, unit = '', limit, signed = false, compact = false }) {
  const id = useId();
  const max = limit ?? Math.max(1, ...rows.map(({ value }) => Math.abs(value ?? 0)));
  return <figure className={compact ? 'metric-chart compact' : 'detail metric-chart'} aria-labelledby={id}>
    <figcaption><h3 id={id}>{title}</h3>{description && <p className="muted">{description}</p>}</figcaption>
    {rows.every(({ value }) => value == null) ? <p className="muted">No comparable data yet.</p> :
      <ul className={signed ? 'metric-bars signed' : 'metric-bars'}>
        {rows.map(({ key, label, value }) => <li key={key ?? label}>
          <span className="metric-label">{label}</span>
          <span className="metric-track" aria-hidden="true">
            {value != null && <span className={value < 0 ? 'metric-fill negative' : 'metric-fill'}
              style={{ width: `${Math.min(100, 100 * Math.abs(value) / max) / (signed ? 2 : 1)}%` }} />}
          </span>
          <strong className="metric-number">{count(value, unit)}</strong>
        </li>)}
      </ul>}
  </figure>;
}
