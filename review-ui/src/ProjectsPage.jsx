import { EffectivenessPage } from './EffectivenessPage.jsx';
import { TokenUsagePage } from './TokenUsagePage.jsx';
import { projectHelpTopics } from './helpTopics.js';
import { ContextHelp } from './ContextHelp.jsx';
import ProjectWorkspace, { CaptureStatus } from './ProjectWorkspace.jsx';
import ProjectGuidance from './ProjectGuidance.jsx';
import TrackerOverview from './TrackerOverview.jsx';
import { InspectorView, PreviewLink } from './RecordInspector.jsx';
import ProjectChanges from './ProjectChanges.jsx';
import { ProjectHealth, ProjectTesting } from './ProjectTesting.jsx';
import { Network, Search } from 'lucide-react';
import { useEffect, useState } from 'react';
import { reviewRequest } from './api.js';
import { formatDate, formatWindow } from './Details.jsx';
import { SuggestionItem, SuggestionResolutionDetail } from './SuggestionReview.jsx';

const CODE_TYPES = new Set(['Repository', 'Service', 'Module', 'Class', 'Function',
  'SourceFile', 'ApiEndpoint', 'DatabaseCollection', 'TestSuite']);
const BATCH_SIZE = 25;

function InventoryGroup({ title, items, renderItem, open = false }) {
  const [limit, setLimit] = useState(BATCH_SIZE);
  return (
    <details className="detail project-inventory-group" open={open}>
      <summary><strong>{title}</strong> <span>{items.length}</span></summary>
      {items.length ? (
        <>
          <ul className="project-inventory-list">
            {items.slice(0, limit).map((item) => <li key={item.key}>{renderItem(item)}</li>)}
          </ul>
          {limit < items.length && <button className="button outlined" type="button"
            onClick={() => setLimit(limit + BATCH_SIZE)}>Show more</button>}
        </>
      ) : <p className="muted">No matching items.</p>}
    </details>
  );
}

function buildRepositoryTree(items) {
  const roots = items.filter((node) => node.entity_type === 'Repository' && node.role === 'source');
  const repos = new Map(roots.map((node) =>
    [node.key, { key: node.key, name: node.name, node, files: new Map(), other: [] }]));
  for (const node of items) {
    if (repos.has(node.key)) continue;
    const legacyMatches = node.repository_key ? [] : roots.filter((root) => root.name === node.repository);
    const name = node.repository || 'Unassigned repository';
    const key = node.repository_key || (legacyMatches.length === 1 ? legacyMatches[0].key : `legacy:${name}`);
    if (!repos.has(key)) repos.set(key, { key, name, node: null, files: new Map(), other: [] });
    const repo = repos.get(key);
    if (!node.source_path) {
      repo.other.push(node);
      continue;
    }
    if (!repo.files.has(node.source_path)) {
      repo.files.set(node.source_path, { key: node.source_path, node: null, declarations: [] });
    }
    const file = repo.files.get(node.source_path);
    if (node.entity_type === 'SourceFile') file.node = node;
    else file.declarations.push(node);
  }
  return [...repos.values()].sort((a, b) => a.name.localeCompare(b.name));
}

function FileDeclarations({ declarations, renderEntity }) {
  const keys = new Set(declarations.map(({ key }) => key));
  const children = new Map();
  for (const node of declarations) {
    const parent = keys.has(node.parent_key) ? node.parent_key : null;
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(node);
  }
  function renderChildren(parent) {
    const nodes = children.get(parent);
    if (!nodes?.length) return null;
    return (
      <ul className="project-code-declarations">{nodes.map((node) => (
        <li key={node.key}>
          {renderEntity(node)}
          {renderChildren(node.key)}
        </li>
      ))}</ul>
    );
  }
  return renderChildren(null);
}

