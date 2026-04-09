const NATIVE_HOST = 'com.haps.local_bridge';
const ALLOWED_ACTIONS = new Set([
  'native_open_terminal',
  'native_run_cfgprosh',
  'native_create_jobs_browse',
  'native_validate_create_jobs',
  'ping',
]);

function callNativeHost(payload) {
  return new Promise((resolve, reject) => {
    try {
      chrome.runtime.sendNativeMessage(NATIVE_HOST, payload, (response) => {
        const err = chrome.runtime.lastError;
        if (err) {
          reject(new Error(err.message || 'native host error'));
          return;
        }
        resolve(response || { ok: false, detail: 'empty native host response' });
      });
    } catch (error) {
      reject(error instanceof Error ? error : new Error(String(error)));
    }
  });
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.type !== 'HAPS_EXTENSION_FORWARD') return;
  if (!ALLOWED_ACTIONS.has(String(message.action || ''))) {
    sendResponse({ ok: false, detail: `unsupported action: ${String(message.action || '')}` });
    return false;
  }
  callNativeHost({
    action: message.action,
    payload: message.payload || {},
    page_url: sender?.url || '',
    tab_id: sender?.tab?.id || null,
  }).then((result) => {
    sendResponse({ ok: !!result.ok, payload: result, detail: result.detail || '' });
  }).catch((error) => {
    sendResponse({ ok: false, detail: error instanceof Error ? error.message : String(error) });
  });
  return true;
});
