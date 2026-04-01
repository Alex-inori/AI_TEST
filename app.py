from __future__ import annotations

import json
import asyncio
import os
import pwd
import socket
import secrets
import subprocess
import threading
import uuid
import time
import shlex
import re
import select
import pty
import zlib

try:
    import fcntl
    import termios
except ImportError:  # pragma: no cover
    fcntl = None
    termios = None
from dataclasses import dataclass, field
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_ROOT = Path(__file__).resolve().parent
CFGSHELL_CONFIG_FILE = APP_ROOT / "cfgshell.conf"
UTILIZATION_LOG_FILE = APP_ROOT / "ultization.log"
DEFAULT_SERVICE_PORT = 8000
DEFAULT_CREATE_JOBS_MAX_NUM = 5
DEFAULT_RECENT_JOBS_MAX_NUM = 10
REQUIRED_HAPS_SETTINGS = {
    "HAPS_CONFPROSH",
    "HAPS_DB_LOADING_TCL",
    "HAPS_PLATFORM",
    "UART_DEVICE",
    "UART_LOG_PATH",
    "HAPS_RESET_TCL",
    "HAPS_IMG_LOADING_TCL",
    "HAPS_HMF_TXT",
}

try:
    import serial  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    serial = None


def _parse_cfg_list(raw: str) -> list[str]:
    value = (raw or "").strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return [item.strip() for item in value.split(",") if item.strip()]


def _load_cfg_entries(*, required: bool) -> dict[str, str]:
    if required and not CFGSHELL_CONFIG_FILE.exists():
        raise ValueError(f"missing config file: {CFGSHELL_CONFIG_FILE}")
    if not CFGSHELL_CONFIG_FILE.exists():
        return {}

    entries: dict[str, str] = {}
    for raw_line in CFGSHELL_CONFIG_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        entries[key.strip()] = raw_value.strip()
    return entries


def _parse_positive_int(raw: str | None, default: int) -> int:
    try:
        parsed = int(raw or "")
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _parse_port(raw: str | None, default: int) -> int:
    parsed = _parse_positive_int(raw, default)
    return parsed if 1 <= parsed <= 65535 else default


def load_haps_settings() -> dict[str, Any]:
    cfg_entries = _load_cfg_entries(required=True)
    settings: dict[str, Any] = {}

    for key in REQUIRED_HAPS_SETTINGS:
        value = cfg_entries.get(key)
        if value is None:
            continue
        if key in {"HAPS_PLATFORM", "UART_DEVICE"}:
            parsed_platforms = _parse_cfg_list(value)
            if parsed_platforms:
                settings[key] = parsed_platforms
            continue
        settings[key] = value

    missing = sorted(REQUIRED_HAPS_SETTINGS - settings.keys())
    if missing:
        raise ValueError(f"missing required keys in {CFGSHELL_CONFIG_FILE}: {', '.join(missing)}")
    return settings


def load_ui_limits() -> dict[str, int]:
    cfg_entries = _load_cfg_entries(required=False)
    return {
        "service_port": _parse_port(cfg_entries.get("SERVICE_PORT"), DEFAULT_SERVICE_PORT),
        "create_jobs_max_num": _parse_positive_int(
            cfg_entries.get("CREATE_JOBS_MAX_NUM"), DEFAULT_CREATE_JOBS_MAX_NUM
        ),
        "recent_jobs_max_num": _parse_positive_int(
            cfg_entries.get("RECENT_JOBS_MAX_NUM"), DEFAULT_RECENT_JOBS_MAX_NUM
        ),
    }


def load_service_base_url() -> str:
    cfg_entries = _load_cfg_entries(required=False)
    return str(cfg_entries.get("SERVICE_BASE_URL") or "").strip().rstrip("/")


def load_terminal_path() -> str:
    cfg_entries = _load_cfg_entries(required=False)
    return str(cfg_entries.get("TERMINAL") or "").strip()


class OpenOcdCfgInput(BaseModel):
    tool_path: str = ""
    cfg_file: str = ""


class JobInput(BaseModel):
    jobs_id: str = ""
    haps_platform: str = "BJ-HAPS80"
    database_path: str = ""
    database_path_enabled: bool = False
    reset_script: str = ""
    reset_script_enabled: bool = False
    imgload_script: str = ""
    imgload_script_enabled: bool = False
    binfile: str = ""
    img_file: str = ""
    log_path: str = ""
    openocd_cfg: OpenOcdCfgInput = Field(default_factory=OpenOcdCfgInput)
    uart_paths: list[str] = Field(default_factory=list)
    duration_minutes: int = 0
    auto_finish: bool = True
    user_id: str = ""


class SubmitJobsRequest(BaseModel):
    jobs: list[JobInput] = Field(default_factory=list)


@dataclass
class JobRecord:
    id: str
    payload: dict[str, Any]
    status: str
    submit_time: str
    running_since: str
    end_time: str | None = None
    message: str = ""
    stop_confirmed: bool = False
    stop_confirm_time: str | None = None
    run_token: int = 0
    process: subprocess.Popen[str] | None = field(default=None, repr=False)


@dataclass
class WaitingJobRecord:
    id: str
    payload: dict[str, Any]
    submit_time: str


@dataclass
class HapsLockSession:
    process: subprocess.Popen[str]
    device_id: str
    handle: str
    io_fd: int


