import { useCallback, useEffect, useState } from 'react';
import { reviewRequest } from './api.js';
import { PreviewLink } from './RecordInspector.jsx';

const TYPES = { Task: 'Tasks', Goal: 'Goals', Risk: 'Risks', Constraint: 'Constraints' };
const STATES = { open: 'Open', in_progress: 'In progress', done: 'Completed', cancelled: 'Cancelled' };
const MODES = { PLAN_ONLY: 'Plan', IMPLEMENT: 'Implement', REVIEW: 'Review checkout', FULL_LIFECYCLE: 'Full lifecycle' };
const ACTIVE = ['READY', 'RUNNING', 'BLOCKED', 'CANCEL_REQUESTED'];
const EMPTY = { name: '', description: '', scope: '', authority: '' };
const stamp = (date) => date ? new Date(date).toLocaleString() : '—';

function WorkEditor({ kind, send, close, draftAvailable, refresh }) {
  const [fields, setFields] = useState(EMPTY);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const update = (name) => (event) => setFields({ ...fields, [name]: event.target.value });
  async function action(assist) {
    setError('');
    setBusy(assist ? 'Drafting…' : 'Saving…');
    try {
      if (assist) {
        const result = await send('/workspace/draft', { kind, brief: [fields.name, fields.description].filter(Boolean).join('\n') });
        setFields({ ...EMPTY, ...result, scope: result.scope || '', authority: result.authority || '' });
      } else {
        await send('/workspace', { kind, ...fields, scope: fields.scope || null, authority: fields.authority || null });
        refresh();
        close();
      }
    } catch (err) { setError(err.message); }
    finally { setBusy(''); }
  }
  return <form className="detail workspace-form" onSubmit={(event) => { event.preventDefault(); action(false); }}>
    <h3>New {kind.toLowerCase()}</h3>
    <label>Name<input maxLength={500} value={fields.name} onChange={update('name')} required /></label>
    <label>{kind === 'Constraint' ? 'Rule' : 'Description'}
      <textarea value={fields.description} onChange={update('description')} rows={4} maxLength={kind === 'Constraint' ? 4000 : 8000} required />
    </label>
    {kind === 'Constraint' && <>
      <label>Scope<input maxLength={500} value={fields.scope} onChange={update('scope')} required /></label>
      <label>Authority<input maxLength={500} value={fields.authority} onChange={update('authority')} required /></label>
    </>}
    <div className="workspace-actions">
      <button className="button primary" disabled={!!busy}>Save {kind.toLowerCase()}</button>
      {draftAvailable && <button className="button outlined" type="button"
        disabled={!!busy || !(fields.name.trim() || fields.description.trim())} onClick={() => action(true)}>Help me draft</button>}
      <button className="button outlined" type="button" disabled={!!busy} onClick={close}>Close</button>
    </div>
    {busy && <p role="status">{busy}</p>}
    {error && <p role="alert" className="status error">{error}</p>}
  </form>;
}

const PHASES = {
  PLAN_ONLY: ['Plan'], IMPLEMENT: ['Implement'], REVIEW: ['Review'],
  FULL_LIFECYCLE: ['Plan', 'Implement', 'Review'],
};

const number = (value) => value.toLocaleString();
const dollars = (value) => value.toLocaleString(undefined, {
  style: 'currency', currency: 'USD', maximumFractionDigits: 6,
});

function UsageTotals({ attempts }) {
  const finished = attempts.filter((attempt) => attempt.ended_at);
  const reported = finished.map((attempt) => attempt.usage).filter(Boolean);
  const priced = reported.filter((usage) => usage.estimated_cost_usd != null);
  const total = reported.reduce((sum, usage) => sum + usage.input_tokens + usage.output_tokens, 0);
  const cost = priced.reduce((sum, usage) => sum + usage.estimated_cost_usd, 0);
  if (!finished.length) return <span>Usage pending</span>;
  return <span>
    {reported.length ? `${number(total)} reported tokens` : 'Tokens unavailable'}
    {reported.length > 0 && reported.length < finished.length ? ` (${reported.length} of ${finished.length} attempts)` : ''}
    {' · '}{priced.length ? `${dollars(cost)} estimated` : 'Cost unavailable'}
    {priced.length > 0 && priced.length < finished.length ? ` (${priced.length} of ${finished.length} attempts)` : ''}
  </span>;
}

