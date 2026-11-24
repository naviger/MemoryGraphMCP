import { ContextHelp } from './ContextHelp.jsx';
import { SimilarRecords } from './SimilarRecords.jsx';
import { CircleAlert, FileCode2, FileText, FlaskConical, Link2, Waypoints } from 'lucide-react';
import { useEffect, useState } from 'react';
import { reviewRequest } from './api.js';
import { CodeEvidenceLink, DetailLink, Evidence, formatDate, formatScore } from './Details.jsx';

const LINK_GROUPS = [
  ['ADR', 'ADRs', FileText],
  ['Service', 'Services', Waypoints],
  ['SourceFile', 'Source files', FileCode2],
  ['TestSuite', 'Tests', FlaskConical],
];

const ENTITY_NAMES = Object.fromEntries(LINK_GROUPS.map(([type, title]) => [type, title]));
const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });

function ConfidenceHistory({ history }) {
  if (!history.length) return <p className="muted">No validations recorded.</p>;
  return (
    <div className="table-scroll">
      <table className="confidence-history">
        <thead><tr><th scope="col">Validated</th><th scope="col">Reviewer</th>
          <th scope="col">Previous</th><th scope="col">Resulting</th></tr></thead>
        <tbody>
          {history.slice().reverse().map((entry, index) => (
            <tr key={`${entry.checked_at}-${index}`}>
              <td><time dateTime={entry.checked_at}>{dateTime.format(new Date(entry.checked_at))}</time></td>
              <td>{entry.reviewer}</td>
              <td>{formatScore(entry.previous_confidence)}</td>
              <td>{formatScore(entry.new_confidence)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const EMBEDDING_STATES = {
  matches: 'Matches active search profile',
  mismatch: 'Does not match active search profile',
  missing: 'No stored embedding',
  unknown: 'Active search profile unavailable',
};

function EmbeddingDiagnostics({ embedding }) {
  const facts = [
    ['Provider', embedding.provider],
    ['Model', embedding.model],
    ['Version', embedding.version],
    ['Dimensions', embedding.dimension],
    ['Compatibility', EMBEDDING_STATES[embedding.state]],
  ];
  return (
    <details className="record-section embedding-diagnostics">
      <summary>Embedding diagnostics</summary>
      <dl className="facts">{facts.map(([label, value]) => (
        <div key={label}><dt>{label}</dt><dd>{value ?? 'Unrecorded'}</dd></div>
      ))}</dl>
    </details>
  );
}

function RecordDetails({ record, embedding }) {
  const facts = [
    ['Effective date', formatDate(record.effective_date)],
    ['Expiration date', formatDate(record.expiration_date)],
    ['Last validated', formatDate(record.last_validated)],
    ['Next review due', formatDate(record.next_review_due)],
    ['Confidence', formatScore(record.confidence)],
  ];
  if (record.date) facts.push(['Decision date', formatDate(record.date)]);
  return (
    <>
      <dl className="facts">{facts.map(([label, value]) => (
        <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
      ))}</dl>
      {record.decision ? (
        <dl className="record-text">
          {Object.entries({ Decision: record.decision, Rationale: record.rationale, Consequences: record.consequences })
            .filter(([, value]) => value).map(([label, value]) => (
              <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
          ))}
        </dl>
      ) : <p className="record-content">{record.content}</p>}
      <section className="record-section">
        <h2>Confidence history</h2>
        <ConfidenceHistory history={record.validation_history} />
      </section>
      <div className="record-section">
        <Evidence history={record.validation_history.slice().reverse()} source={record.source} sourceId={record.source_id} />
      </div>
      {embedding && <EmbeddingDiagnostics key={record.key} embedding={embedding} />}
    </>
  );
}

function RelatedEntities({ links, navigate }) {
  return (
    <section className="links-panel" aria-labelledby="links-heading">
      <h2 id="links-heading"><Link2 size={18} aria-hidden="true" /> Linked entities</h2>
      {LINK_GROUPS.map(([type, label, Icon]) => {
        const group = links.filter((link) => link.entity_type === type);
        return (
          <div className="link-group" key={type}>
            <h3><Icon size={16} aria-hidden="true" /> {label} <span>{group.length}</span></h3>
            {group.length ? (
              <ul>
                {group.map((link) => (
                  <li key={`${link.destination}/${link.key}`}>
                    <DetailLink destination={link.destination} entityKey={link.key} navigate={navigate}>{link.name}</DetailLink>
                    {link.source_path && <code>{link.source_path}</code>}
                    <CodeEvidenceLink location={link.code_location} label="Indexed source at" />
                    {(link.cited_code || []).filter((location) =>
                      location.revision.commit !== link.code_location?.revision.commit
                      || location.path !== link.code_location?.path
                      || location.line !== link.code_location?.line
                    ).map((location, index) => <CodeEvidenceLink
                      key={index} location={location} label="Linked evidence at" />)}
                    <span>{link.relationships.map((kind) => kind.replaceAll('_', ' ')).join(' · ')}
                      {link.index_state === 'stale' ? ' · Stale index' : ''}</span>
                  </li>
                ))}
              </ul>
            ) : <p className="muted">No links recorded.</p>}
          </div>
        );
      })}
    </section>
  );
}

export function EntityDetailPage({ route, token, navigate, onUnauthorized, returnTo }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    reviewRequest(`/${route.destination}/${encodeURIComponent(route.key)}`, { token, signal: controller.signal })
      .then(setDetail)
      .catch((err) => {
        if (err.name === 'AbortError') return;
        if (err.status === 401) onUnauthorized();
        else setError(err.message);
      });
    return () => controller.abort();
  }, [route.destination, route.key, token, attempt, onUnauthorized]);

  const { record, entity } = detail ?? {};
  return (
    <>
      <nav className="record-navigation" aria-label="Record navigation">
        {returnTo ? <a href={returnTo} onClick={(event) => {
          if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey
            || event.altKey || event.button !== 0) return;
          event.preventDefault();
          window.history.back();
        }}>Back to previous view</a> : <a href="/" onClick={(event) => navigate(event, '/')}>Back to Home</a>}
      </nav>
      {error ? (
        <div>
          <p className="status error" role="alert"><CircleAlert size={18} aria-hidden="true" />{error}</p>
          <button type="button" className="button outlined" onClick={() => { setError(''); setAttempt(attempt + 1); }}>Retry</button>
        </div>
      ) : !detail ? <p className="initial-status" role="status">Loading record details…</p> : (
        <>
          <header className="page-heading record-heading">
            <div>
              <p className="item-key">{record?.key ?? entity.key}</p>
              <div className="heading-with-help"><h1>{record?.title ?? entity.name}</h1><ContextHelp topic="records" /></div>
              <p>{record?.kind === 'adr' ? 'ADR' : record?.kind ?? ENTITY_NAMES[entity?.entity_type] ?? entity?.entity_type}
                {record?.status ? ` · ${record.status}` : ''}{entity?.index_state === 'stale' ? ' · Stale index' : ''}</p>
            </div>
          </header>
          <div className="record-layout">
            <article className="detail record-body">
              {record ? <RecordDetails record={record} embedding={detail.embedding} /> : (
                <>
                  {entity.source_path && <dl className="facts"><div><dt>Source path</dt><dd><code>{entity.source_path}</code></dd></div></dl>}
                  {detail.code_location && <dl className="facts">
                    <div><dt>Repository key</dt><dd><code>{detail.code_location.revision.repository_key}</code></dd></div>
                    <div><dt>Indexed revision</dt><dd><CodeEvidenceLink location={detail.code_location} /></dd></div>
                  </dl>}
                  {entity.index_state && !detail.code_location && <p className="muted">Indexed revision unavailable.</p>}
                  {entity.description && <p className="record-content">{entity.description}</p>}
                  {entity.evidence?.length > 0 && <section>
                    <h3>Evidence</h3>
                    <ul>{entity.evidence.map((citation, index) => <li key={index}>{citation}</li>)}</ul>
                  </section>}
                  {!entity.source_path && !entity.description && !detail.code_location && <p className="muted">No additional details recorded.</p>}
                </>
              )}
            </article>
            <div className="record-related">
              <RelatedEntities links={detail.links} navigate={navigate} />
              {record && <div className="detail"><SimilarRecords key={record.key} recordKey={record.key}
                token={token} navigate={navigate} onUnauthorized={onUnauthorized} /></div>}
            </div>
          </div>
        </>
      )}
    </>
  );
}