class UartStreamManager:
    MAX_LINES_PER_DEVICE = 400

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connections: set[WebSocket] = set()
        self._connections_by_client: dict[str, WebSocket] = {}
        self._buffers: dict[str, dict[str, deque[dict[str, str]]]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=self.MAX_LINES_PER_DEVICE))
        )
        self._threads: dict[tuple[str, str], tuple[threading.Event, threading.Thread]] = {}
        self._last_line_seen: dict[tuple[str, str], tuple[str, float]] = {}
        self._writers: dict[tuple[str, str], Any] = {}
        self._writer_locks: dict[tuple[str, str], threading.Lock] = {}
        self._uart_users: dict[tuple[str, str], str] = {}
        self._uart_log_paths: dict[tuple[str, str], str] = {}
        self._uart_log_files: dict[tuple[str, str], Any] = {}
        self._uart_log_locks: dict[tuple[str, str], threading.Lock] = {}
        self._lock = threading.Lock()

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def snapshot(self) -> dict[str, dict[str, list[dict[str, str]]]]:
        with self._lock:
            return {
                job_id: {device: list(lines) for device, lines in by_device.items()}
                for job_id, by_device in self._buffers.items()
            }

    def register(self, websocket: WebSocket, client_key: str) -> WebSocket | None:
        replaced: WebSocket | None = None
        with self._lock:
            previous = self._connections_by_client.get(client_key)
            if previous is not None and previous is not websocket:
                self._connections.discard(previous)
                replaced = previous
            self._connections.add(websocket)
            self._connections_by_client[client_key] = websocket
        return replaced

    def unregister(self, websocket: WebSocket, client_key: str) -> None:
        with self._lock:
            self._connections.discard(websocket)
            tracked = self._connections_by_client.get(client_key)
            if tracked is websocket:
                self._connections_by_client.pop(client_key, None)

    @staticmethod
    def _sanitize_device_name(device: str) -> str:
        cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(device))
        return cleaned.strip("_") or "uart"

    @staticmethod
    def _resolve_log_directory(log_path: str) -> Path:
        path_text = str(log_path or "").strip()
        if not path_text:
            return Path.cwd()
        return Path(path_text).expanduser()

    def _write_uart_log(self, job_id: str, device: str, line: str) -> None:
        key = (str(job_id), str(device))
        with self._lock:
            handle = self._uart_log_files.get(key)
            lock = self._uart_log_locks.get(key)
            run_as_user = str(self._uart_users.get(key) or "")
            fallback_log_path = str(self._uart_log_paths.get(key) or "")
        if handle is None or lock is None:
            if fallback_log_path and run_as_user:
                try:
                    subprocess.run(
                        _wrap_command_for_user(
                            ["bash", "-lc", f"printf '%s\\n' {shlex.quote(line)} >> {shlex.quote(fallback_log_path)}"],
                            run_as_user,
                        ),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        text=True,
                    )
                except Exception:
                    pass
            return
        try:
            with lock:
                handle.write(f"{line}\n")
                handle.flush()
        except Exception:
            if fallback_log_path and run_as_user:
                try:
                    subprocess.run(
                        _wrap_command_for_user(
                            ["bash", "-lc", f"printf '%s\\n' {shlex.quote(line)} >> {shlex.quote(fallback_log_path)}"],
                            run_as_user,
                        ),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        text=True,
                    )
                except Exception:
                    pass
            return

    def start_capture(self, job_id: str, jobs_id: str, uart_paths: list[str], log_path: str, run_as_user: str = "") -> None:
        unique_paths = sorted({path.strip() for path in uart_paths if path and path.strip()})
        if not unique_paths:
            return

        # UART capture is single-owner: only the current running job can hold listeners.
        with self._lock:
            previous_job_ids = {running_job_id for running_job_id, _ in self._threads.keys() if running_job_id != job_id}
        for previous_job_id in previous_job_ids:
            self.stop_capture(previous_job_id)

        for device in unique_paths:
            key = (job_id, device)
            with self._lock:
                if key in self._threads:
                    continue
                safe_device = self._sanitize_device_name(device)
                safe_jobs_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(jobs_id or "job"))
                resolved_log_dir = self._resolve_log_directory(log_path)
                uart_log_path = resolved_log_dir / f"{safe_jobs_id}{safe_device}.log"
                log_handle = None
                try:
                    resolved_log_dir.mkdir(parents=True, exist_ok=True)
                    log_handle = uart_log_path.open("a", encoding="utf-8")
                except Exception:
                    try:
                        fallback_dir = Path("/tmp/uart_logs")
                        fallback_dir.mkdir(parents=True, exist_ok=True)
                        uart_log_path = fallback_dir / f"{safe_jobs_id}{safe_device}.log"
                        log_handle = uart_log_path.open("a", encoding="utf-8")
                    except Exception:
                        log_handle = None
                self._uart_log_files[key] = log_handle
                self._uart_log_locks[key] = threading.Lock()
                self._uart_users[key] = str(run_as_user or "")
                self._uart_log_paths[key] = str(uart_log_path)
                stop_event = threading.Event()
                worker = threading.Thread(target=self._read_serial_worker, args=(job_id, device, stop_event), daemon=True)
                self._threads[key] = (stop_event, worker)
            worker.start()

    def stop_capture(self, job_id: str) -> None:
        with self._lock:
            targets = [key for key in self._threads if key[0] == job_id]
            workers = [self._threads.pop(key) for key in targets]
            for key in targets:
                self._writers.pop(key, None)
                self._writer_locks.pop(key, None)
                handle = self._uart_log_files.pop(key, None)
                self._uart_log_locks.pop(key, None)
                self._uart_users.pop(key, None)
                self._uart_log_paths.pop(key, None)
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        pass

        for stop_event, _ in workers:
            stop_event.set()
        for _, thread in workers:
            thread.join(timeout=2.0)

    def stop_all_capture(self) -> None:
        with self._lock:
            workers = list(self._threads.values())
            self._threads.clear()
            self._writers.clear()
            self._writer_locks.clear()
            handles = list(self._uart_log_files.values())
            self._uart_log_files.clear()
            self._uart_log_locks.clear()
            self._uart_users.clear()
            self._uart_log_paths.clear()

        for stop_event, _ in workers:
            stop_event.set()
        for _, thread in workers:
            thread.join(timeout=2.0)
        for handle in handles:
            try:
                handle.close()
            except Exception:
                pass

    def _append_and_broadcast(self, message: dict[str, str]) -> None:
        device = message.get("device", "unknown")
        job_id = message.get("job_id", "")
        with self._lock:
            self._buffers[job_id][device].append(message)
        self._broadcast(message)

    @staticmethod
    def _try_fix_uart_permission(device: str, run_as_user: str) -> bool:
        target = str(device or "").strip()
        user = str(run_as_user or "").strip()
        if not target:
            return False
        commands: list[list[str]] = []
        if user:
            commands.append(["sudo", "-n", "setfacl", "-m", f"u:{user}:rw", target])
        commands.append(["sudo", "-n", "chmod", "666", target])
        for command in commands:
            try:
                rc = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True).returncode
                if rc == 0:
                    return True
            except Exception:
                continue
        return False

    def write_input(self, job_id: str, device: str, content: str, append_newline: bool = False) -> tuple[bool, str]:
        key = (str(job_id), str(device))
        with self._lock:
            writer = self._writers.get(key)
            writer_lock = self._writer_locks.get(key)
        if writer is None or writer_lock is None:
            return False, f"[{job_id}] {device} is not available for input"
        payload = content
        if append_newline and not payload.endswith("\n"):
            payload = f"{payload}\n"
        data = payload.encode("utf-8", errors="replace")
        try:
            with writer_lock:
                writer.write(data)
                writer.flush()
            preview = payload.replace("\r", "\\r").replace("\n", "\\n")
            ts = datetime.now().isoformat(timespec="seconds")
            self._write_uart_log(str(job_id), str(device), f"[{ts}] TX> {preview}")
            self._append_and_broadcast({
                "type": "status",
                "job_id": str(job_id),
                "device": str(device),
                "line": f"[{job_id}] TX> {preview}",
                "ts": ts,
            })
            return True, "ok"
        except Exception as exc:
            return False, f"[{job_id}] write failed on {device}: {exc}"

    def _read_serial_worker(self, job_id: str, device: str, stop_event: threading.Event) -> None:
        with self._lock:
            run_as_user = str(self._uart_users.get((job_id, device)) or "")
        permission_fixed = False
        if serial is None:
            ts = datetime.now().isoformat(timespec="seconds")
            message = "pyserial is not installed on server"
            self._write_uart_log(job_id, device, f"[{ts}] {message}")
            self._append_and_broadcast({
                "type": "status",
                "job_id": job_id,
                "device": device,
                "line": message,
                "ts": ts,
            })
            return

        open_ts = datetime.now().isoformat(timespec="seconds")
        self._write_uart_log(job_id, device, f"[{open_ts}] [{job_id}] opening {device}")
        self._append_and_broadcast({
            "type": "status",
            "job_id": job_id,
            "device": device,
            "line": f"[{job_id}] opening {device}",
            "ts": open_ts,
        })
        try:
            open_kwargs = {"baudrate": 115200, "timeout": 0.5, "exclusive": True}
            uart = None
            warned_busy = False
            open_started = time.monotonic()
            last_wait_notice = 0.0
            while not stop_event.is_set():
                try:
                    uart = serial.Serial(device, **open_kwargs)
                    break
                except TypeError:
                    # Older pyserial may not support "exclusive" kwarg.
                    open_kwargs.pop("exclusive", None)
                    uart = serial.Serial(device, **open_kwargs)
                    break
                except Exception as open_exc:
                    message = str(open_exc).lower()
                    is_busy = any(token in message for token in ("resource busy", "device or resource busy", "permission denied", "could not exclusively lock"))
                    if not is_busy:
                        raise
                    if ("permission denied" in message) and (not permission_fixed):
                        permission_fixed = True
                        fixed = self._try_fix_uart_permission(device, run_as_user)
                        fix_ts = datetime.now().isoformat(timespec="seconds")
                        fix_message = f"[{job_id}] try fix UART permission on {device}: {'ok' if fixed else 'failed'}"
                        self._write_uart_log(job_id, device, f"[{fix_ts}] {fix_message}")
                        self._append_and_broadcast({
                            "type": "status",
                            "job_id": job_id,
                            "device": device,
                            "line": fix_message,
                            "ts": fix_ts,
                        })
                        if fixed:
                            continue
                    now_wait = time.monotonic()
                    if (not warned_busy) or (now_wait - last_wait_notice >= 5):
                        warned_busy = True
                        last_wait_notice = now_wait
                        busy_ts = datetime.now().isoformat(timespec="seconds")
                        elapsed = int(max(0, now_wait - open_started))
                        busy_message = f"[{job_id}] waiting for UART release ({elapsed}s): {open_exc}"
                        self._write_uart_log(job_id, device, f"[{busy_ts}] {busy_message}")
                        self._append_and_broadcast({
                            "type": "status",
                            "job_id": job_id,
                            "device": device,
                            "line": busy_message,
                            "ts": busy_ts,
                        })
                    if (now_wait - open_started) >= 120:
                        raise TimeoutError(f"open {device} timeout after 120s: {open_exc}")
                    time.sleep(0.3)

            if uart is None:
                return

            with uart:
                if fcntl is not None and termios is not None and hasattr(termios, "TIOCEXCL"):
                    try:
                        fcntl.ioctl(uart.fileno(), termios.TIOCEXCL)
                    except OSError:
                        # Some drivers/pty devices do not support TIOCEXCL; continue with best-effort lock.
                        pass
                self._append_and_broadcast({
                    "type": "status",
                    "job_id": job_id,
                    "device": device,
                    "line": f"[{job_id}] {device} locked exclusively",
                    "ts": datetime.now().isoformat(timespec="seconds"),
                })
                self._write_uart_log(job_id, device, f"[{datetime.now().isoformat(timespec='seconds')}] [{job_id}] {device} locked exclusively")
                with self._lock:
                    self._writers[(job_id, device)] = uart
                    self._writer_locks[(job_id, device)] = threading.Lock()
                while not stop_event.is_set():
                    raw = uart.readline()
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        continue

                    dedup_key = (job_id, device)
                    now_mono = time.monotonic()
                    with self._lock:
                        prev = self._last_line_seen.get(dedup_key)
                        self._last_line_seen[dedup_key] = (line, now_mono)
                    # Filter accidental immediate duplicate sampling caused by some UART adapters/drivers.
                    if prev and prev[0] == line and (now_mono - prev[1]) < 0.6:
                        continue

                    ts = datetime.now().isoformat(timespec="seconds")
                    self._append_and_broadcast({
                        "type": "line",
                        "job_id": job_id,
                        "device": device,
                        "line": line,
                        "ts": ts,
                    })
                    self._write_uart_log(job_id, device, f"[{ts}] RX> {line}")
        except Exception as exc:
            fail_ts = datetime.now().isoformat(timespec="seconds")
            fail_message = f"[{job_id}] serial read failed: {exc}"
            self._write_uart_log(job_id, device, f"[{fail_ts}] {fail_message}")
            self._append_and_broadcast({
                "type": "status",
                "job_id": job_id,
                "device": device,
                "line": fail_message,
                "ts": fail_ts,
            })
        finally:
            with self._lock:
                self._threads.pop((job_id, device), None)
                self._last_line_seen.pop((job_id, device), None)
                self._writers.pop((job_id, device), None)
                self._writer_locks.pop((job_id, device), None)
                handle = self._uart_log_files.pop((job_id, device), None)
                self._uart_log_locks.pop((job_id, device), None)
                self._uart_users.pop((job_id, device), None)
                self._uart_log_paths.pop((job_id, device), None)
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        pass
            self._append_and_broadcast({
                "type": "status",
                "job_id": job_id,
                "device": device,
                "line": f"[{job_id}] closed {device}",
                "ts": datetime.now().isoformat(timespec="seconds"),
            })

    def _broadcast(self, message: dict[str, str]) -> None:
        loop = self._loop
        if loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._broadcast_async(message), loop)

    async def _broadcast_async(self, message: dict[str, str]) -> None:
        with self._lock:
            connections = list(self._connections)
        disconnected: list[WebSocket] = []
        for websocket in connections:
            try:
                await websocket.send_json(message)
            except Exception:
                disconnected.append(websocket)
        if disconnected:
            with self._lock:
                for websocket in disconnected:
                    self._connections.discard(websocket)