function ExecutionUsage({ job }) {
  const attempts = job.attempts || [];
  if (!attempts.length) return <p className="muted">No usage was recorded for this execution.</p>;
  return <section aria-label="Execution token usage">
    <p><strong>Execution usage</strong> · <UsageTotals attempts={attempts} /></p>
    {PHASES[job.mode]?.map((label, phase) => {
      const entries = attempts.filter((attempt) => attempt.phase === phase);
      if (!entries.length) return null;
      return <details key={label}>
        <summary>{label} · <UsageTotals attempts={entries} /></summary>
        <div className="table-scroll"><table className="change-table">
          <thead><tr><th>Attempt</th><th>Outcome</th><th>Input</th><th>Cached input</th><th>Output</th><th>Estimated cost</th></tr></thead>
          <tbody>{entries.map((entry, index) => <tr key={entry.started_at}>
            <td>{index + 1} · {stamp(entry.started_at)}</td><td>{entry.state}{entry.failure && <p>{entry.failure}</p>}</td>
            <td>{entry.usage ? number(entry.usage.input_tokens) : '—'}</td>
            <td>{entry.usage?.cached_input_tokens != null ? number(entry.usage.cached_input_tokens) : '—'}</td>
            <td>{entry.usage ? number(entry.usage.output_tokens) : '—'}</td>
            <td>{entry.usage?.estimated_cost_usd != null ? dollars(entry.usage.estimated_cost_usd) : 'Unavailable'}</td>
          </tr>)}</tbody>
        </table></div>
      </details>;
    })}
    <p className="muted">Totals include reported usage from failed attempts. Cached tokens are part of input tokens.</p>
  </section>;
}

const reviewBlocked = (result) => result.review_verdict === 'blocking' ||
  (!result.review_verdict && /^blocking finding:/i.test(result.summary || ''));

function ReviewResult({ result, navigate }) {
  const target = result.reviewed_target;
  if (!target) return reviewBlocked(result) ? <div className="status error">
    <strong>Changes required — review blocked</strong><p>This older result did not record the reviewed target.</p>
  </div> : null;
  return <div className={result.review_verdict === 'blocking' ? 'status error' : 'status'}>
    <strong>{result.review_verdict === 'blocking' ? 'Changes required — review blocked' : 'Review passed'}</strong>
    <p>Reviewed {target.kind === 'implementation patch' ? (
      <>the patch from <PreviewLink destination="entities" entityKey={target.execution_id} navigate={navigate}>
        execution {target.execution_id.slice(0, 12)}
      </PreviewLink><br />Base commit <code>{target.base_commit}</code><br />Patch SHA-256 <code>{target.patch_sha256}</code></>
    ) : <>checkout commit <code>{target.commit}</code></>}</p>
  </div>;
}

