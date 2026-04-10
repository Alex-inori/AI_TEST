(() => {
  const SOURCE_WEB = 'cfgshell-web';
  const SOURCE_EXT = 'cfgshell-extension';
  window.postMessage({ source: SOURCE_EXT, type: 'CFGSHELL_EXTENSION_READY' }, '*');

  window.addEventListener('message', async (event) => {
    if (event.source !== window) return;
    const data = event.data || {};
    if (data.source === SOURCE_WEB && data.type === 'CFGSHELL_EXTENSION_PING') {
      window.postMessage({ source: SOURCE_EXT, type: 'CFGSHELL_EXTENSION_READY' }, '*');
      return;
    }
    if (data.source !== SOURCE_WEB || data.type !== 'CFGSHELL_EXTENSION_REQUEST') return;

    const requestId = String(data.requestId || '');
    const action = String(data.action || '');
    const payload = data.payload || {};

    try {
      const response = await chrome.runtime.sendMessage({ action, payload });
      if (!response) {
        window.postMessage({
          source: SOURCE_EXT,
          type: 'CFGSHELL_EXTENSION_RESPONSE',
          requestId,
          ok: false,
          error: 'empty response from extension background',
        }, '*');
        return;
      }
      window.postMessage({
        source: SOURCE_EXT,
        type: 'CFGSHELL_EXTENSION_RESPONSE',
        requestId,
        ok: typeof response.ok === 'boolean' ? response.ok : true,
        data: Object.prototype.hasOwnProperty.call(response, 'data') ? response.data : response,
        error: response.error || '',
      }, '*');
    } catch (error) {
      window.postMessage({
        source: SOURCE_EXT,
        type: 'CFGSHELL_EXTENSION_RESPONSE',
        requestId,
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      }, '*');
    }
  });
})();
