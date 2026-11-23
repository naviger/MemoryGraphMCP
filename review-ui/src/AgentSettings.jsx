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
  const approved = data.allowed_urls[agent] || [];
  const endpoint = config.url.trim().replace(/\/+$/, '');
  const allowed = approved.includes(endpoint);

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
    <label>{local ? 'Ollama endpoint' : 'Bridge endpoint'}
      <input type="url" value={config.url} onChange={(event) => change({ url: event.target.value })}
        list={`agent-endpoints-${agent}`} disabled={!!busy}
        placeholder={local ? 'http://ollama:11434' : 'http://agent-bridge:8080'} />
      <datalist id={`agent-endpoints-${agent}`}>
        {approved.map((url) => <option key={url} value={url} />)}
      </datalist>
    </label>
    {!local && !approved.length && <p>No bridge endpoint is approved for {AGENTS[agent]} yet. Start a compatible bridge and add its URL to <code>MGMCP_WORKSPACE_AGENT_ALLOWED_URLS</code>.</p>}
    {endpoint && !allowed && <p role="status">This URL needs server approval for {AGENTS[agent]} before you can test or save it.</p>}
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
      <button className="button primary" disabled={!!busy || (!!endpoint && !allowed)}>Save</button>
      <button type="button" className="button outlined" disabled={!!busy || !allowed || (local && !config.model.trim())}
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
      <div className="project-tabs" role="group" aria-label="Agents">
        {Object.entries(AGENTS).map(([key, label]) => <button key={key} type="button"
          aria-pressed={agent === key} onClick={() => setAgent(key)}>{label}</button>)}
      </div>
      <AgentForm key={agent} agent={agent} data={data} onSave={setData}
        request={(path, body) => reviewRequest(path, { token, body })} />
    </>}
  </section>;
}