function RepositoryFiles({ repo, renderEntity, navigate, searching }) {
  const [limit, setLimit] = useState(BATCH_SIZE);
  const files = [...repo.files.values()].sort((a, b) => a.key.localeCompare(b.key));
  return (
    <details className="project-code-repo" open>
      <summary><strong>{repo.name}</strong> <span>· {files.length} {files.length === 1 ? 'file' : 'files'}</span></summary>
      {repo.node && <PreviewLink destination="entities" entityKey={repo.node.key} navigate={navigate}>Open repository</PreviewLink>}
      {files.length > 0 && <ul className="project-code-files">
        {files.slice(0, limit).map((file) => (
          <li key={file.key}>
            <details open={searching || undefined}>
              <summary>{file.key}</summary>
              {file.node && <PreviewLink destination="entities" entityKey={file.node.key} navigate={navigate}>Open file</PreviewLink>}
              <FileDeclarations declarations={file.declarations} renderEntity={renderEntity} />
            </details>
          </li>
        ))}
      </ul>}
      {limit < files.length && <button className="button outlined" type="button"
        onClick={() => setLimit(limit + BATCH_SIZE)}>Show more files</button>}
      <FileDeclarations declarations={repo.other} renderEntity={renderEntity} />
    </details>
  );
}

function RepositoryInventory({ items, renderEntity, navigate, searching }) {
  const repositories = buildRepositoryTree(items);
  return (
    <details className="detail project-inventory-group" open>
      <summary><strong>Repositories and code</strong> <span>{items.length}</span></summary>
      {repositories.length ? repositories.map((repo) => (
        <RepositoryFiles key={repo.key} repo={repo} renderEntity={renderEntity}
          navigate={navigate} searching={searching} />
      )) : <p className="muted">No matching items.</p>}
    </details>
  );
}

const TABS = ['Overview', 'Guidance', 'Decisions', 'Code', 'Testing', 'Source Control', 'Memory', 'Review', 'Token usage', 'Effectiveness'];

const tabSlug = (name) => name.toLowerCase().replaceAll(' ', '-');

function projectTab(requested) {
  const tab = requested === 'Changes' ? 'Source Control' : requested;
  return TABS.includes(tab) ? tab : 'Overview';
}

