import { ContextHelp } from './ContextHelp.jsx';
import { useEffect, useState } from 'react';
import { MetricChart, MetricSummary, count, shortDay } from './MetricCharts.jsx';
import { reviewRequest } from './api.js';

const format = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
const dates = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
const amount = (value) => value == null ? '—' : format.format(value);

export function TokenUsagePage({ projectKey, projects = [], token, onUnauthorized, embedded = false }) {
  const [scope, setScope] = useState(projectKey ? 'project' : 'lifetime');
  const [selected, setSelected] = useState(projectKey || '');
  const [compared, setCompared] = useState('');
  const [comparison, setComparison] = useState(null);
  const [trendMetric, setTrendMetric] = useState('context');
  const selectedProject = embedded ? projectKey : selected;
  const actualScope = embedded ? 'project' : scope === 'project' && !selectedProject ? 'lifetime' : scope;
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const remote = new AbortController();
    setData(null);
    setError('');
    const query = new URLSearchParams({ scope: actualScope });
    if (actualScope === 'project') query.set('project_key', selectedProject);
    reviewRequest(`/token-usage?${query}`, { token, signal: remote.signal })
      .then((result) => { if (!remote.signal.aborted) setData(result); })
      .catch((failure) => {
        if (remote.signal.aborted) return;
        if (failure.status === 401) onUnauthorized();
        else setError(failure.message);
      });
    return () => remote.abort();
  }, [actualScope, selectedProject, token, onUnauthorized, refresh]);

  useEffect(() => {
    setComparison(null);
    if (!compared || actualScope !== 'project' || compared === selectedProject) return;
    const remote = new AbortController();
    const query = new URLSearchParams({ scope: 'project', project_key: compared });
    reviewRequest(`/token-usage?${query}`, { token, signal: remote.signal })
      .then((result) => { if (!remote.signal.aborted) setComparison(result); })
      .catch((failure) => {
        if (remote.signal.aborted) return;
        if (failure.status === 401) onUnauthorized();
        else setError(failure.message);
      });
    return () => remote.abort();
  }, [actualScope, compared, selectedProject, token, onUnauthorized, refresh]);

  const insights = data && [
    data.requests ? `${count(data.requests)} retrieval requests used about ${count(data.average_context_tokens)} context tokens each on average.`
      : 'No retrieval requests have been recorded for this scope.',
    data.total_savings == null ? 'There is no full-document baseline to estimate savings for these requests.'
      : `Comparable requests used ${count(Math.abs(data.total_savings))} ${data.total_savings < 0 ? 'more' : 'fewer'} estimated tokens in total. The comparison covers ${count(data.measured_savings_requests)} of ${count(data.requests)} requests.`,
    data.retry_reduction_estimate == null ? 'Both graph and document retrieval need reported outcomes before retries can be compared.'
      : `Reported graph retrieval averaged ${count(Math.abs(data.retry_reduction_estimate))} ${data.retry_reduction_estimate < 0 ? 'more' : 'fewer'} retries per request than document retrieval.`,
  ];
  const dailyRows = (series) => (series?.trend ?? []).slice(-14).map((day) => ({
    key: day.date, label: shortDay(day.date), value: trendMetric === 'savings'
      ? (day.measured_savings_requests ? day.savings_tokens : null)
      : trendMetric === 'requests' ? day.requests : (day.requests ? day.context_tokens / day.requests : null),
  }));
  const chartRows = dailyRows(data);
  const compareRows = dailyRows(comparison);
  const trendScale = Math.max(1, ...[...chartRows, ...compareRows].map((row) => Math.abs(row.value ?? 0)));
  const projectName = (key) => projects.find((item) => item.key === key)?.name ?? 'Selected project';

  return <>
    <header className="page-heading">
      <div><div className="heading-with-help">
        {embedded ? <h2>Token usage</h2> : <h1>Token usage</h1>}
        <ContextHelp topic="token-usage" /></div>
        <p>See how much context requests used and when they saved tokens.</p></div>
      <div className="toolbar">
        {!embedded && <label>Scope <select value={actualScope} onChange={(event) => { if (!selected && projects.length) setSelected(projects[0].key); setScope(event.target.value); }}>
          <option value="session">Current session</option>
          <option value="project" disabled={!projects.length && !selectedProject}>By project</option>
          <option value="lifetime">Lifetime</option>
        </select></label>}
        {!embedded && actualScope === 'project' && projects.length > 0 && <>
          <label>Project <select value={selectedProject} onChange={(event) => setSelected(event.target.value)}>
            {projects.map((item) => <option value={item.key} key={item.key}>{item.name}</option>)}
          </select></label>
          <label>Compare with <select value={compared} onChange={(event) => setCompared(event.target.value)}>
            <option value="">No comparison</option>
            {projects.filter((item) => item.key !== selectedProject)
              .map((item) => <option value={item.key} key={item.key}>{item.name}</option>)}
          </select></label>
        </>}
        <button type="button" className="button outlined" onClick={() => setRefresh((previous) => previous + 1)}>Refresh</button>
      </div>
    </header>
    {error ? <p role="alert" className="status error">{error}</p> : !data ? <p role="status">Loading token usage…</p> : <>
      <MetricSummary metrics={[
        ['Requests', count(data.requests)], ['Typical context', data.requests ? count(data.average_context_tokens, ' tokens') : '—'],
        [data.total_savings < 0 ? 'Estimated extra tokens' : 'Estimated tokens saved', count(data.total_savings == null ? null : Math.abs(data.total_savings))],
      ]} insights={insights} />
      <div className="metric-grid">
        <section className="detail metric-section">
          <label className="metric-control">Trend metric <select value={trendMetric} onChange={(event) => setTrendMetric(event.target.value)}>
            <option value="context">Average context</option><option value="savings">Net token savings</option><option value="requests">Requests</option>
          </select></label>
          <MetricChart compact title="Daily trend" description={trendMetric === 'savings'
            ? 'Positive bars show tokens saved. Negative bars show extra tokens.' : 'Latest recorded days, in UTC.'}
            rows={chartRows} limit={trendScale} signed={trendMetric === 'savings'} />
        </section>
        <MetricChart title="How requests retrieved evidence" rows={['Graph', 'ContextPackage', 'Semantic', 'Hybrid', 'Lexical'].map(
          (method) => ({ label: method === 'ContextPackage' ? 'Context package' : method, value: data.methods[method] ?? 0 }),
        )} />
      </div>
      {comparison && <div className="metric-grid">
        <MetricChart title={`Daily trend for ${projectName(compared)}`}
          description="Uses the same scale as the primary daily trend."
          rows={compareRows} limit={trendScale} signed={trendMetric === 'savings'} />
        <MetricChart title="Average context by project" description="Estimated tokens per request." rows={[
          { label: projectName(selectedProject), value: data.requests ? data.average_context_tokens : null },
          { label: projectName(compared), value: comparison.requests ? comparison.average_context_tokens : null },
        ]} />
        <MetricChart title="Net token savings by project" signed rows={[
          { label: projectName(selectedProject), value: data.total_savings },
          { label: projectName(compared), value: comparison.total_savings },
        ]} />
      </div>}
      <details className="detail metric-section"><summary>Detailed measurements</summary>
      <dl className="facts usage-facts">
        {[
          ['Query tokens', data.total_query_tokens], ['Context tokens', data.total_context_tokens],
          ['Reported response tokens', data.reported_outcomes ? data.total_reported_response_tokens : null],
          ['Total known tokens', data.total_tokens], ['Reported completions', data.successful_completions],
          ['Average reported retries', data.average_follow_up_retries],
        ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{count(value)}</dd></div>)}
      </dl>
      {data.trend.length > 0 && <section className="detail table-scroll usage-section">
        <h2>Daily history</h2>
        <table className="confidence-history">
          <thead><tr><th scope="col">Date (UTC)</th><th scope="col">Requests</th>
            <th scope="col">Average context</th><th scope="col">Estimated savings</th></tr></thead>
          <tbody>{data.trend.map((day) => <tr key={day.date}><td>{day.date}</td><td>{amount(day.requests)}</td>
            <td>{amount(day.context_tokens / day.requests)}</td>
            <td>{amount(day.measured_savings_requests ? day.savings_tokens : null)}</td></tr>)}</tbody>
        </table>
      </section>}
      {data.recent.length > 0 && <section className="detail table-scroll usage-section">
        <h2>Recent requests</h2>
        <table className="confidence-history">
          <thead><tr><th scope="col">Time</th><th scope="col">Method and path</th><th scope="col">Query</th>
            <th scope="col">Context</th><th scope="col">Response</th><th scope="col">Total</th>
            <th scope="col">Baseline</th><th scope="col">Savings</th><th scope="col">Entities</th></tr></thead>
          <tbody>{data.recent.map((row) => <tr key={row.request_id}><td>{dates.format(new Date(row.timestamp))}</td>
            <td>{row.method}<p className="muted">{row.retrieval_path}</p></td>
            {[row.query_tokens, row.context_tokens, row.response_tokens, row.total_tokens, row.naive_rag_tokens,
              row.savings_tokens, row.entities_supplied].map((value, index) => <td key={index}>{amount(value)}</td>)}</tr>)}</tbody>
        </table>
      </section>}
      </details>
    </>}
  </>;
}
