import { CalendarClock, Check, CircleAlert, FileCheck2, FileText, GitPullRequest, Link2 } from 'lucide-react';
import { useState } from 'react';
import { validationStatus, validityStatus } from './validationStatus.js';

const dateFormat = new Intl.DateTimeFormat(undefined, {
  month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC',
});

export function formatDate(date) {
  return date ? dateFormat.format(new Date(date)) : '—';
}

export function formatScore(score) {
  return score == null ? '—' : new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(score);
}

export function formatWindow(start, end) {
  return `${start ? formatDate(start) : 'Open start'} · ${end ? formatDate(end) : 'Open end'}`;
}

function citationUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
  } catch {
    return null;
  }
}

function Citations({ evidence }) {
  return (
    <ul className="citations">
      {evidence.map((citation, index) => {
        const url = citationUrl(citation);
        return (
          <li key={`${index}-${citation}`}>
            <Link2 size={15} aria-hidden="true" />
            {url
              ? <a href={url} target="_blank" rel="noopener noreferrer">{citation}</a>
              : <span>{citation}</span>}
          </li>
        );
      })}
    </ul>
  );
}

export function CodeEvidenceLink({ location, label = 'Source at' }) {
  if (!location) return null;
  const { revision, path, url } = location;
  const title = `${label} ${revision.commit.slice(0, 12)}`;
  const safeUrl = citationUrl(url);
  return safeUrl
    ? <a href={safeUrl} target="_blank" rel="noreferrer" title={path || revision.commit}>{title}</a>
    : <span title={revision.commit}>{title}</span>;
}

export function Evidence({ history, source, sourceId }) {
  return (
    <section className="evidence">
      <h3><FileText size={16} aria-hidden="true" /> Cited evidence</h3>
      {source && <Citations evidence={[source]} />}
      {sourceId && <p className="muted">Source ID {sourceId}</p>}
      {history.length === 0 && !source && !sourceId && <p className="muted">No citations recorded.</p>}
      {history.map((entry, index) => (
        <div className="evidence-entry" key={`${entry.checked_at}-${index}`}>
          <p className="muted">{entry.reviewer} · {formatDate(entry.checked_at)}</p>
          <Citations evidence={entry.evidence} />
        </div>
      ))}
    </section>
  );
}

export function ValidationEvidence({ record, asOf, latestOnly = false }) {
  const history = record.validation_history?.slice().reverse() ?? [];
  const entries = latestOnly ? history.slice(0, 1) : history;
  const citations = (record.last_validation ? [record.last_validation] : entries)
    .filter((entry) => !asOf || entry.checked_at.slice(0, 10) <= asOf);

  return (
    <section className="record-section validation-evidence">
      <h2>Confidence and validation</h2>
      <dl className="facts">
        <div><dt>Content confidence</dt><dd>{record.confidence == null ? 'Unrecorded' : formatScore(record.confidence)}</dd></div>
        <div><dt>Validation status</dt><dd>{validationStatus(record, asOf)}</dd></div>
        <div><dt>Last validated</dt><dd>{formatDate(record.last_validated)}</dd></div>
        <div><dt>Next review due</dt><dd>{formatDate(record.next_review_due)}</dd></div>
      </dl>
      <Evidence history={citations} source={record.source} sourceId={record.source_id} />
    </section>
  );
}

export function ValidityDates({ record, asOf }) {
  const status = validityStatus(record, asOf);
  return (
    <dl className="facts record-validity">
      <div><dt>Effective date</dt><dd>{formatDate(record.effective_date)}</dd></div>
      <div><dt>Expiration date</dt><dd>{formatDate(record.expiration_date)}</dd></div>
      {status && <div><dt>Validity status</dt><dd>{status}</dd></div>}
      {record.date && <div><dt>Decision date</dt><dd>{formatDate(record.date)}</dd></div>}
    </dl>
  );
}

function WriterNotice() {
  return <p className="notice"><CircleAlert size={16} aria-hidden="true" /> A writer or admin token is required to save changes.</p>;
}

function RevalidateForm({ record, busy, onSubmit }) {
  const [evidence, setEvidence] = useState('');
  const [confidence, setConfidence] = useState('');
  const citations = evidence.split('\n').map((line) => line.trim()).filter(Boolean);
  return (
    <form className="action-form" onSubmit={(event) => {
      event.preventDefault();
      if (!citations.length) return;
      onSubmit(record.key, citations, confidence === '' ? null : Number(confidence));
    }}>
      <div className="form-title"><Check size={18} aria-hidden="true" /><h3>Revalidate record</h3></div>
      <label>
        Evidence citations
        <textarea required value={evidence} onChange={(event) => setEvidence(event.target.value)}
          rows="3" placeholder="One source URL or commit reference per line" />
      </label>
      <label className="score-field">
        New confidence <span className="hint">Optional · 0 to 1</span>
        <input type="number" min="0" max="1" step="any" value={confidence}
          onChange={(event) => setConfidence(event.target.value)} placeholder={formatScore(record.confidence)} />
      </label>
      <button className="button primary" disabled={busy || !citations.length} type="submit">
        {busy ? 'Saving…' : 'Save validation'}
      </button>
    </form>
  );
}