function TaskExecution({ task, send, agents, close, navigate, canWrite, draftAvailable }) {
  const [jobs, setJobs] = useState(null);
  const [agent, setAgent] = useState(Object.keys(agents)[0] || '');
  const [mode, setMode] = useState('');
  const [recommendation, setRecommendation] = useState(null);
  const [requested, setRequested] = useState(() => crypto.randomUUID());
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const path = `/workspace/tasks/${encodeURIComponent(task.key)}/executions`;
  const reload = useCallback(() => send(path).then(setJobs).catch((err) => setError(err.message)), [path, send]);
  useEffect(() => {
    reload();
    const timer = window.setInterval(reload, 3000);
    return () => window.clearInterval(timer);
  }, [reload]);
  const active = (jobs || []).some((job) => ACTIVE.includes(job.state));
  const workflows = Object.keys(MODES).filter((value) => Object.values(agents).some((allowed) => allowed.includes(value)));
  const selectedMode = workflows.includes(mode) ? mode : workflows[0] || '';
  const eligible = Object.keys(agents).filter((value) => agents[value].includes(selectedMode));
  const selectedAgent = eligible.includes(agent) ? agent : eligible[0] || '';
  async function recommend() {
    setBusy(true); setError(''); setRecommendation(null);
    try {
      const choice = await send(`/workspace/tasks/${task.key}/recommend-agent`, { mode: selectedMode });
      setAgent(choice.agent);
      setRecommendation(choice);
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  async function submit(job, action = 'cancel') {
    setBusy(true);
    setError('');
    try {
      if (action === 'review-patch') {
        await send(path, { request_id: requested, agent: 'codex', mode: 'REVIEW', patch_execution_key: job.key });
        setRequested(crypto.randomUUID());
      } else if (job) await send(`${path}/${job.key}/${action}`, {});
      else {
        await send(path, { request_id: requested, agent: selectedAgent, mode: selectedMode });
        setRequested(crypto.randomUUID());
      }
      await reload();
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  return <section className="detail workspace-form">
    <div className="workspace-actions"><h3>{task.name}</h3>
      <button className="button outlined" type="button" onClick={close}>Close</button></div>
    {canWrite && !active && !['done', 'cancelled'].includes(task.status) && (selectedAgent ? (
      <form className="workspace-form" onSubmit={(event) => { event.preventDefault(); submit(); }}>
        <div className="workspace-actions">
          <label>Workflow<select disabled={busy} value={selectedMode} onChange={(event) => { setMode(event.target.value); setRecommendation(null); }}>
            {workflows.map((item) => <option key={item} value={item}>{MODES[item]}</option>)}
          </select></label>
          <label>Agent<select disabled={busy} value={selectedAgent} onChange={(event) => setAgent(event.target.value)}>
            {eligible.map((item) => <option key={item}>{item}</option>)}
          </select></label>
          {draftAvailable && <button className="button outlined" type="button" disabled={busy} onClick={recommend}>Recommend agent</button>}
          <button className="button primary" disabled={busy || !jobs}>Start execution</button>
        </div>
        <p>Phases: {PHASES[selectedMode]?.join(' → ')}</p>
        {selectedMode === 'REVIEW' && <p className="muted">This reviews the checkout’s latest commit. For an implementation artifact, choose Review this patch on its execution.</p>}
        {recommendation && <p role="status"><strong>Ollama recommends {recommendation.agent}.</strong> {recommendation.reason}{selectedAgent !== recommendation.agent ? ' You selected a different agent.' : ''}</p>}
      </form>
    ) : <p>No agent is configured for this project.</p>)}
    {jobs === null && !error && <p role="status">Loading execution history…</p>}
    {jobs?.length === 0 && <p>No executions yet.</p>}
    {jobs?.map((job, index) => <details key={job.key} open={index === 0 ? true : undefined}>
      <summary>{job.patch_execution_key ? 'Review patch' : MODES[job.mode]} · {job.agent} · {job.state} · {stamp(job.created_at)}{job.results.some(reviewBlocked) && <strong className="status error"> · Changes required</strong>}</summary>
      <p><PreviewLink destination="entities" entityKey={job.key} navigate={navigate}>Open execution in the graph</PreviewLink></p>
      {canWrite && job.state !== 'CANCEL_REQUESTED' && ACTIVE.includes(job.state) && <button className="button outlined" type="button" disabled={busy}
        onClick={() => submit(job)}>Cancel execution</button>}
      {job.failure && <p role="alert">{job.failure}</p>}
      {job.state === 'BLOCKED' && job.lease_until && <p>Automatic retry scheduled for {stamp(job.lease_until)}.</p>}
      {canWrite && ['BLOCKED', 'FAILED'].includes(job.state) && !['done', 'cancelled'].includes(task.status) &&
        <button className="button primary" type="button"
          disabled={busy || (job.state === 'FAILED' && active)} onClick={() => submit(job, 'retry')}>Retry now</button>}
      <ol className="workspace-actions" aria-label="Workflow phases">{PHASES[job.mode]?.map((phase, phaseIndex) => (
        <li key={phase}>{phase} · {phaseIndex < job.phase ? (job.results[phaseIndex]?.review_verdict === 'blocking' ? 'Blocked' : 'Finished') :
          phaseIndex === job.phase ? job.state : 'Waiting'}</li>
      ))}</ol>
      {canWrite && !active && task.status !== 'cancelled' && agents.codex?.includes('REVIEW') &&
        job.agent === 'codex' && job.results.some((result) => result.artifacts.some((artifact) => artifact.name === 'Local implementation patch')) &&
        <button className="button primary" type="button" disabled={busy} onClick={() => submit(job, 'review-patch')}>Review this patch</button>}
      <ExecutionUsage job={job} />
      <ol>{job.timeline.map((item, eventIndex) => <li key={eventIndex}>
        {item.action === 'retry' ? 'Retry requested' : item.state} · {stamp(item.timestamp)} · {item.actor}
      </li>)}</ol>
      {job.results.map((result, resultIndex) => <div key={resultIndex}>
        <ReviewResult result={result} navigate={navigate} />
        <p>{result.summary}</p>
        {result.plan && <p><strong>Plan</strong> {result.plan}</p>}
        {result.pull_request && <p><strong>Pull request</strong> {result.pull_request}</p>}
        {result.lesson && <p>{result.lesson}</p>}
        {result.logs.length > 0 && <details><summary>Execution log</summary><pre>{result.logs.join('\n')}</pre></details>}
        <ul>{[...result.tests, ...result.files_modified, ...result.review_comments, ...result.artifacts.map((item) => `${item.name} — ${item.reference}`)]
          .map((entry, entryIndex) => <li key={entryIndex}>{entry}</li>)}</ul>
      </div>)}
    </details>)}
    {error && <p className="status error" role="alert">{error}</p>}
  </section>;
}

function EditTask({ task, close, send, refresh }) {
  const [status, setStatus] = useState(task.status);
  const [outcome, setOutcome] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  async function save(event) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      await send(`/workspace/tasks/${task.key}`, { status, outcome: status === 'done' ? outcome : null });
      refresh();
      close();
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  return <form className="detail workspace-form" onSubmit={save}>
    <h3>Update {task.name}</h3>
    <label>State<select value={status} onChange={(event) => setStatus(event.target.value)}>
      {Object.entries(STATES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
    </select></label>
    {status === 'done' && <label>Outcome and evidence<textarea required value={outcome}
      maxLength={8000} rows={4} onChange={(event) => setOutcome(event.target.value)} /></label>}
    <div className="workspace-actions"><button className="button primary" disabled={busy}>Save state</button>
      <button type="button" className="button outlined" onClick={close}>Close</button></div>
    {error && <p role="alert" className="status error">{error}</p>}
  </form>;
}

export default function ProjectWorkspace({ projectKey, token, navigate, onUnauthorized }) {
  const [kind, setKind] = useState('Task');
  const [states, setStates] = useState(['open', 'in_progress']);
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [editor, setEditor] = useState(null);
  const send = useCallback((suffix, body) => reviewRequest(`/projects/${encodeURIComponent(projectKey)}${suffix}`, { token, body }), [projectKey, token]);
  const refresh = useCallback(() => send('/workspace').then((result) => { setData(result); setError(''); })
    .catch((err) => { if (err.status === 401) onUnauthorized(); else setError(err.message); }), [send, onUnauthorized]);
  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 10000);
    return () => window.clearInterval(timer);
  }, [refresh]);
  const items = (data?.items || []).filter((item) => item.entity_type === kind && (kind !== 'Task' || states.includes(item.status)));
  return <section aria-label="Project workspace">
    <div className="project-tabs" role="group" aria-label="Workspace views">
      {Object.entries(TYPES).map(([value, label]) => <button key={value} type="button" aria-pressed={kind === value}
        className={kind === value ? 'button primary' : 'button outlined'} onClick={() => { setKind(value); setEditor(null); }}>{label}</button>)}
    </div>
    <div className="workspace-actions">
      {kind === 'Task' && <details className="workspace-filter">
        <summary className="button outlined">State ({states.length})</summary>
        <fieldset aria-label="Filter tasks by state">{Object.entries(STATES).map(([key, label]) => <label key={key}>
          <input type="checkbox" checked={states.includes(key)} onChange={() => setStates((current) =>
            current.includes(key) ? current.filter((item) => item !== key) : [...current, key])} /> {label}
        </label>)}</fieldset>
      </details>}
      {data?.can_write && <button className="button primary" type="button" onClick={() => setEditor({ type: 'create' })}>Add {kind.toLowerCase()}</button>}
    </div>
    {editor?.type === 'create' && <WorkEditor kind={kind} send={send} refresh={refresh} draftAvailable={data.draft_available} close={() => setEditor(null)} />}
    {editor?.type === 'state' && <EditTask key={editor.task.key} task={editor.task} send={send} refresh={refresh} close={() => setEditor(null)} />}
    {editor?.type === 'execution' && <TaskExecution key={editor.task.key} task={editor.task} send={send}
      agents={data.agents} draftAvailable={data.draft_available} canWrite={data.can_write} navigate={navigate} close={() => { setEditor(null); refresh(); }} />}
    {error && <p className="status error" role="alert">{error}</p>}
    {!data && !error && <p role="status">Loading workspace…</p>}
    {data && <div className="table-scroll"><table className="change-table workspace-table">
      <thead><tr><th scope="col">{kind}</th><th scope="col">State</th>{kind === 'Task' && <th scope="col">Actions</th>}</tr></thead>
      <tbody>{items.map((item) => <tr key={item.key}>
        <td><PreviewLink destination="entities" entityKey={item.key} navigate={navigate}>{item.name}</PreviewLink></td>
        <td>{STATES[item.status] || item.status || '—'}</td>
        {kind === 'Task' && <td><div className="workspace-actions">
          {data.can_write && <button type="button" className="button outlined" onClick={() => setEditor({ type: 'state', task: item })}>Update state</button>}
          <button type="button" className="button outlined" onClick={() => setEditor({ type: 'execution', task: item })}>
            {data.can_write && !['done', 'cancelled'].includes(item.status) ? 'Execute' : 'Execution history'}
          </button>
        </div></td>}
      </tr>)}</tbody>
    </table>{items.length === 0 && <p>No {TYPES[kind].toLowerCase()} match this view.</p>}</div>}
  </section>;
}

function elapsed(value) {
  if (!value) return '—';
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(value).valueOf()) / 60000));
  return minutes < 60 ? `${minutes}m` : minutes < 1440 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${Math.floor(minutes / 1440)}d`;
}

export function CaptureStatus({ projectKey, token }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const base = `/projects/${encodeURIComponent(projectKey)}`;
  const reload = useCallback(() => reviewRequest(`${base}/capture`, { token }).then(setData)
    .catch((err) => setError(err.message)), [base, token]);
  const [write, setWrite] = useState(false);
  useEffect(() => {
    reload();
    reviewRequest(`${base}/workspace`, { token }).then((result) => setWrite(result.can_write)).catch(() => {});
    const timer = window.setInterval(reload, 12000);
    return () => window.clearInterval(timer);
  }, [base, reload, token]);
  async function schedule(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true); setError(''); setNotice('');
    try {
      await reviewRequest(`${base}/capture`, { token, body: {
        stream: form.get('stream'), action: form.get('action'), since: new Date(form.get('since')).toISOString(),
      } });
      setNotice('History scan queued.');
      await reload();
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  return <details className="detail">
    <summary>Knowledge capture {data?.streams.some((stream) => stream.failure) ? ' · needs attention' : ''}</summary>
    {error && <p className="status error" role="alert">{error}</p>}
    {!data ? <p>Loading capture status…</p> : !data.configured ? <p>Capture is not configured for this project.</p> : <>
      <p>Poll interval {data.interval_seconds} seconds. Lag shows pending items or a newer source cursor. Poll age shows time since the last successful check.</p>
      <div className="table-scroll"><table className="change-table">
        <thead><tr><th>Source</th><th>Last successful check</th><th>Poll age</th><th>Source cursor</th><th>Lag</th><th>Status</th></tr></thead>
        <tbody>{data.streams.map((item) => <tr key={item.stream}>
          <td>{item.stream}</td><td>{stamp(item.last_success)}</td><td>{elapsed(item.last_success)}</td>
          <td title={item.cursor || undefined}>{stamp(item.source_timestamp)}</td>
          <td>{item.pending_count != null ? `${item.pending_count} pending` :
            item.observed_head && item.observed_head !== item.cursor ? 'Behind latest source cursor' : '—'}
            {item.oldest_pending && <div>Oldest {elapsed(item.oldest_pending)}</div>}</td>
          <td>{item.failure || (item.last_success ? 'Healthy' : 'Waiting for first check')}
            {item.last_failure && <div>Last failed {stamp(item.last_failure)}</div>}
            {item.retry_at && <div>Retry {stamp(item.retry_at)}</div>}
            {item.last_history_completed && <div>Last {item.last_history_action} finished {stamp(item.last_history_completed)}</div>}
            {item.history && <div>{item.history} queued from {stamp(item.history_since)}</div>}
          </td>
        </tr>)}</tbody>
      </table></div>
      {write && data.streams.some((item) => item.stream === 'commits') && <form className="workspace-actions" onSubmit={schedule}>
        <label>Source<select name="stream"><option value="commits">Commits</option><option value="pulls">Pull requests</option></select></label>
        <label>Action<select name="action"><option value="replay">Replay</option><option value="backfill">Backfill</option></select></label>
        <label>Since<input type="date" name="since" required /></label>
        <button className="button outlined" disabled={busy}>Queue history scan</button>
      </form>}
      {notice && <p role="status">{notice}</p>}
    </>}
  </details>;
}