class JobManager:
    STOP_CONFIRM_REMINDER_MINUTES = 5
    STOP_GRACE_MINUTES = 5
    PREPARE_POLL_INTERVAL_SECONDS = 0.2
    PREPARE_DB_TO_IMG_DELAY_SECONDS = 5
    PREPARE_RESET_DELAY_AFTER_IMG_SECONDS = 5
    PREPARE_RESET_DELAY_NO_IMG_SECONDS = 20

    def __init__(self, uart_stream: UartStreamManager) -> None:
        limits = load_ui_limits()
        self.max_recent_jobs = int(limits.get("recent_jobs_max_num", DEFAULT_RECENT_JOBS_MAX_NUM))
        self._jobs: dict[str, JobRecord] = {}
        self._order: list[str] = []
        self._waiting_jobs: dict[str, WaitingJobRecord] = {}
        self._waiting_order: list[str] = []
        self._haps_lock_sessions: dict[str, HapsLockSession] = {}
        self._img_dedup_signatures: set[str] = set()
        self._lock = threading.Lock()
        self._uart_stream = uart_stream

    @staticmethod
    def _compute_duration_seconds(submit_time: str, end_time: str) -> int:
        try:
            submit_at = datetime.fromisoformat(str(submit_time))
            ended_at = datetime.fromisoformat(str(end_time))
            return max(0, int((ended_at - submit_at).total_seconds()))
        except (TypeError, ValueError):
            return 0

    def _append_utilization_log(self, job: JobRecord) -> None:
        jobs_id = str((job.payload or {}).get("jobs_id") or job.id)
        end_time = str(job.end_time or datetime.now().isoformat(timespec="seconds"))
        duration_seconds = self._compute_duration_seconds(job.submit_time, end_time)
        line = f"JobsID={jobs_id}, Duration={duration_seconds}s, EndTime={end_time}"
        try:
            UTILIZATION_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with UTILIZATION_LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(f"{line}\n")
        except Exception:
            return

    def _start_job(self, payload: dict[str, Any]) -> JobRecord:
        now = datetime.now().isoformat(timespec="seconds")
        initial_status = "Running::Loading HAPS_DB" if self._should_run_prepare(payload) else "Running::HAPS_RDY"
        job = JobRecord(
            id=str(uuid.uuid4()),
            payload=payload,
            status=initial_status,
            submit_time=now,
            running_since=now,
            message="job started",
        )
        self._jobs[job.id] = job
        self._order.insert(0, job.id)
        self._prune_jobs_locked()
        self._launch_job_process_locked(job)

        return job

    def _launch_job_process_locked(self, job: JobRecord) -> None:
        job.run_token += 1
        run_token = job.run_token
        threading.Thread(target=self._prepare_and_launch_job, args=(job.id, run_token), daemon=True).start()

    @staticmethod
    def _is_running_status(status: str) -> bool:
        return str(status).startswith("Running::")

    @staticmethod
    def _read_haps_settings() -> dict[str, Any]:
        settings = load_haps_settings()
        shell_cmd = shlex.split(str(settings.get("HAPS_CONFPROSH") or "").strip())
        if not shell_cmd:
            raise ValueError("HAPS_CONFPROSH is empty")
        settings["HAPS_CONFPROSH_CMD"] = shell_cmd
        return settings

    @staticmethod
    def _should_run_prepare(payload: dict[str, Any]) -> bool:
        db_enabled = bool(payload.get("database_path_enabled", False))
        reset_enabled = bool(payload.get("reset_script_enabled", False))
        database_path = str(payload.get("database_path") or "").strip()
        reset_script = str(payload.get("reset_script") or "").strip()
        return bool(db_enabled and reset_enabled and database_path and reset_script)

    @staticmethod
    def _should_run_imgload(payload: dict[str, Any]) -> bool:
        imgload_enabled = bool(payload.get("imgload_script_enabled", False))
        imgload_script = str(payload.get("imgload_script") or "").strip()
        img_file = str(payload.get("img_file") or "").strip()
        return bool(imgload_enabled and imgload_script and img_file)

    @staticmethod
    def _extract_platform_family(payload: dict[str, Any]) -> str:
        haps_platform = str(payload.get("haps_platform") or "").upper()
        match = re.search(r"(HAPS\d+)", haps_platform)
        return match.group(1) if match else ""

    @staticmethod
    def _cfgshell_eval(proc: subprocess.Popen[str], io_fd: int, command: str, timeout_seconds: float = 20) -> str:
        fd = io_fd

        # Drain possible banner/help text printed when entering cfgshell.
        for _ in range(8):
            readable, _, _ = select.select([fd], [], [], 0.05)
            if not readable:
                break
            drained = os.read(fd, 4096)
            if not drained:
                break

        os.write(fd, f"{command}\n".encode("utf-8"))

        deadline = time.monotonic() + max(1.0, timeout_seconds)
        raw_chunks: list[str] = []
        last_output_at = time.monotonic()
        saw_output = False
        while time.monotonic() < deadline:
            if proc.poll() is not None and not saw_output:
                raise RuntimeError("cfgshell exited unexpectedly")
            readable, _, _ = select.select([fd], [], [], 0.2)
            if not readable:
                if saw_output and (time.monotonic() - last_output_at) >= 0.5:
                    break
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                continue
            raw_chunks.append(chunk.decode("utf-8", errors="replace"))
            saw_output = True
            last_output_at = time.monotonic()
        if not saw_output:
            raise TimeoutError(f"cfgshell command timeout: {command}")
        raw_output = "".join(raw_chunks).strip()
        if not raw_output:
            raise TimeoutError(f"cfgshell command timeout: {command}")

        lines = [line.strip("\r") for line in raw_output.splitlines()]
        filtered = [line for line in lines if line.strip() and line.strip() != str(command).strip()]
        if filtered:
            return "\n".join(filtered).strip()
        return raw_output

    @staticmethod
    def _extract_available_device(cfg_scan_output: str, payload: dict[str, Any]) -> str | None:
        normalized = " ".join(str(cfg_scan_output or "").split())
        preferred_family = JobManager._extract_platform_family(payload)
        fallback_device: str | None = None
        for match in re.finditer(r"DEVICE\s+(\S+).*?TYPE\s+(\S+).*?STATE\s+(\S+)", normalized):
            device_id, device_type, state = match.groups()
            if not state.startswith("available"):
                continue
            if fallback_device is None:
                fallback_device = device_id
            if preferred_family and preferred_family in device_type.upper():
                return device_id
        return fallback_device

    @staticmethod
    def _summarize_cfg_scan_states(cfg_scan_output: str) -> str:
        normalized = " ".join(str(cfg_scan_output or "").split())
        details: list[str] = []
        for match in re.finditer(r"DEVICE\s+(\S+).*?TYPE\s+(\S+).*?STATE\s+(\S+)", normalized):
            device_id, device_type, state = match.groups()
            details.append(f"{device_id}|{device_type}|{state}")
        if details:
            return "; ".join(details)
        return normalized[:300] if normalized else "empty cfg_scan output"

    @staticmethod
    def _extract_cfg_handle(cfg_open_output: str) -> str | None:
        match = re.search(r"\b(cfg\d+)\b", str(cfg_open_output or ""))
        return match.group(1) if match else None

    @staticmethod
    def _write_process_log(log_file: Any, message: str) -> None:
        if log_file is None:
            return
        try:
            log_file.write(f"{message}\n")
            log_file.flush()
        except Exception:
            return

    @staticmethod
    def _log_stage_timestamp(
        log_file: Any,
        stage: str,
        event: str,
        *,
        log_path: str = "",
        run_as_user: str = "",
    ) -> None:
        timestamp = datetime.now().isoformat(timespec="seconds")
        message = f"[HAPS_STAGE] stage={stage} event={event} ts={timestamp}"
        if log_file is not None:
            JobManager._write_process_log(log_file, message)
            return
        if log_path and run_as_user:
            subprocess.run(
                _wrap_command_for_user(
                    ["bash", "-lc", f"printf '%s\\n' {shlex.quote(message)} >> {shlex.quote(log_path)}"],
                    run_as_user,
                ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )

    @staticmethod
    def _calculate_img_dedup_signature(file_path: str) -> tuple[str, str]:
        sample_bytes = 4 * 1024 * 1024
        file_size = os.path.getsize(file_path)
        with open(file_path, "rb") as handle:
            head = handle.read(sample_bytes)
            tail = b""
            if file_size > sample_bytes:
                handle.seek(max(0, file_size - sample_bytes))
                tail = handle.read(sample_bytes)
        head_crc = zlib.crc32(head) & 0xFFFFFFFF
        tail_crc = zlib.crc32(tail) & 0xFFFFFFFF
        algo = "size+crc32(head4MB,tail4MB)"
        signature = f"{file_size:016x}-{head_crc:08x}-{tail_crc:08x}"
        return algo, signature

    @staticmethod
    def _calculate_img_dedup_signature_as_user(file_path: str, run_as_user: str) -> tuple[str, str]:
        script = """
import json
import os
import pathlib
import sys
import zlib

sample_bytes = 4 * 1024 * 1024
path = pathlib.Path(sys.argv[1]).expanduser()
file_size = os.path.getsize(path)
with path.open("rb") as handle:
    head = handle.read(sample_bytes)
    tail = b""
    if file_size > sample_bytes:
        handle.seek(max(0, file_size - sample_bytes))
        tail = handle.read(sample_bytes)
head_crc = zlib.crc32(head) & 0xFFFFFFFF
tail_crc = zlib.crc32(tail) & 0xFFFFFFFF
algo = "size+crc32(head4MB,tail4MB)"
signature = f"{file_size:016x}-{head_crc:08x}-{tail_crc:08x}"
print(json.dumps({"algo": algo, "signature": signature}))
"""
        result = subprocess.run(
            _wrap_command_for_user(["python3", "-c", script, file_path], run_as_user),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "SW_IMG_CHECK failed").strip())
        try:
            payload = json.loads(result.stdout or "{}")
            return str(payload["algo"]), str(payload["signature"])
        except Exception as exc:
            raise RuntimeError("SW_IMG_CHECK parse failed") from exc

    def _acquire_device_lock(
        self,
        job_id: str,
        payload: dict[str, Any],
        cfgshell_cmd: list[str],
        log_file: Any,
        run_as_user: str,
        log_path: str = "",
        log_owner_user: str = "",
    ) -> None:
        def lock_log(message: str) -> None:
            if log_file is not None:
                self._write_process_log(log_file, message)
                return
            if log_path and log_owner_user:
                subprocess.run(
                    _wrap_command_for_user(
                        ["bash", "-lc", f"printf '%s\\n' {shlex.quote(message)} >> {shlex.quote(log_path)}"],
                        log_owner_user,
                    ),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )

        service_user = get_system_user(None)
        attempts = [run_as_user]
        if service_user and service_user not in attempts:
            attempts.append(service_user)
        if "root" not in attempts:
            attempts.append("root")
        last_error: Exception | None = None
        for lock_user in attempts:
            master_fd, slave_fd = pty.openpty()
            process = subprocess.Popen(
                _wrap_command_for_user(cfgshell_cmd, lock_user),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
            )
            os.close(slave_fd)
            try:
                scan_output = self._cfgshell_eval(process, master_fd, "cfg_scan")
                device_id = self._extract_available_device(scan_output, payload)
                if not device_id:
                    state_info = self._summarize_cfg_scan_states(scan_output)
                    raise RuntimeError(f"no available device from cfg_scan: {state_info}")
                open_output = self._cfgshell_eval(process, master_fd, f"cfg_open {device_id}")
                handle = self._extract_cfg_handle(open_output)
                if not handle:
                    raise RuntimeError(f"cfg_open failed, output={open_output!r}")
                lock_scan_output = self._cfgshell_eval(process, master_fd, "cfg_scan")
                lock_log(
                    f"[HAPS_LOCK] job={job_id} platform={payload.get('haps_platform')} device={device_id} handle={handle} lock_user={lock_user}",
                )
                lock_log(f"[HAPS_LOCK] cfg_scan(after open): {lock_scan_output}")
                with self._lock:
                    self._haps_lock_sessions[job_id] = HapsLockSession(
                        process=process, device_id=device_id, handle=handle, io_fd=master_fd
                    )
                return
            except Exception as exc:
                last_error = exc
                try:
                    process.terminate()
                    process.wait(timeout=3)
                except Exception:
                    try:
                        process.kill()
                        process.wait(timeout=3)
                    except Exception:
                        pass
                try:
                    os.close(master_fd)
                except Exception:
                    pass
                lock_log(f"[HAPS_LOCK] lock attempt failed for user={lock_user}: {exc}")
        if last_error is not None:
            raise last_error

    def _release_haps_lock_locked(self, job_id: str, log_file: Any = None) -> None:
        session = self._haps_lock_sessions.pop(job_id, None)
        if not session:
            return
        try:
            self._cfgshell_eval(session.process, session.io_fd, f"cfg_close {session.handle}", timeout_seconds=10)
            self._write_process_log(log_file, f"[HAPS_LOCK] released job={job_id} handle={session.handle}")
        except Exception as exc:
            self._write_process_log(log_file, f"[HAPS_LOCK] release failed job={job_id}: {exc}")
        finally:
            try:
                os.close(session.io_fd)
            except Exception:
                pass
            try:
                session.process.terminate()
                session.process.wait(timeout=3)
            except Exception:
                try:
                    session.process.kill()
                    session.process.wait(timeout=3)
                except Exception:
                    pass

    def _job_is_current_locked(self, job_id: str, run_token: int) -> bool:
        job = self._jobs.get(job_id)
        return bool(job and job.run_token == run_token and self._is_running_status(job.status))

    def _wait_prepare_delay(self, job_id: str, run_token: int, delay_seconds: int) -> bool:
        deadline = time.monotonic() + max(0, delay_seconds)
        while time.monotonic() < deadline:
            with self._lock:
                if not self._job_is_current_locked(job_id, run_token):
                    return False
            time.sleep(self.PREPARE_POLL_INTERVAL_SECONDS)
        return True

    def _prepare_reset_delay_seconds(self, ran_imgload: bool) -> int:
        if ran_imgload:
            return self.PREPARE_RESET_DELAY_AFTER_IMG_SECONDS
        return self.PREPARE_RESET_DELAY_NO_IMG_SECONDS

    def _prepare_and_launch_job(self, job_id: str, run_token: int) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.run_token != run_token:
                return
            payload = dict(job.payload or {})

        log_file = None
        lock_acquired = False
        try:
            run_as_user = _validate_linux_username(str(payload.get("user_id") or ""))
            log_path = str(payload.get("log_path") or "").strip()
            log_target = subprocess.DEVNULL
            process_log_path_text = ""
            lock_settings: dict[str, Any] | None = None
            lock_cfgshell_cmd: list[str] | None = None
            if log_path:
                mkdir_cmd = _wrap_command_for_user(["mkdir", "-p", log_path], run_as_user)
                subprocess.run(mkdir_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
                jobs_id = str(payload.get("jobs_id") or job_id)
                safe_jobs_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in jobs_id)
                process_log_path = Path(log_path).expanduser() / f"{safe_jobs_id}.log"
                process_log_path_text = str(process_log_path)
                subprocess.run(
                    _wrap_command_for_user(["bash", "-lc", f": >> {shlex.quote(process_log_path_text)}"], run_as_user),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                try:
                    log_file = process_log_path.open("a", encoding="utf-8")
                except Exception:
                    log_file = None
                log_target = log_file if log_file is not None else subprocess.DEVNULL

            def run_as_user_with_log(command_args: list[str]) -> int:
                if process_log_path_text:
                    wrapped = _wrap_command_for_user(
                        ["bash", "-lc", f"{shlex.join(command_args)} >> {shlex.quote(process_log_path_text)} 2>&1"],
                        run_as_user,
                    )
                    return subprocess.run(wrapped, text=True).returncode
                return subprocess.run(
                    _wrap_command_for_user(command_args, run_as_user),
                    stdout=log_target,
                    stderr=log_target,
                    text=True,
                ).returncode

            def append_log_line(message: str) -> None:
                if log_file is not None:
                    self._write_process_log(log_file, message)
                    return
                if process_log_path_text:
                    subprocess.run(
                        _wrap_command_for_user(
                            ["bash", "-lc", f"printf '%s\\n' {shlex.quote(message)} >> {shlex.quote(process_log_path_text)}"],
                            run_as_user,
                        ),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        text=True,
                    )

            lock_settings = self._read_haps_settings()
            lock_cfgshell_cmd = list(lock_settings.get("HAPS_CONFPROSH_CMD") or [])

            if self._should_run_prepare(payload):
                settings = lock_settings or self._read_haps_settings()
                cfgshell_cmd = lock_cfgshell_cmd or list(settings.get("HAPS_CONFPROSH_CMD") or [])
                db_load_script = str(settings.get("HAPS_DB_LOADING_TCL") or "").strip()
                database_path = str(payload.get("database_path") or "").strip()
                reset_script = str(payload.get("reset_script") or "").strip()
                haps_platform = str(payload.get("haps_platform") or "").strip()
                hmf_txt = str(payload.get("haps_hmf_txt") or "").strip()
                ran_imgload = False

                with self._lock:
                    if not self._job_is_current_locked(job_id, run_token):
                        return
                    self._jobs[job_id].status = "Running::Loading HAPS_DB"

                db_load_cmd = [*cfgshell_cmd, db_load_script, database_path]
                if "HAPS100" in haps_platform:
                    db_load_cmd.append(hmf_txt)
                self._log_stage_timestamp(
                    log_file, "load_db", "start", log_path=process_log_path_text, run_as_user=run_as_user
                )
                rc1 = run_as_user_with_log(db_load_cmd)
                self._log_stage_timestamp(
                    log_file, "load_db", "end", log_path=process_log_path_text, run_as_user=run_as_user
                )
                if rc1 != 0:
                    self._write_process_log(log_file, f"[HAPS_LOCK] HAPS_DB load failed, exit={rc1}")
                    with self._lock:
                        if self._job_is_current_locked(job_id, run_token):
                            self._jobs[job_id].status = "Failed"
                            self._jobs[job_id].end_time = datetime.now().isoformat(timespec="seconds")
                            self._jobs[job_id].message = f"HAPS_DB load failed (exit={rc1})"
                            self._append_utilization_log(self._jobs[job_id])
                            self._promote_waiting_locked()
                    return

                if self._should_run_imgload(payload):
                    imgload_script = str(payload.get("imgload_script") or "").strip()
                    img_file = str(payload.get("img_file") or "").strip()
                    jobs_id = str(payload.get("jobs_id") or job_id)
                    duration_minutes = self._duration_minutes(payload)
                    if not self._wait_prepare_delay(job_id, run_token, self.PREPARE_DB_TO_IMG_DELAY_SECONDS):
                        return
                    with self._lock:
                        if not self._job_is_current_locked(job_id, run_token):
                            return
                        self._jobs[job_id].status = "Running::Loading SW_IMG"

                    dedup_result: dict[str, str] = {}
                    dedup_error: dict[str, str] = {}

                    def _collect_img_signature() -> None:
                        try:
                            algo, signature = self._calculate_img_dedup_signature_as_user(img_file, run_as_user)
                            dedup_result["algo"] = algo
                            dedup_result["signature"] = signature
                        except Exception as exc:
                            dedup_error["value"] = str(exc)

                    dedup_thread = threading.Thread(target=_collect_img_signature, daemon=True)
                    dedup_thread.start()
                    self._log_stage_timestamp(
                        log_file, "load_img", "start", log_path=process_log_path_text, run_as_user=run_as_user
                    )
                    rc_img = run_as_user_with_log([*cfgshell_cmd, imgload_script, img_file])
                    self._log_stage_timestamp(
                        log_file, "load_img", "end", log_path=process_log_path_text, run_as_user=run_as_user
                    )
                    dedup_thread.join()
                    if "signature" in dedup_result:
                        signature = dedup_result["signature"]
                        with self._lock:
                            duplicate = signature in self._img_dedup_signatures
                            if not duplicate:
                                self._img_dedup_signatures.add(signature)
                        dedup_text = "file is remain unchanged" if duplicate else "file is new"
                        append_log_line(
                            f"[HAPS_LOCK] SW_IMG_CHECK algo={dedup_result['algo']} signature={signature} {dedup_text}: {img_file}"
                        )
                    elif "value" in dedup_error:
                        append_log_line(f"[HAPS_LOCK] SW_IMG_CHECK failed: {dedup_error['value']}")
                    if rc_img != 0:
                        self._write_process_log(log_file, f"[HAPS_LOCK] SW_IMG load failed, exit={rc_img}")
                        with self._lock:
                            if self._job_is_current_locked(job_id, run_token):
                                self._jobs[job_id].status = "Failed"
                                self._jobs[job_id].end_time = datetime.now().isoformat(timespec="seconds")
                                self._jobs[job_id].message = f"SW_IMG load failed (exit={rc_img})"
                                self._append_utilization_log(self._jobs[job_id])
                                self._promote_waiting_locked()
                        return
                    ran_imgload = True

                with self._lock:
                    if not self._job_is_current_locked(job_id, run_token):
                        return
                    self._jobs[job_id].status = "Running::Resetting HAPS_ENV"

                prepare_delay = self._prepare_reset_delay_seconds(ran_imgload)
                if not self._wait_prepare_delay(job_id, run_token, prepare_delay):
                    return

                self._log_stage_timestamp(
                    log_file, "reset", "start", log_path=process_log_path_text, run_as_user=run_as_user
                )
                rc2 = run_as_user_with_log([*cfgshell_cmd, reset_script])
                self._log_stage_timestamp(
                    log_file, "reset", "end", log_path=process_log_path_text, run_as_user=run_as_user
                )
                if rc2 != 0:
                    self._write_process_log(log_file, f"[HAPS_LOCK] HAPS_ENV reset failed, exit={rc2}")
                    with self._lock:
                        if self._job_is_current_locked(job_id, run_token):
                            self._jobs[job_id].status = "Failed"
                            self._jobs[job_id].end_time = datetime.now().isoformat(timespec="seconds")
                            self._jobs[job_id].message = f"HAPS_ENV reset failed (exit={rc2})"
                            self._append_utilization_log(self._jobs[job_id])
                            self._promote_waiting_locked()
                    return

            with self._lock:
                if not self._job_is_current_locked(job_id, run_token):
                    return
                job = self._jobs[job_id]
                job.status = "Running::HAPS_RDY"

            # cfgshell 不支持并行启动：先完成 prepare，再在 HAPS_RDY 阶段进行设备 lock。
            cfgshell_cmd_for_lock = lock_cfgshell_cmd or []
            self._acquire_device_lock(
                job_id,
                payload,
                cfgshell_cmd_for_lock,
                log_file,
                run_as_user=run_as_user,
                log_path=process_log_path_text,
                log_owner_user=run_as_user,
            )
            lock_acquired = True

            with self._lock:
                if not self._job_is_current_locked(job_id, run_token):
                    self._release_haps_lock_locked(job_id, log_file=log_file)
                    lock_acquired = False
                    return
                job = self._jobs[job_id]
                command = self._build_job_command(job.payload)
                if process_log_path_text:
                    command = f"{command} >> {shlex.quote(process_log_path_text)} 2>&1"
                process = subprocess.Popen(
                    _wrap_command_for_user(["bash", "-lc", command], run_as_user),
                    stdout=log_file if (log_file is not None and not process_log_path_text) else subprocess.DEVNULL,
                    stderr=log_file if (log_file is not None and not process_log_path_text) else subprocess.DEVNULL,
                    text=True,
                )
                job.process = process
                uart_paths = list((job.payload or {}).get("uart_paths") or [])
                jobs_id = str((job.payload or {}).get("jobs_id") or job.id)
                log_path = str((job.payload or {}).get("log_path") or "")
                run_as_user_for_uart = str((job.payload or {}).get("user_id") or "")
                self._uart_stream.start_capture(job.id, jobs_id, uart_paths, log_path, run_as_user=run_as_user_for_uart)
                threading.Thread(target=self._watch_job, args=(job.id, job.run_token), daemon=True).start()
        except Exception as exc:
            self._write_process_log(log_file, f"[HAPS_LOCK] prepare exception: {exc}")
            with self._lock:
                if self._job_is_current_locked(job_id, run_token):
                    self._release_haps_lock_locked(job_id, log_file=log_file)
                    lock_acquired = False
                    self._jobs[job_id].status = "Failed"
                    self._jobs[job_id].end_time = datetime.now().isoformat(timespec="seconds")
                    self._jobs[job_id].message = f"db/reset prepare failed: {exc}"
                    self._append_utilization_log(self._jobs[job_id])
                    self._promote_waiting_locked()
        finally:
            if lock_acquired:
                with self._lock:
                    if not self._job_is_current_locked(job_id, run_token):
                        self._release_haps_lock_locked(job_id, log_file=log_file)
            if log_file is not None:
                try:
                    log_file.flush()
                    log_file.close()
                except Exception:
                    pass


    @staticmethod
    def _duration_minutes(payload: dict[str, Any]) -> int:
        try:
            return max(0, int(payload.get("duration_minutes") or 0))
        except (TypeError, ValueError):
            return 0

    def _active_running_for_platform(self, jobs: dict[str, JobRecord], order: list[str], platform: str) -> JobRecord | None:
        for job_id in order:
            job = jobs.get(job_id)
            if not job or not self._is_running_status(job.status):
                continue
            if (job.payload or {}).get("haps_platform") == platform:
                return job
        return None

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._apply_timeouts_locked()
            self._promote_waiting_locked()

            user_id = str(payload.get("user_id") or "user")
            platform = str(payload.get("haps_platform") or "")
            running = self._active_running_for_platform(self._jobs, self._order, platform)
            if running:
                if any((self._waiting_jobs[jid].payload or {}).get("user_id") == user_id for jid in self._waiting_order if jid in self._waiting_jobs):
                    raise ValueError("same user can only have one waiting job")
                waiting = WaitingJobRecord(
                    id=str(uuid.uuid4()),
                    payload=payload,
                    submit_time=datetime.now().isoformat(timespec="seconds"),
                )
                self._waiting_jobs[waiting.id] = waiting
                self._waiting_order.append(waiting.id)
                return {"type": "waiting", "job": self._waiting_to_api(waiting)}

            job = self._start_job(payload)
            return {"type": "running", "job": self._to_api(job)}

    def _promote_waiting_locked(self) -> None:
        promoted = True
        while promoted:
            promoted = False
            for waiting_id in list(self._waiting_order):
                waiting = self._waiting_jobs.get(waiting_id)
                if not waiting:
                    continue
                platform = str((waiting.payload or {}).get("haps_platform") or "")
                running = self._active_running_for_platform(self._jobs, self._order, platform)
                if running:
                    continue
                self._waiting_jobs.pop(waiting_id, None)
                self._waiting_order = [jid for jid in self._waiting_order if jid != waiting_id]
                self._start_job(waiting.payload)
                promoted = True
                break

    def cancel_waiting(self, waiting_id: str, user_id: str) -> bool:
        with self._lock:
            waiting = self._waiting_jobs.get(waiting_id)
            if not waiting:
                raise KeyError(waiting_id)
            if str((waiting.payload or {}).get("user_id") or "") != user_id:
                raise PermissionError("can only cancel own waiting job")
            self._waiting_jobs.pop(waiting_id, None)
            self._waiting_order = [jid for jid in self._waiting_order if jid != waiting_id]
            return True

    def list_waiting_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            self._apply_timeouts_locked()
            self._promote_waiting_locked()
            return [self._waiting_to_api(self._waiting_jobs[job_id]) for job_id in self._waiting_order if job_id in self._waiting_jobs]

    def _build_job_command(self, payload: dict[str, Any]) -> str:
        """
        Build a demo command that keeps running long enough for timeout logic to take effect.

        Previously this was hard-coded to 20s, which made jobs finish quickly even when the
        UI selected a longer auto-finish duration (for example 10 minutes).
        """
        try:
            duration_minutes = JobManager._duration_minutes(payload)
        except (TypeError, ValueError):
            duration_minutes = 0

        if duration_minutes <= 0:
            # "longtime" jobs are represented as duration_minutes=0 and should not
            # self-finish. Keep the process alive until user manually clicks Finish.
            return "python3 -c \"import time\nwhile True:\n    time.sleep(3600)\""
        else:
            # Add a small buffer so the process won't naturally exit before timeout handling.
            sleep_seconds = duration_minutes * 60 + self.STOP_GRACE_MINUTES * 60 + 30

        return f"python3 -c \"import time; time.sleep({sleep_seconds}); print('job done')\""

    def _watch_job(self, job_id: str, run_token: int) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.run_token != run_token:
                return
            process = job.process
        if not process:
            return

        rc = process.wait()
        with self._lock:
            current = self._jobs.get(job_id)
            if not current or current.run_token != run_token:
                return
            self._uart_stream.stop_capture(job_id)
            self._release_haps_lock_locked(job_id)
            # If timeout/manual handlers already finalized this job, preserve that status.
            if not self._is_running_status(current.status):
                return
            current.end_time = datetime.now().isoformat(timespec="seconds")
            if rc == 0:
                current.status = "Finish"
                current.message = "job finished"
                self._append_utilization_log(current)
                self._promote_waiting_locked()
            else:
                current.status = "Failed"
                current.message = f"job failed (exit={rc})"
                self._append_utilization_log(current)
                self._promote_waiting_locked()

    def stop(self, job_id: str) -> JobRecord:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            if not self._is_running_status(job.status):
                return job
            process = job.process
            # Invalidate existing watcher callbacks before terminating process to avoid
            # duplicate finalization/logging races with _watch_job.
            job.run_token += 1
            job.process = None

        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        with self._lock:
            job = self._jobs[job_id]
            self._uart_stream.stop_capture(job_id)
            self._release_haps_lock_locked(job_id)
            job.status = "Finish"
            job.end_time = datetime.now().isoformat(timespec="seconds")
            job.message = "job manually finished"
            self._append_utilization_log(job)
            self._promote_waiting_locked()
            return job

    def confirm_stop(self, job_id: str, user_id: str) -> JobRecord:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            owner = str((job.payload or {}).get("user_id") or "")
            if owner != user_id:
                raise PermissionError("can only confirm own running job")
            if not self._is_running_status(job.status):
                return job
            job.stop_confirmed = True
            job.stop_confirm_time = datetime.now().isoformat(timespec="seconds")
            job.message = "stop timing confirmed"
            return job

    def stop_and_resubmit(self, job_id: str, user_id: str) -> JobRecord:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            owner = str((job.payload or {}).get("user_id") or "")
            if owner != user_id:
                raise PermissionError("can only resubmit own running job")
            if not self._is_running_status(job.status):
                raise ValueError("job is not running")
            if job.status != "Running::HAPS_RDY":
                raise ValueError("stop and resubmit is only allowed in Running::HAPS_RDY")
            duration_minutes = self._duration_minutes(job.payload or {})
            if duration_minutes > 0:
                try:
                    submit_at = datetime.fromisoformat(job.submit_time)
                except ValueError:
                    submit_at = datetime.now()
                elapsed_seconds = (datetime.now() - submit_at).total_seconds()
                remaining_seconds = duration_minutes * 60 - elapsed_seconds
                if remaining_seconds <= self.STOP_CONFIRM_REMINDER_MINUTES * 60:
                    raise ValueError("remaining execution time is less than 5 minutes, stop and resubmit is not allowed")
            process = job.process
            self._uart_stream.stop_all_capture()
            self._release_haps_lock_locked(job_id)
            # Immediately invalidate old watcher callbacks to guarantee resubmit priority
            # over waiting queue promotion while old process exits.
            job.run_token += 1
            job.process = None
            job.status = "Running::Loading HAPS"
            job.message = "job resubmitting from Running::Loading HAPS"
            job.stop_confirmed = False
            job.stop_confirm_time = None

        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        with self._lock:
            current = self._jobs.get(job_id)
            if not current:
                raise KeyError(job_id)
            if not self._is_running_status(current.status):
                raise ValueError("job is not running")
            current.end_time = None
            current.status = "Running::Loading HAPS"
            current.message = "job stopped and resubmitted from Running::Loading HAPS"
            current.stop_confirmed = False
            current.stop_confirm_time = None
            self._launch_job_process_locked(current)
            return current

    def _finish_running_job_locked(self, job: JobRecord, message: str) -> None:
        process = job.process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self._uart_stream.stop_capture(job.id)
        self._release_haps_lock_locked(job.id)
        job.run_token += 1
        job.process = None
        job.status = "Finish"
        job.end_time = datetime.now().isoformat(timespec="seconds")
        job.message = message
        self._append_utilization_log(job)

    def _apply_timeouts_locked(self) -> None:
        now = datetime.now()
        for job_id in list(self._order):
            job = self._jobs.get(job_id)
            if not job or not self._is_running_status(job.status):
                continue
            payload = job.payload or {}
            duration_minutes = self._duration_minutes(payload)
            if duration_minutes <= 0:
                continue
            try:
                submit_at = datetime.fromisoformat(job.submit_time)
            except ValueError:
                continue

            auto_finish = bool(payload.get("auto_finish", True))
            elapsed_seconds = (now - submit_at).total_seconds()
            timeout_seconds = duration_minutes * 60
            remaining_seconds = timeout_seconds - elapsed_seconds

            if auto_finish:
                if elapsed_seconds >= timeout_seconds:
                    self._finish_running_job_locked(job, "job auto finished on timeout")
                continue

            if elapsed_seconds < timeout_seconds:
                if remaining_seconds <= self.STOP_CONFIRM_REMINDER_MINUTES * 60 and not job.stop_confirmed:
                    job.message = "less than 5 minutes left, waiting for stop confirmation"
                continue

            if job.stop_confirmed:
                self._finish_running_job_locked(job, "job finished on timeout after owner confirmation")
                continue

            grace_seconds = self.STOP_GRACE_MINUTES * 60
            if elapsed_seconds >= timeout_seconds + grace_seconds:
                self._finish_running_job_locked(job, "job auto finished 5 minutes after timeout without confirmation")
            else:
                job.message = "Unconfirmed Stop in 5 minutes"

    def list_jobs(self, viewer_user_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            self._apply_timeouts_locked()
            self._promote_waiting_locked()
            self._prune_jobs_locked()
            return [self._to_api(self._jobs[job_id], viewer_user_id=viewer_user_id) for job_id in self._order]

    def _prune_jobs_locked(self) -> None:
        self._order = [job_id for job_id in self._order if job_id in self._jobs]
        overflow = self._order[self.max_recent_jobs :]
        if not overflow:
            return

        for job_id in overflow:
            self._jobs.pop(job_id, None)
        del self._order[self.max_recent_jobs :]


    def _estimate_waiting_schedule(self, waiting_id: str) -> tuple[datetime | None, JobRecord | None]:
        waiting = self._waiting_jobs.get(waiting_id)
        if not waiting:
            return None, None
        platform = str((waiting.payload or {}).get("haps_platform") or "")
        now = datetime.now()

        start_time: datetime | None = None
        running = self._active_running_for_platform(self._jobs, self._order, platform)
        current_running = running
        if running:
            try:
                running_submit = datetime.fromisoformat(running.submit_time)
            except ValueError:
                running_submit = now
            running_duration = self._duration_minutes(running.payload)
            running_end = running_submit + timedelta(minutes=running_duration) if running_duration > 0 else running_submit
            running_auto_finish = bool((running.payload or {}).get("auto_finish", True))
            if running_duration > 0 and (not running_auto_finish) and (not running.stop_confirmed) and now >= running_end:
                running_end = running_end + timedelta(minutes=self.STOP_GRACE_MINUTES)
            start_time = max(now, running_end)

        for qid in self._waiting_order:
            queued = self._waiting_jobs.get(qid)
            if not queued or qid == waiting_id:
                if qid == waiting_id:
                    break
                continue
            if str((queued.payload or {}).get("haps_platform") or "") != platform:
                continue
            q_duration = self._duration_minutes(queued.payload)
            duration_delta = timedelta(minutes=q_duration)
            if start_time is None:
                start_time = now + duration_delta
            else:
                start_time = start_time + duration_delta

        return start_time, current_running

    def _waiting_to_api(self, waiting: WaitingJobRecord) -> dict[str, Any]:
        start_time, running = self._estimate_waiting_schedule(waiting.id)
        now = datetime.now()
        wait_seconds = max(0, int((start_time - now).total_seconds())) if start_time else 0
        overdue = bool(start_time and now >= start_time and running and self._is_running_status(running.status))
        return {
            "id": waiting.id,
            "submit_time": waiting.submit_time,
            "payload": waiting.payload,
            "estimated_start_time": start_time.isoformat(timespec="seconds") if start_time else None,
            "wait_seconds": wait_seconds,
            "running_user_id": ((running.payload or {}).get("user_id") if running else None),
            "running_job_id": (running.id if running else None),
            "overdue": overdue,
        }

    @staticmethod
    def _to_api(job: JobRecord, viewer_user_id: str | None = None) -> dict[str, Any]:
        payload = dict(job.payload or {})
        owner_user_id = str(payload.get("user_id") or "")
        if viewer_user_id is not None and owner_user_id != str(viewer_user_id):
            payload["log_info"] = "-"
        else:
            payload["log_info"] = build_log_info(str(payload.get("log_path") or ""))
        return {
            "id": job.id,
            "status": job.status,
            "submit_time": job.submit_time,
            "running_since": job.running_since,
            "end_time": job.end_time,
            "message": job.message,
            "stop_confirmed": job.stop_confirmed,
            "stop_confirm_time": job.stop_confirm_time,
            "payload": payload,
        }




def build_log_info(log_path: str) -> str:
    path_text = (log_path or "").strip()
    if not path_text:
        return ""

    source = Path(path_text)
    directory = source if source.is_dir() else source.parent
    if not directory.exists() or not directory.is_dir():
        return ""

    files = sorted([entry.name for entry in directory.iterdir() if entry.is_file() and entry.suffix.lower() in {".log", ".txt"}])
    if not files:
        return f"No log files in {directory}"

    preview = ", ".join(files[:3])
    if len(files) > 3:
        preview += f" ... (+{len(files)-3} more)"
    return f"{directory}: {preview}"


def build_default_log_path(log_root: str, user_id: str, jobs_id: str) -> str:
    root = (log_root or "").strip()
    if not root:
        return ""
    base = Path(root).expanduser()
    safe_job = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(jobs_id or "job"))
    return str(base / safe_job)


def build_jobs_id(jobs_id: str, user_id: str = "") -> str:
    if jobs_id.strip():
        return jobs_id
    user = (user_id or "").strip() or os.getenv("USER") or "user"
    ts = datetime.now().strftime("%y%m%d%H%M%S")
    return f"{user}_{ts}"


def _inspect_path_as_user(path_text: str, run_as_user: str) -> dict[str, Any]:
    script = """
import json
import pathlib
import sys

raw = sys.argv[1]
p = pathlib.Path(raw).expanduser()
print(json.dumps({
    "resolved": str(p),
    "exists": p.exists(),
    "is_file": p.is_file(),
    "is_dir": p.is_dir(),
    "suffix": p.suffix.lower(),
}))
"""
    result = subprocess.run(
        _wrap_command_for_user(["python3", "-c", script, path_text], run_as_user),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"path access failed for user {run_as_user}: {path_text}")
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid path check result for user {run_as_user}: {path_text}") from exc


def _validate_tcl_file(path_text: str, *, field_name: str, run_as_user: str) -> str:
    value = (path_text or "").strip()
    if not value:
        raise ValueError(f"{field_name} is enabled but empty")
    path_info = _inspect_path_as_user(value, run_as_user)
    path_resolved = str(path_info.get("resolved") or value)
    if not bool(path_info.get("exists")):
        raise ValueError(f"{field_name} not found: {path_resolved}")
    if not bool(path_info.get("is_file")):
        raise ValueError(f"{field_name} must be a file: {path_resolved}")
    if str(path_info.get("suffix") or "") != ".tcl":
        raise ValueError(f"{field_name} must be a .tcl script: {path_resolved}")
    return path_resolved


def _validate_img_file(path_text: str, *, field_name: str, run_as_user: str) -> str:
    value = (path_text or "").strip()
    if not value:
        raise ValueError(f"{field_name} is required when imgload_script_enabled=true")
    path_info = _inspect_path_as_user(value, run_as_user)
    path_resolved = str(path_info.get("resolved") or value)
    if not bool(path_info.get("exists")):
        raise ValueError(f"{field_name} not found: {path_resolved}")
    if not bool(path_info.get("is_file")):
        raise ValueError(f"{field_name} must be a file: {path_resolved}")
    if str(path_info.get("suffix") or "") not in {".img", ".bin"}:
        raise ValueError(f"{field_name} must be a .img or .bin file: {path_resolved}")
    return path_resolved


def validate_submit_payload(
    payload: dict[str, Any],
    settings: dict[str, Any],
    run_as_user: str,
    used_uart_paths: set[str] | None = None,
) -> None:
    haps_platform = str(payload.get("haps_platform") or "").strip()
    allowed_platforms = [str(item).strip() for item in list(settings.get("HAPS_PLATFORM") or []) if str(item).strip()]
    if not haps_platform:
        raise ValueError("haps_platform is required")
    if allowed_platforms and haps_platform not in allowed_platforms:
        raise ValueError(f"haps_platform not supported: {haps_platform}")

    db_enabled = bool(payload.get("database_path_enabled", False))
    db_path_text = str(payload.get("database_path") or "").strip()
    if db_enabled:
        if not db_path_text:
            raise ValueError("database_path is enabled but empty")
        database_info = _inspect_path_as_user(db_path_text, run_as_user)
        database_path = str(database_info.get("resolved") or db_path_text)
        if not bool(database_info.get("exists")):
            raise ValueError(f"database_path not found: {database_path}")
        if not bool(database_info.get("is_dir")):
            raise ValueError(f"database_path must be a directory: {database_path}")

    if "HAPS100" in haps_platform and db_enabled:
        hmf_txt = str(payload.get("haps_hmf_txt") or "").strip()
        if not hmf_txt:
            raise ValueError("haps_hmf_txt is required when loading database on HAPS100")

    reset_enabled = bool(payload.get("reset_script_enabled", False))
    imgload_enabled = bool(payload.get("imgload_script_enabled", False))
    if reset_enabled:
        _validate_tcl_file(str(payload.get("reset_script") or ""), field_name="reset_script", run_as_user=run_as_user)
    if imgload_enabled:
        if not db_enabled:
            raise ValueError("imgload_script_enabled requires database_path_enabled=true")
        if not reset_enabled:
            raise ValueError("imgload_script_enabled requires reset_script_enabled=true")
        _validate_tcl_file(str(payload.get("imgload_script") or ""), field_name="imgload_script", run_as_user=run_as_user)
        _validate_img_file(str(payload.get("img_file") or ""), field_name="img_file", run_as_user=run_as_user)

    seen_in_job: set[str] = set()
    normalized: list[str] = []
    allowed_uart_devices = [str(item).strip() for item in list(settings.get("UART_DEVICE") or []) if str(item).strip()]
    allowed_uart_set = set(allowed_uart_devices)
    for raw in list(payload.get("uart_paths") or []):
        text = str(raw or "").strip()
        if not text:
            continue
        if allowed_uart_set and text not in allowed_uart_set:
            raise ValueError(f"uart_path not supported: {text}")
        if text in seen_in_job:
            raise ValueError(f"duplicate UART path in same job: {text}")
        seen_in_job.add(text)
        normalized.append(text)

    if used_uart_paths is not None:
        duplicated_across_jobs = sorted(path for path in normalized if path in used_uart_paths)
        if duplicated_across_jobs:
            raise ValueError(f"duplicate UART path across submitted jobs: {duplicated_across_jobs[0]}")
        used_uart_paths.update(normalized)

    payload["uart_paths"] = normalized


def _uid_to_username(uid: int | None) -> str | None:
    if uid is None:
        return None
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


def _validate_linux_username(username: str) -> str:
    candidate = (username or "").strip()
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", candidate):
        raise ValueError(f"invalid linux username: {candidate!r}")
    try:
        pwd.getpwnam(candidate)
    except KeyError as exc:
        raise ValueError(f"linux user not found: {candidate}") from exc
    return candidate


def _sudo_prefix_for_user(run_as_user: str) -> list[str]:
    service_user = get_system_user(None)
    if run_as_user == service_user:
        return []
    return ["sudo", "-n", "-u", run_as_user, "-H"]


def _wrap_command_for_user(command: list[str], run_as_user: str) -> list[str]:
    return [*_sudo_prefix_for_user(run_as_user), *command]


@dataclass
class SessionRecord:
    token: str
    user_id: str
    username: str
    created_at: float
    expires_at: float


class SessionManager:
    def __init__(self, ttl_seconds: int = 12 * 60 * 60) -> None:
        self._ttl_seconds = ttl_seconds
        self._sessions: dict[str, SessionRecord] = {}
        self._lock = threading.Lock()

    def _cleanup(self, now: float) -> None:
        expired = [token for token, record in self._sessions.items() if record.expires_at <= now]
        for token in expired:
            self._sessions.pop(token, None)

    def issue(self, user_id: str, username: str, reuse_token: str | None = None) -> SessionRecord:
        now = time.time()
        with self._lock:
            self._cleanup(now)
            if reuse_token:
                existing = self._sessions.get(reuse_token)
                if existing and existing.expires_at > now and existing.user_id == user_id:
                    existing.expires_at = now + self._ttl_seconds
                    return existing
            token = secrets.token_urlsafe(32)
            record = SessionRecord(
                token=token,
                user_id=str(user_id),
                username=str(username),
                created_at=now,
                expires_at=now + self._ttl_seconds,
            )
            self._sessions[token] = record
            return record

    def resolve(self, token: str | None) -> SessionRecord | None:
        if not token:
            return None
        now = time.time()
        with self._lock:
            self._cleanup(now)
            session = self._sessions.get(token)
            if session is None or session.expires_at <= now:
                return None
            return session


def get_system_user_id(request: Request | None = None) -> str:
    """Resolve stable user identity based on linux login name (whoami style)."""
    if request is not None:
        for key in ("x-linux-user", "x-remote-user", "remote-user", "x-user", "x-auth-request-user"):
            value = (request.headers.get(key) or "").strip()
            if value:
                return value

        for key in ("x-linux-uid", "x-user-id", "x-auth-request-uid"):
            value = (request.headers.get(key) or "").strip()
            if value.isdigit():
                username = _uid_to_username(int(value))
                if username:
                    return username

        # On shared Linux hosts, requests usually come from localhost. In that case we can
        # map the client socket to the kernel-recorded UID in /proc/net/tcp* to identify the
        # actual login user instead of the account that started this FastAPI service.
        client = request.client
        local_host = request.url.hostname or ""
        if client and client.port:
            uid = _get_local_socket_uid(
                local_host=local_host,
                local_port=request.url.port,
                remote_host=client.host,
                remote_port=client.port,
            )
            username = _uid_to_username(uid)
            if username:
                return username

    return get_system_user(None)


def get_system_user(request: Request | None = None) -> str:
    try:
        user = os.getlogin().strip()
        if user:
            return user
    except OSError:
        pass

    for key in ("LOGNAME", "USER", "USERNAME"):
        user = (os.getenv(key) or "").strip()
        if user:
            return user

    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return "user"


def _is_loopback_host(host: str) -> bool:
    normalized = (host or "").strip().lower()
    return normalized in {"127.0.0.1", "::1", "localhost"}


def _ipv4_hex(host: str) -> str:
    packed = socket.inet_aton(host)
    # /proc/net/tcp stores IPv4 bytes in little-endian order.
    return packed[::-1].hex().upper()


def _parse_proc_tcp_uid(
    table_path: str,
    local_hex: str,
    local_port: int,
    remote_hex: str,
    remote_port: int,
) -> int | None:
    try:
        with open(table_path, encoding="utf-8") as handle:
            next(handle, None)
            local_port_hex = f"{local_port:04X}"
            remote_port_hex = f"{remote_port:04X}"
            target_local = f"{local_hex}:{local_port_hex}"
            target_remote = f"{remote_hex}:{remote_port_hex}"
            for line in handle:
                fields = line.split()
                if len(fields) < 8:
                    continue
                if fields[1] != target_local or fields[2] != target_remote:
                    continue
                try:
                    return int(fields[7])
                except ValueError:
                    return None
    except OSError:
        return None
    return None


def _get_local_socket_uid(local_host: str, local_port: int | None, remote_host: str, remote_port: int) -> int | None:
    if not local_port:
        return None
    if not (_is_loopback_host(local_host) and _is_loopback_host(remote_host)):
        return None

    # We only match IPv4 localhost here; if service is accessed via IPv6 (::1), fallback logic applies.
    loopback_hex = _ipv4_hex("127.0.0.1")

    # Prefer client side socket entry (local=client_port, remote=server_port),
    # because its UID belongs to the user's browser/process rather than uvicorn.
    client_uid = _parse_proc_tcp_uid("/proc/net/tcp", loopback_hex, remote_port, loopback_hex, local_port)
    if client_uid is not None:
        return client_uid

    # Fallback to server side entry if client side is not found.
    return _parse_proc_tcp_uid("/proc/net/tcp", loopback_hex, local_port, loopback_hex, remote_port)


app = FastAPI(title="HAPS Jobs Console Platform")
app.mount("/static", StaticFiles(directory=APP_ROOT / "static"), name="static")
uart_stream_manager = UartStreamManager()
manager = JobManager(uart_stream_manager)
session_manager = SessionManager()
SESSION_HEADER = "x-session-token"
SESSION_COOKIE = "cfgshell_session"


@app.on_event("startup")
async def _on_startup() -> None:
    uart_stream_manager.attach_loop(asyncio.get_running_loop())


@app.get("/")
def index() -> FileResponse:
    return FileResponse(APP_ROOT / "static" / "index.html")


def _session_token_from_request(request: Request) -> str:
    header_token = (request.headers.get(SESSION_HEADER) or "").strip()
    if header_token:
        return header_token
    return (request.cookies.get(SESSION_COOKIE) or "").strip()


def _require_session(request: Request) -> SessionRecord:
    token = _session_token_from_request(request)
    if token:
        session = session_manager.resolve(token)
        if session is None:
            raise HTTPException(status_code=401, detail="invalid or expired session")
        return session

    # Backward-compatible fallback: when no session token is provided, derive linux user
    # from the request context and mint a short-lived in-memory session.
    # This keeps legacy deployment/health-check flows working for service user A.
    try:
        fallback_user_id = _validate_linux_username(get_system_user_id(request))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=f"session required: {exc}") from exc
    fallback_user = get_system_user(request)
    return session_manager.issue(user_id=fallback_user_id, username=fallback_user)

