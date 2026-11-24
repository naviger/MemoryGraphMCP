const BASE = '/api/review';
let usageSession;

// Interaction keeps a browser session active. Background polling carries no activity marker.
let activityHeartbeat;
let lastHeartbeat = 0;
if (typeof window !== 'undefined') {
  const reportInteraction = (event) => {
    if (!event.isTrusted || document.visibilityState !== 'visible' || activityHeartbeat) return;
    const delay = Math.max(0, 30000 - (Date.now() - lastHeartbeat));
    activityHeartbeat = window.setTimeout(() => {
      activityHeartbeat = undefined;
      if (document.visibilityState !== 'visible') return;
      lastHeartbeat = Date.now();
      fetch(BASE + '/session', {
        headers: { 'X-MemoryGraph-Activity': '1' }, cache: 'no-store',
      }).catch(() => {});
    }, delay);
  };
  for (const event of ['pointerdown', 'keydown', 'wheel']) {
    window.addEventListener(event, reportInteraction, { passive: true });
  }
}

function newSessionId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

function tokenUsageSession() {
  if (!usageSession) {
    try {
      usageSession = sessionStorage.getItem('memorygraph.usageSession') || newSessionId();
      sessionStorage.setItem('memorygraph.usageSession', usageSession);
    } catch {
      usageSession ||= newSessionId();
    }
  }
  return usageSession;
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function reviewRequest(path, { token, body, signal } = {}) {
  const response = await fetch(BASE + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {
      Accept: 'application/json',
      ...(token ? { Authorization: `Bearer ${token}`, 'X-MemoryGraph-Session': tokenUsageSession() } : {}),
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: 'no-store',
    signal,
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = payload?.detail;
    const message = typeof detail === 'string'
      ? detail
      : Array.isArray(detail)
        ? detail.map((item) => item.msg).join(' ')
        : `The review API returned an error (${response.status}).`;
    throw new ApiError(response.status, message);
  }
  if (payload === null) throw new ApiError(response.status, 'The review API returned an invalid response.');
  return payload;
}
