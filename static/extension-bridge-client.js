(function initHapsLocalBridge(global) {
  const PAGE_SOURCE = 'haps-page';
  const CONTENT_TARGET = 'haps-extension-content';
  const REQUEST_TYPE = 'HAPS_EXTENSION_REQUEST';
  const REPLY_TYPE = 'HAPS_EXTENSION_REPLY';
  const DEFAULT_TIMEOUT_MS = 20000;

  function createRequestId() {
    return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
  }

  function request(action, payload = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
    return new Promise((resolve, reject) => {
      const requestId = createRequestId();
      const timer = global.setTimeout(() => {
        global.removeEventListener('message', onReply);
        reject(new Error(`extension bridge timeout: ${action}`));
      }, timeoutMs);

      const onReply = (event) => {
        if (event.source !== global) return;
        const message = event.data || {};
        if (message.type !== REPLY_TYPE) return;
        if (message.target !== PAGE_SOURCE) return;
        if (String(message.request_id || '') !== requestId) return;
        global.clearTimeout(timer);
        global.removeEventListener('message', onReply);
        if (message.ok) resolve(message.payload || {});
        else reject(new Error(String(message.error || 'extension bridge failed')));
      };

      global.addEventListener('message', onReply);
      global.postMessage({
        type: REQUEST_TYPE,
        source: PAGE_SOURCE,
        target: CONTENT_TARGET,
        action: String(action || ''),
        request_id: requestId,
        payload,
      }, global.location.origin);
    });
  }

  async function openFrontendTerminal(input) {
    const data = await request('native_open_terminal', input || {});
    return { ok: !!data.ok, detail: data.detail || '' };
  }

  async function createJobsBrowse(input) {
    return request('native_create_jobs_browse', input || {});
  }

  async function runCfgprosh(input) {
    const data = await request('native_run_cfgprosh', input || {});
    return { ok: !!data.ok, detail: data.detail || '', output: data.output || '' };
  }

  async function validateCreateJobs(input) {
    const data = await request('native_validate_create_jobs', input || {});
    return { ok: !!data.ok, detail: data.detail || '', errors: data.errors || [] };
  }

  async function ping() {
    const data = await request('ping', { ts: new Date().toISOString() }, 5000);
    return !!data.ok;
  }

  global.HapsLocalBridge = {
    request,
    openFrontendTerminal,
    createJobsBrowse,
    runCfgprosh,
    validateCreateJobs,
    ping,
  };
}(window));
