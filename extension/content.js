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
    const extensionId = String(data.extensionId || '');
    const action = String(data.action || '');
    const payload = data.payload || {};

    try {
      const response = extensionId
        ? await chrome.runtime.sendMessage(extensionId, { action, payload })
        : await chrome.runtime.sendMessage({ action, payload });
      window.postMessage({
        source: SOURCE_EXT,
        type: 'CFGSHELL_EXTENSION_RESPONSE',
        requestId,
        ok: !!response?.ok,
        data: response?.data,
        error: response?.error || '',
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
