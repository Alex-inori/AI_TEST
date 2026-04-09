# Native Host (Python) for `com.haps.local_bridge`

本目录提供了可直接运行的 Python Native Messaging Host：`local_bridge.py`。

## 已实现 API

- `native_open_terminal`
- `native_create_jobs_browse`
- `native_run_cfgprosh`
- `native_validate_create_jobs`
- `ping`

## 环境变量

- `HAPS_TERMINAL`: 指定终端程序路径（可选）。
- `HAPS_BROWSE_ROOT`: CreateJobs 可浏览根目录（默认用户 home）。
- `HAPS_CFGPROSH`: cfgprosh 程序路径（`native_run_cfgprosh` 必需）。

## 安装步骤（Linux）

1. 将 `com.haps.local_bridge.json` 放到 Chrome Native Messaging 配置目录（如 `~/.config/google-chrome/NativeMessagingHosts/`）。
2. 修改 `path` 为 `local_bridge.py` 的绝对路径。
3. 把 `allowed_origins` 改为你实际扩展 ID。
4. 确保 `local_bridge.py` 可执行：`chmod +x local_bridge.py`。
