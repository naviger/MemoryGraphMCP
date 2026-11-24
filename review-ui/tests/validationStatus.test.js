import assert from 'node:assert/strict';
import test from 'node:test';
import { validationStatus, validityStatus } from '../src/validationStatus.js';

const asOf = '2026-11-24';

test('reports an overdue review independently of confidence and expiration', () => {
  assert.equal(validationStatus({
    last_validated: '2026-08-01', next_review_due: '2026-11-01', confidence: 1,
  }, asOf), 'Due for review');
  assert.equal(validationStatus({
    last_validated: '2026-11-01', next_review_due: '2026-11-24', expiration_date: '2026-11-10',
  }, asOf), 'Current');
});

test('distinguishes never validated from a missing review schedule', () => {
  assert.equal(validationStatus({}, asOf), 'Never validated');
  assert.equal(validationStatus({ last_validated: '2026-11-01' }, asOf), 'Review date unknown');
});

test('validity marks expiration separately on its exclusive boundary', () => {
  assert.equal(validityStatus({ expiration_date: asOf }, asOf), 'Expired');
  assert.equal(validityStatus({ effective_date: '2026-11-25' }, asOf), 'Not yet effective');
  assert.equal(validityStatus({}, asOf), null);
});