function ProjectReview({ projectKey, entities, asOf, token, navigate, onUnauthorized, active }) {
  const [review, setReview] = useState(null);
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const base = `/projects/${encodeURIComponent(projectKey)}`;
  const [queue, setQueue] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    const params = new URLSearchParams({ project_key: projectKey, limit: '5' });
    if (asOf) params.set('as_of', asOf);
    setQueue(null);
    setError('');
    const options = { token, signal: controller.signal };
    Promise.all([reviewRequest(`/queue?${params}`, options), reviewRequest(`${base}/suggestion-resolutions`, options)])
      .then(([data, decisions]) => {
        if (controller.signal.aborted) return;
        setQueue(data);
        setReview(decisions);
      }).catch((err) => {
      if (controller.signal.aborted) return;
      if (err.status === 401) onUnauthorized();
      else setError(err.message);
    });
    return () => controller.abort();
  }, [active, projectKey, asOf, token, onUnauthorized, reload, base]);

  async function save(observation, body) {
    setBusy(true);
    setActionError('');
    try {
      await reviewRequest(`${base}/suggestions/${encodeURIComponent(observation)}/resolve`, { token, body });
      setReload((version) => version + 1);
      return true;
    } catch (err) {
      if (err.status === 401) onUnauthorized();
      else setActionError(err.message);
      return false;
    } finally {
      setBusy(false);
    }
  }

  const names = new Map([[projectKey, 'Project'], ...entities.map((node) => [node.key, node.name])]);

  if (error) return <p role="alert" className="status error">{error}</p>;
  if (!queue) return <p role="status">Loading project review…</p>;
  return <>
    <p className="muted">Review date {formatDate(queue.review_date)} · Up to five items per queue</p>
    <InventoryGroup title="Overdue validations" items={queue.overdue_validations} open renderItem={(item) => (
      <><PreviewLink entityKey={item.key} navigate={navigate}>{item.title}</PreviewLink>
        <p className="muted">Due {formatDate(item.next_review_due)}</p></>
    )} />
    <a href="/review/validations" onClick={(event) => navigate(event, '/review/validations')}>Open validation queue</a>
    <InventoryGroup title="Unresolved ADR conflicts" open
      items={queue.unresolved_adr_conflicts.map((item) => ({ ...item, key: `${item.original.key}/${item.competing.key}` }))}
      renderItem={(item) => (
        <><PreviewLink entityKey={item.original.key} navigate={navigate}>{item.original.title}</PreviewLink>
          {' and '}<PreviewLink entityKey={item.competing.key} navigate={navigate}>{item.competing.title}</PreviewLink></>
      )} />
    <a href="/review/conflicts" onClick={(event) => navigate(event, '/review/conflicts')}>Open conflict queue</a>
    {actionError && <p className="status error" role="alert">{actionError}</p>}
    <InventoryGroup title="Suggested relationships" items={queue.inferred_relationships ?? []} open
      renderItem={(item) => (
        <details>
          <summary>{item.summary.split('\n')[0]} · {item.suggestions.length} suggestions</summary>
          <ul>{item.suggestions.map((link) => <SuggestionItem
            key={`${link.source_key}/${link.relationship_type}/${link.target_key}`}
            observation={item.key} link={link} names={names} navigate={navigate}
            canWrite={review?.can_write} busy={busy} save={save} />)}</ul>
          <PreviewLink destination="entities" entityKey={item.key} navigate={navigate}>Open observation</PreviewLink>
        </details>
      )} />
    <InventoryGroup title="Recent suggestion decisions" items={(review?.resolutions ?? []).map((entry) => ({
      ...entry, key: `${entry.suggestion.source_key}/${entry.suggestion.relationship_type}/${entry.suggestion.target_key}`,
    }))} renderItem={(entry) => <SuggestionResolutionDetail entry={entry} names={names} navigate={navigate} />} />
  </>;
}

