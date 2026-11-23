import { useEffect, useState } from 'react';
import { reviewRequest } from './api.js';

const dates = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });

function Published({ entry }) {
  return <>
    <p className="muted">Version {entry.version} · Published {dates.format(new Date(entry.published_at))}
      {' '}by {entry.approved_by} · Draft by {entry.edited_by}</p>
    <div className="guidance-text">{entry.text}</div>
  </>;
}

export default function ProjectGuidance({ projectKey, token, active, onUnauthorized }) {
  const [data, setData] = useState(null);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [reload, setReload] = useState(0);
  const path = `/projects/${encodeURIComponent(projectKey)}/guidance`;
  const handleError = (err) => {
    if (err.status === 401) onUnauthorized();
    else setError(err.message);
  };
  function accept(saved) {
    setData(saved);
    setText(saved.draft?.text ?? saved.published?.text ?? '');
    setConfirming(false);
  }

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    setBusy(true);
    setError('');
    reviewRequest(path, { token, signal: controller.signal })
      .then((saved) => { if (!controller.signal.aborted) accept(saved); })
      .catch((err) => { if (!controller.signal.aborted) handleError(err); })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [active, path, token, onUnauthorized, reload]);

  async function submit(action) {
    setBusy(true);
    setNotice('');
    setError('');
    const body = action === 'draft'
      ? { revision: data.revision, text }
      : { revision: data.revision, approve: true };
    try {
      const saved = await reviewRequest(`${path}/${action}`, { token, body });
      accept(saved);
      setNotice(action === 'draft' ? 'Draft saved for approval.' : `Version ${saved.published.version} published.`);
    } catch (err) {
      handleError(err);
    } finally {
      setBusy(false);
    }
  }

  async function loadOlder() {
    setBusy(true);
    setError('');
    try {
      const older = await reviewRequest(`${path}?before_version=${data.next_before_version}`, { token });
      setData((previous) => ({
        ...previous, history: [...previous.history, ...older.history],
        next_before_version: older.next_before_version,
      }));
    } catch (err) {
      handleError(err);
    } finally {
      setBusy(false);
    }
  }

  const dirty = data && text.trim() !== (data.draft?.text ?? data.published?.text ?? '');
  return <section className="detail" aria-busy={busy}>
    <h3>Project Guidance</h3>
    {busy && <p role="status">Working…</p>}
    {notice && <p role="status">{notice}</p>}
    {error && <div role="alert">
      <p className="status error">{error}</p>
      <button type="button" className="button outlined" disabled={busy}
        onClick={() => setReload((value) => value + 1)}>Reload guidance</button>
    </div>}
    {data && <>
      <h4>Published guidance</h4>
      {data.published ? <Published entry={data.published} /> : <p>No guidance has been published.</p>}
      {data.can_edit && <form onSubmit={(event) => { event.preventDefault(); submit('draft'); }}>
        <h4>Draft</h4>
        {data.draft && <p className="muted">Saved by {data.draft.edited_by} · {dates.format(new Date(data.draft.edited_at))}</p>}
        <label>Guidance text
          <textarea className="guidance-editor" value={text} rows={10} maxLength={12000}
            disabled={busy} onChange={(event) => { setText(event.target.value); setConfirming(false); }} />
        </label>
        <button className="button primary" disabled={busy || !dirty || !text.trim()}>Save draft</button>
        {data.can_publish && data.draft && (confirming ? <>
          <p>Publish this saved draft for coding agents?</p>
          <button type="button" className="button primary" disabled={busy || dirty}
            onClick={() => submit('approve')}>Confirm publish</button>
          <button type="button" className="button outlined" disabled={busy}
            onClick={() => setConfirming(false)}>Cancel</button>
        </> : <button type="button" className="button outlined" disabled={busy || dirty}
          onClick={() => setConfirming(true)}>Approve and publish</button>)}
      </form>}
      <h4>Version history</h4>
      {data.history.length === 0 ? <p>No published versions yet.</p> : data.history.map((entry) => (
        <details key={entry.version}>
          <summary>Version {entry.version} · {dates.format(new Date(entry.published_at))}</summary>
          <Published entry={entry} />
        </details>
      ))}
      {data.next_before_version && <button type="button" className="button outlined"
        disabled={busy} onClick={loadOlder}>Load older versions</button>}
    </>}
  </section>;
}
