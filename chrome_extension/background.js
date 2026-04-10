const NATIVE_HOST = 'com.cfgshell.native_host';

function sendNative(payload) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendNativeMessage(NATIVE_HOST, payload, (response) => {
      const err = chrome.runtime.lastError;
      if (err) {
        reject(new Error(err.message || 'native host error'));
        return;
      }
      resolve(response || { ok: true });
    });
  });
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.channel !== 'cfgshell-bridge') return;
  (async () => {
    try {
      const response = await sendNative(message.payload || {});
      sendResponse({ ok: true, response });
    } catch (error) {
      sendResponse({ ok: false, error: error instanceof Error ? error.message : String(error) });
    }
  })();
  return true;
});
