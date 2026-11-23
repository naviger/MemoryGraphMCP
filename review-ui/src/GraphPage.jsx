import { ChevronDown } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import { reviewRequest } from './api.js';
import { GraphDiagram, NodeMarker } from './GraphDiagram.jsx';
import { CATEGORIZED_TYPES, NODE_TYPE_CATEGORIES } from './nodeCategories.js';

const NO_RELATIONSHIPS = {};

function NodeTypeCategory({ name, members, hiddenTypes, onToggle, onToggleCategory }) {
  const [expanded, setExpanded] = useState(true);
  const listId = useId();
  const enabledCount = members.filter((type) => !hiddenTypes.includes(type)).length;
  const allEnabled = enabledCount === members.length;
  const mixed = enabledCount > 0 && !allEnabled;

  return (
    <div className="node-type-category" role="group" aria-label={name}>
      <div className="node-type-category-header">
        <label>
          <input type="checkbox" checked={allEnabled}
            ref={(input) => { if (input) input.indeterminate = mixed; }}
            onChange={(event) => onToggleCategory(members, event.target.checked)} />
          {name}
        </label>
        <button type="button" className="node-type-disclosure" aria-expanded={expanded}
          aria-controls={listId} aria-label={`${expanded ? 'Collapse' : 'Expand'} ${name}`}
          onClick={() => setExpanded((current) => !current)}>
          <ChevronDown size={16} aria-hidden="true" />
        </button>
      </div>
      <ul id={listId} hidden={!expanded}>{members.map((type) => (
        <li key={type}><label>
          <input type="checkbox" checked={!hiddenTypes.includes(type)} onChange={() => onToggle(type)} />
          <NodeMarker type={type} />{type}
        </label></li>
      ))}</ul>
    </div>
  );
}

function NodeTypeFilters({ types, hiddenTypes, onToggle, onToggleCategory, onReset }) {
  const groups = [
    ...NODE_TYPE_CATEGORIES.map(([name, , members]) => [name, members.filter((type) => types.includes(type))]),
    ['Other', types.filter((type) => !CATEGORIZED_TYPES.has(type))],
  ];
  return (
    <fieldset className="node-type-filters">
      <legend>Node types</legend>
      <div className="node-type-actions">
        <button type="button" className="button outlined" onClick={() => onReset([])}>Select all</button>
        <button type="button" className="button outlined" onClick={() => onReset(types)}>Clear all</button>
      </div>
      {groups.filter(([, members]) => members.length > 0).map(([name, members]) => (
        <NodeTypeCategory key={name} name={name} members={members} hiddenTypes={hiddenTypes}
          onToggle={onToggle} onToggleCategory={onToggleCategory} />
      ))}
    </fieldset>
  );
}

function EntityName({ entity }) {
  return <><span className="graph-kind">{entity.entity_type}{entity.role && ` · ${entity.role}`}</span>
    <strong>{entity.name}</strong>{entity.source_path && <small>{entity.source_path}</small>}</>;
}

