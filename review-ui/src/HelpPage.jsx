import { useEffect, useRef, useState } from 'react';
import { reviewRequest } from './api.js';
import { helpHref, helpTopics } from './helpTopics.js';

function Metamodel({ model }) {
  return (
    <>
      <h3>Node types</h3>
      <ul className="graph-types">
        {model.entity_types.map((type) => <li key={type}>{type}</li>)}
      </ul>
      <p><code>Repository</code> uses the <code>source</code> role for codebases and the <code>persistence</code> role for repository components.</p>
      <h3>Relationships</h3>
      <p>The graph stores a forward edge. Traversal in the opposite direction uses its inverse.</p>
      <div className="table-scroll">
        <table className="confidence-history">
          <thead><tr><th scope="col">Stored relationship</th><th scope="col">Inverse</th></tr></thead>
          <tbody>{Object.entries(model.relationships).map(([forward, inverse]) => (
            <tr key={forward}><td><code>{forward}</code></td><td><code>{inverse}</code></td></tr>
          ))}</tbody>
        </table>
      </div>
      <p>Each edge carries its source, confidence, creation time, and last update time.</p>
    </>
  );
}

function LiveMetamodel({ token, signedIn, onUnauthorized }) {
  const [model, setModel] = useState(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!signedIn) return;
    const request = new AbortController();
    setError('');
    reviewRequest('/graph/metamodel', { token, signal: request.signal }).then((value) => {
      if (!request.signal.aborted) setModel(value);
    }).catch((err) => {
      if (request.signal.aborted) return;
      if (err.status === 401) onUnauthorized();
      else setError(err.message);
    });
    return () => request.abort();
  }, [signedIn, token, onUnauthorized, attempt]);
  if (!signedIn) return <p>Sign in to see the node and relationship types supported by this server.</p>;
  if (error) return <>
    <p role="alert">{error}</p>
    <button type="button" className="button outlined" onClick={() => setAttempt((value) => value + 1)}>Retry metamodel</button>
  </>;
  return model ? <Metamodel model={model} /> : <p role="status">Loading metamodel…</p>;
}

const entries = Object.entries(helpTopics);
const groups = [...new Set(entries.map(([, topic]) => topic.group))];

export function HelpPage({ topicKey, projectKey, navigate, token, signedIn, onUnauthorized }) {
  const heading = useRef(null);
  const [search, setSearch] = useState('');
  const current = Object.hasOwn(helpTopics, topicKey) ? helpTopics[topicKey] : null;
  const terms = search.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const matches = entries.filter(([, topic]) => {
    const text = [topic.title, topic.brief, ...topic.sections.flat()].join(' ').toLowerCase();
    return terms.every((term) => text.includes(term));
  });
  useEffect(() => { heading.current?.focus({ preventScroll: true }); }, [topicKey]);
  function link(key, label) {
    const path = helpHref(key);
    const href = projectKey ? `${path}?project=${encodeURIComponent(projectKey)}` : path;
    return <a href={href} aria-current={key && key === topicKey ? 'page' : undefined}
      onClick={(event) => navigate(event, path)}>{label}</a>;
  }

  return <>
    <header className="page-heading"><div><h1 ref={heading} tabIndex={-1}>{current?.title ?? 'Help center'}</h1>
      <p>{current?.brief ?? 'Find instructions for using MemoryGraphMCP and interpreting its evidence.'}</p></div></header>
    <div className={current ? 'help-layout help-has-topic' : 'help-layout'}>
      <nav className="detail help-navigation" aria-label="Help topics">
        <label>Search help <input type="search" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
        {link(null, 'Help home')}
        {matches.length === 0 && <p role="status">No topics match your search.</p>}
        {groups.map((group) => {
          const list = matches.filter(([, topic]) => topic.group === group);
          return list.length > 0 && <section key={group}>
            <h2>{group}</h2>
            <ul>{list.map(([key, topic]) => <li key={key}>{link(key, topic.title)}</li>)}</ul>
          </section>;
        })}
      </nav>
      <div className="help-document">
        {current ? <>
          <article className="detail help-article">
            {current.sections.map(([title, body]) => <section key={title}><h2>{title}</h2><p>{body}</p></section>)}
            {topicKey === 'model' && <LiveMetamodel token={token} signedIn={signedIn} onUnauthorized={onUnauthorized} />}
          </article>
          <nav className="detail help-related" aria-label="Related help">
            <h2>Related topics</h2>
            <ul>{current.related.map((key) => <li key={key}>{link(key, helpTopics[key].title)}</li>)}</ul>
          </nav>
        </> : <>
          {topicKey && <p role="status">This topic could not be found. Choose a topic below.</p>}
          <div className="help-cards">
            {(terms.length ? matches : entries.filter(([key]) =>
              ['home', 'projects', 'search', 'graph', 'review', 'guidance', 'token-usage', 'effectiveness'].includes(key)
            )).map(([key, topic]) => <section className="detail" key={key}>
              <h2>{link(key, topic.title)}</h2><p>{topic.brief}</p>
            </section>)}
          </div>
        </>}
      </div>
    </div>
  </>;
}
