import { Network, X } from 'lucide-react';
import { createContext, useContext, useEffect, useId, useRef, useState } from 'react';
import { reviewRequest } from './api.js';
import { DetailLink, formatDate } from './Details.jsx';

const InspectorContext = createContext(null);

export function PreviewLink({ destination = 'records', entityKey, navigate, children }) {
  const inspector = useContext(InspectorContext);
  const href = `/${destination}/${encodeURIComponent(entityKey)}`;
  return (
    <a href={href} aria-current={inspector?.selection?.href === href ? 'true' : undefined}
      onClick={(event) => {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey
          || event.altKey || event.shiftKey) return;
        if (!inspector) {
          navigate(event, href);
          return;
        }
        event.preventDefault();
        inspector.select({ destination, entityKey, href }, event.currentTarget);
      }}>{children}</a>
  );
}

function excerpt(text) {
  return text.length > 650 ? `${text.slice(0, 647).trimEnd()}…` : text;
}

function Inspector({ selection, close, token, navigate, onUnauthorized }) {
  const heading = useId();
  const title = useRef(null);
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => { title.current?.focus({ preventScroll: true }); }, []);
  useEffect(() => {
    const request = new AbortController();
    reviewRequest(selection.href, { token, signal: request.signal })
      .then((data) => { if (!request.signal.aborted) setDetail(data); })
      .catch((err) => {
        if (request.signal.aborted) return;
        if (err.status === 401) onUnauthorized();
        else setError(err.message);
      });
    return () => request.abort();
  }, [selection.href, attempt, token, onUnauthorized]);

  const { record, entity } = detail ?? {};
  const facts = [
    ['Status', record?.status || entity?.status],
    ['Source', record?.source],
    ['File', entity?.source_path],
    ['Qualified name', entity?.qualified_name],
    ['Endpoint', entity?.method && entity?.path ? `${entity.method} ${entity.path}` : null],
    ['Effective date', record?.effective_date && formatDate(record.effective_date)],
    ['Expiration date', record?.expiration_date && formatDate(record.expiration_date)],
  ].filter(([, value]) => Boolean(value));
  const description = record?.decision || record?.content || entity?.summary || entity?.description;
  const graphKey = entity?.key ?? (record && `record:${record.key}`);
  const graphHref = graphKey && `/graph/${encodeURIComponent(graphKey)}`;
  return (
    <aside className="detail record-inspector" aria-labelledby={heading} onKeyDown={(event) => {
      if (event.key === 'Escape') close();
    }}>
      <header>
        <h2 id={heading} ref={title} tabIndex={-1}>Inspector</h2>
        <button type="button" className="button icon outlined" aria-label="Close inspector" onClick={close}>
          <X size={18} aria-hidden="true" />
        </button>
      </header>
      {error ? <>
        <p role="alert">{error}</p>
        <button type="button" className="button outlined" onClick={() => {
          setError('');
          setAttempt(attempt + 1);
        }}>Retry</button>
      </> : detail ? <>
        <p className="item-key">{record?.kind === 'adr' ? 'ADR' : record?.kind || entity?.entity_type}</p>
        <h3>{record?.title ?? entity?.name}</h3>
        {description && <p className="inspector-description">{excerpt(description)}</p>}
        {record?.rationale && <div><h4>Rationale</h4><p>{excerpt(record.rationale)}</p></div>}
        {facts.length > 0 && <dl className="graph-node-facts">
          {facts.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
        </dl>}
      </> : <p role="status">Loading preview…</p>}
      {graphHref && <a href={graphHref} className="button outlined"
        onClick={(event) => navigate(event, graphHref)}>
        <Network size={16} aria-hidden="true" /> Explore relationships
      </a>}
      <DetailLink destination={selection.destination} entityKey={selection.entityKey} navigate={navigate}>
        Open full record
      </DetailLink>
    </aside>
  );
}

export function InspectorView({ children, token, navigate, onUnauthorized }) {
  const [selection, setSelection] = useState(null);
  const trigger = useRef(null);
  function select(item, element) {
    trigger.current = element;
    setSelection(item);
  }
  function close() {
    setSelection(null);
    trigger.current?.focus({ preventScroll: true });
  }
  return (
    <InspectorContext.Provider value={{ selection, select }}>
      <div className={selection ? 'inspector-layout inspector-open' : 'inspector-layout'}>
        <div className="inspector-content">{children}</div>
        {selection && <Inspector key={selection.href} selection={selection} close={close}
          token={token} navigate={navigate} onUnauthorized={onUnauthorized} />}
      </div>
    </InspectorContext.Provider>
  );
}
