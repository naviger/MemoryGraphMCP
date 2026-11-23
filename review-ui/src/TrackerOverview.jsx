import { useEffect, useState } from 'react';
import { reviewRequest } from './api.js';
import { PreviewLink } from './RecordInspector.jsx';

const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });

function EventTime({ value }) {
  return <time dateTime={value}>{dateTime.format(new Date(value))}</time>;
}

export default function TrackerOverview({ projectKey, token, active, navigate, onUnauthorized }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    setError('');
    reviewRequest(`/projects/${encodeURIComponent(projectKey)}/tracker`, {
      token, signal: controller.signal,
    }).then((result) => {
      if (!controller.signal.aborted) setData(result);
    }).catch((err) => {
      if (controller.signal.aborted) return;
      if (err.status === 401) onUnauthorized();
      else setError(err.message);
    });
    return () => controller.abort();
  }, [active, projectKey, token, onUnauthorized]);

  const events = data?.recent_events ?? [];
  const last = events[0];
  return <>
    <section className="detail">
      <h3>Tracker Status</h3>
      {error ? <p role="alert" className="status error">{error}</p>
        : !data ? <p role="status">Loading tracker status…</p>
        : <>
          <p>{data.configured ? 'Ingestion configured' : 'Ingestion unconfigured for this project'}</p>
          <p>Last event {last ? <><code>{last.event_id}</code> · <EventTime value={last.recorded_at} /></> : '— none recorded'}</p>
        </>}
    </section>
    {data && !error && <section className="detail">
      <h3>Recent Tracker Activity</h3>
      {events.length === 0 ? <p>No tracker events recorded.</p> : <div className="table-scroll">
        <table className="tracker-activity" aria-label="Recent tracker events">
          <thead><tr>
            <th scope="col">Event</th><th scope="col">Task</th>
            <th scope="col">Occurred</th><th scope="col">Outcome</th>
          </tr></thead>
          <tbody>{events.map((event) => (
            <tr key={`${event.recorded_at}/${event.event_id}`}>
              <td>{event.event_type === 'task.completed' ? 'Task completed' : 'Task upserted'}
                <div><code>{event.event_id}</code></div>
              </td>
              <td>{event.task_name ? <PreviewLink destination="entities"
                entityKey={event.task_key} navigate={navigate}>{event.task_name}</PreviewLink>
                : <code>{event.task_key}</code>}</td>
              <td><EventTime value={event.occurred_at} /></td>
              <td>{event.outcome === 'applied' ? 'Applied' : 'Ignored · stale timestamp'}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>}
    </section>}
  </>;
}
