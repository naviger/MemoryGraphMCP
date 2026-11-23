import { useEffect, useState } from 'react';
import { MetricChart, MetricSummary, count, shortDay } from './MetricCharts.jsx';
import { reviewRequest } from './api.js';

const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
const money = new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 6 });
const amount = (value, suffix = '') => value == null ? '—' : `${number.format(value)}${suffix}`;
const emptyScope = (project = '') => ({ project_key: project, repository_key: '', start: '', end: '' });

function ScopeFields({ title, scope, projects, change, token, onUnauthorized, pinned = false }) {
  const [repositories, setRepositories] = useState({});
  const [error, setError] = useState('');
  useEffect(() => {
    setRepositories({});
    setError('');
    if (!scope.project_key) return;
    const remote = new AbortController();
    const query = new URLSearchParams({ project_key: scope.project_key });
    reviewRequest(`/effectiveness/repositories?${query}`, { token, signal: remote.signal })
      .then((result) => { if (!remote.signal.aborted) setRepositories(result); })
      .catch((failure) => {
        if (remote.signal.aborted) return;
        if (failure.status === 401) onUnauthorized();
        else setError(failure.message);
      });
    return () => remote.abort();
  }, [scope.project_key, token, onUnauthorized]);

  const set = (field) => (event) => change({ ...scope, [field]: event.target.value });
  return <fieldset className="effectiveness-scope">
    <legend>{title}</legend>
    {!pinned && <label>Project <select required value={scope.project_key}
      onChange={(event) => change({ ...scope, project_key: event.target.value, repository_key: '' })}>
      <option value="">Choose a project</option>
      {projects.map((project) => <option key={project.key} value={project.key}>{project.name}</option>)}
    </select></label>}
    <label>Repository <select value={scope.repository_key} onChange={set('repository_key')}>
      <option value="">All repositories</option>
      {Object.entries(repositories).map(([key, name]) => <option key={key} value={key}>{name}</option>)}
    </select></label>
    <label>From <input type="date" value={scope.start} max={scope.end || undefined} onChange={set('start')} /></label>
    <label>Through <input type="date" value={scope.end} min={scope.start || undefined} onChange={set('end')} /></label>
    {error && <p role="alert">{error}</p>}
  </fieldset>;
}

function ScopeHeading({ report, label }) {
  return <><strong>{label}</strong><p>{report.project_name}{report.repository_name && ` / ${report.repository_name}`}</p>
    <p className="muted">{report.start ?? 'All history'}{report.end ? ` through ${report.end}` : report.start ? ' onward' : ''}</p></>;
}

function HealthScore({ score }) {
  return <>
    <strong>{amount(score.percent, '%')}</strong>
    {score.percent != null && <meter aria-label="Score" min="0" max="100" value={score.percent} />}
    <span className="muted">{score.numerator} of {score.denominator}</span>
  </>;
}

function EstimateCell({ estimate, format }) {
  return <>
    <strong>{estimate.value == null ? '—' : format(estimate.value)}</strong>
    <p className="muted">{estimate.confidence == null ? 'Confidence unavailable' : `Confidence ${amount(estimate.confidence * 100, '%')}`}
      {estimate.samples > 0 && ` · ${estimate.samples} evidence items`}</p>
  </>;
}

const INVENTORY = [
  ['ADRs', 'adrs'], ['Constraints', 'constraints'], ['Goals', 'goals'],
  ['Observations', 'observations'], ['Architecture entities', 'entities'], ['Architecture links', 'links'],
];
const DELIVERY = [
  ['Tasks completed', 'tasks_completed'], ['Tasks abandoned', 'tasks_abandoned'],
  ['Currently completed tasks', 'current_completed_tasks'], ['Currently abandoned tasks', 'current_abandoned_tasks'],
  ['Agent-generated changes', 'agent_changes'], ['Human-generated changes', 'human_changes'],
  ['Agent first-pass success', 'first_pass_percent', '%', 'first_pass_samples'],
  ['Agent retry count', 'retries', '', 'retry_samples'],
  ['Agent retry rate', 'retry_percent', '%', 'retry_samples'],
  ['Average task completion', 'average_task_completion_hours', ' h', 'timed_tasks'],
];
const ARCHITECTURE = [
  ['ADR conflicts detected', 'adr_conflicts_detected'], ['Unresolved conflicts', 'unresolved_adr_conflicts'],
  ['ADR conflicts resolved', 'adr_conflicts_resolved'], ['Reported constraint violations', 'constraint_violations'],
  ['Active constraint coverage', 'constraint_coverage'], ['Services linked to ADRs', 'services_linked_to_adrs'],
  ['Tests linked to constraints', 'tests_linked_to_constraints'],
];
const CAPTURE = [
  ['Auto-generated observations', 'automatic_observations'], ['Manual observations', 'manual_observations'],
  ['Inferred links', 'inferred_links'], ['Pending link suggestions', 'pending_link_suggestions'],
  ['Explicit links', 'explicit_links'], ['Stale entities', 'stale_entities'], ['Revalidated records', 'revalidated_entities'],
];
const ECONOMICS = [
  ['Token savings', 'token_savings', amount],
  ['Context reduction', 'context_reduction_percent', (value) => amount(value, '%')],
  ['Model costs avoided', 'model_cost_avoided', (value) => money.format(value)],
  ...['Engineering', 'Rework', 'Review', 'Onboarding'].map((label) => [
    `${label} hours avoided`, `${label.toLowerCase()}_hours_avoided`, (value) => amount(value, ' h'),
  ]),
];

