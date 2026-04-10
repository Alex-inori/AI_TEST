# Chrome Extension Bridge

This extension injects `window.CfgShellExtension` into the frontend page and forwards calls to a Chrome Native Messaging host (`com.cfgshell.native_host`).

## Exposed methods

- `openNativeTerminal({ terminalPath, cwd, jobId })`
- `runJobStage({ stage, job, timeoutMs })`
- `ensureLogDirectory({ path })`
- `validatePath({ path, type })`
- `listDirectory({ path, mode })`

## Python native-host implementation

- `native_host.py` provides a production-ready stdio native messaging host in Python.
- `native_host_manifest.json` is a template manifest. Replace:
  - `path` with absolute path to `native_host.py`
  - `allowed_origins` with your extension ID.

### Supported actions

- `open_terminal`
- `run_stage` (`load_db`, `load_img`, `reset`)
- `ensure_log_dir`
- `validate_path`
- `list_directory`
