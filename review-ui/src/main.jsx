import {
  Activity, CalendarClock, CheckCircle2, ChevronLeft, ChevronRight, CircleAlert, CircleHelp, FolderKanban, GitPullRequest,
  House, KeyRound, LogOut, MessageCircleQuestion, Network, RefreshCw, Search, ShieldCheck,
} from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ApiError, reviewRequest } from './api.js';
import { EffectivenessPage } from './EffectivenessPage.jsx';
import { EntityDetailPage } from './EntityDetailPage.jsx';
import { HomePage } from './HomePage.jsx';
import { ProjectsPage } from './ProjectsPage.jsx';
import { HelpPage } from './HelpPage.jsx';
import ProjectGuidance from './ProjectGuidance.jsx';
import { GraphPage } from './GraphPage.jsx';
import { SearchMemoryPage } from './SearchMemoryPage.jsx';
import { AskGraphPage } from './AskGraphPage.jsx';
import { ConflictDetail, EmptyDetail, ValidationDetail, formatDate, formatWindow } from './Details.jsx';
import './styles.css';

const PAGE_SIZE = 12;
const SECTIONS = {
  validations: { label: 'Overdue validations', icon: CalendarClock, tone: 'amber', description: 'Review records with overdue validations and update their confidence.' },
  conflicts: { label: 'ADR conflicts', icon: GitPullRequest, tone: 'violet', description: 'Compare unresolved decisions, review their evidence, and record a resolution.' },
};
const INITIAL_OFFSETS = { validations: 0, conflicts: 0 };

function conflictKey(conflict) {
  return `${conflict.original.key}/${conflict.competing.key}`;
}

function SignIn({ onConnect, checking, error, oidcEnabled }) {
  const [draft, setDraft] = useState('');
  const returnTo = window.location.pathname + window.location.search + window.location.hash;
  return (
    <div className="connect-card">
      <div className="detail-icon teal"><KeyRound size={24} aria-hidden="true" /></div>
      <h2>Connect to MemoryGraphMCP</h2>
      {oidcEnabled ? <>
        {error && <p role="alert" className="status error"><CircleAlert size={17} />{error}</p>}
        <a className="button primary" href={`/api/auth/login?return_to=${encodeURIComponent(returnTo)}`}>
          Sign in
        </a>
      </> : <>
      <p>Enter a bearer token from your configured identity provider to load the review queue.</p>
      <form onSubmit={(event) => {
        event.preventDefault();
        const value = draft.trim().replace(/^Bearer\s+/i, '');
        if (!value) return;
        setDraft('');
        onConnect(value);
      }}>
        <label>Access token
          <input type="password" value={draft} onChange={(event) => setDraft(event.target.value)}
            autoComplete="off" spellCheck={false} required placeholder="Paste your bearer token" />
        </label>
        <p className="hint">The token is held in this tab until you disconnect or reload.</p>
        {error && <p role="alert" className="status error"><CircleAlert size={17} />{error}</p>}
        <button type="submit" className="button primary" disabled={checking || !draft.trim()}>
          {checking ? 'Connecting…' : 'Connect'}
        </button>
      </form>
      </>}
    </div>
  );
}

