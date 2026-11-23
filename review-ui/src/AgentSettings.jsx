import { useEffect, useState } from 'react';
import { reviewRequest } from './api.js';

const PATH = '/admin/agents';
const AGENTS = { codex: 'Codex', 'claude-code': 'Claude Code', openhands: 'OpenHands', local: 'Ollama' };
const MODES = { PLAN_ONLY: 'Plan', IMPLEMENT: 'Implement', REVIEW: 'Review', FULL_LIFECYCLE: 'Full lifecycle' };
const EMPTY = { url: '', model: '', modes: [] };

function AgentForm({ agent, data, request, onSave }) {
  const [config, setConfig] = useState(data.agents[agent] || EMPTY);
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState(null);
  const local = agent === 'local';

  function change(fields) {
    setConfig({ ...config, ...fields });
    setMessage(null);
  }

  async function submit(test) {
    setBusy(test ? 'Testing connection…' : 'Saving…');
    setMessage(null);
    try {
      const result = await request(test ? PATH + '/test' : PATH, { agent, revision: data.revision, config });
      if (test) {
        setMessage(result);
        if (result.ok) setConfig((current) => ({ ...current, modes: result.modes }));
      } else {
        onSave(result);
        setMessage({ ok: true, message: 'Settings saved.' });
      }
    } catch (err) {
      setMessage({ ok: false, message: err.message });
    } finally {
      setBusy('');
    }
  }

  return <form className="detail workspace-form" onSubmit={(event) => { event.preventDefault(); submit(false); }}>
    <h2>{AGENTS[agent]}</h2>
    <label>Endpoint
      <select value={config.url} onChange={(event) => change({ url: event.target.value })} disabled={!!busy}>
        <option value="">Disabled</option>
        {config.url && !data.allowed_urls.includes(config.url) && <option value={config.url}>{config.url} (unapproved)</option>}
        {data.allowed_urls.map((url) => <option key={url}>{url}</option>)}
      </select>
    </label>
    {local && <label>Model
      <input value={config.model} maxLength={300} onChange={(event) => change({ model: event.target.value })}
        disabled={!!busy || !config.url} required={!!config.url} />
    </label>}
    <fieldset disabled={!!busy || !config.url}><legend>Supported execution modes</legend>
      {local ? <p>Plan and Review. Ollama also provides task and record drafting.</p> :
        Object.entries(MODES).map(([mode, label]) => <label key={mode}>
          <input type="checkbox" checked={config.modes.includes(mode)} onChange={() => change({
            modes: config.modes.includes(mode) ? config.modes.filter((item) => item !== mode) : [...config.modes, mode],
          })} /> {label}{' '}
        </label>)}
    </fieldset>
    <div className="workspace-actions">
      <button className="button primary" disabled={!!busy}>Save</button>
      <button type="button" className="button outlined" disabled={!!busy || !config.url || (local && !config.model.trim())}
        onClick={() => submit(true)}>Test connection</button>
    </div>
    {busy && <p role="status">{busy}</p>}
    {message && <p role={message.ok ? 'status' : 'alert'} className={message.ok ? 'status' : 'status error'}>
      {message.message}{message.modes?.length ? ' ' + message.modes.map((mode) => MODES[mode]).join(', ') + '.' : ''}
    </p>}
  </form>;
}

export default function AgentSettings({ token, onUnauthorized }) {
  const [agent, setAgent] = useState('codex');
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    reviewRequest(PATH, { token, signal: controller.signal }).then(setData).catch((err) => {
      if (controller.signal.aborted) return;
      if (err.status === 401) onUnauthorized();
      else setError(err.message);
    });
    return () => controller.abort();
  }, [token, onUnauthorized]);
  return <section>
    <header className="page-heading"><div><h1>Agent Settings</h1>
      <p>Configure agents for project task execution and Ollama drafting.</p></div></header>
    {error && <p role="alert" className="status error">{error}</p>}
    {!data && !error && <p role="status">Loading agent settings…</p>}
    {data && <>
      <p>Endpoints are approved in the server configuration. Testing a connection does not start an execution.</p>
      {!data.allowed_urls.length && <p>No endpoints have been approved. Set <code>MGMCP_WORKSPACE_AGENT_ALLOWED_URLS</code> in the server configuration.</p>}
      <div className="project-tabs" role="group" aria-label="Agents">
        {Object.entries(AGENTS).map(([key, label]) => <button key={key} type="button"
          aria-pressed={agent === key} onClick={() => setAgent(key)}>{label}</button>)}
      </div>
      <AgentForm key={agent} agent={agent} data={data} onSave={setData}
        request={(path, body) => reviewRequest(path, { token, body })} />
    </>}
  </section>;
}
