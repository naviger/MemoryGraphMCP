import { useCallback, useEffect, useState } from 'react';
import { reviewRequest } from './api.js';
import { PreviewLink } from './RecordInspector.jsx';

const TYPES = { Task: 'Tasks', Goal: 'Goals', Risk: 'Risks', Constraint: 'Constraints' };
const STATES = { open: 'Open', in_progress: 'In progress', done: 'Completed', cancelled: 'Cancelled' };
const MODES = { PLAN_ONLY: 'Plan', IMPLEMENT: 'Implement', REVIEW: 'Review', FULL_LIFECYCLE: 'Full lifecycle' };
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

function TaskExecution({ task, send, agents, close, navigate, canWrite }) {
  const [jobs, setJobs] = useState(null);
  const [agent, setAgent] = useState(Object.keys(agents)[0] || '');
  const [mode, setMode] = useState('');
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
  const selectedMode = mode || agents[agent]?.[0] || '';
  async function submit(job) {
    setBusy(true);
    setError('');
    try {
      if (job) await send(`${path}/${job.key}/cancel`, {});
      else {
        await send(path, { request_id: requested, agent, mode: selectedMode });
        setRequested(crypto.randomUUID());
      }
      await reload();
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }
  return <section className="detail workspace-form">
    <div className="workspace-actions"><h3>{task.name}</h3>
      <button className="button outlined" type="button" onClick={close}>Close</button></div>
    {canWrite && !active && !['done', 'cancelled'].includes(task.status) && (agent ? (
      <form className="workspace-actions" onSubmit={(event) => { event.preventDefault(); submit(); }}>
        <label>Agent<select value={agent} onChange={(event) => { setAgent(event.target.value); setMode(''); }}>
          {Object.keys(agents).map((item) => <option key={item}>{item}</option>)}
        </select></label>
        <label>Mode<select value={selectedMode} onChange={(event) => setMode(event.target.value)}>
          {(agents[agent] || []).map((item) => <option key={item} value={item}>{MODES[item]}</option>)}
        </select></label>
        <button className="button primary" disabled={busy || !jobs}>Start execution</button>
      </form>
    ) : <p>No agent is configured for this project.</p>)}
    {jobs === null && !error && <p role="status">Loading execution history…</p>}
    {jobs?.length === 0 && <p>No executions yet.</p>}
    {jobs?.map((job, index) => <details key={job.key} open={index === 0 ? true : undefined}>
      <summary>{MODES[job.mode]} · {job.agent} · {job.state} · {stamp(job.created_at)}</summary>
      <p><PreviewLink destination="entities" entityKey={job.key} navigate={navigate}>Open execution in the graph</PreviewLink></p>
      {canWrite && job.state !== 'CANCEL_REQUESTED' && ACTIVE.includes(job.state) && <button className="button outlined" type="button" disabled={busy}
        onClick={() => submit(job)}>Cancel execution</button>}
      {job.failure && <p role="alert">{job.failure}</p>}
      <ol>{job.timeline.map((item, eventIndex) => <li key={eventIndex}>
        {item.state} · {stamp(item.timestamp)} · {item.actor}
      </li>)}</ol>
      {job.results.map((result, resultIndex) => <div key={resultIndex}>
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
      agents={data.agents} canWrite={data.can_write} navigate={navigate} close={() => { setEditor(null); refresh(); }} />}
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
