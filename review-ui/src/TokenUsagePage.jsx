import { ContextHelp } from './ContextHelp.jsx';
import { useEffect, useState } from 'react';
import { reviewRequest } from './api.js';

const format = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
const dates = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
const amount = (value) => value == null ? '—' : format.format(value);

export function TokenUsagePage({ projectKey, token, onUnauthorized }) {
  const [scope, setScope] = useState(projectKey ? 'project' : 'lifetime');
  const actualScope = scope === 'project' && !projectKey ? 'lifetime' : scope;
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const remote = new AbortController();
    setData(null);
    setError('');
    const query = new URLSearchParams({ scope: actualScope });
    if (actualScope === 'project') query.set('project_key', projectKey);
    reviewRequest(`/token-usage?${query}`, { token, signal: remote.signal })
      .then((result) => { if (!remote.signal.aborted) setData(result); })
      .catch((failure) => {
        if (remote.signal.aborted) return;
        if (failure.status === 401) onUnauthorized();
        else setError(failure.message);
      });
    return () => remote.abort();
  }, [actualScope, projectKey, token, onUnauthorized, refresh]);

  return <>
    <header className="page-heading">
      <div><div className="heading-with-help"><h1>Token usage</h1><ContextHelp topic="token-usage" /></div><p>Compare context size, estimated savings, and reported retrieval outcomes.</p></div>
      <div className="toolbar">
        <label>Scope <select value={actualScope} onChange={(event) => setScope(event.target.value)}>
          <option value="session">Current session</option>
          <option value="project" disabled={!projectKey}>Selected project</option>
          <option value="lifetime">Lifetime</option>
        </select></label>
        <button type="button" className="button outlined" onClick={() => setRefresh((previous) => previous + 1)}>Refresh</button>
      </div>
    </header>
    {error ? <p role="alert" className="status error">{error}</p> : !data ? <p role="status">Loading token usage…</p> : <>
      <section className="detail usage-section">
        <h2>Overview</h2>
        <dl className="facts usage-facts">
          {[
            ['Requests', data.requests], ['Average context', data.average_context_tokens],
            ['Average estimated savings', data.average_savings], ['Estimated savings total', data.total_savings],
            ['Estimated query tokens', data.total_query_tokens], ['Estimated context tokens', data.total_context_tokens],
            ['Reported response tokens', data.reported_outcomes > 0 ? data.total_reported_response_tokens : null],
            ['Total known tokens', data.total_tokens], ['Reported completions', data.successful_completions],
            ['Average reported follow-up retries', data.average_follow_up_retries],
            ['Estimated fewer retries per graph request', data.retry_reduction_estimate],
          ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{amount(value)}</dd></div>)}
        </dl>
        <p className="muted">{data.measured_savings_requests} requests had a comparable document baseline. Retry reduction compares client-reported graph and document retrieval outcomes.</p>
      </section>
      <section className="detail usage-section">
        <h2>Retrieval methods</h2>
        <dl className="facts usage-facts">{['Graph', 'ContextPackage', 'Semantic', 'Hybrid', 'Lexical'].map((method) =>
          <div key={method}><dt>{method === 'ContextPackage' ? 'Context package' : method}</dt>
            <dd>{amount(data.methods[method] ?? 0)}</dd></div>)}</dl>
      </section>
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
    </>}
  </>;
}