export function DetailLink({ destination = 'records', entityKey, navigate, children, className }) {
  const href = `/${destination}/${encodeURIComponent(entityKey)}`;
  return <a className={className} href={href} onClick={(event) => navigate(event, href)}>{children}</a>;
}

export function ValidationDetail({ record, asOf, canWrite, busy, onSubmit, navigate }) {
  return (
    <article className="detail">
      <div className="detail-header">
        <div className="detail-icon amber"><CalendarClock size={23} aria-hidden="true" /></div>
        <div><p className="item-key">{record.key}</p><h2>{record.title}</h2></div>
        <DetailLink entityKey={record.key} navigate={navigate} className="button outlined open-record">Open record</DetailLink>
      </div>
      <ValidationEvidence record={record} asOf={asOf} />
      <ValidityDates record={record} asOf={asOf} />
      {canWrite ? <RevalidateForm key={record.key} record={record} busy={busy} onSubmit={onSubmit} /> : <WriterNotice />}
    </article>
  );
}

function ADRCard({ record, navigate }) {
  return (
    <section className="adr-card">
      <p className="item-key">{record.key} · {record.status}</p>
      <h3>{record.title}</h3>
      <dl className="adr-text">
        <div><dt>Decision</dt><dd>{record.decision}</dd></div>
        <div><dt>Rationale</dt><dd>{record.rationale}</dd></div>
        <div><dt>Consequences</dt><dd>{record.consequences}</dd></div>
      </dl>
      <Evidence history={record.validation_history} source={record.source} sourceId={record.source_id} />
      <DetailLink entityKey={record.key} navigate={navigate} className="card-record-link">Open ADR {record.key}</DetailLink>
    </section>
  );
}

function ResolveForm({ conflict, busy, onSubmit }) {
  const [action, setAction] = useState('coexist');
  const [superseded, setSuperseded] = useState(conflict.original.key);
  const [rationale, setRationale] = useState('');
  const original = conflict.original.key;
  const competing = conflict.competing.key;
  const source = action === 'supersede' ? superseded : original;
  const target = source === original ? competing : original;
  return (
    <form className="action-form" onSubmit={(event) => {
      event.preventDefault();
      if (!rationale.trim()) return;
      onSubmit({ source_key: source, target_key: target, action, rationale: rationale.trim() });
    }}>
      <div className="form-title"><GitPullRequest size={18} aria-hidden="true" /><h3>Resolve conflict</h3></div>
      <div className="form-columns">
        <label>Resolution
          <select value={action} onChange={(event) => setAction(event.target.value)}>
            <option value="coexist">Allow both to coexist</option>
            <option value="supersede">Supersede one ADR</option>
          </select>
        </label>
        {action === 'supersede' && (
          <label>ADR to supersede
            <select value={superseded} onChange={(event) => setSuperseded(event.target.value)}>
              <option value={original}>{original}</option>
              <option value={competing}>{competing}</option>
            </select>
          </label>
        )}
      </div>
      <label>Rationale
        <textarea required value={rationale} onChange={(event) => setRationale(event.target.value)} rows="3"
          placeholder={action === 'coexist' ? 'Explain why both decisions can apply.' : `Explain why ${target} replaces ${source}.`} />
      </label>
      <button className="button primary" disabled={busy || !rationale.trim()} type="submit">
        {busy ? 'Saving…' : 'Record resolution'}
      </button>
    </form>
  );
}

export function ConflictDetail({ conflict, canWrite, busy, onSubmit, navigate }) {
  return (
    <article className="detail">
      <div className="detail-header">
        <div className="detail-icon violet"><GitPullRequest size={23} aria-hidden="true" /></div>
        <div><p className="item-key">{conflict.original.conflict_scope?.topic || 'Architectural decisions'}</p>
          <h2>Overlapping decisions</h2></div>
      </div>
      <dl className="facts conflict-facts">
        <div><dt>Original overlap</dt><dd>{formatWindow(conflict.overlap_start, conflict.overlap_end)}</dd></div>
        <div><dt>System</dt><dd>{conflict.original.conflict_scope?.system || conflict.competing.conflict_scope?.system || '—'}</dd></div>
        <div><dt>Environment</dt><dd>{conflict.original.conflict_scope?.environment || conflict.competing.conflict_scope?.environment || '—'}</dd></div>
      </dl>
      <div className="adr-grid"><ADRCard record={conflict.original} navigate={navigate} /><ADRCard record={conflict.competing} navigate={navigate} /></div>
      {canWrite
        ? <ResolveForm key={`${conflict.original.key}/${conflict.competing.key}`} conflict={conflict} busy={busy} onSubmit={onSubmit} />
        : <WriterNotice />}
    </article>
  );
}

export function EmptyDetail({ children }) {
  return <div className="empty-detail"><FileCheck2 size={30} aria-hidden="true" /><p>{children}</p></div>;
}
