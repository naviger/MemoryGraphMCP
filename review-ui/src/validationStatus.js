const todayUtc = () => new Date().toISOString().slice(0, 10);

export function validationStatus(record, asOf = todayUtc()) {
  if (!record.last_validated || record.last_validated > asOf) return 'Never validated';
  if (!record.next_review_due) return 'Review date unknown';
  return record.next_review_due < asOf ? 'Due for review' : 'Current';
}

export function validityStatus(record, asOf = todayUtc()) {
  if (record.expiration_date && record.expiration_date <= asOf) return 'Expired';
  if (record.effective_date && record.effective_date > asOf) return 'Not yet effective';
  return null;
}
