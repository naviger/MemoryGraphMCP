import { useEffect, useRef, useState } from 'react';
import { reviewRequest } from './api.js';
import { DetailLink } from './Details.jsx';

const scoreFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });

export function SimilarRecords({ recordKey, token, navigate, onUnauthorized }) {
  const [result, setResult] = useState(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const request = useRef(null);

  useEffect(() => () => request.current?.abort(), []);

  async function load() {
    if (pending || result) return;
    const controller = new AbortController();
    request.current = controller;
    setPending(true);
    setError('');
    try {
      const data = await reviewRequest(`/records/${encodeURIComponent(recordKey)}/similar`,
        { token, signal: controller.signal });
      if (!controller.signal.aborted) setResult(data);
    } catch (err) {
      if (controller.signal.aborted) return;
      if (err.status === 401) onUnauthorized();
      else setError(err.message);
    } finally {
      if (!controller.signal.aborted) setPending(false);
    }
  }

  return (
    <details className="similar-records" onToggle={(event) => { if (event.currentTarget.open && !error) load(); }}>
      <summary>Similar records</summary>
      {pending && <p role="status">Finding semantic matches…</p>}
      {error && <>
        <p role="alert">{error}</p>
        <button type="button" className="button outlined" onClick={load}>Retry</button>
      </>}
      {result && (!result.available ? <p className="muted">This record has no usable embedding profile.</p>
        : result.matches.length ? (
          <ul>{result.matches.map((match) => (
            <li key={match.key}>
              <DetailLink entityKey={match.key} navigate={navigate}>{match.title}</DetailLink>
              <p className="muted">Semantic match · {match.kind} · Similarity {scoreFormat.format(match.similarity)}</p>
            </li>
          ))}</ul>
        ) : <p className="muted">No semantic matches with a compatible embedding profile.</p>)}
    </details>
  );
}