function Inventory({ project, navigate, searching, tab, setTab, token, asOf, onUnauthorized }) {
  const documentsByKey = new Map(project.documents.map((doc) => [doc.key, doc]));
  const renderEntity = (entity) => {
    const record = documentsByKey.get(entity.memory_key);
    return (
      <>
        <PreviewLink destination={entity.memory_key ? 'records' : 'entities'}
          entityKey={entity.memory_key || entity.key} navigate={navigate}>{entity.name}</PreviewLink>
        <p className="muted">{[entity.entity_type, record?.status || entity.status, entity.reason, entity.source_path].filter(Boolean).join(' · ')}</p>
        {entity.decision_links.length > 0 && (
          <details className="project-decision-links">
            <summary>Implementation and tests ({entity.decision_links.length})</summary>
            <ul>{entity.decision_links.map((link) => (
              <li key={`${link.relationship}/${link.key}`}>
                {link.relationship === 'validated_by' ? 'Validated by ' : 'Implemented by '}
                <PreviewLink destination="entities" entityKey={link.key} navigate={navigate}>{link.name}</PreviewLink>
              </li>
            ))}</ul>
          </details>
        )}
      </>
    );
  };

  const decisions = project.entities.filter((item) => item.entity_type === 'ADR');
  const decisionDocuments = project.documents.filter((doc) => doc.kind === 'adr');
  const decisionStatuses = new Map(decisionDocuments.map((doc) => [doc.key, doc.status]));
  const linkedDecisions = new Set(decisions.map((decision) => decision.memory_key));
  const proposed = decisions.filter((decision) =>
    (decisionStatuses.has(decision.memory_key) ? decisionStatuses.get(decision.memory_key) : decision.status) === 'proposed');
  const proposedKeys = new Set(proposed.map((decision) => decision.key));
  const documentOnlyDecisions = decisionDocuments.filter((doc) => !linkedDecisions.has(doc.key));
  const renderDecisionDocument = (doc) => (
    <PreviewLink entityKey={doc.key} navigate={navigate}>{doc.title}</PreviewLink>
  );
  const code = project.entities.filter((item) => CODE_TYPES.has(item.entity_type));
  return <>
    <div role="tablist" aria-label="Project sections" className="project-tabs"
      onKeyDown={(event) => {
        const index = TABS.indexOf(tab);
        const next = { ArrowRight: (index + 1) % TABS.length,
          ArrowLeft: (index + TABS.length - 1) % TABS.length, Home: 0, End: TABS.length - 1 }[event.key];
        if (next === undefined) return;
        event.preventDefault();
        setTab(TABS[next]);
        event.currentTarget.querySelectorAll('[role="tab"]')[next].focus();
      }}>
      {TABS.map((name) => (
        <button type="button" key={name} role="tab" id={`project-tab-${tabSlug(name)}`}
          aria-controls={`project-panel-${tabSlug(name)}`} aria-selected={tab === name}
          tabIndex={tab === name ? 0 : -1} onClick={() => setTab(name)}>{name}</button>
      ))}
    </div>
    {[
      ['Token usage', tab === 'Token usage' && <TokenUsagePage projectKey={project.key}
        token={token} onUnauthorized={onUnauthorized} embedded />],
      ['Effectiveness', tab === 'Effectiveness' && <EffectivenessPage projectKey={project.key}
        projects={[project]} token={token} onUnauthorized={onUnauthorized} embedded />],
      ['Overview', tab === 'Overview' && <>
        <ProjectWorkspace projectKey={project.key} token={token} navigate={navigate} onUnauthorized={onUnauthorized} />
        <CaptureStatus projectKey={project.key} token={token} />
        <TrackerOverview projectKey={project.key} token={token} active navigate={navigate} onUnauthorized={onUnauthorized} />
      </>],
      ['Guidance', <ProjectGuidance projectKey={project.key} token={token}
        onUnauthorized={onUnauthorized} active={tab === 'Guidance'} />],
      ['Decisions', <>
        <InventoryGroup title="Awaiting a decision" open
          items={[...proposed, ...documentOnlyDecisions.filter((doc) => doc.status === 'proposed')]}
          renderItem={(item) => item.entity_type ? renderEntity(item) : renderDecisionDocument(item)} />
        <InventoryGroup title="Other decisions" open
          items={[...decisions.filter((item) => !proposedKeys.has(item.key)),
            ...documentOnlyDecisions.filter((doc) => doc.status !== 'proposed')]}
          renderItem={(item) => item.entity_type ? renderEntity(item) : renderDecisionDocument(item)} />
      </>],
      ['Code', <>
        <RepositoryInventory items={code} renderEntity={renderEntity} navigate={navigate} searching={searching} />
        <InventoryGroup title="Cross-project dependencies" items={project.dependencies} renderItem={renderEntity} />
      </>],
      ['Testing', <ProjectTesting projectKey={project.key} token={token} navigate={navigate}
        onUnauthorized={onUnauthorized} active={tab === 'Testing'} />],
      ['Source Control', <ProjectChanges projectKey={project.key} token={token}
        onUnauthorized={onUnauthorized} active={tab === 'Source Control'} />],
      ['Memory', <InventoryGroup title="Memory documents" items={project.documents} open renderItem={(doc) => (
        <>
          <PreviewLink entityKey={doc.key} navigate={navigate}>{doc.title}</PreviewLink>
          <p className="muted">{[doc.kind, doc.status, doc.reason, doc.source].filter(Boolean).join(' · ')}</p>
          {(doc.effective_date || doc.expiration_date) &&
            <p className="muted">Validity {formatWindow(doc.effective_date, doc.expiration_date)}</p>}
        </>
      )} />],
      ['Review', <ProjectReview projectKey={project.key} entities={project.entities} asOf={asOf} token={token}
        navigate={navigate} onUnauthorized={onUnauthorized} active={tab === 'Review'} />],
    ].map(([name, panel]) => (
      <section key={name} role="tabpanel" id={`project-panel-${tabSlug(name)}`} aria-labelledby={`project-tab-${tabSlug(name)}`}
        hidden={tab !== name} className="project-tab-panel">{panel}</section>
    ))}
  </>;
}

