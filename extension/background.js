const DEFAULT_NATIVE_HOST_NAME = 'com.haps.job_console_host';

function sendNative(nativeHostName, payload) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendNativeMessage(nativeHostName, payload, (response) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message || 'native host call failed'));
        return;
      }
      resolve(response || {});
    });
  });
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const action = String(message?.action || '');
  const payload = message?.payload || {};
  const nativeHostName = String(payload?.nativeHostName || DEFAULT_NATIVE_HOST_NAME);

  (async () => {
    if (!action) throw new Error('missing action');
    const response = await sendNative(nativeHostName, { action, payload, url: sender?.url || '' });
    if (response && response.ok === false) {
      sendResponse({ ok: false, error: response.error || 'native host action failed' });
      return;
    }
    sendResponse({ ok: true, data: response?.data ?? response });
  })().catch((error) => {
    sendResponse({ ok: false, error: error instanceof Error ? error.message : String(error) });
  });

  return true;
});
