# Native Host (Python)

This folder contains the Python implementation for Chrome Native Messaging host `com.cfgshell.native_host`.

- `native_host.py`: stdio-based native host implementation.
- `native_host_manifest.json`: template manifest for Chrome registration.

Update `native_host_manifest.json` before installing:
1. Replace `path` with absolute path to `native_host.py`.
2. Replace `allowed_origins` with your extension id.