export function ProjectsPage({ projectKey, token, navigate, onUnauthorized, requestedTab }) {
  const [project, setProject] = useState(null);
  const [error, setError] = useState('');
  const [tab, setTab] = useState(() => projectTab(requestedTab));
  const [query, setQuery] = useState('');
  const [filters, setFilters] = useState({ query: '', asOf: '' });

  useEffect(() => {
    setTab(projectTab(requestedTab));
  }, [requestedTab]);

  function selectTab(name) {
    setTab(name);
    const url = new URL(window.location.href);
    url.searchParams.set('tab', name);
    window.history.replaceState(window.history.state, '', url);
  }

  useEffect(() => {
    if (!projectKey) return;
    const controller = new AbortController();
    const params = new URLSearchParams({ query: filters.query });
    if (filters.asOf) params.set('as_of', filters.asOf);
    setProject(null);
    setError('');
    reviewRequest(`/projects/${encodeURIComponent(projectKey)}?${params}`, { token, signal: controller.signal })
      .then(setProject).catch((err) => {
        if (controller.signal.aborted) return;
        if (err.status === 401) onUnauthorized();
        else setError(err.message);
      });
    return () => controller.abort();
  }, [filters, projectKey, token, onUnauthorized]);

  const current = project?.key === projectKey ? project : null;
  return (
    <InspectorView token={token} navigate={navigate} onUnauthorized={onUnauthorized}>
      <section className="project-page">
        <header className="page-heading"><div><div className="heading-with-help"><h1>Project Overview</h1><ContextHelp topic={projectHelpTopics[tab]} /></div>
          <p>Browse graph entities and memory documents associated with a project.</p></div></header>
        <header className="page-heading"><div><h1>Project Overview</h1>
          <p>Manage project work and explore its architectural memory.</p></div></header>
        {!projectKey && <p className="muted">Choose a project in the top bar to browse its inventory.</p>}
        <form className="detail ask-form" onSubmit={(event) => {
          event.preventDefault();
          setFilters({ query: query.trim(), asOf: new FormData(event.currentTarget).get('as_of') });
        }}>
          <div className="project-search-controls">
            <label>Search within project
              <input type="search" value={query} maxLength={500} onChange={(event) => setQuery(event.target.value)} />
            </label>
            <label>As of
              <input type="date" name="as_of" />
            </label>
            <button className="button primary" type="submit" disabled={!projectKey}>
              <Search size={16} aria-hidden="true" /> Apply filters
            </button>
          </div>
        </form>
        {projectKey && (error ? <p className="status error" role="alert">{error}</p>
          : current ? (
            <>
              <div className="page-heading">
                <div><h2>{current.name}</h2>{current.description && <p>{current.description}</p>}
                  <ProjectHealth projectKey={current.key} token={token} /></div>
                <a className="button outlined"
                  href={`/graph/${encodeURIComponent(current.key)}`}
                  onClick={(event) => navigate(event, `/graph/${encodeURIComponent(current.key)}`)}>
                  <Network size={16} aria-hidden="true" /> Open in Graph
                </a>
              </div>
              <div className="project-inventory"><Inventory key={current.key} project={current} navigate={navigate} searching={Boolean(filters.query)} tab={tab}
                setTab={selectTab} token={token} asOf={filters.asOf} onUnauthorized={onUnauthorized} /></div>
            </>
          ) : <p role="status">Loading project inventory…</p>)}
      </section>
    </InspectorView>
  );
}
