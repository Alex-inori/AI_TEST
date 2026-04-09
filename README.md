# Job Console (CentOS 7 Friendly)

这是一个最小可运行的前后端示例：

- 前端：网页提交 Job（New Jobs）
- 后端：FastAPI 接收并执行 Job
- Recent Jobs：显示运行中/已完成任务，支持 Stop、Copy

## 功能对应

1. Job 提交：支持在 New Jobs 填写并提交。
2. 页面包含 New Jobs 与 Recent Jobs。
3. New Jobs 条目支持 Bitfile、Binfile。
4. 条目支持工具链配置：UART1~UART4、OpenODC 路径。
5. 条目支持 log_path。
6. New Jobs 支持新增条目。
7. Submit 后批量提交到后端执行。
8. 提交后 Recent Jobs 显示状态。
9. Recent Jobs 显示 Runing / Finish（以及 Stopped/Failed）。
10. Runing 条目支持 Stop，并二次确认。
11. Stop 后后端终止对应进程。
12. Recent Jobs 记录提交时间与结束时间。
13. Recent Jobs 支持 Copy 到 New Jobs。
14. 在 Recent Jobs 中新增与 Job 绑定的 Open UART Console；当提交 jobs 包含串口 `uart_paths` 时，后端通过 pyserial 独占打开并捕获串口输出，若端口暂时被占用会等待释放后自动重试，并通过 websocket 实时按设备（dev）分栏展示。
15. Open Terminal、Create Jobs 本地目录浏览、Running 阶段脚本执行均通过 Chrome Extension + Native Messaging 走前端本机链路，不依赖后端执行本地命令。
16. HAPS_DB / SW_IMG / Resetting 阶段由前端 native-host 执行；后端只保留流程控制与状态展示。

## Python 版本要求

- **最低版本：Python 3.10**（`app.py` 使用了 `str | None`、`list[...]` 等较新类型标注语法）
- **推荐版本：Python 3.11**
- 可先执行 `python3 --version` 确认

## 你在自己环境上如何运行（推荐步骤）

> 以下步骤适用于 Linux（CentOS 7 / Ubuntu / Debian 都可，命令略有差异）。

### 1) 准备代码

```bash
git clone <你的仓库地址>
cd AI_TEST
```

### 2) 安装 Python 3 与 venv

- CentOS 7（常见命令）：

```bash
sudo yum install -y epel-release
sudo yum install -y python3 python3-pip
```

- Ubuntu / Debian：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
```

### 3) 创建虚拟环境并安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install fastapi 'uvicorn[standard]' pyserial
```

### 4) 启动服务

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

看到类似 `Uvicorn running on http://0.0.0.0:8000` 即启动成功。

### 5) 浏览器访问（Chrome / Firefox）

- 在服务所在机器打开：`http://127.0.0.1:8000`
- 在局域网其他机器打开：`http://<服务器IP>:8000`
- 推荐使用最新版 **Google Chrome** 或 **Mozilla Firefox**

### 6) 基础检查（可选）

```bash
curl http://127.0.0.1:8000/api/jobs
```

正常会返回 JSON，例如：`{"jobs":[]}`。

---

## 常见问题

### Q1: `python3: command not found`

说明系统未安装 Python3，按上面的系统命令先安装。

### Q2: `No module named fastapi`

说明你没有在虚拟环境里安装依赖，重新执行：

```bash
source .venv/bin/activate
pip install fastapi uvicorn pyserial
```

### Q3: 局域网其它机器访问不到

请检查：

1. 服务是否用 `--host 0.0.0.0` 启动；
2. 服务器防火墙是否放行 8000 端口；
3. 访问的是正确的服务器 IP。

---

## API

- `POST /api/jobs`：提交 jobs
- `GET /api/jobs`：查询 recent jobs
- `POST /api/jobs/{job_id}/stop`：停止运行中的 job
- `POST /api/jobs/{job_id}/frontend-stage`：前端回传阶段执行结果（按 Loading HAPS_DB → Loading SW_IMG → Resetting HAPS_ENV）
- `WS /ws/uart`：UART 实时流（按 Job + 设备输出）

## 后端调试日志

- 后端会把关键调用流程与 `Status=Failed` 原因追加到：`/tmp/haps_bg.log`。

## cfgshell.conf 可选配置

- `SERVICE_PORT`：前端请求后端服务端口。未配置时默认使用 `127.0.0.1:8000`。
- `CREATE_JOBS_MAX_NUM`：New Jobs 页面允许创建/提交的最大 Job 数量。未配置时默认 `5`。
- `RECENT_JOBS_MAX_NUM`：Recent Jobs 最多保留显示条数。未配置时默认 `10`。

## Chrome Extension 本地桥接

- 扩展代码位于 `chrome_extension/`（`manifest.json`、`content-script.js`、`background.js`）。
- 前端页面通过 `window.postMessage` 请求扩展，扩展再通过 `chrome.runtime.sendNativeMessage` 调用本机 Host：`com.haps.local_bridge`。
- Native Host 的 Python 实现位于 `native_host/local_bridge.py`，并附带 manifest 模板 `native_host/com.haps.local_bridge.json`。
- 前端当前使用的本机动作（具体执行逻辑在 Native Host）：
  - `native_open_terminal`：打开本地 Terminal；
  - `native_prepare_log_dir`：创建前端用户日志目录；
  - `native_run_cfgprosh`：执行 Running 阶段 cfgprosh；
  - `native_create_jobs_browse`：Create Jobs 浏览本地目录。
  - `native_validate_create_jobs`：Create Jobs 提交前本地合法性校验。
- `GET /api/client-config` 除 UI 配置外，还会传递前端默认参数：`HAPS_CONFPROSH`、`HAPS_DB_LOADING_TCL`、`HAPS_RESET_TCL`、`HAPS_IMG_LOADING_TCL`、`TERMINAL`、`UART_LOG_PATH`（以小写键返回）。
- Submit 时仅完成本地路径校验与日志目录准备；`native_run_cfgprosh` 在 Submit 后的 Recent Jobs 阶段触发执行。

### 前端调用示例（具体代码）

```js
// 1) 打开本地终端
await window.HapsLocalBridge.openFrontendTerminal({
  job_id: 'job-001',
  service_base_url: 'http://127.0.0.1:8000',
  user_id: '1001',
});

// 2) 执行 cfgprosh（阶段编排由 native-host 完成）
const logInit = await window.HapsLocalBridge.prepareLogDir({
  user_id: '1001',
  jobs_id: 'job-001',
  log_path: '/tmp/haps_local_logs/1001/job-001',
});
await window.HapsLocalBridge.runCfgprosh({
  source: 'running_job',
  job_id: 'job-001',
  log_path: logInit.log_path,
  payload: {/* 整个 job payload */},
  user_id: '1001',
});

// 3) Create Jobs 浏览本地目录
const fsData = await window.HapsLocalBridge.createJobsBrowse({
  source: 'create_jobs',
  path: '/home/user',
  mode: 'file',
  user_id: '1001',
});
console.log(fsData.entries);

// 4) Create Jobs 提交前校验
const validate = await window.HapsLocalBridge.validateCreateJobs({
  user_id: '1001',
  jobs: [/* collectNewJobs() 结果 */],
});
if (!validate.ok) console.error(validate.errors);
```