@app.websocket("/ws/uart")
async def ws_uart(websocket: WebSocket) -> None:
    token = (websocket.query_params.get("session_token") or "").strip()
    session = session_manager.resolve(token)
    if session is None:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    client_key = websocket.query_params.get("client_id") or "anonymous"
    replaced = uart_stream_manager.register(websocket, client_key)
    if replaced is not None:
        await replaced.close()
    await websocket.send_json({"type": "snapshot", "jobs": uart_stream_manager.snapshot()})
    try:
        while True:
            raw_text = await websocket.receive_text()
            if raw_text == "ping":
                continue
            try:
                message = json.loads(raw_text)
            except json.JSONDecodeError:
                continue
            if message.get("type") != "uart_input":
                continue
            job_id = str(message.get("job_id") or "")
            device = str(message.get("device") or "")
            content = str(message.get("content") or "")
            append_newline = bool(message.get("append_newline", False))
            if not job_id or not device:
                await websocket.send_json({
                    "type": "status",
                    "job_id": job_id,
                    "device": device or "unknown",
                    "line": "UART input ignored: missing job_id/device",
                    "ts": datetime.now().isoformat(timespec="seconds"),
                })
                continue
            job_list = manager.list_jobs(viewer_user_id=session.user_id)
            has_access = any(str(item.get("id")) == job_id for item in job_list)
            if not has_access:
                await websocket.send_json({
                    "type": "status",
                    "job_id": job_id,
                    "device": device,
                    "line": "UART input ignored: permission denied",
                    "ts": datetime.now().isoformat(timespec="seconds"),
                })
                continue
            ok, detail = uart_stream_manager.write_input(job_id, device, content, append_newline=append_newline)
            if not ok:
                await websocket.send_json({
                    "type": "status",
                    "job_id": job_id,
                    "device": device,
                    "line": detail,
                    "ts": datetime.now().isoformat(timespec="seconds"),
                })
    except WebSocketDisconnect:
        pass
    finally:
        uart_stream_manager.unregister(websocket, client_key)




