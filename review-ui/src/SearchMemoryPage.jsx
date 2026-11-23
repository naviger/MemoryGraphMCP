import { InspectorView, PreviewLink } from './RecordInspector.jsx';
import { Search } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { reviewRequest } from './api.js';
import { formatDate } from './Details.jsx';

const TYPE_NAMES = {
  adr: 'ADR',
  architecture: 'Architecture',
  'design-language': 'Design language',
  'domain-concept': 'Domain concept',
  memory: 'Memory',
};

export function SearchMemoryPage({ token, navigate, onUnauthorized, projectKey, projectName }) {
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState('hybrid');
  const [kinds, setKinds] = useState([]);
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const request = useRef(null);

  useEffect(() => () => request.current?.abort(), []);

  async function search(event) {
    event.preventDefault();
    if (pending || !query.trim()) return;
    const asOf = new FormData(event.currentTarget).get('as_of');
    const controller = new AbortController();
    request.current = controller;
    setPending(true);
    setResult(null);
    setError('');
    try {
      const data = await reviewRequest('/search', {
        token, signal: controller.signal, body: { query: query.trim(), mode, as_of: asOf || null, kinds, project_key: projectKey || null },
      });
      setResult({ ...data, asOf, kinds, mode });
    } catch (err) {
      if (controller.signal.aborted) return;
      if (err.status === 401) onUnauthorized();
      else setError(err.message);
    } finally {
      if (!controller.signal.aborted) setPending(false);
    }
  }

  return (
    <InspectorView token={token} navigate={navigate} onUnauthorized={onUnauthorized}>
      <section className="memory-search-page" aria-labelledby="memory-search-heading">
        <header className="page-heading">
          <div><h1 id="memory-search-heading">Search Memory</h1>
            <p>{projectKey ? `Searching ${projectName || "the selected project"}.` : "Searching all projects."}</p></div>
        </header>
        <form className="detail ask-form" onSubmit={search}>
          <label>Search terms
            <input type="search" required maxLength={500} value={query} onChange={(event) => setQuery(event.target.value)} />
          </label>
          <fieldset className="memory-search-types">
            <legend>Document type</legend>
            <div className="memory-search-type-options">
              <button type="button" className="button outlined" aria-pressed={kinds.length === 0}
                onClick={() => setKinds([])}>All types</button>
              {Object.entries(TYPE_NAMES).map(([kind, label]) => (
                <label key={kind}>
                  <input type="checkbox" checked={kinds.includes(kind)} onChange={(event) => {
                    const checked = event.target.checked;
                    setKinds((current) => checked ? [...current, kind] : current.filter((type) => type !== kind));
                  }} />
                  {label}
                </label>
              ))}
            </div>
          </fieldset>
          <div className="ask-actions">
            <label>As of <input type="date" name="as_of" /></label>
            <button className="button primary" type="submit" disabled={pending || !query.trim()}>
              <Search size={17} aria-hidden="true" />{pending ? 'Searching…' : 'Search'}
            </button>
          </div>
          <details className="search-advanced">
            <summary>Advanced options</summary>
              <label>Search mode
                <select value={mode} onChange={(event) => setMode(event.target.value)}>
                  <option value="hybrid">Hybrid</option>
                  <option value="semantic">Semantic</option>
                </select>
              </label>
            <div>
              <h2>Embedding diagnostics</h2>
              {!result ? <p className="muted">Run a search to see embedding diagnostics.</p> : <>
                <p className="muted">{result.mode === 'semantic' ? 'Semantic' : 'Hybrid'} search · {result.fallback_used ? 'Fallback used' : 'No fallback reported'}</p>
                {result.embedding ? <dl className="graph-node-facts">
                  {Object.entries(result.embedding).filter(([, value]) => value != null).map(([name, value]) => (
                    <div key={name}><dt>{name.charAt(0).toUpperCase() + name.slice(1)}</dt><dd>{value}</dd></div>
                  ))}
                </dl> : <p className="muted">No embedding profile was reported for these results.</p>}
              </>}
            </div>
          </details>
        </form>
        {pending && <p role="status">Searching memory…</p>}
        {error && <p role="alert" className="status error">{error}</p>}
        {result && <>
          <p role="status" className="memory-search-summary">
            {result.matches.length ? `${result.matches.length} ${result.matches.length === 1 ? 'match' : 'matches'}` : 'No matching records'}
          </p>
          <ul className="memory-result-filters" aria-label="Applied filters">
            <li>{result.asOf ? `As of ${formatDate(result.asOf)}` : 'Any date'}</li>
            <li>{result.kinds.length ? result.kinds.map((kind) => TYPE_NAMES[kind]).join(', ') : 'All document types'}</li>
          </ul>
          {result.fallback_used && <p className="muted">Search used a fallback for some results.</p>}
          <ul className="memory-search-results">
            {result.matches.map((match) => (
              <li className="detail" key={match.key}>
                <h2><PreviewLink entityKey={match.key} navigate={navigate}>{match.title}</PreviewLink></h2>
                {match.excerpt && <p className="memory-result-excerpt">{match.excerpt}</p>}
                <p className="muted">Source { [match.source, match.source_id].filter(Boolean).join(' · ') || 'unrecorded'} · {TYPE_NAMES[match.kind] ?? match.kind}</p>
                <dl className="facts">
                  <div><dt>Effective date</dt><dd>{match.effective_date ? formatDate(match.effective_date) : 'Unrecorded'}</dd></div>
                  <div><dt>Expiration date</dt><dd>{match.expiration_date ? formatDate(match.expiration_date) : 'Unrecorded'}</dd></div>
                </dl>
              </li>
            ))}
          </ul>
        </>}
      </section>
    </InspectorView>
  );
}
