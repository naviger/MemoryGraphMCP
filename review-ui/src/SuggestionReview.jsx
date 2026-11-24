import { useState } from 'react';
import { Evidence } from './Details.jsx';
import { PreviewLink } from './RecordInspector.jsx';

const ACTIONS = { accept: 'Accept', reject: 'Reject', replace: 'Replace' };
const RESOLVED = { accept: 'Accepted', reject: 'Rejected', replace: 'Replaced' };
const TYPES = ['implements', 'validates', 'persists_to', 'supports_goal', 'references'];

export function RelationshipLabel({ link, names, navigate }) {
  const endpoint = (key) => <PreviewLink destination="entities" entityKey={key} navigate={navigate}>{names.get(key) || key}</PreviewLink>;
  return <>{endpoint(link.source_key)} {' '}{link.relationship_type.replaceAll('_', ' ')}{' '}{endpoint(link.target_key)}</>;
}

export function SuggestionItem({ observation, link, names, canWrite, busy, save, navigate }) {
  const [action, setAction] = useState('');
  const [rationale, setRationale] = useState('');
  const [replacement, setReplacement] = useState({
    source_key: link.source_key, target_key: link.target_key, relationship_type: link.relationship_type,
  });
  const choices = [...new Set([link.source_key, link.target_key, ...names.keys()])];
  const changed = Object.keys(replacement).some((key) => replacement[key] !== link[key]);

  return <li className="capture-suggestion">
    <p><RelationshipLabel link={link} names={names} navigate={navigate} />
      {' · '}{Math.round(link.confidence * 100)}% confidence</p>
    <p className="muted">{link.evidence}</p>
    {canWrite && !action && <div className="workspace-actions">
      {Object.entries(ACTIONS).map(([value, label]) => <button key={value} type="button"
        className="button outlined" disabled={busy} onClick={() => setAction(value)}>{label}</button>)}
    </div>}
    {canWrite && action && <form className="action-form" onSubmit={async (event) => {
      event.preventDefault();
      if (await save(observation, {
        source_key: link.source_key, target_key: link.target_key, relationship_type: link.relationship_type,
        action, rationale: rationale.trim(), replacement: action === 'replace' ? replacement : null,
      })) setAction('');
    }}>
      <h3>{ACTIONS[action]} suggestion</h3>
      {action === 'replace' && <div className="form-columns">
        {[['source_key', 'Source'], ['target_key', 'Target']].map(([field, label]) =>
          <label key={field}>{label}<select value={replacement[field]}
            onChange={(event) => setReplacement({ ...replacement, [field]: event.target.value })}>
            {choices.map((key) => <option key={key} value={key}>{names.get(key) || key}</option>)}
          </select></label>)}
        <label>Relationship<select value={replacement.relationship_type}
          onChange={(event) => setReplacement({ ...replacement, relationship_type: event.target.value })}>
          {TYPES.map((kind) => <option key={kind} value={kind}>{kind.replaceAll('_', ' ')}</option>)}
        </select></label>
      </div>}
      <label>Rationale<textarea required maxLength={4000} rows={2} value={rationale}
        onChange={(event) => setRationale(event.target.value)} /></label>
      <div className="workspace-actions">
        <button type="submit" className="button primary" disabled={busy || !rationale.trim()
          || (action === 'replace' && (!changed || replacement.source_key === replacement.target_key))}>
          {busy ? 'Saving…' : 'Record decision'}
        </button>
        <button type="button" className="button outlined" disabled={busy} onClick={() => setAction('')}>Cancel</button>
      </div>
    </form>}
  </li>;
}

export function SuggestionResolutionDetail({ entry, names, navigate }) {
  const { decision, suggestion } = entry;
  return <>
    <p><strong>{RESOLVED[decision.action]}</strong> · {entry.reviewer} · <time dateTime={entry.resolved_at}>
      {new Date(entry.resolved_at).toLocaleString()}</time></p>
    <p><RelationshipLabel link={suggestion} names={names} navigate={navigate} /></p>
    {decision.replacement && <p>Confirmed replacement{' '}
      <RelationshipLabel link={decision.replacement} names={names} navigate={navigate} /></p>}
    <p>{decision.rationale}</p>
    <Evidence source={suggestion.evidence} history={[]} />
    <PreviewLink destination="entities" entityKey={entry.observation_key} navigate={navigate}>Open observation</PreviewLink>
  </>;
}
