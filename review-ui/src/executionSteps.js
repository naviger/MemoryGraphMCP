export const PHASES = {
  PLAN_ONLY: ['Plan'], IMPLEMENT: ['Implement'], REVIEW: ['Review'],
  FULL_LIFECYCLE: ['Plan', 'Implement', 'Review'],
  APPLY_APPROVED: ['Apply patch', 'Review commit'],
};

export function reviewBlocked(result) {
  return result?.review_verdict === 'blocking' ||
    (!result?.review_verdict && /^blocking finding:/i.test(result?.summary || ''));
}

const activeStates = new Set(['READY', 'RUNNING', 'BLOCKED', 'CANCEL_REQUESTED']);
const endedStates = new Set(['BLOCKED', 'COMPLETED', 'PLANNED', 'REVIEW', 'FAILED', 'CANCELLED']);

export function executionSteps(jobs) {
  const steps = jobs.flatMap((job) => (PHASES[job.mode] || []).flatMap((phase, index) => {
    const result = job.results[index];
    const current = index === job.phase && !result;
    const future = index > job.phase;
    if (future && !activeStates.has(job.state)) return [];
    const events = job.timeline.filter((event) => (event.phase ?? 0) === index);
    const attempts = (job.attempts || []).filter((attempt) => attempt.phase === index);
    if (!result && index > 0 && !events.length && !attempts.length && !activeStates.has(job.state)) return [];
    const start = attempts[0]?.started_at || events.find((event) => event.state === 'RUNNING')?.timestamp;
    const lastEvent = events.at(-1);
    const lastAttempt = attempts.at(-1);
    const end = lastAttempt ? lastAttempt.ended_at : start && (
      endedStates.has(lastEvent?.state) || (result && lastEvent?.state === 'READY')
    ) ? lastEvent.timestamp : undefined;
    const state = reviewBlocked(result) ? 'Changes required'
      : result ? 'Finished'
        : current ? ({
          READY: 'Queued', RUNNING: 'Running', BLOCKED: 'Waiting to retry',
          CANCEL_REQUESTED: 'Cancelling', CANCELLED: 'Cancelled', FAILED: 'Failed',
        }[job.state] || job.state) : 'Waiting';
    return [{
      key: `${job.key}/${index}`, job, phase, index, result, current, state, events, attempts, start, end,
      attention: reviewBlocked(result) || (current && ['FAILED', 'BLOCKED'].includes(job.state)),
      sortTime: Date.parse(start || (current ? lastEvent?.timestamp || job.created_at : result ? job.created_at : '')),
    }];
  }));
  return steps.sort((left, right) =>
    (Number.isFinite(right.sortTime) ? right.sortTime : -Infinity) -
    (Number.isFinite(left.sortTime) ? left.sortTime : -Infinity) ||
    Date.parse(right.job.created_at) - Date.parse(left.job.created_at) || right.index - left.index);
}

export function elapsedRuntime(step, now) {
  const ranges = step.attempts.length ? step.attempts.map((attempt) => [
    attempt.started_at, attempt.ended_at || (attempt.state === 'running' ? now : null),
  ]) : [[step.start, step.end || (step.state === 'Running' ? now : null)]];
  const durations = ranges.map(([start, end]) => start && end
    ? Math.max(0, new Date(end).valueOf() - Date.parse(start)) : null);
  if (durations.every((value) => value == null)) return '—';
  const seconds = Math.floor(durations.reduce((sum, value) => sum + (value || 0), 0) / 1000);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return [hours ? `${hours}h` : '', hours || minutes ? `${minutes}m` : '', `${seconds % 60}s`].filter(Boolean).join(' ');
}


export function patchProgress(jobs) {
  const superseded = new Set();
  const reviewed = new Set();
  const latestReview = new Map();
  const applications = new Set();
  const newestFirst = [...jobs].sort((left, right) =>
    Date.parse(right.created_at) - Date.parse(left.created_at));
  for (const job of newestFirst) {
    if (job.approved_review_key && job.patch_execution_key) applications.add(job.patch_execution_key);
    const hasPatch = job.results.some((result) =>
      result.artifacts?.some((artifact) => artifact.name === 'Local implementation patch'));
    if (job.repair_review_key && job.patch_execution_key && hasPatch) {
      superseded.add(job.patch_execution_key);
    }
    for (const result of job.results) {
      if (result.reviewed_target?.kind !== 'implementation patch' || !result.review_verdict) continue;
      const patch = result.reviewed_target.execution_id;
      reviewed.add(patch);
      if (!latestReview.has(patch)) latestReview.set(patch, job.key);
    }
  }
  return { superseded, reviewed, latestReview, applications };
}
