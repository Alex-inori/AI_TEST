const NATIVE_HOST_NAME = 'com.haps.job_console_host';

function sendNative(payload) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendNativeMessage(NATIVE_HOST_NAME, payload, (response) => {
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

  (async () => {
    if (!action) throw new Error('missing action');
    const response = await sendNative({ action, payload, url: sender?.url || '' });
    sendResponse({ ok: true, data: response });
  })().catch((error) => {
    sendResponse({ ok: false, error: error instanceof Error ? error.message : String(error) });
  });

  return true;
});