@app.get("/api/session")
def get_session(request: Request) -> JSONResponse:
    user = get_system_user(request)
    try:
        user_id = _validate_linux_username(get_system_user_id(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    current_token = _session_token_from_request(request)
    session = session_manager.issue(user_id=user_id, username=user, reuse_token=current_token or None)
    response = JSONResponse(
        {
            "user": session.username,
            "user_id": session.user_id,
            "session_token": session.token,
        }
    )
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session.token,
        httponly=True,
        samesite="lax",
        max_age=12 * 60 * 60,
    )
    return response


@app.get("/api/client-config")
def get_client_config() -> dict[str, Any]:
    limits = load_ui_limits()
    limits["service_base_url"] = load_service_base_url()
    return limits


def _run_json_python_as_user(run_as_user: str, script: str, args: list[str]) -> dict[str, Any]:
    command = _wrap_command_for_user(["python3", "-c", script, *args], run_as_user)
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "permission denied").strip()
        raise HTTPException(status_code=403, detail=detail)
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="invalid filesystem response") from exc


def _list_directories_as_user(run_as_user: str) -> dict[str, list[str]]:
    script = """
import json
import pathlib
import pwd

home = pathlib.Path(pwd.getpwuid(__import__("os").getuid()).pw_dir)
bases = [home, pathlib.Path.cwd()]
found = []
seen = set()
limit = 20
for base in bases:
    if not base.exists() or not base.is_dir():
        continue
    for item in [base, *sorted(base.iterdir())]:
        if not item.is_dir():
            continue
        text = str(item)
        if text not in seen:
            seen.add(text)
            found.append(text)
        if len(found) >= limit:
            break
    if len(found) >= limit:
        break
print(json.dumps({"directories": found[:limit]}))
"""
    return _run_json_python_as_user(run_as_user, script, [])


