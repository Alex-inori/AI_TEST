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
- `WS /ws/uart`：UART 实时流（按 Job + 设备输出）

## cfgshell.conf 可选配置

- `SERVICE_PORT`：前端请求后端服务端口。未配置时默认使用 `127.0.0.1:8000`。
- `CREATE_JOBS_MAX_NUM`：New Jobs 页面允许创建/提交的最大 Job 数量。未配置时默认 `5`。
- `RECENT_JOBS_MAX_NUM`：Recent Jobs 最多保留显示条数。未配置时默认 `10`。

## Chrome Extension 部署（Terminal 自动拉起）

仓库提供了可直接部署的 extension 前端桥接代码：

- `extension/chrome-terminal-launcher/manifest.json`
- `extension/chrome-terminal-launcher/content.js`
- `extension/chrome-terminal-launcher/background.js`

以及 Native Messaging Host 示例：

- `extension/native-host/terminal_launcher.py`
- `extension/native-host/com.cfgshell.terminal_launcher.json`

### 1) 安装 Chrome Extension（开发者模式）

1. 打开 `chrome://extensions`
2. 开启「开发者模式」
3. 点击「加载已解压的扩展程序」
4. 选择目录：`extension/chrome-terminal-launcher`

### 2) 注册 Native Messaging Host（每个 ETX 登录用户都要注册）

1. 修改 `extension/native-host/com.cfgshell.terminal_launcher.json`：
   - `path` 改为本机 `terminal_launcher.py` 的绝对路径
   - `allowed_origins` 改为你本机扩展的真实 extension id
2. 把该 json 放到当前用户的 Native Messaging 配置目录（Linux 常见目录）：
   - `~/.config/google-chrome/NativeMessagingHosts/`
3. 确保脚本可执行：

```bash
chmod +x extension/native-host/terminal_launcher.py
```

### 3) 行为说明

- 页面会先尝试通过 extension + native host 在**前端用户会话**中启动 `TERMINAL`。
- 如果 extension / native host 没有就绪，页面会自动回退到后端现有启动逻辑。
- Native Host 同时支持四种 action：
  - `launch_terminal`：启动终端可执行文件
  - `launch_cfgshell`：按传入 `cmd`（例如 `HAPS_CONFPROSH_CMD`）直接启动 cfgshell 进程
  - `list_dir`：按前端本地用户权限读取目录，返回文件浏览器需要的 `cwd/parent/entries`
- `validate_job_payload`：在前端本地用户权限下校验 database/reset/imgload/img_file 路径有效性
- 前端会周期性拉取 `/api/native/next-task`，并通过 extension 调用 native host 执行（如 `run_cfgshell_sync`），再把结果回传到 `/api/native/tasks/{id}/result`，后端仅做流程编排与状态控制。
- `run_cfgshell_sync` 会在 native host 侧写入命令执行日志（若传入 `log_file`）并在传入 `sw_img_check_file` 时完成 SW_IMG_CHECK。
- log 目录创建与日志落盘均由 native host 执行；前端回传每个 native task 的执行结果（action/task_id/finished_at），后端仅做流程控制与状态推进。
