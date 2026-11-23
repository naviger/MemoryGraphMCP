import { useEffect, useState } from 'react';
import { reviewRequest } from './api.js';
import { formatDate } from './Details.jsx';

const COUNT_LIMIT = 100;

const projectDate = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' });

function ProjectDate({ value, fallback }) {
  return value ? <time dateTime={value}>{projectDate.format(new Date(value))}</time>
    : fallback ? <time dateTime={fallback} title="Date inferred from project records">{formatDate(fallback)}</time>
      : <span className="muted">Unrecorded</span>;
}

function ProjectRow({ project, token, navigate, onUnauthorized, refresh }) {
  const [queue, setQueue] = useState(null);
  const [error, setError] = useState('');
  const [summary, setSummary] = useState(null);
  const [summaryError, setSummaryError] = useState('');
  const overview = `/projects/${encodeURIComponent(project.key)}`;
  useEffect(() => {
    const controller = new AbortController();
    setQueue(null);
    setError('');
    setSummary(null);
    setSummaryError('');
    const params = new URLSearchParams({ project_key: project.key, limit: String(COUNT_LIMIT) });
    reviewRequest(`/queue?${params}`, { token, signal: controller.signal })
      .then((data) => { if (!controller.signal.aborted) setQueue(data); })
      .catch((err) => {
        if (controller.signal.aborted) return;
        if (err.status === 401) onUnauthorized();
        else setError(err.message);
      });
    reviewRequest(`${overview}/summary`, { token, signal: controller.signal })
      .then((data) => { if (!controller.signal.aborted) setSummary(data); })
      .catch((err) => {
        if (controller.signal.aborted) return;
        if (err.status === 401) onUnauthorized();
        else setSummaryError(err.message);
      });
    return () => controller.abort();
  }, [overview, project.key, token, onUnauthorized, refresh]);

  const countLink = (tab, label, count, failure) => {
    if (failure) return <span className="muted" title={failure}>Unavailable</span>;
    if (count == null) return <span className="muted" role="status">Loading…</span>;
    const href = `${overview}?tab=${tab}`;
    return <a href={href} aria-label={`${project.name}: ${count} ${label}`}
      onClick={(event) => navigate(event, href, project.key)}>{count}</a>;
  };
  const reviewCount = (items) => items?.length === COUNT_LIMIT ? `${COUNT_LIMIT}+` : items?.length;

  return (
    <tr>
      <th scope="row">
        <a href={`${overview}?tab=Overview`}
          onClick={(event) => navigate(event, `${overview}?tab=Overview`, project.key)}>{project.name}</a>
        {project.description && <p className="muted">{project.description}</p>}
      </th>
      <td><ProjectDate value={project.created_at} fallback={summary?.earliest_record} /></td>
      <td><ProjectDate value={project.updated_at} fallback={summary?.latest_record} /></td>
      <td>{countLink('Overview', 'open tasks', summary?.open_tasks, summaryError)}</td>
      <td>{countLink('Decisions', 'ADRs awaiting a decision', summary?.proposed_adrs, summaryError)}</td>
      <td>{countLink('Review', 'overdue validations', reviewCount(queue?.overdue_validations), error)}</td>
      <td>{countLink('Review', 'unresolved ADR conflicts', reviewCount(queue?.unresolved_adr_conflicts), error)}</td>
      <td>{summaryError ? <span className="muted" title={summaryError}>Unavailable</span>
        : !summary ? <span className="muted" role="status">Loading…</span>
        : summary.last_index_date ? <a href={`${overview}?tab=Code`}
          onClick={(event) => navigate(event, `${overview}?tab=Code`, project.key)}>
          <time dateTime={summary.last_index_date}>{formatDate(summary.last_index_date)}</time>
        </a> : <span className="muted">Unrecorded</span>}</td>
    </tr>
  );
}

export function HomePage({ projects, token, navigate, onUnauthorized, refresh }) {
  if (projects === null) return <p role="status">Loading projects…</p>;
  if (projects.length === 0) return <p className="muted">No projects to show.</p>;

  return (
    <div className="table-scroll detail">
      <table className="home-project-table" aria-label="Projects">
        <thead>
          <tr>
            <th scope="col">Project</th>
            <th scope="col">Created</th>
            <th scope="col">Last edited</th>
            <th scope="col">Open tasks</th>
            <th scope="col" title="ADRs awaiting a decision">Proposed ADRs</th>
            <th scope="col" title="Overdue validations">Overdue reviews</th>
            <th scope="col" title="Unresolved ADR conflicts">ADR conflicts</th>
            <th scope="col" title="Last successful code index">Code indexed</th>
          </tr>
        </thead>
        <tbody>
          {projects.map((project) => (
            <ProjectRow key={project.key} project={project} token={token} navigate={navigate}
              onUnauthorized={onUnauthorized} refresh={refresh} />
          ))}
        </tbody>
      </table>
    </div>
  );
}