def _list_fs_entries_as_user(run_as_user: str, path: str, mode: str) -> dict[str, Any]:
    script = """
import json
import os
import pathlib
import pwd
import sys

target_raw = sys.argv[1]
mode = sys.argv[2]
home = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
target = pathlib.Path(target_raw).expanduser() if target_raw else home
try:
    resolved = target.resolve()
except OSError:
    print(json.dumps({"error": "invalid path"}))
    raise SystemExit(2)
if (not resolved.exists()) or (not resolved.is_dir()):
    print(json.dumps({"error": "path is not a directory"}))
    raise SystemExit(2)
entries = []
for entry in sorted(resolved.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
    if entry.is_dir():
        entries.append({"name": entry.name, "path": str(entry), "type": "directory"})
    elif mode == "file" and entry.is_file():
        entries.append({"name": entry.name, "path": str(entry), "type": "file"})
    if len(entries) >= 200:
        break
parent = str(resolved.parent) if resolved.parent != resolved else ""
print(json.dumps({"cwd": str(resolved), "parent": parent, "mode": mode, "entries": entries}))
"""
    result = _run_json_python_as_user(run_as_user, script, [path, mode])
    if "error" in result:
        raise HTTPException(status_code=400, detail=str(result.get("error")))
    return result




@app.get("/api/directories")
def get_directories(request: Request) -> dict[str, list[str]]:
    session = _require_session(request)
    return _list_directories_as_user(session.user_id)


