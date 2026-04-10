(function () {
  async function callNative(action, data) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage({
        channel: 'cfgshell-bridge',
        payload: { action, ...(data || {}) },
      }, (resp) => {
        if (!resp || !resp.ok) {
          reject(new Error((resp && resp.error) || 'extension bridge failed'));
          return;
        }
        resolve(resp.response || { ok: true });
      });
    });
  }

  window.CfgShellExtension = {
    async openNativeTerminal(payload) {
      return callNative('open_terminal', payload);
    },
    async runJobStage(payload) {
      return callNative('run_stage', payload);
    },
    async ensureLogDirectory(payload) {
      return callNative('ensure_log_dir', payload);
    },
    async validatePath(payload) {
      return callNative('validate_path', payload);
    },
    async listDirectory(payload) {
      return callNative('list_directory', payload);
    },
  };
})();
