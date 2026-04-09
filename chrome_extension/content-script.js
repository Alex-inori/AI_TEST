const PAGE_SOURCE = 'haps-page';
const CONTENT_TARGET = 'haps-extension-content';

window.addEventListener('message', (event) => {
  if (event.source !== window) return;
  const data = event.data || {};
  if (data.type !== 'HAPS_EXTENSION_REQUEST') return;
  if (data.source !== PAGE_SOURCE || data.target !== CONTENT_TARGET) return;
  const requestId = String(data.request_id || '');
  chrome.runtime.sendMessage({
    type: 'HAPS_EXTENSION_FORWARD',
    action: String(data.action || ''),
    payload: data.payload || {},
  }, (resp) => {
    const lastError = chrome.runtime.lastError;
    const ok = !!(resp && resp.ok) && !lastError;
    window.postMessage({
      type: 'HAPS_EXTENSION_REPLY',
      source: CONTENT_TARGET,
      target: PAGE_SOURCE,
      request_id: requestId,
      ok,
      payload: resp && resp.payload ? resp.payload : null,
      error: lastError ? lastError.message : (resp && resp.detail ? resp.detail : ''),
    }, window.location.origin);
  });
});
