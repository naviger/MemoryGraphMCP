import { useEffect, useId, useRef, useState } from 'react';
import { reviewRequest } from './api.js';
import { DetailLink } from './Details.jsx';

const timestamp = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
const runStatus = { passed: 'Passed', failed: 'Failed', cancelled: 'Cancelled' };
const buildStatus = { passed: 'Passed', failed: 'Failed', skipped: 'Skipped' };
const healthLabels = { green: 'Healthy', yellow: 'Warnings', red: 'Failing', unknown: 'Unknown' };
const freshnessLabels = {
  current: 'Latest commit tested',
  stale: 'CI stale',
  unverified: 'No test result for latest commit',
  unknown: 'Freshness unknown',
};
const exceptionDescriptions = {
  AssertionError: 'An assertion failed.',
  InvalidAudienceError: 'The token audience did not match the verifier configuration.',
};

function PatchDiff({ diff }) {
  let inHunk = false;
  return <pre className="patch-diff" aria-label="Patch diff"><code>{diff.split('\n').map((line, index) => {
    if (line.startsWith('diff --git ')) inHunk = false;
    if (line.startsWith('@@ ')) inHunk = true;
    const tone = inHunk && { '+': 'patch-added', '-': 'patch-removed' }[line[0]];
    return <span className={tone || undefined} key={index}>{index > 0 ? '\n' : ''}{line}</span>;
  })}</code></pre>;
}

const fixSteps = [
  'Issue', 'Suggested fix', 'Draft patch', 'Check draft',
  'Publish PR', 'Merge PR', 'Verify merged fix', 'Delete branch',
];

function FixWait({ children }) {
  return <p className="fix-wait" role="status">
    <span className="fix-spinner" aria-hidden="true" />{children}
  </p>;
}

