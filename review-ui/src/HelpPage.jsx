import { useEffect, useState } from 'react';
import { reviewRequest } from './api.js';

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

export function HelpPage({ token, onUnauthorized }) {
  const [model, setModel] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    const request = new AbortController();
    setError('');
    reviewRequest('/graph/metamodel', { token, signal: request.signal })
      .then(setModel)
      .catch((err) => {
        if (request.signal.aborted) return;
        if (err.status === 401) onUnauthorized();
        else setError(err.message);
      });
    return () => request.abort();
  }, [token, onUnauthorized]);

  return <>
    <header className="page-heading"><h1>Help</h1></header>
    <div className="help-content">
      <section className="detail">
        <h2>Find and review project knowledge</h2>
        <p>Choose a project in the header to scope Projects, Graph, Ask the Graph, and Search Memory. Home shows work across projects.</p>
        <p>Use Ask the Graph for answers with citations and Search Memory for matching excerpts. Set an as-of date to see what applied on a specific day.</p>
        <p>In Graph, choose a starting node and set the number of hops to control how far relationships extend. Select a node or edge to open its details.</p>
      </section>
      <section className="detail">
        <h2>Guidance for coding agents</h2>
        <p>Global Guidance applies across this MemoryGraphMCP deployment. Project Guidance adds instructions for an individual project.</p>
        <p>Admins can save and publish global drafts. Project writers can save drafts and admins can publish them. Both editors retain the history of published versions.</p>
        <p><code>AGENTS.md</code> directs coding agents to <code>get_project_guidance</code>, which returns the published global and project guidance separately, along with current ADRs and constraints.</p>
      </section>
      <section className="detail">
        <h2>Graph metamodel</h2>
        <p>The metamodel defines the kinds of records stored as nodes and the names and directions of the relationships that connect them.</p>
        {error ? <p role="alert">{error}</p> : model ? <Metamodel model={model} /> : <p role="status">Loading metamodel…</p>}
      </section>
    </div>
  </>;
}
