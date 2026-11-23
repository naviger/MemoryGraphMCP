import { useEffect, useState } from 'react';
import { reviewRequest } from './api.js';

const dates = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
const statusLabels = { open: 'Open', draft: 'Draft', merged: 'Merged', closed: 'Closed' };
const ciLabels = { passed: 'Passed', failed: 'Failed', cancelled: 'Cancelled' };

function GitHubLink({ href, children }) {
  return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
}

export default function ProjectChanges({ projectKey, token, onUnauthorized, active }) {
  const [view, setView] = useState('pulls');
  const [repositoryKey, setRepositoryKey] = useState('');
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    const params = new URLSearchParams({ view });
    if (repositoryKey) params.set('repository_key', repositoryKey);
    setLoading(true);
    setError('');
    reviewRequest(`/projects/${encodeURIComponent(projectKey)}/changes?${params}`, {
      token, signal: controller.signal,
    }).then((result) => {
      if (!controller.signal.aborted) setData(result);
    }).catch((err) => {
      if (controller.signal.aborted) return;
      if (err.status === 401) onUnauthorized();
      else setError(err.message);
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [active, projectKey, view, repositoryKey, token, onUnauthorized]);

  const repositories = data?.repositories ?? [];
  const byKey = new Map(repositories.map((repo) => [repo.key, repo]));
  const selected = repositoryKey ? repositories.filter((repo) => repo.key === repositoryKey) : repositories;
  const pulls = view === 'pulls';

  return <div className="change-history">
    <div className="change-toolbar">
      <div role="group" aria-label="Change history view">
        {[['pulls', 'Pull Requests'], ['commits', 'Commits']].map(([value, label]) => (
          <button type="button" key={value}
            className={`button ${view === value ? 'primary' : 'outlined'}`}
            aria-pressed={view === value} onClick={() => setView(value)}>{label}</button>
        ))}
      </div>
      {repositories.length > 1 && <label>Repository
        <select value={repositoryKey} onChange={(event) => setRepositoryKey(event.target.value)}>
          <option value="">All repositories</option>
          {repositories.map((repo) => <option value={repo.key} key={repo.key}>
            {repo.name}{repo.github ? '' : ' (GitHub connection unavailable)'}
          </option>)}
        </select>
      </label>}
    </div>
    <div aria-busy={loading}>
      {loading ? <p role="status">Loading {pulls ? 'pull requests' : 'commits'}…</p>
        : error ? <p role="alert" className="status error">{error}</p>
        : !selected.some((repo) => repo.github) ? <p>No GitHub history is configured for the selected repositories.</p>
        : <>
          {data.unavailable_repositories.length > 0 && <p role="status">
            GitHub history is unavailable for {data.unavailable_repositories.map((key) => byKey.get(key)?.name).join(', ')}.
          </p>}
          {data.changes.length === 0 ? <p>No {pulls ? 'pull requests' : 'commits'} to show.</p> : <>
            <p className="muted">Up to {data.limit_per_repository} recent {pulls ? 'pull requests' : 'default branch commits'} per repository.</p>
            <div className="change-table-scroll">
              <table className="change-table">
                <thead><tr>
                  <th scope="col">{pulls ? 'Pull request' : 'Commit'}</th>
                  <th scope="col">Repository</th>
                  <th scope="col">Author</th>
                  <th scope="col">{pulls ? 'Opened or merged' : 'Committed'}</th>
                  <th scope="col">CI result</th>
                  {pulls && <th scope="col">Branch cleanup</th>}
                </tr></thead>
                <tbody>
                  {data.changes.map((change) => <tr key={change.url}>
                    <td>
                      <GitHubLink href={change.url}>
                        {change.number ? `#${change.number} ` : ''}{change.title.split('\n')[0]}
                      </GitHubLink>
                      <div className="muted">
                        {change.status && statusLabels[change.status]}{change.status && change.commit && ' · '}
                        {change.commit && <code>{change.commit.slice(0, 8)}</code>}
                      </div>
                    </td>
                    <td>{byKey.get(change.repository_key)?.name}</td>
                    <td>{change.author || 'Unknown'}</td>
                    <td><time dateTime={change.date}>{dates.format(new Date(change.date))}</time></td>
                    <td>{change.ci
                      ? <GitHubLink href={change.ci.url}>{ciLabels[change.ci.status]} · {change.ci.run_id}</GitHubLink>
                      : <span className="muted">No imported run</span>}</td>
                    {pulls && <td>{change.cleanup ? <>
                      <strong>Remote branch deleted</strong>
                      <div><code>{change.cleanup.branch}</code></div>
                      <time dateTime={change.cleanup.deleted_at}>{dates.format(new Date(change.cleanup.deleted_at))}</time>
                    </> : <span className="muted">No cleanup recorded</span>}</td>}
                  </tr>)}
                </tbody>
              </table>
            </div>
          </>}
        </>}
    </div>
  </div>;
}
