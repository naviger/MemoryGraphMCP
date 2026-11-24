import { useCallback, useEffect, useState } from 'react';
import { reviewRequest } from './api.js';
import { PreviewLink } from './RecordInspector.jsx';
import { elapsedRuntime, executionSteps, patchProgress, reviewBlocked } from './executionSteps.js';

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

function ExecutionUsage({ job, phaseIndex }) {
  const attempts = job.attempts || [];
  if (!attempts.length) return <p className="muted">No usage was recorded for this execution.</p>;
  return <section aria-label="Execution token usage">
    <p><strong>Execution usage</strong> · <UsageTotals attempts={attempts} /></p>
    {PHASES[job.mode]?.map((label, phase) => {
      if (phase !== phaseIndex) return null;
      const entries = attempts.filter((attempt) => attempt.phase === phase);
      if (!entries.length) return null;
      return <details key={label} open>
        <summary>{label} attempts · <UsageTotals attempts={entries} /></summary>
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
    ) : <>{target.kind} <code>{target.commit}</code></>}</p>
  </div>;
}

function stepTitle(step) {
  if (step.phase === 'Review' && step.job.patch_execution_key) return 'Review patch';
  if (step.phase === 'Implement' && step.job.repair_review_key) return 'Fix review findings';
  return step.phase;
}

function StepTimes({ step, now, compact = false }) {
  if (compact) return <span className="execution-times compact">
    <span><span>Started</span> {stamp(step.start)}</span>
    <span><span>Ended</span> {stamp(step.end)}</span>
    <span><span>Runtime</span> {elapsedRuntime(step, now)}</span>
  </span>;
  return <dl className="execution-times">
    <div><dt>Started</dt><dd>{stamp(step.start)}</dd></div>
    <div><dt>Ended</dt><dd>{stamp(step.end)}</dd></div>
    <div><dt>Runtime</dt><dd>{elapsedRuntime(step, now)}</dd></div>
  </dl>;
}

function StepDetails({ step, now, navigate }) {
  const { job, result } = step;
  return <section className="execution-inspector" aria-label="Selected phase">
    <header>
      <h4>{stepTitle(step)}</h4>
      <p>{job.mode === 'APPLY_APPROVED' ? 'Apply approved patch' : MODES[job.mode]} · {job.agent} · {step.state}</p>
    </header>
    <StepTimes step={step} now={now} />
    <p><PreviewLink destination="entities" entityKey={job.key} navigate={navigate}>Open execution in the graph</PreviewLink></p>
    {step.current && job.failure && <p role="alert" className="status error">{job.failure}</p>}
    {step.current && job.state === 'BLOCKED' && job.lease_until && <p>Automatic retry scheduled for {stamp(job.lease_until)}.</p>}
    {result && <section>
      {result.applied_commit && <div className="status">
        <p>Local fix branch <code>{result.applied_commit.branch}</code></p>
        <p>Commit <code>{result.applied_commit.commit}</code></p>
        <p>Publishing requires separate approval.</p>
      </div>}
      {(!result.applied_commit || result.reviewed_target) && <ReviewResult result={result} navigate={navigate} />}
      {result.verification && <details open={!result.verification.passed}>
        <summary>{result.verification.passed ? 'Required Docker tests passed' : 'Required Docker tests failed'}</summary>
        <p><code>{result.verification.command}</code></p>
        <pre>{result.verification.output}</pre>
      </details>}
      {result.regression_proof && <details open={!result.regression_proof.passed}>
        <summary>{result.regression_proof.passed ? 'Regression reproduced and fixed' : 'Regression proof incomplete'}</summary>
        <p>{result.regression_proof.summary}</p>
        {[
          ['Blocked patch with the revised tests', result.regression_proof.baseline],
          ['Revised patch with the same failing tests', result.regression_proof.revision],
        ].map(([label, check]) => check && <section key={label}>
          <h5>{label}</h5>
          <p><code>{check.command}</code></p>
          <pre>{check.output}</pre>
        </section>)}
      </details>}
      <p>{result.summary}</p>
      {result.plan && <section><h5>Plan</h5><p className="execution-text">{result.plan}</p></section>}
      {result.pull_request && <p><strong>Pull request</strong> {result.pull_request}</p>}
      {result.lesson && <p>{result.lesson}</p>}
      {[
        ['Checks reported by the agent', result.tests],
        ['Files changed', result.files_modified],
        ['Review findings', result.review_comments.filter((item) => item !== result.summary)],
        ['Artifacts', result.artifacts.map((item) => `${item.name} — ${item.reference}`)],
      ].map(([title, entries]) => entries.length > 0 && <section key={title}>
        <h5>{title}</h5><ul>{entries.map((entry, index) => <li key={index}>{entry}</li>)}</ul>
      </section>)}
      {result.logs.length > 0 && <details><summary>Execution log</summary><pre>{result.logs.join('\n')}</pre></details>}
    </section>}
    <ExecutionUsage job={job} phaseIndex={step.index} />
    {step.events.length > 0 && <section>
      <h5>Phase activity</h5>
      <ol className="execution-events">{step.events.slice().reverse().map((event, index) => <li key={index}>
        <strong>{event.action === 'retry' ? 'Retry requested' : event.state}</strong>
        <span>{stamp(event.timestamp)} · {event.actor}</span>
      </li>)}</ol>
    </section>}
  </section>;
}

function TaskExecutionHistory({ task, send, close, navigate }) {
  const [jobs, setJobs] = useState(null);
  const [selection, setSelection] = useState(null);
  const [now, setNow] = useState(Date.now);
  const [error, setError] = useState('');
  const path = `/workspace/tasks/${encodeURIComponent(task.key)}/executions`;
  const reload = useCallback(() => send(path).then((result) => { setJobs(result); setError(''); }).catch((err) => setError(err.message)), [path, send]);
  useEffect(() => {
    reload();
    const timer = window.setInterval(reload, 3000);
    return () => window.clearInterval(timer);
  }, [reload]);
  const active = (jobs || []).some((job) => ACTIVE.includes(job.state));
  useEffect(() => {
    if (!active) return;
    const clock = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(clock);
  }, [active]);
  const steps = executionSteps(jobs || []);
  const progress = patchProgress(jobs || []);
  const selected = steps.find((step) => step.key === selection) || steps[0];
  return <section className="detail workspace-form">
    <div className="workspace-actions"><h3>Execution history for {task.name}</h3>
      <button className="button outlined" type="button" onClick={close}>Close</button></div>
    <p className="muted">Use Codex to implement and review this task. Record the verified outcome with Update state.</p>
    {jobs === null && !error && <p role="status">Loading execution history…</p>}
    {jobs?.length === 0 && <p>No executions yet.</p>}
    {steps.length > 0 && <div className="execution-layout">
      <ol className="execution-steps" aria-label="Execution steps, newest first">
        {steps.map((step) => {
          const { job, result } = step;
          return <li key={step.key} className="execution-step" data-selected={selected?.key === step.key}>
            <button type="button" className="execution-step-select" aria-pressed={selected?.key === step.key}
              onClick={() => setSelection(step.key)}>
              <span className="execution-step-heading"><strong>{stepTitle(step)}</strong>
                <span className={step.attention ? 'execution-badge attention' : 'execution-badge'}>{step.state}</span></span>
              {(progress.superseded.has(job.key) || progress.superseded.has(result?.reviewed_target?.execution_id)) &&
                <span className="muted">Superseded by a revised patch</span>}
              <span className="muted">{job.agent} · {job.mode === 'APPLY_APPROVED' ? 'Apply approved patch' : MODES[job.mode]}{step.attempts.length > 0 ?
                ` · ${step.attempts.length} ${step.attempts.length === 1 ? 'attempt' : 'attempts'}` : ''}</span>
              <StepTimes step={step} now={now} compact />
            </button>
          </li>;
        })}
      </ol>
      {selected && <StepDetails step={selected} now={now} navigate={navigate} />}
    </div>}
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
    {editor?.type === 'execution' && <TaskExecutionHistory key={editor.task.key} task={editor.task} send={send}
      navigate={navigate} close={() => setEditor(null)} />}
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
            Execution history
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
