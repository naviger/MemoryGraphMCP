import test from 'node:test';
import assert from 'node:assert/strict';
import { elapsedRuntime, executionSteps } from '../src/executionSteps.js';

const stamp = (minute) => `2026-11-23T19:${String(minute).padStart(2, '0')}:00Z`;
const event = (state, phase, minute) => ({ state, phase, timestamp: stamp(minute) });
const run = (overrides) => ({
  key: 'one', mode: 'FULL_LIFECYCLE', phase: 0, state: 'READY', created_at: stamp(0),
  timeline: [], results: [], attempts: [], ...overrides,
});

test('orders actual phases newest first and exposes the blocking review', () => {
  const [review, implement, plan] = executionSteps([run({
    phase: 3, state: 'REVIEW',
    timeline: [
      event('RUNNING', 0, 1), event('READY', 0, 2),
      event('RUNNING', 1, 3), event('READY', 1, 5),
      event('RUNNING', 2, 6), event('REVIEW', 2, 7),
    ],
    results: [{}, { completed: true }, { review_verdict: 'blocking' }],
  })]);
  assert.deepEqual([review.phase, implement.phase, plan.phase], ['Review', 'Implement', 'Plan']);
  assert.equal(review.state, 'Changes required');
  assert.equal(review.attention, true);
  assert.equal(elapsedRuntime(implement, Date.parse(stamp(8))), '2m 0s');
});

test('live runtime sums attempts without counting retry backoff', () => {
  const [step] = executionSteps([run({
    mode: 'IMPLEMENT', state: 'RUNNING',
    attempts: [
      { phase: 0, started_at: stamp(1), ended_at: stamp(2), state: 'failed' },
      { phase: 0, started_at: stamp(6), ended_at: null, state: 'running' },
    ],
  })]);
  assert.equal(step.start, stamp(1));
  assert.equal(step.end, null);
  assert.equal(elapsedRuntime(step, Date.parse(stamp(7))), '2m 0s');
});

test('does not display a review phase that never ran after implementation stopped', () => {
  const steps = executionSteps([run({
    phase: 2, state: 'REVIEW', results: [{}, { completed: false }],
    timeline: [event('RUNNING', 0, 1), event('READY', 0, 2), event('RUNNING', 1, 3), event('REVIEW', 1, 4)],
  })]);
  assert.deepEqual(steps.map((step) => step.phase), ['Implement', 'Plan']);
});