export function GraphPage({ initialFocus, projectKey, token, navigate, onUnauthorized, returnTo }) {
  const [model, setModel] = useState(null);
  const [modelError, setModelError] = useState('');
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState({ query: '', type: 'Project' });
  const [results, setResults] = useState(null);
  const [searchError, setSearchError] = useState('');
  const [focus, setFocus] = useState(initialFocus ?? null);
  const [selected, setSelected] = useState(null);
  const selectionTrigger = useRef(null);
  const [hops, setHops] = useState('4');
  const [hiddenTypes, setHiddenTypes] = useState([]);
  const [graph, setGraph] = useState(null);
  const [graphError, setGraphError] = useState('');
  const types = model?.entity_types ?? [];
  const enabledTypes = hiddenTypes.length
    ? types.filter((type) => !hiddenTypes.includes(type))
    : [];
  const requestedTypes = enabledTypes.join(',');

  useEffect(() => {
    const request = new AbortController();
    reviewRequest('/graph/metamodel', { token, signal: request.signal }).then(setModel).catch((error) => {
      if (request.signal.aborted) return;
      if (error.status === 401) onUnauthorized();
      else setModelError(error.message);
    });
    return () => request.abort();
  }, [token, onUnauthorized]);

  useEffect(() => {
    const request = new AbortController();
    const params = new URLSearchParams({ query: filter.query, limit: '20' });
    if (filter.type) params.set('entity_type', filter.type);
    const projectSearch = projectKey && !filter.type;
    const path = projectSearch
      ? `/projects/${encodeURIComponent(projectKey)}?${new URLSearchParams({ query: filter.query })}`
      : `/graph/entities?${params}`;
    reviewRequest(path, { token, signal: request.signal }).then((data) => {
      const items = projectSearch ? data.entities.slice(0, 20) : data;
      const scoped = projectKey && filter.type === 'Project'
        ? items.filter((item) => item.key === projectKey) : items;
      if (request.signal.aborted) return;
      setResults(scoped);
      setFocus((current) => current ?? scoped[0]?.key ?? null);
    }).catch((error) => {
      if (request.signal.aborted) return;
      if (error.status === 401) onUnauthorized();
      else setSearchError(error.message);
    });
    return () => request.abort();
  }, [filter, projectKey, token, onUnauthorized]);

  useEffect(() => {
    if (!focus) return;
    const request = new AbortController();
    const params = new URLSearchParams({ hops });
    if (requestedTypes) requestedTypes.split(',').forEach((type) => params.append('entity_types', type));
    const options = { token, signal: request.signal };
    const load = focus.startsWith('record:')
      ? reviewRequest(`/records/${encodeURIComponent(focus.slice(7))}`, options).then(({ record }) => ({
          center: { key: focus, name: record.title, description: record.decision || record.content,
            entity_type: 'Memory record', memory_key: record.key, virtual: true },
          paths: [], truncated: false,
        }))
      : reviewRequest(`/graph/entities/${encodeURIComponent(focus)}?${params}`, options);
    load
      .then((data) => { if (!request.signal.aborted) setGraph(data); })
      .catch((error) => {
        if (request.signal.aborted) return;
        if (error.status === 401) onUnauthorized();
        else setGraphError(error.message);
      });
    return () => request.abort();
  }, [focus, hops, requestedTypes, token, onUnauthorized]);

  function selectNode(entity, trigger) {
    selectionTrigger.current = trigger;
    setSelected({ node: entity });
  }

  function selectEdge(edgeId, trigger) {
    selectionTrigger.current = trigger;
    setSelected({ edgeId });
  }

  function closeDetails() {
    setSelected(null);
    selectionTrigger.current?.focus();
  }

  function focusNode(key) {
    if (key === focus) return;
    setGraph(null);
    setGraphError('');
    setFocus(key);
  }

  return (
    <div className="graph-page">
      {returnTo && <nav className="record-navigation" aria-label="Graph navigation">
        <a href={returnTo} onClick={(event) => {
          if (event.button !== 0 || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
          event.preventDefault();
          window.history.back();
        }}>Back to previous view</a>
      </nav>}
      <header className="page-heading"><div><h1>Graph</h1>
        <p>Explore architecture nodes and follow their relationships.</p></div></header>
      {modelError && <p role="alert">{modelError}</p>}
      <div className="graph-workspace">
        <section className="graph-panel graph-sidebar" aria-label="Find nodes">
          <div className="graph-picker">
            <h2>Find nodes</h2>
            <form className="graph-search" onSubmit={(event) => {
              event.preventDefault();
              setResults(null);
              setSearchError('');
              setFilter({ query: query.trim(), type: '' });
            }}>
              <label>Search <input type="search" maxLength={500} value={query} onChange={(event) => setQuery(event.target.value)} /></label>
              <button type="submit" className="button primary">Find nodes</button>
            </form>
            {searchError ? <p role="alert">{searchError}</p> : results === null ? <p role="status">Loading nodes…</p> : (
              results.length ? <ul className="graph-results">{results.map((entity) => (
                <li key={entity.key}><button type="button" className="graph-node" aria-pressed={selected?.node?.key === entity.key}
                  onClick={(event) => { selectNode(entity, event.currentTarget); focusNode(entity.key); }}><EntityName entity={entity} /></button></li>
              ))}</ul> : <p className="muted">No matching nodes. Try another search.</p>
            )}
          </div>
          {types.length > 0 && (
            <div className="graph-filter-scroll">
              <NodeTypeFilters types={types} hiddenTypes={hiddenTypes}
                onToggle={(type) => setHiddenTypes((current) => current.includes(type)
                  ? current.filter((item) => item !== type) : [...current, type])}
                onToggleCategory={(members, enabled) => setHiddenTypes((current) => enabled
                  ? current.filter((type) => !members.includes(type))
                  : [...new Set([...current, ...members])])}
                onReset={setHiddenTypes} />
            </div>
          )}
        </section>
        <section className="graph-panel graph-view" aria-label="Node neighborhood">
          <div className="graph-view-heading">
            <h2>Neighborhood</h2>
            <div className="graph-controls">
            <label>Maximum hops
              <select value={hops} onChange={(event) => {
                setGraph(null);
                setGraphError('');
                setHops(event.target.value);
              }}>
                {Array.from({ length: 10 }, (_, index) => index + 1)
                  .map((count) => <option key={count}>{count}</option>)}
              </select>
            </label>
            </div>
          </div>
          {graphError ? <p role="alert">{graphError}</p> : graph ? (
            <GraphDiagram graph={graph} relationships={model?.relationships ?? NO_RELATIONSHIPS}
              hiddenTypes={hiddenTypes} selected={selected} onSelect={selectNode}
              onSelectEdge={selectEdge} onClose={closeDetails} onFocus={focusNode} navigate={navigate}
              token={token} onUnauthorized={onUnauthorized} />
          ) : <p role="status">{focus ? 'Loading relationships…' : 'Choose a node to explore its relationships.'}</p>}
        </section>
      </div>
    </div>
  );
}
