const NATIVE_HOST = 'com.cfgshell.terminal_launcher';

function launchViaNative(action, payload) {
  const normalizedAction = String(action || 'launch_terminal');
  return new Promise((resolve) => {
    chrome.runtime.sendNativeMessage(
      NATIVE_HOST,
      {
        action: normalizedAction,
        payload: payload || {},
      },
      (response) => {
        if (chrome.runtime.lastError) {
          resolve({ ok: false, error: chrome.runtime.lastError.message });
          return;
        }
        if (!response || response.ok !== true) {
          resolve({ ok: false, error: (response && response.error) || 'native host rejected request' });
          return;
        }
        resolve({ ...response, ok: true });
      },
    );
  });
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== 'CFGSHELL_TERMINAL_LAUNCH_REQUEST') return;
  launchViaNative(message.action, message.payload)
    .then((result) => sendResponse(result))
    .catch((err) => sendResponse({ ok: false, error: String(err || 'unknown error') }));
  return true;
});