function MetricsTable({ title, reports, rows, render }) {
  return <section className="detail table-scroll effectiveness-metrics">
    <h2>{title}</h2>
    <table className="confidence-history">
      <thead><tr><th scope="col">Metric</th>{reports.map((report, index) =>
        <th scope="col" key={index}><ScopeHeading report={report} label={index ? 'Comparison' : 'Primary'} /></th>)}</tr></thead>
      <tbody>{rows.map(([label, key, ...settings]) => <tr key={key}>
        <th scope="row">{label}</th>
        {reports.map((report, index) => <td key={index}>{render(report, key, ...settings)}</td>)}
      </tr>)}</tbody>
    </table>
  </section>;
}

export function EffectivenessPage({ projectKey, projects, token, onUnauthorized, embedded = false }) {
  const [primary, setPrimary] = useState(() => emptyScope(projectKey || projects[0]?.key));
  const [comparison, setComparison] = useState(null);
  const [price, setPrice] = useState('');
  const [trendMetric, setTrendMetric] = useState('tasks_completed');
  const [submitted, setSubmitted] = useState(() => ({ primary }));
  const [reports, setReports] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!submitted.primary.project_key) return;
    const remote = new AbortController();
    setReports(null);
    setError('');
    const scopes = [submitted.primary, submitted.comparison].filter(Boolean);
    Promise.all(scopes.map((scope) => {
      const entries = Object.entries({ ...scope, input_price_per_million: submitted.price })
        .filter(([, value]) => value != null && value !== '');
      return reviewRequest(`/effectiveness?${new URLSearchParams(entries)}`, { token, signal: remote.signal });
    })).then((values) => { if (!remote.signal.aborted) setReports(values); }).catch((failure) => {
      if (remote.signal.aborted) return;
      if (failure.status === 401) onUnauthorized();
      else setError(failure.message);
    });
    return () => remote.abort();
  }, [submitted, token, onUnauthorized]);

  const report = reports?.[0];
  const insights = report && [
    report.delivery.first_pass_samples
      ? `About ${count(report.delivery.first_pass_percent, '%')} of agent changes with check evidence passed on the first attempt (${count(report.delivery.first_pass_samples)} changes measured).`
      : 'First-pass success needs reported outcomes or imported CI linked to agent changes.',
    report.health.traceability_score.denominator
      ? `${report.health.traceability_score.numerator} of ${report.health.traceability_score.denominator} services and active constraints have the expected ADR or test link.`
      : 'Traceability will appear when this scope has services or active constraints.',
    report.economics.token_savings.value == null
      ? 'There are no comparable token measurements for this period.'
      : `Comparable requests ${report.economics.token_savings.value < 0 ? 'used' : 'saved'} about ${count(Math.abs(report.economics.token_savings.value))} ${report.economics.token_savings.value < 0 ? 'extra ' : ''}tokens across ${count(report.economics.token_savings.samples)} measurements.`,
  ];
  const scopeLabel = (scope, index) => reports[0].project_key === reports[1]?.project_key
    ? (scope.repository_name || (index ? 'Comparison period' : 'Primary period')) : scope.project_name;

  return <>
    <header className="page-heading"><div>{embedded ? <h2>Engineering effectiveness</h2> : <h1>Engineering effectiveness</h1>}
      <p>Compare delivery outcomes, architectural coverage, and estimated cost avoidance.</p></div></header>
    <form className="detail effectiveness-filters" onSubmit={(event) => {
      event.preventDefault();
      setSubmitted({ primary, comparison, price });
    }}>
      <ScopeFields title={embedded ? "This project" : "Primary"} pinned={embedded} scope={primary} change={setPrimary} {...{ projects, token, onUnauthorized }} />
      {comparison && <ScopeFields title="Comparison" pinned={embedded} scope={comparison} change={setComparison}
        {...{ projects, token, onUnauthorized }} />}
      <div className="toolbar">
        <label>Input price (USD per million tokens)
          <input type="number" min="0" max="10000" step="any" value={price} placeholder="Optional"
            onChange={(event) => setPrice(event.target.value)} />
        </label>
        <button type="button" className="button outlined"
          onClick={() => setComparison(comparison ? null : { ...primary })}>
          {comparison ? 'Remove comparison' : 'Add comparison'}
        </button>
        <button type="submit" className="button primary" disabled={!primary.project_key}>Apply filters</button>
      </div>
    </form>
    {error ? <p role="alert" className="status error">{error}</p> : !reports ? (
      <p role="status">{submitted.primary.project_key ? 'Loading engineering evidence…' : 'Choose a project to view its metrics.'}</p>
    ) : <>
      <MetricSummary metrics={[
        ['Completed tasks', count(report.delivery.tasks_completed)],
        ['Agent first-pass success', count(report.delivery.first_pass_percent, '%')],
        ['Traceability', count(report.health.traceability_score.percent, '%')],
      ]} insights={insights} />
      <div className="metric-grid">
        <section className="detail metric-section">
          <label className="metric-control">Weekly measure <select value={trendMetric} onChange={(event) => setTrendMetric(event.target.value)}>
            <option value="tasks_completed">Completed tasks</option>
            <option value="agent_changes">Agent changes</option>
            <option value="human_changes">Human changes</option>
          </select></label>
          <MetricChart compact title="Weekly delivery" description="Latest weeks with recorded delivery events. Weeks start Monday, UTC."
            rows={(report.delivery_trend ?? []).map((week) => ({ label: shortDay(week.week_start), value: week[trendMetric] }))} />
        </section>
        <MetricChart title="Current graph health" unit="%" limit={100}
          description="A blank score means this scope has no eligible evidence."
          rows={[['Memory', 'memory_score'], ['Traceability', 'traceability_score'], ['Validation', 'validation_score']]
            .map(([label, key]) => ({ label, value: report.health[key].percent }))} />
      </div>
      {reports.length > 1 && <div className="metric-grid">
        <MetricChart title="First-pass success by scope" unit="%" limit={100}
          rows={reports.map((scope, index) => ({ key: index, label: scopeLabel(scope, index), value: scope.delivery.first_pass_percent }))} />
        <MetricChart title="Completed tasks by scope"
          rows={reports.map((scope, index) => ({ key: index, label: scopeLabel(scope, index), value: scope.delivery.tasks_completed }))} />
      </div>}
      <details className="detail metric-section"><summary>Detailed metrics and methodology</summary>
      <MetricsTable title="Health" reports={reports}
        rows={[[ 'Memory score', 'memory_score' ], [ 'Traceability score', 'traceability_score' ], [ 'Validation score', 'validation_score' ]]}
        render={(report, key) => <HealthScore score={report.health[key]} />} />
      <MetricsTable title="Agent effectiveness and delivery" reports={reports} rows={DELIVERY}
        render={(report, key, suffix, samples) => <>
          <strong>{amount(report.delivery[key], suffix)}</strong>
          {samples && <p className="muted">{report.delivery[samples]} reported outcomes</p>}
        </>} />
      <MetricsTable title="Cost avoidance estimates" reports={reports} rows={ECONOMICS}
        render={(report, key, format) => <EstimateCell estimate={report.economics[key]} format={format} />} />
      <MetricsTable title="Memory usage" reports={reports} rows={INVENTORY} render={(report, key) => amount(report.inventory[key])} />
      <MetricsTable title="Architecture evidence" reports={reports} rows={ARCHITECTURE}
        render={(report, key) => key === 'constraint_coverage' ? <HealthScore score={report.architecture[key]} />
          : amount(report.architecture[key])} />
      <MetricsTable title="Knowledge capture" reports={reports} rows={CAPTURE} render={(report, key) => amount(report.capture[key])} />
      <details className="detail effectiveness-metrics">
        <summary>Methodology and evidence limits</summary>
        <dl className="effectiveness-methodology">
          {Object.entries(reports[0].methodology).map(([name, text]) =>
            <div key={name}><dt>{name}</dt><dd>{text}</dd></div>)}
          {ECONOMICS.map(([label, key]) => <div key={key}><dt>{label}</dt><dd>{reports[0].economics[key].methodology}</dd></div>)}
        </dl>
      </details>
      </details>
    </>}
  </>;
}
