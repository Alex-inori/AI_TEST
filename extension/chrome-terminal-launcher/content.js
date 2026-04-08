(() => {
  const REQ_TYPE = 'CFGSHELL_TERMINAL_LAUNCH';
  const RESP_TYPE = 'CFGSHELL_TERMINAL_LAUNCH_RESULT';

  window.addEventListener('message', (event) => {
    if (event.source !== window) return;
    const data = event.data || {};
    if (data.type !== REQ_TYPE) return;

    chrome.runtime.sendMessage(
      {
        type: 'CFGSHELL_TERMINAL_LAUNCH_REQUEST',
        request_id: String(data.request_id || ''),
        action: String(data.action || 'launch_terminal'),
        payload: data.payload || {},
      },
      (response) => {
        const runtimeErr = chrome.runtime.lastError;
        const ok = !runtimeErr && !!(response && response.ok);
        window.postMessage(
          {
            type: RESP_TYPE,
            request_id: String(data.request_id || ''),
            ok,
            error: runtimeErr ? runtimeErr.message : (response && response.error) || '',
          },
          '*',
        );
      },
    );
  });
})();