function FixTimeline({ evidence, projectKey, runId, index, result, token, onUnauthorized, issueType, active, onClose, onCompletion }) {
  const [workflow, setWorkflow] = useState(null);
  const [selected, setSelected] = useState(0);
  const [pending, setPending] = useState('');
  const [error, setError] = useState('');
  const [confirming, setConfirming] = useState(false);
  const [confirmation, setConfirmation] = useState('');
  const id = useId();
  const base = `/projects/${encodeURIComponent(projectKey)}/testing`;
  const query = new URLSearchParams({ run_id: runId, failure_index: String(index), issue_type: issueType });
  const workflowPath = `${base}/fix-workflow?${query}`;

  useEffect(() => {
    if (!active || !result.can_review_fix) return;
    const controller = new AbortController();
    reviewRequest(workflowPath, { token, signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        setWorkflow(data);
        onCompletion(Boolean(data.fix_branch ? data.branch_deleted_at : data.completed_at));
        setSelected((current) => {
          if (current !== 0) return current;
          if (data.confirming_run_id) return 7;
          if (data.publication?.state === 'merged') return 6;
          if (data.publication) return 5;
          return data.fix_branch ? 4 : current;
        });
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        if (err.status === 401) onUnauthorized();
        else setError(err.message);
      });
    return () => controller.abort();
  }, [active, result.can_review_fix, workflowPath, token, onUnauthorized, onCompletion]);

  const { suggestion, draft, suggested_at: suggestedAt, drafted_at: draftedAt,
    verified_at: verifiedAt, applied_at: appliedAt, post_verification: postVerification,
    post_verified_at: postVerifiedAt, completed_at: completedAt, completed_by: completedBy,
    fix_branch: fix, publication, confirming_run_id: confirmingRun, branch_deleted_at: branchDeletedAt,
    merge_ci_run_id: mergeRun, merge_ci_status: mergeStatus } = workflow ?? {};
  const waitingForCI = selected === 6 && Boolean(publication?.merge_commit) && !confirmingRun && !mergeStatus;

  useEffect(() => {
    if (!active || !waitingForCI || pending) return;
    const controller = new AbortController();
    let timer;
    async function checkCI() {
      try {
        const saved = await reviewRequest(`${base}/refresh-fix`, {
          token, signal: controller.signal,
          body: { run_id: runId, failure_index: index, issue_type: issueType },
        });
        if (controller.signal.aborted) return;
        setWorkflow(saved);
        if (saved.confirming_run_id) setSelected(7);
        if (saved.confirming_run_id || saved.merge_ci_status) return;
      } catch (err) {
        if (controller.signal.aborted) return;
        if (err.status === 401) { onUnauthorized(); return; }
      }
      timer = setTimeout(checkCI, 15000);
    }
    checkCI();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [active, waitingForCI, pending, base, runId, index, issueType, token, onUnauthorized]);

  if (!result.can_review_fix) return evidence;
  const busy = Boolean(pending) || !workflow;

  async function perform(action, tab, rerun = false) {
    setError('');
    setPending(action);
    const issue = { run_id: runId, failure_index: index, issue_type: issueType };
    const applying = action === 'apply';
    const path = applying ? `drafts/${draft.draft_id}/apply` : action;
    const approving = action === 'publish-fix' || action.startsWith('finish-fix/');
    const body = applying ? { ...issue, approve: true }
      : approving ? { ...issue, approve: true, commit: fix.commit }
      : { ...issue, rerun };
    try {
      await reviewRequest(`${base}/${path}`, { token, body });
      const saved = await reviewRequest(workflowPath, { token });
      setWorkflow(saved);
      onCompletion(Boolean(saved.fix_branch ? saved.branch_deleted_at : saved.completed_at));
      setConfirmation('');
      setConfirming(false);
      if (action === 'refresh-fix' && saved.confirming_run_id) setSelected(7);
      else if (action === 'refresh-fix' && saved.publication?.state === 'merged') setSelected(6);
      else setSelected(tab);
      if (action === 'complete-fix') { onCompletion(true); onClose(); }
    } catch (err) {
      if (err.status === 401) onUnauthorized();
      else setError(err.message);
    } finally {
      setPending('');
    }
  }

  function actionButton(label, action, target, disabled, rerun = false) {
    return <button className="button outlined" type="button" disabled={busy || disabled}
      onClick={() => perform(action, target, rerun)}>{label}</button>;
  }

  function approvalButton(label, prompt, action, target) {
    if (!result.can_publish_fix) return null;
    return confirmation === action ? <>
      <p>{prompt}</p>
      {actionButton(`Confirm ${label.toLowerCase()}`, action, target, false)}
      <button type="button" className="button outlined" disabled={busy}
        onClick={() => setConfirmation('')}>Cancel</button>
    </> : <button type="button" className="button primary" disabled={busy}
      onClick={() => setConfirmation(action)}>{label}</button>;
  }

  const suggest = (rerun = false) => actionButton(
    rerun ? 'Regenerate suggestion' : 'Suggest fix', 'suggest-fix', 1, !result.can_suggest_fix, rerun,
  );
  const draftFix = (rerun = false) => actionButton(
    rerun ? 'Regenerate draft' : 'Draft this fix', 'draft-fix', 2, !result.can_draft_fix, rerun,
  );
  const dateTime = (value) => value && <time dateTime={value}>{timestamp.format(new Date(value))}</time>;

  const runLink = (value) =>
    `${publication.url.split('/pull/')[0]}/actions/runs/${value.replace(':', '/attempts/')}`;

  return <section className="test-fix draft-fix">
    <div className="project-tabs" role="tablist" aria-label="Fix timeline" onKeyDown={(event) => {
      const next = { ArrowRight: (selected + 1) % fixSteps.length,
        ArrowLeft: (selected + fixSteps.length - 1) % fixSteps.length, Home: 0,
        End: fixSteps.length - 1 }[event.key];
      if (next === undefined) return;
      event.preventDefault();
      setSelected(next);
      event.currentTarget.querySelectorAll('[role="tab"]')[next].focus();
    }}>
      {fixSteps.map((label, step) => <button key={label} type="button" role="tab" id={`${id}-tab-${step}`}
        aria-controls={`${id}-panel-${step}`} aria-selected={selected === step}
        tabIndex={selected === step ? 0 : -1} onClick={() => setSelected(step)}>{label}</button>)}
    </div>
    {active && !workflow && !error && <FixWait>Loading saved fix…</FixWait>}
    {pending && <FixWait>
      {{ 'suggest-fix': 'Generating suggestion…', 'draft-fix': 'Drafting and verifying…',
        'verify-fix': 'Rerunning checks on the saved patch…', apply: 'Applying, checking, and committing on the fix branch…',
        'post-verify-fix': 'Checking the applied patch…', 'complete-fix': 'Saving completion…',
        'publish-fix': 'Publishing the approved commit…', 'refresh-fix': 'Checking GitHub and imported CI…',
        'finish-fix/merge': 'Requesting a squash merge…',
        'finish-fix/delete-branch': 'Deleting the remote fix branch…' }[pending]}
    </FixWait>}
    {error && <p role="alert" className="status error">{error}</p>}
    {[
      <>{evidence}{suggestion ? <button type="button" className="button outlined" onClick={() => setSelected(1)}>
        View saved suggestion
      </button> : result.can_suggest_fix && suggest()}</>,
      suggestion ? <>
        <p className="muted">Saved {dateTime(suggestedAt)}</p>
        <dl>
          <div><dt>Likely cause</dt><dd>{suggestion.cause}</dd></div>
          <div><dt>Proposed fix</dt><dd>{suggestion.proposed_fix}</dd></div>
          <div><dt>How to verify</dt><dd>{suggestion.verification}</dd></div>
        </dl>
        {suggestion.source_paths.length > 0 && <p>Source at the tested commit: <code>{suggestion.source_paths.join(', ')}</code></p>}
        {suggestion.evidence_gaps.map((gap) => <p className="muted" key={gap}>{gap}</p>)}
        {!appliedAt && suggest(true)}
        {draft ? <button type="button" className="button outlined" onClick={() => setSelected(2)}>View saved patch</button>
          : draftFix()}
      </> : <><p className="muted">No saved suggestion.</p>{suggest()}</>,
      draft ? <>
        <p className="muted">Saved {dateTime(draftedAt)}</p>
        <PatchDiff diff={draft.diff} />
        <button className="button outlined" type="button" onClick={() => setSelected(3)}>Review draft checks</button>
        {!appliedAt && suggestion && draftFix(true)}
      </> : <><p className="muted">No saved patch.</p>{suggestion ? draftFix() : <button type="button"
        className="button outlined" onClick={() => setSelected(1)}>Go to suggested fix</button>}</>,
      draft ? <>
        <p>Both checks ran in an isolated worktree at the CI commit.</p>
        <p className="muted">Checked {dateTime(verifiedAt)} · {draft.verified ? 'Verified' : 'Not verified'}</p>
        {[
          [draft.baseline, 'Original CI commit'],
          [draft.verification, 'CI commit with draft applied'],
        ].map(([check, label]) => (
          <details key={label}>
            <summary>{label} · {check.passed ? 'Check passed' : 'Check failed'}</summary>
            <p><code>{check.command}</code></p><pre>{check.output}</pre>
          </details>
        ))}
        {fix ? <>
          <p role="status">Applied to <code>{fix.branch}</code> {dateTime(appliedAt)}.</p>
          <button className="button outlined" type="button" onClick={() => setSelected(4)}>Go to Publish PR</button>
        </> : <>
          {appliedAt ? <p>This draft was previously applied to the local checkout. You can still prepare it on a fix branch.</p>
            : actionButton('Rerun pre-checks', 'verify-fix', 3, !result.can_draft_fix)}
          {draft.verified && !result.can_apply_draft && <p className="muted">Applying requires writer or admin access and a configured local worker.</p>}
          {draft.verified && result.can_apply_draft && (confirming ? <div>
            <p>Apply this patch in isolation on a new fix branch, then check and commit it?</p>
            {actionButton('Confirm branch and apply', 'apply', 4, false)}
            <button className="button outlined" type="button" disabled={busy}
              onClick={() => setConfirming(false)}>Cancel</button>
          </div> : <button className="button primary" type="button" disabled={busy}
            onClick={() => setConfirming(true)}>Apply to fix branch</button>)}
        </>}
      </> : <p className="muted">Pre-checks run when a draft is created.</p>,
      appliedAt ? <>
        {fix ? <p>Checked on <code>{fix.branch}</code> at target commit <code>{fix.base_commit.slice(0, 12)}</code>.
          Verified fix commit <code>{fix.commit}</code>.</p>
          : <p>This patch was applied using the earlier local-checkout workflow.</p>}
        {postVerification && <>
          <p role="status">{postVerification.passed ? 'Fix branch check passed' : 'Fix branch check failed'} · {dateTime(postVerifiedAt)}</p>
          <details>
            <summary>Check output for {postVerification.commit.slice(0, 12)}</summary>
            <p><code>{postVerification.check.command}</code></p><pre>{postVerification.check.output}</pre>
          </details>
        </>}
        {fix ? publication ? <button type="button" className="button outlined" onClick={() => setSelected(5)}>
          Go to Merge PR
        </button> : result.can_publish_fix
          ? approvalButton('Push and open PR', 'Publish this exact verified commit to GitHub?', 'publish-fix', 5)
          : <p>Publishing requires writer or admin access and a configured GitHub write credential.</p>
          : completedAt ? <p role="status">Completed {dateTime(completedAt)} by {completedBy}.</p> : <>
          {result.can_complete_fix && actionButton(postVerification ? 'Rerun post-check' : 'Run post-check', 'post-verify-fix', 4, false)}
          {result.can_complete_fix && postVerification?.passed && actionButton('Mark complete', 'complete-fix', 4, false)}
        </>}
      </> : <p className="muted">Apply and verify the patch from Check draft before publishing the PR.</p>,
      publication ? <>
        <p><a href={publication.url} target="_blank" rel="noreferrer">Pull request #{publication.number}</a>
          {' · '}{publication.state === 'merged' ? 'Merged and closed' : publication.state === 'closed' ? 'Closed without merge' : 'Open'}</p>
        <p>Fix commit <code>{fix.commit}</code></p>
        {publication.state === 'open' && approvalButton(
          'Merge pull request', 'GitHub will combine the PR changes into one commit on the target branch and close the PR.', 'finish-fix/merge', 6,
        )}
        {publication.state === 'merged' ? <button type="button" className="button outlined"
          onClick={() => setSelected(6)}>Verify merged fix</button>
          : actionButton('Refresh PR status', 'refresh-fix', 5, false)}
      </> : <p className="muted">Open the PR from Publish PR. Then return here to review and merge it.</p>,
      publication?.merge_commit ? <>
        <p>Merge commit <code>{publication.merge_commit}</code></p>
        {confirmingRun ? <>
          <p role="status">Confirmed in CI · <a target="_blank" rel="noreferrer"
            href={runLink(confirmingRun)}>
            Run {confirmingRun}</a></p>
          <button type="button" className="button outlined" onClick={() => setSelected(7)}>Go to Delete branch</button>
        </> : <>
          {mergeStatus ? <p role="status">
            {mergeStatus === 'failed' ? 'CI failed on the merge commit.' : 'CI did not confirm the fix.'}{' '}
            <a href={runLink(mergeRun)} target="_blank" rel="noreferrer">View GitHub Actions run</a>
          </p> : !pending && <FixWait>Waiting for the GitHub Actions result. Checking every 15 seconds.</FixWait>}
          {actionButton('Check CI now', 'refresh-fix', 6, false)}
        </>}
      </> : <p className="muted">Merge the pull request to check CI on the target branch.</p>,
      branchDeletedAt ? <p role="status">Complete · Remote branch deleted {dateTime(branchDeletedAt)}.</p>
        : confirmingRun ? <>
          <p>GitHub CI confirmed the fix. The remote branch <code>{fix.branch}</code> can now be removed.</p>
          {approvalButton('Delete remote branch', 'Delete the remote fix branch at the approved commit?', 'finish-fix/delete-branch', 7)}
        </> : <p className="muted">Branch cleanup is available after CI confirms the merge.</p>,
    ].map((panel, step) => <div key={fixSteps[step]} role="tabpanel" id={`${id}-panel-${step}`}
      aria-labelledby={`${id}-tab-${step}`} hidden={selected !== step} aria-busy={busy} className="fix-step">{panel}</div>)}
  </section>;
}

function TimelineIssue({ summary, children, ...props }) {
  const [active, setActive] = useState(false);
  const [opened, setOpened] = useState(false);
  const [completed, setCompleted] = useState(false);
  const disclosure = useRef(null);
  return <details ref={disclosure} onToggle={(event) => {
    setActive(event.currentTarget.open);
    if (event.currentTarget.open) setOpened(true);
  }}>
    <summary>{summary}{completed && ' · Completed'}</summary>
    {opened && <FixTimeline {...props} active={active} evidence={children} onCompletion={setCompleted} onClose={() => {
      disclosure.current.open = false;
      disclosure.current.querySelector('summary').focus();
    }} />}
  </details>;
}

function RunHistory({ endpoint, token, active, onUnauthorized, onSelect, selected }) {
  const [history, setHistory] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const activeRequest = useRef(null);

  function load(cursor = null) {
    const controller = new AbortController();
    activeRequest.current?.abort();
    activeRequest.current = controller;
    setError('');
    setLoading(true);
    const params = cursor ? `?cursor=${encodeURIComponent(cursor)}` : '';
    reviewRequest(`${endpoint}/history${params}`, { token, signal: controller.signal })
      .then((page) => {
        if (controller.signal.aborted) return;
        setHistory((previous) => ({
          runs: cursor ? [...previous.runs, ...page.runs] : page.runs,
          next: page.next_cursor,
        }));
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        if (err.status === 401) onUnauthorized();
        else setError(err.message);
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
  }

  useEffect(() => {
    if (!active) return;
    load();
    return () => activeRequest.current?.abort();
  }, [active, endpoint, token, onUnauthorized]);

  return <section className="detail test-run-history" aria-label="CI run history">
    <h3>Run history</h3>
    {error && <p role="alert" className="status error">{error}</p>}
    {loading && <p role="status">Loading run history…</p>}
    {history?.runs.length === 0 && <p className="muted">No imported runs.</p>}
    {Boolean(history?.runs.length) && <div className="table-scroll">
      <table className="test-run-history-table">
        <thead><tr>
          <th scope="col">Finished</th><th scope="col">Attempt</th><th scope="col">Commit</th>
          <th scope="col">Status</th><th scope="col">Build</th><th scope="col">Passed</th>
          <th scope="col">Failed</th><th scope="col">Skipped</th><th scope="col">Warnings</th>
        </tr></thead>
        <tbody>{history.runs.map((run, index) => (
          <tr key={run.run_id}>
            <th scope="row"><button className="button outlined" type="button"
              aria-pressed={(selected ?? history.runs[0].run_id) === run.run_id}
              onClick={() => onSelect(index === 0 ? null : run.run_id)}>
              <time dateTime={run.finished_at}>{timestamp.format(new Date(run.finished_at))}</time>
            </button></th>
            <td>{run.run_id.split(':')[1]}</td>
            <td><code title={run.commit}>{run.commit.slice(0, 12)}</code></td>
            <td>{run.build_status === 'failed' ? 'Build failed' : runStatus[run.status]}</td>
            <td>{buildStatus[run.build_status] ?? '—'}</td>
            <td>{run.passed}</td><td>{run.failed}</td><td>{run.skipped}</td>
            <td>{run.warning_count ?? '—'}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>}
    {history?.next && <button className="button outlined" type="button" disabled={loading}
      onClick={() => load(history.next)}>Load older runs</button>}
  </section>;
}

function RecurringFailures({ endpoint, token, active, onUnauthorized, onSelect, selected }) {
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    setError('');
    reviewRequest(`${endpoint}/recurring-failures`, { token, signal: controller.signal })
      .then((data) => { if (!controller.signal.aborted) setResult(data); })
      .catch((err) => {
        if (controller.signal.aborted) return;
        if (err.status === 401) onUnauthorized();
        else setError(err.message);
      });
    return () => controller.abort();
  }, [active, endpoint, token, onUnauthorized]);

  return <section className="detail recurring-test-failures" aria-label="Recurring failures">
    <h3>Recurring failures</h3>
    {error ? <p role="alert" className="status error">{error}</p> : !result
      ? <p role="status">Loading recurring failures…</p> : <>
        <p className="muted">Most recent imported attempts examined: {result.attempts_examined} (up to {result.window_size}).</p>
        {result.failures.length === 0 ? <p className="muted">No tests failed in multiple attempts.</p>
          : <ul className="project-test-failures">
            {result.failures.map((failure) => (
              <li key={JSON.stringify([failure.name, failure.source_path])}>
                <details>
                  <summary>{failure.name} · {failure.attempts.length} attempts</summary>
                  {failure.source_path && <p><code>{failure.source_path}</code></p>}
                  <ul className="recurring-test-attempts">{failure.attempts.map((attempt) => {
                    const [run, number] = attempt.run_id.split(':');
                    return <li key={attempt.run_id}>
                      <button type="button" className="button outlined" onClick={() => onSelect(attempt.run_id)}
                        aria-pressed={selected === attempt.run_id}>
                        Run {run}, attempt {number} · <time dateTime={attempt.finished_at}>
                          {timestamp.format(new Date(attempt.finished_at))}
                        </time>
                      </button>
                    </li>;
                  })}</ul>
                </details>
              </li>
            ))}
          </ul>}
      </>}
  </section>;
}

export function ProjectTesting({ projectKey, token, navigate, onUnauthorized, active }) {
  const [selectedRun, setSelectedRun] = useState(null);
  const endpoint = `/projects/${encodeURIComponent(projectKey)}/testing`;

  return <>
    <RunPanel endpoint={endpoint} projectKey={projectKey} token={token}
      navigate={navigate} active={active} onUnauthorized={onUnauthorized} onSelectRun={setSelectedRun} />
    <RunHistory endpoint={endpoint} token={token} active={active} onUnauthorized={onUnauthorized}
      onSelect={setSelectedRun} selected={selectedRun} />
    <RecurringFailures endpoint={endpoint} token={token} active={active} onUnauthorized={onUnauthorized}
      onSelect={setSelectedRun} selected={selectedRun} />
    {selectedRun && <RunPanel key={selectedRun} endpoint={endpoint} projectKey={projectKey}
      runId={selectedRun} token={token} navigate={navigate} active={active}
      onUnauthorized={onUnauthorized} />}
  </>;
}

export function ProjectHealth({ projectKey, token, compact = false }) {
  const [result, setResult] = useState(null);
  useEffect(() => {
    const controller = new AbortController();
    setResult(null);
    function refreshHealth() {
      reviewRequest(`/projects/${encodeURIComponent(projectKey)}/testing/health`, {
        token, signal: controller.signal,
      }).then((data) => {
        if (!controller.signal.aborted) setResult(data);
      }).catch(() => {
        if (!controller.signal.aborted) setResult({ health: 'unknown', freshness: 'unknown' });
      });
    }
    refreshHealth();
    const timer = window.setInterval(refreshHealth, 120_000);
    return () => {
      window.clearInterval(timer);
      controller.abort();
    };
  }, [projectKey, token]);
  if (!result) return <span className="muted">Checking CI…</span>;
  const { health, freshness, head_commit: head, run_commit: imported } = result;
  const commits = [head && `Branch head: ${head}`, imported && `Imported attempt: ${imported}`]
    .filter(Boolean).join('\n');
  return <div className="project-health-summary" title={commits || undefined}>
    <span className="project-health" data-health={health}>
      <span aria-hidden="true" /> {compact ? '' : 'CI health · '}{healthLabels[health]}
    </span>
    <small className={freshness === 'stale' ? 'project-health-stale' : 'muted'}>
      {freshness === 'unknown' && !imported ? 'No known CI result' : freshnessLabels[freshness]}
    </small>
  </div>;
}

function RunPanel({ endpoint, projectKey, runId, token, navigate, active, onUnauthorized, onSelectRun }) {
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    setResult(null);
    setError('');
    const params = runId ? `?run_id=${encodeURIComponent(runId)}` : '';
    reviewRequest(endpoint + params, {
      token, signal: controller.signal,
    }).then((data) => {
      if (!controller.signal.aborted) setResult(data);
    }).catch((err) => {
      if (controller.signal.aborted) return;
      if (err.status === 401) onUnauthorized();
      else setError(err.message);
    });
    return () => controller.abort();
  }, [active, endpoint, runId, token, onUnauthorized]);

  if (error) return <p role="alert" className="status error">{error}</p>;
  if (!result) return <p role="status">Loading run details…</p>;
  return <RunDetails result={result} latest={!runId} projectKey={projectKey}
    token={token} navigate={navigate} onUnauthorized={onUnauthorized} onSelectRun={onSelectRun} />;
}

function LastPassing({ passing, latestRunId, onSelectRun }) {
  if (!passing) return <span className="muted">No passing run recorded</span>;
  const finished = <time dateTime={passing.finished_at}>{timestamp.format(new Date(passing.finished_at))}</time>;
  if (passing.run_url) {
    return <a href={passing.run_url} target="_blank" rel="noreferrer" aria-label={`Open last passing attempt on GitHub, ${timestamp.format(new Date(passing.finished_at))}`}>
      {finished}
    </a>;
  }
  if (passing.run_id === latestRunId) return finished;
  return <button type="button" className="button outlined" onClick={() => onSelectRun(passing.run_id)}>
    {finished}
  </button>;
}

function RunDetails({ result, latest, projectKey, token, navigate, onUnauthorized, onSelectRun }) {
  const run = result.latest_run;
  if (!run) return null;

  const title = latest ? 'Latest CI test run' : 'Selected CI test run';
  const facts = [
    ['Status', run.build_status === 'failed' ? 'Build failed' : runStatus[run.status]],
    ['Build', buildStatus[run.build_status] ?? 'Unrecorded'],
    ['Commit', <code title={run.commit}>{run.commit.slice(0, 12)}</code>],
    ['Finished', <time dateTime={run.finished_at}>{timestamp.format(new Date(run.finished_at))}</time>],
    ...(latest ? [['Last passing run', <LastPassing passing={result.last_passing_run}
      latestRunId={run.run_id} onSelectRun={onSelectRun} />]] : []),
    ['Passed', run.passed],
    ['Failed', run.failed],
    ['Skipped', run.skipped],
    ['Warnings', run.warning_count ?? 'Unrecorded'],
  ];

  return (
    <section className="detail test-run-details" aria-label={title}>
      <h3>{title}</h3>
      {result.run_url && <a href={result.run_url} target="_blank" rel="noreferrer">Open GitHub Actions attempt</a>}
      <dl className="facts">{facts.map(([label, value]) => (
        <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
      ))}</dl>
      {run.warning_count > 0 && <section className="record-section">
        <h4>Warnings</h4>
        {run.warnings.length < run.warning_count &&
          <p className="muted">Showing {run.warnings.length} of {run.warning_count} warnings.</p>}
        <ul className="project-test-failures">{run.warnings.map((warning, index) => (
          <li key={`${run.run_id}/warning/${index}`}>
            <TimelineIssue summary={`${warning.stage === 'build' ? 'Build' : 'Test'} · ${warning.name}`}
              projectKey={projectKey} runId={run.run_id} index={index} token={token}
              issueType="warning" result={result} onUnauthorized={onUnauthorized}>
              <p>{warning.message}</p>
              {warning.source_path && <p><code>{warning.source_path}</code></p>}
            </TimelineIssue>
          </li>
        ))}</ul>
      </section>}
      {(run.failures.length > 0 || run.status === 'failed') && (
        <section className="record-section">
          <h4>Failures</h4>
          {run.failures.length < run.failed &&
            <p className="muted">Showing {run.failures.length} of {run.failed} reported failures.</p>}
          {run.failures.length === 0 && run.failed === 0 &&
            <p className="muted">CI reported a failure without an individual test result.</p>}
          {run.failures.length > 0 && <ul className="project-test-failures">{run.failures.map((failure, index) => (
            <li key={`${run.run_id}/${failure.name}/${index}`}>
              <TimelineIssue summary={`${failure.kind === 'error' ? 'Error' : 'Failed'} · ${failure.name}`}
                projectKey={projectKey} runId={run.run_id} index={index} token={token}
                issueType="failure" result={result} onUnauthorized={onUnauthorized}>
                {failure.source_path && <p><code>{failure.source_path}</code></p>}
                {failure.exception_type && (
                  <p>
                    <code>{failure.exception_type}</code>
                    {Object.hasOwn(exceptionDescriptions, failure.exception_type) &&
                      ` · ${exceptionDescriptions[failure.exception_type]}`}
                  </p>
                )}
                {result.run_url && <p>
                  <a href={result.run_url} target="_blank" rel="noreferrer">View full failure log on GitHub</a>
                </p>}
                {failure.validations.length ? <ul>
                  {failure.validations.map((link) => (
                    <li key={link.key}>
                      {link.entity_type} · <DetailLink destination={link.memory_key ? 'records' : 'entities'}
                        entityKey={link.memory_key ?? link.key} navigate={navigate}>{link.name}</DetailLink>
                    </li>
                  ))}
                </ul> : <p className="muted">No linked ADRs or constraints for this test suite.</p>}
              </TimelineIssue>
            </li>
          ))}</ul>}
        </section>
      )}
    </section>
  );
}