function QueueList({ section, rows, selectedKey, select, offset, setOffset, hasMore, loading }) {
  const LabelIcon = SECTIONS[section].icon;
  return (
    <section className="queue-list" aria-label={SECTIONS[section].label} aria-busy={loading}>
      <header className="list-heading">
        <h2><LabelIcon size={17} aria-hidden="true" />{SECTIONS[section].label}</h2>
        <span className="list-count">{rows.length}{hasMore ? '+' : ''}</span>
      </header>
      {loading && !rows.length && <p className="empty-list" role="status">Loading the queue…</p>}
      {!loading && !rows.length && <p className="empty-list">No entries on this page.</p>}
      <ul className="entries">
        {rows.map((row) => {
          const isValidation = section === 'validations';
          const key = isValidation ? row.key : conflictKey(row);
          return (
            <li key={key}>
              <button type="button" aria-pressed={key === selectedKey}
                className={key === selectedKey ? 'entry selected' : 'entry'} onClick={() => select(key)}>
                <span className="entry-meta">
                  <span className={`dot ${SECTIONS[section].tone}`} />
                  {isValidation ? row.kind : row.original.conflict_scope?.topic || 'ADR pair'}
                </span>
                <strong>{isValidation ? row.title : `${row.original.title} / ${row.competing.title}`}</strong>
                <span className="entry-date">
                  {isValidation ? `Due ${formatDate(row.next_review_due)}` : formatWindow(row.overlap_start, row.overlap_end)}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      <footer className="pagination">
        <span>{offset ? `Page ${Math.floor(offset / PAGE_SIZE) + 1}` : 'Page 1'}</span>
        <button className="button icon outlined" type="button" aria-label="Previous page"
          disabled={loading || offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
          <ChevronLeft size={17} aria-hidden="true" />
        </button>
        <button className="button icon outlined" type="button" aria-label="Next page"
          disabled={loading || !hasMore} onClick={() => setOffset(offset + PAGE_SIZE)}>
          <ChevronRight size={17} aria-hidden="true" />
        </button>
      </footer>
    </section>
  );
}

function detailLocation() {
  if (/^\/search\/?$/.test(window.location.pathname)) return { destination: 'search' };
  if (/^\/help\/?$/.test(window.location.pathname)) return { destination: 'help' };
  if (/^\/global-guidance\/?$/.test(window.location.pathname)) return { destination: 'global-guidance' };
  if (/^\/effectiveness\/?$/.test(window.location.pathname)) return { destination: 'effectiveness' };
  const review = window.location.pathname.match(/^\/review\/(validations|conflicts)\/?$/);
  if (review) return { destination: 'review', key: review[1] };
  if (/^\/projects\/?$/.test(window.location.pathname)) return { destination: 'projects' };
  if (/^\/graph\/?$/.test(window.location.pathname)) return { destination: 'graph' };
  if (/^\/ask\/?$/.test(window.location.pathname)) return { destination: 'ask' };
  const match = window.location.pathname.match(/^\/(records|entities|projects|graph)\/([^/]+)\/?$/);
  if (!match) return null;
  try {
    return { destination: match[1], key: decodeURIComponent(match[2]),
      tab: match[1] === 'projects' ? new URLSearchParams(window.location.search).get('tab') : null };
  } catch {
    return null;
  }
}

const PROJECT_SCOPE_STORAGE = 'memorygraph.projectScope';

function readNavigation() {
  const route = detailLocation();
  const params = new URLSearchParams(window.location.search);
  const projectKey = (route?.destination === 'projects' && route.key)
    || (params.has('project') ? params.get('project') : sessionStorage.getItem(PROJECT_SCOPE_STORAGE))
    || '';
  return { route, projectKey };
}

function App() {
  const [{ route, projectKey, restore }, setNavigation] = useState(readNavigation);
  const [projects, setProjects] = useState(null);
  const [projectError, setProjectError] = useState('');
  const [graphTarget, setGraphTarget] = useState(() =>
    route?.destination === 'graph' ? route.key ?? '' : null);
  const projectName = projects?.find((item) => item.key === projectKey)?.name;
  const graphHref = graphTarget ? `/graph/${encodeURIComponent(graphTarget)}` : '/graph';
  const queueSection = route?.destination === 'review' ? route.key : null;
  const section = queueSection ?? 'validations';
  const isQueueRoute = !route || Boolean(queueSection);
  const [selection, setSelection] = useState({ validations: null, conflicts: null });
  const [offsets, setOffsets] = useState(INITIAL_OFFSETS);
  const [asOf, setAsOf] = useState('');
  const [token, setToken] = useState('');
  const [session, setSession] = useState(null);
  const [connectionAttempt, setConnectionAttempt] = useState(0);
  const [checking, setChecking] = useState(true);
  const [authError, setAuthError] = useState(() =>
    new URLSearchParams(window.location.search).has('auth_error') ? 'Sign-in failed. Please try again.' : '');
  const [oidcEnabled, setOidcEnabled] = useState(false);
  const [queue, setQueue] = useState(null);
  const [queueError, setQueueError] = useState('');
  const [message, setMessage] = useState(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const sessionEpoch = useRef(0);
  const returnTargets = useRef(new Map());

  useEffect(() => {
    const controller = new AbortController();
    fetch('/api/auth/config', { signal: controller.signal })
      .then((response) => response.ok ? response.json() : null)
      .then((config) => { if (!controller.signal.aborted) setOidcEnabled(Boolean(config?.oidc_enabled)); })
      .catch(() => {});
    const url = new URL(window.location.href);
    if (url.searchParams.has('auth_error')) {
      url.searchParams.delete('auth_error');
      window.history.replaceState(window.history.state, '', url);
    }
    return () => controller.abort();
  }, []);

  useEffect(() => {
    sessionStorage.setItem(PROJECT_SCOPE_STORAGE, projectKey);
  }, [projectKey]);

  useEffect(() => {
    const initial = new URL(window.location.href);
    if (!initial.searchParams.has('project')) {
      initial.searchParams.set('project', readNavigation().projectKey);
      window.history.replaceState(window.history.state, '', initial);
    }
    const priorRestoration = window.history.scrollRestoration;
    window.history.scrollRestoration = 'manual';
    const onHistory = (event) => {
      const next = readNavigation();
      setNavigation({ ...next, restore: event.state ?? {} });
      if (next.route?.destination === 'graph') setGraphTarget(next.route.key ?? '');
    };
    window.addEventListener('popstate', onHistory);
    return () => {
      window.removeEventListener('popstate', onHistory);
      window.history.scrollRestoration = priorRestoration;
    };
  }, []);

  useLayoutEffect(() => {
    if (!restore) return;
    const { scrollY, selectedPath } = restore;
    const saved = returnTargets.current.get(window.location.href);
    const link = saved?.isConnected ? saved : selectedPath
      ? [...document.querySelectorAll('main a[href]')].find((item) =>
          new URL(item.href).pathname === selectedPath && item.getClientRects().length)
      : null;
    link?.classList.add('return-selection');
    link?.focus({ preventScroll: true });
    window.scrollTo(0, scrollY ?? 0);
  }, [restore]);

  function openPage(href, scope = projectKey) {
    const url = new URL(href, window.location.origin);
    url.searchParams.set('project', scope);
    if (url.pathname === window.location.pathname && url.search === window.location.search) return;
    const canReturn = /^\/(records|entities|graph)\//.test(url.pathname);
    window.history.replaceState({
      ...window.history.state,
      scrollY: window.scrollY,
      selectedPath: canReturn ? url.pathname : null,
    }, '');
    window.history.pushState(
      canReturn ? { returnTo: window.location.pathname + window.location.search } : {}, '', url
    );
    const next = readNavigation();
    setNavigation(next);
    if (next.route?.destination === 'graph') setGraphTarget(next.route.key ?? '');
    window.scrollTo(0, 0);
  }

  function navigate(event, href, scope = projectKey) {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || (event.button != null && event.button !== 0)) return;
    event.preventDefault();
    if (/^\/(records|entities|graph)\//.test(href) && event.currentTarget instanceof HTMLAnchorElement) {
      const origin = window.location.href;
      returnTargets.current.get(origin)?.classList.remove('return-selection');
      returnTargets.current.set(origin, event.currentTarget);
      event.currentTarget.classList.add('return-selection');
    }
    openPage(href, scope);
  }

  function changeProject(event) {
    const key = event.target.value;
    const tab = new URLSearchParams(window.location.search).get('tab');
    const path = route?.destination === 'projects'
      ? tab ? `/projects?tab=${encodeURIComponent(tab)}` : '/projects'
      : route?.destination === 'graph' ? '/graph' : window.location.pathname;
    setOffsets(INITIAL_OFFSETS);
    setSelection({ validations: null, conflicts: null });
    setGraphTarget(route?.destination === 'graph' ? '' : null);
    openPage(path, key);
  }

  const expireSession = useCallback(() => {
    setSession(null);
    setAuthError('Your session has expired. Sign in to reconnect.');
  }, []);

  function changeToken(value) {
    sessionEpoch.current += 1;
    returnTargets.current.clear();
    setToken(value);
    setConnectionAttempt((attempt) => attempt + 1);
    setAuthError('');
    setChecking(true);
    setSession(null);
    setQueue(null);
    setMessage(null);
    setSaving(false);
    setOffsets(INITIAL_OFFSETS);
    setSelection({ validations: null, conflicts: null });
  }

  async function signOut() {
    setAuthError('');
    try {
      const response = await fetch('/api/auth/logout', { method: 'POST' });
      if (!response.ok) throw new Error('Could not sign out. Please try again.');
      const { logout_url: logoutUrl } = await response.json();
      window.location.assign(logoutUrl);
    } catch {
      setAuthError('Could not sign out. Please try again.');
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    reviewRequest('/session', { token, signal: controller.signal })
      .then((result) => {
        setAuthError('');
        setSession(result);
      })
      .catch((error) => {
        if (error.name === 'AbortError') return;
        if (error.status !== 401 || token) setAuthError(error.message);
        setSession(null);
      })
      .finally(() => { if (!controller.signal.aborted) setChecking(false); });
    return () => controller.abort();
  }, [token, connectionAttempt]);

  useEffect(() => {
    if (!session) return;
    const controller = new AbortController();
    setProjects(null);
    setProjectError('');
    reviewRequest('/graph/entities?entity_type=Project&limit=100', { token, signal: controller.signal })
      .then(setProjects).catch((error) => {
        if (controller.signal.aborted) return;
        if (error.status === 401) expireSession();
        else setProjectError(error.message);
      });
    return () => controller.abort();
  }, [session, token, expireSession]);

  useEffect(() => {
    if (!session || !queueSection) return undefined;
    const controller = new AbortController();
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE + 1),
      validation_offset: String(offsets.validations),
      conflict_offset: String(offsets.conflicts),
    });
    if (asOf) params.set('as_of', asOf);
    if (projectKey) params.set('project_key', projectKey);
    setLoading(true);
    setQueueError('');
    reviewRequest(`/queue?${params}`, { token, signal: controller.signal })
      .then(setQueue)
      .catch((error) => {
        if (error.name === 'AbortError') return;
        setQueueError(error.message);
        if (error.status === 401) expireSession();
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [session, token, asOf, offsets, refresh, projectKey, queueSection, expireSession]);

  async function save(path, body, success) {
    const epoch = sessionEpoch.current;
    setSaving(true);
    setMessage(null);
    try {
      const result = await reviewRequest(path, { token, body });
      if (sessionEpoch.current !== epoch) return;
      setMessage({ kind: 'success', text: success(result) });
      setOffsets(INITIAL_OFFSETS);
      setRefresh((value) => value + 1);
    } catch (error) {
      if (sessionEpoch.current !== epoch) return;
      if (error instanceof ApiError && error.status === 401) {
        expireSession();
      } else {
        setMessage({ kind: 'error', text: error.message });
      }
    } finally {
      if (sessionEpoch.current === epoch) setSaving(false);
    }
  }

  const entries = {
    validations: queue?.overdue_validations ?? [],
    conflicts: queue?.unresolved_adr_conflicts ?? [],
  };
  const keyFor = section === 'validations' ? (row) => row.key : conflictKey;
  const rows = entries[section].slice(0, PAGE_SIZE);
  const selected = rows.find((row) => keyFor(row) === selection[section]) ?? rows[0];
  const signedIn = Boolean(session?.subject);
  return (
    <>
      <header className="topbar">
        <div className="brand"><span className="brand-icon"><Network size={21} aria-hidden="true" /></span>
          <span>MemoryGraph<span className="brand-suffix">MCP</span></span>
        </div>
        {session && (
          <div className="session">
            <label className="project-scope-picker">Project
              <select value={projectKey} onChange={changeProject} disabled={!projects && !projectKey}>
                <option value="">All projects</option>
                {projectKey && !projectName && <option value={projectKey}>Selected project</option>}
                {projects?.map((item) => <option key={item.key} value={item.key}>{item.name}</option>)}
              </select>
            </label>
            <span className="access"><ShieldCheck size={16} aria-hidden="true" />
              {session.role === 'admin' ? 'Admin access' : session.can_write ? 'Writer access' : 'Read-only'}</span>
            {signedIn && session.display_name && (
              <span className="subject" title={session.display_name}>{session.display_name}</span>
            )}
            {signedIn && <button className="button top-button"
              onClick={session.browser_session ? signOut : () => changeToken('')}>
              <LogOut size={16} aria-hidden="true" /> {session.browser_session ? 'Sign out' : 'Disconnect'}
            </button>}
            {session && authError && <span role="alert">{authError}</span>}
          </div>
        )}
      </header>
      <div className="layout">
        <aside className="rail">
          <nav aria-label="Review sections">
            <a href="/" className={!route ? 'nav-item active' : 'nav-item'}
              aria-current={!route ? 'page' : undefined} onClick={(event) => navigate(event, '/')}>
              <House size={18} aria-hidden="true" /> Home
            </a>
            <a href="/projects" className={route?.destination === 'projects' ? 'nav-item active' : 'nav-item'}
              aria-current={route?.destination === 'projects' ? 'page' : undefined}
              onClick={(event) => navigate(event, '/projects')}>
              <FolderKanban size={18} aria-hidden="true" /> Projects
            </a>
            <a href={graphHref} className={route?.destination === 'graph' ? 'nav-item active' : 'nav-item'}
              aria-current={route?.destination === 'graph' ? 'page' : undefined}
              onClick={(event) => navigate(event, graphHref)}>
              <Network size={18} aria-hidden="true" /> Graph
            </a>
            <a href="/ask" className={route?.destination === 'ask' ? 'nav-item active' : 'nav-item'}
              aria-current={route?.destination === 'ask' ? 'page' : undefined}
              onClick={(event) => navigate(event, '/ask')}>
              <MessageCircleQuestion size={18} aria-hidden="true" /> Ask the Graph
            </a>
            <a href="/search" className={route?.destination === 'search' ? 'nav-item active' : 'nav-item'}
              aria-current={route?.destination === 'search' ? 'page' : undefined}
              onClick={(event) => navigate(event, '/search')}>
              <Search size={18} aria-hidden="true" /> Search Memory
            </a>
            {session && <a href="/effectiveness" className={route?.destination === 'effectiveness' ? 'nav-item active' : 'nav-item'}
              aria-current={route?.destination === 'effectiveness' ? 'page' : undefined}
              onClick={(event) => navigate(event, '/effectiveness')}>
              <Activity size={18} aria-hidden="true" /> Effectiveness
            </a>}
            {session?.role === 'admin' && (
              <a href="/global-guidance" className={route?.destination === 'global-guidance' ? 'nav-item active' : 'nav-item'}
                aria-current={route?.destination === 'global-guidance' ? 'page' : undefined}
                onClick={(event) => navigate(event, '/global-guidance')}>
                <ShieldCheck size={18} aria-hidden="true" /> Global Guidance
              </a>
            )}
          </nav>
          <nav className="rail-help" aria-label="Help">
            <a href="/help" className={route?.destination === 'help' ? 'nav-item active' : 'nav-item'}
              aria-current={route?.destination === 'help' ? 'page' : undefined}
              onClick={(event) => navigate(event, '/help')}>
              <CircleHelp size={18} aria-hidden="true" /> Help
            </a>
          </nav>
        </aside>
        <main className={session && route?.destination === 'graph' ? 'graph-main' : undefined}>
          {session && projectError && <p role="alert" className="status error">{projectError}</p>}
          {session && (
            <div hidden={route?.destination !== 'ask'}>
              <AskGraphPage key={projectKey} projectKey={projectKey} token={token} navigate={navigate} onUnauthorized={expireSession} />
            </div>
          )}
          {session && (
            <div hidden={route?.destination !== 'search'}>
              <SearchMemoryPage key={projectKey} projectKey={projectKey} projectName={projectName}
                token={token} navigate={navigate} onUnauthorized={expireSession} />
            </div>
          )}
          {session && (
            <div hidden={route?.destination !== 'projects'}>
              <ProjectsPage key={projectKey} projectKey={projectKey} token={token} requestedTab={route?.tab}
                navigate={navigate} onUnauthorized={expireSession} />
            </div>
          )}
          {session && graphTarget !== null && (
            <div className="graph-host" hidden={route?.destination !== 'graph'}>
              <GraphPage key={`${projectKey}/${graphTarget}`} initialFocus={graphTarget || projectKey}
                projectKey={projectKey} token={token} navigate={navigate} onUnauthorized={expireSession}
                returnTo={route?.destination === 'graph' ? window.history.state?.returnTo : null} />
            </div>
          )}
          {session && route?.destination === 'help' && <HelpPage token={token} onUnauthorized={expireSession} />}
          {session && route?.destination === 'global-guidance' && <>
            <header className="page-heading"><div>
              <h1>Global Guidance</h1>
              <p>Publish instructions that apply across all projects.</p>
            </div></header>
            {session.role === 'admin'
              ? <ProjectGuidance global active token={token} onUnauthorized={expireSession} />
              : <p role="alert">Administrator access is required.</p>}
          </>}
          {session && projects && route?.destination === 'effectiveness' && <EffectivenessPage
            key={projectKey} projectKey={projectKey} projects={projects} token={token} onUnauthorized={expireSession} />}
          {session && !projects && route?.destination === 'effectiveness' && !projectError &&
            <p role="status">Loading projects…</p>}
          {session && ['ask', 'search', 'graph', 'projects', 'help', 'global-guidance', 'effectiveness'].includes(route?.destination)
            ? null : session && !isQueueRoute ? (
            <EntityDetailPage key={`${route.destination}/${route.key}`}
              route={route} token={token} navigate={navigate} onUnauthorized={expireSession}
              returnTo={window.history.state?.returnTo} />
          ) : (
          <>
          <header className="page-heading">
            <div>
              <h1>{queueSection ? SECTIONS[section].label : 'Home'}</h1>
              <p>{queueSection ? SECTIONS[section].description : 'Review open issues across projects or open a project overview.'}</p>
            </div>
            {session && queueSection && <div className="toolbar">
              <label>As of <input type="date" value={asOf} onChange={(event) => {
                setAsOf(event.target.value); setOffsets(INITIAL_OFFSETS);
              }} /></label>
              {asOf && <button className="button text-button" type="button" onClick={() => {
                setAsOf(''); setOffsets(INITIAL_OFFSETS);
              }}>Clear</button>}
              <button className="button outlined refresh" type="button" disabled={loading || saving}
                onClick={() => setRefresh((value) => value + 1)}>
                <RefreshCw size={17} className={loading ? 'spinning' : ''} aria-hidden="true" /> Refresh
              </button>
            </div>}
          </header>
          {session && !queueSection ? (
            <HomePage projects={projectError ? [] : projects} token={token} navigate={navigate}
              onUnauthorized={expireSession} refresh={refresh} />
          ) : session ? (
            <>
              {queue && <p className="review-date">Review date <strong>{formatDate(queue.review_date)}</strong></p>}
              {queueError && <p role="alert" className="status error"><CircleAlert size={18} />{queueError}</p>}
              {message && <p role={message.kind === 'error' ? 'alert' : 'status'} className={`status ${message.kind}`}>
                {message.kind === 'error' ? <CircleAlert size={18} /> : <CheckCircle2 size={18} />}{message.text}
              </p>}
              <div className="workspace">
                <QueueList section={section} rows={rows} selectedKey={selected && keyFor(selected)}
                  select={(value) => {
                    setSelection((current) => ({ ...current, [section]: value }));
                    setMessage(null);
                  }}
                  offset={offsets[section]} hasMore={entries[section].length > PAGE_SIZE} loading={loading}
                  setOffset={(value) => setOffsets((current) => ({ ...current, [section]: value }))} />
                {!selected ? <EmptyDetail>{loading ? 'Loading review items…' : queueError ? 'Refresh to try loading the queue again.' :
                  section === 'validations' ? 'No overdue validations on this page.' : 'No unresolved conflicts on this page.'}</EmptyDetail>
                : section === 'validations'
                  ? <ValidationDetail record={selected} canWrite={session.can_write} busy={saving || loading} navigate={navigate}
                    onSubmit={(key, evidence, confidence) => save(
                      `/records/${encodeURIComponent(key)}/revalidate`, { evidence, confidence },
                      (result) => `Validation saved. Next review ${formatDate(result.next_review_due)}.`,
                    )} />
                  : <ConflictDetail conflict={selected} canWrite={session.can_write} busy={saving || loading} navigate={navigate}
                    onSubmit={(body) => save('/conflicts/resolve', body, () => 'Conflict resolution recorded.')} />}
              </div>
            </>
          ) : checking && !token
            ? <p className="initial-status" role="status">Checking the review service…</p>
            : <SignIn onConnect={changeToken} checking={checking} error={authError} oidcEnabled={oidcEnabled} />}
          </>
          )}
        </main>
      </div>
    </>
  );
}

createRoot(document.getElementById('root')).render(<App />);