@app.get("/api/fs")
def get_fs_entries(request: Request, path: str = "", mode: str = "file") -> dict[str, Any]:
    session = _require_session(request)
    return _list_fs_entries_as_user(session.user_id, path, mode)


@app.get("/api/jobs")
def get_jobs(request: Request) -> dict[str, Any]:
    session = _require_session(request)
    return {"jobs": manager.list_jobs(viewer_user_id=session.user_id)}


@app.get("/api/platform-options")
def get_platform_options() -> dict[str, list[str]]:
    try:
        settings = load_haps_settings()
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    platforms = [str(item).strip() for item in list(settings.get("HAPS_PLATFORM") or []) if str(item).strip()]
    uart_devices = [str(item).strip() for item in list(settings.get("UART_DEVICE") or []) if str(item).strip()]
    return {"haps_platforms": platforms, "uart_devices": uart_devices}


@app.post("/api/jobs")
def submit_jobs(payload: SubmitJobsRequest, request: Request) -> dict[str, Any]:
    if not payload.jobs:
        raise HTTPException(status_code=400, detail="jobs cannot be empty")
    limits = load_ui_limits()
    create_jobs_max_num = int(limits.get("create_jobs_max_num", DEFAULT_CREATE_JOBS_MAX_NUM))
    if len(payload.jobs) > create_jobs_max_num:
        raise HTTPException(status_code=400, detail=f"jobs count cannot exceed {create_jobs_max_num}")

    created: list[dict[str, Any]] = []
    session = _require_session(request)
    system_user = session.user_id
    used_uart_paths: set[str] = set()
    try:
        settings = load_haps_settings()
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    default_platforms = [str(item).strip() for item in list(settings.get("HAPS_PLATFORM") or []) if str(item).strip()]
    default_platform = default_platforms[0] if default_platforms else "BJ-HAPS80"

    for item in payload.jobs:
        data = json.loads(item.model_dump_json())
        try:
            data["user_id"] = system_user
            if not str(data.get("haps_platform") or "").strip():
                data["haps_platform"] = default_platform
            data["jobs_id"] = build_jobs_id(data.get("jobs_id", ""), data["user_id"])
            if bool(data.get("reset_script_enabled", False)) and not str(data.get("reset_script") or "").strip():
                data["reset_script"] = str(settings.get("HAPS_RESET_TCL") or "").strip()
            if bool(data.get("imgload_script_enabled", False)) and not str(data.get("imgload_script") or "").strip():
                data["imgload_script"] = str(settings.get("HAPS_IMG_LOADING_TCL") or "").strip()
            data["log_path"] = build_default_log_path(
                str(settings.get("UART_LOG_PATH") or ""),
                data["user_id"],
                data["jobs_id"],
            )
            if data["log_path"]:
                mkdir_rc = subprocess.run(
                    _wrap_command_for_user(["mkdir", "-p", str(data["log_path"])], data["user_id"]),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                ).returncode
                if mkdir_rc != 0:
                    raise ValueError(f"log_path create failed for user {data['user_id']}: {data['log_path']}")
            if "HAPS100" in str(data.get("haps_platform") or ""):
                data["haps_hmf_txt"] = str(settings.get("HAPS_HMF_TXT") or "").strip()

            validate_submit_payload(data, settings=settings, run_as_user=data["user_id"], used_uart_paths=used_uart_paths)
            data["log_info"] = build_log_info(data.get("log_path", ""))
            result = manager.submit(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        created.append(result)

    return {"created": created}


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str, request: Request) -> dict[str, Any]:
    viewer_user_id = _require_session(request).user_id
    all_jobs = manager.list_jobs(viewer_user_id=viewer_user_id)
    target = next((job for job in all_jobs if str(job.get("id")) == str(job_id)), None)
    if target is None:
        raise HTTPException(status_code=404, detail="job not found")
    try:
        job = manager.stop(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    return manager._to_api(job)


@app.post("/api/jobs/{job_id}/open-terminal")
def open_job_terminal(job_id: str, request: Request) -> dict[str, Any]:
    viewer_user_id = _require_session(request).user_id
    all_jobs = manager.list_jobs(viewer_user_id=viewer_user_id)
    target = next((job for job in all_jobs if str(job.get("id")) == str(job_id)), None)
    if target is None:
        raise HTTPException(status_code=404, detail="job not found")
    payload = target.get("payload") or {}
    run_as_user = _validate_linux_username(str(payload.get("user_id") or viewer_user_id))
    if str(payload.get("user_id") or "") != str(viewer_user_id):
        raise HTTPException(status_code=403, detail="only owner can open terminal")
    if not manager._is_running_status(str(target.get("status") or "")):
        raise HTTPException(status_code=400, detail="terminal can only be opened for running jobs")

    terminal_path = load_terminal_path()
    if not terminal_path:
        raise HTTPException(status_code=400, detail="missing TERMINAL in cfgshell.conf")
    if not Path(terminal_path).exists():
        raise HTTPException(status_code=400, detail=f"terminal path not found: {terminal_path}")
    if not os.access(terminal_path, os.X_OK):
        raise HTTPException(status_code=400, detail=f"terminal path is not executable: {terminal_path}")

    launch_cwd = str(payload.get("log_path") or "").strip() or str(Path.home())
    if not Path(launch_cwd).exists():
        launch_cwd = str(Path.home())

    try:
        subprocess.Popen(  # noqa: S603
            _wrap_command_for_user([terminal_path], run_as_user),  # noqa: S607
            cwd=launch_cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to open terminal: {exc}") from exc

    return {"ok": True}


@app.post("/api/jobs/{job_id}/confirm-stop")
def confirm_stop(job_id: str, request: Request) -> dict[str, Any]:
    user_id = _require_session(request).user_id
    try:
        job = manager.confirm_stop(job_id, user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return manager._to_api(job)


@app.post("/api/jobs/{job_id}/stop-and-resubmit")
def stop_and_resubmit(job_id: str, request: Request) -> dict[str, Any]:
    user_id = _require_session(request).user_id
    try:
        job = manager.stop_and_resubmit(job_id, user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return manager._to_api(job)


@app.get("/api/waiting-jobs")
def get_waiting_jobs(request: Request) -> dict[str, Any]:
    _require_session(request)
    return {"jobs": manager.list_waiting_jobs()}


@app.delete("/api/waiting-jobs/{waiting_id}")
def cancel_waiting_job(waiting_id: str, request: Request) -> dict[str, bool]:
    user_id = _require_session(request).user_id
    try:
        manager.cancel_waiting(waiting_id, user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="waiting job not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"ok": True}
