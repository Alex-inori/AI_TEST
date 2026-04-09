# Native Host (Python) for `com.haps.local_bridge`

本目录提供了可直接运行的 Python Native Messaging Host：`local_bridge.py`。

## 已实现 API

- `native_open_terminal`
- `native_prepare_log_dir`
- `native_create_jobs_browse`
- `native_run_cfgprosh`
- `native_validate_create_jobs`
- `ping`

## 环境变量

- `HAPS_TERMINAL`: 指定终端程序路径（可选）。
- `HAPS_BROWSE_ROOT`: CreateJobs 可浏览根目录（默认用户 home）。
- `HAPS_CFGPROSH`: cfgprosh 程序路径（`native_run_cfgprosh` 必需）。
- `HAPS_DB_LOADING_TCL`: 当 payload 未传 `db_loading_tcl` 时的后备值。

## 日志

- native-host 会把关键执行日志与错误原因落盘到 `<log_path>/native_host.log`。
- `log_path` 由前端在提交前调用 `native_prepare_log_dir` 创建并传入后续 action。
- `native_prepare_log_dir` 会将目录权限设置为 `0777`，确保后端串口抓取流程也可写入同目录。
- `SW_IMG_CHK` 在 native-host 执行，摘要缓存位于 `~/.haps_local_bridge/sw_img_signatures.json`。

## CreateJobs 校验规则（native_validate_create_jobs）

- `database_path`：仅在 `database_path_enabled=true` 时校验，要求路径存在且可访问。
- `reset_script` / `imgload_script`：仅在对应 enabled=true 时校验，要求存在、可访问、且后缀为 `.tcl`。
- `img_file`：仅在 `imgload_script_enabled=true` 时校验，要求存在、可访问、且后缀为 `.bin/.img/.dat`。

## native_run_cfgprosh 阶段执行

- `native_run_cfgprosh` 每次调用只执行一个阶段，需传 `stage_only`：
  - `load_db`
  - `load_img`
  - `reset`
- `cfgprosh` 可通过 `cfgprosh` 或 `haps_cfgprosh` 字段传入（payload 顶层或嵌套 `payload` 均可）。
- 阶段顺序由后端流程控制并通过前端回传接口推进。

## 安装步骤（Linux）

1. 将 `com.haps.local_bridge.json` 放到 Chrome Native Messaging 配置目录（如 `~/.config/google-chrome/NativeMessagingHosts/`）。
2. 修改 `path` 为 `local_bridge.py` 的绝对路径。
3. 把 `allowed_origins` 改为你实际扩展 ID。
4. 确保 `local_bridge.py` 可执行：`chmod +x local_bridge.py`。
