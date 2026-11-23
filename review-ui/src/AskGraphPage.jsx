import { ContextHelp } from './ContextHelp.jsx';
import { CircleAlert, FileText, GitPullRequest, Search } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { reviewRequest } from './api.js';
import { DetailLink, Evidence, formatDate, formatWindow } from './Details.jsx';

function CitedConflict({ pair, navigate }) {
  return (
    <article className="ask-conflict">
      <p className="item-key">{pair.original.conflict_scope?.topic || 'ADR conflict'} · {formatWindow(pair.overlap_start, pair.overlap_end)}</p>
      <div className="adr-grid">
        {[pair.original, pair.competing].map((record) => (
          <div className="adr-card" key={record.key}>
            <h3><DetailLink entityKey={record.key} navigate={navigate}>{record.title}</DetailLink></h3>
            <p className="ask-claim">{record.decision}</p>
            <Evidence history={record.validation_history} source={record.source} sourceId={record.source_id} />
          </div>
        ))}
      </div>
    </article>
  );
}

function Answer({ answer, navigate }) {
  return (
    <article className="detail ask-answer" aria-label="Graph answer">
      <header className="page-heading">
        <div><p className="item-key">As of {formatDate(answer.as_of)}</p><h2>Answer</h2></div>
      </header>
      {answer.summary && <p role="status">{answer.summary}</p>}
      {answer.findings.length > 0 && (
        <ul className="ask-findings">
          {answer.findings.map(({ text, citations }) => (
            <li key={text}>
              <p>{text}</p>
              <div className="claim-citations">
                {citations.map((citation) => (
                  <DetailLink key={`${citation.destination}/${citation.key}`}
                    destination={citation.destination} entityKey={citation.key} navigate={navigate}
                    className="citation-link"><FileText size={15} aria-hidden="true" />{citation.title}</DetailLink>
                ))}
              </div>
            </li>
          ))}
        </ul>
      )}
      {answer.conflicts.length > 0 && (
        <section className="record-section">
          <h2><GitPullRequest size={18} aria-hidden="true" /> Conflicting decisions</h2>
          {answer.conflicts.map((pair) => <CitedConflict key={`${pair.original.key}/${pair.competing.key}`} pair={pair} navigate={navigate} />)}
        </section>
      )}
      {answer.gaps.length > 0 && (
        <section className="record-section">
          <h2><CircleAlert size={18} aria-hidden="true" /> Evidence gaps</h2>
          <ul className="evidence-gaps">{answer.gaps.map((gap) => <li key={gap}>{gap}</li>)}</ul>
        </section>
      )}
    </article>
  );
}

export function AskGraphPage({ token, navigate, onUnauthorized, projectKey }) {
  const [question, setQuestion] = useState('');
  const [asOf, setAsOf] = useState('');
  const [answer, setAnswer] = useState(null);
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const request = useRef(null);

  useEffect(() => () => request.current?.abort(), []);

  async function ask(event) {
    event.preventDefault();
    if (pending || question.trim().length < 3) return;
    const controller = new AbortController();
    request.current = controller;
    setPending(true);
    setError('');
    setAnswer(null);
    try {
      const result = await reviewRequest('/ask', {
        token, signal: controller.signal,
        body: { question: question.trim(), as_of: asOf || null, project_key: projectKey || null },
      });
      setAnswer(result);
    } catch (err) {
      if (err.name === 'AbortError') return;
      if (err.status === 401) onUnauthorized();
      else setError(err.message);
    } finally {
      if (!controller.signal.aborted) setPending(false);
    }
  }

  return (
    <>
      <header className="page-heading">
        <div><div className="heading-with-help"><h1>Ask the Graph</h1><ContextHelp topic="ask" /></div><p>Ask about architecture decisions, implementation, or validation evidence.</p></div>
      </header>
      <form className="detail ask-form" onSubmit={ask}>
        <label>Question
          <textarea required minLength="3" maxLength="500" rows="3" value={question}
            placeholder="What is the current authentication strategy?"
            onChange={(event) => setQuestion(event.target.value)} />
        </label>
        <div className="ask-actions">
          <label>As of <input type="date" value={asOf} onChange={(event) => setAsOf(event.target.value)} /></label>
          <button className="button primary" type="submit" disabled={pending || question.trim().length < 3}>
            <Search size={17} aria-hidden="true" /> {pending ? 'Searching…' : 'Ask the Graph'}
          </button>
        </div>
      </form>
      {pending && <p className="initial-status" role="status">Checking records and conflicts…</p>}
      {error && <p className="status error" role="alert"><CircleAlert size={18} aria-hidden="true" />{error}</p>}
      {answer && <Answer answer={answer} navigate={navigate} />}
    </>
  );
}
