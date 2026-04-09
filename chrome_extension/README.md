# HAPS Chrome Extension Bridge

该扩展将网页前端请求转发给本机 Native Messaging Host（`com.haps.local_bridge`）。

## 支持动作

- `native_open_terminal`：由 Native Host 打开本地终端。
- `native_prepare_log_dir`：由 Native Host 创建前端用户日志目录。
- `native_run_cfgprosh`：由 Native Host 负责执行 cfgprosh（阶段编排在 host 中处理）。
- `native_create_jobs_browse`：CreateJobs 文件/目录浏览（由 Native Host 返回条目）。
- `native_validate_create_jobs`：CreateJobs 本地合法性校验（路径存在性/后缀等）。
- `native_append_log`：前端把后端通知到的状态日志落盘到本地日志目录。

## 消息链路

1. Web 页面通过 `window.postMessage(HAPS_EXTENSION_REQUEST)` 发起请求。
2. `content-script.js` 监听并转发给 `background.js`。
3. `background.js` 通过 `chrome.runtime.sendNativeMessage` 调用本机桥接程序。
4. 执行结果逐层回传到页面。

## 安装

1. `chrome://extensions` 打开开发者模式。
2. 选择“加载已解压的扩展程序”，指向 `chrome_extension/`。
3. 在系统安装 Native Messaging Host `com.haps.local_bridge`。本仓库已提供 Python 版本实现：`native_host/local_bridge.py`。
