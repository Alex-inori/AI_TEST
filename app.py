from __future__ import annotations

import json
import os
import pwd
import shlex
import socket
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_ROOT = Path(__file__).resolve().parent


class OpenOcdCfgInput(BaseModel):
    tool_path: str = ""
    cfg_file: str = ""


class JobInput(BaseModel):
    jobs_id: str = ""
    haps_platform: str = "BJ-HAPS80"
    database_path: str = "auto"
    database_path_enabled: bool = True
    reset_script: str = "auto"
    reset_script_enabled: bool = True
    imgload_script: str = "auto"
    imgload_script_enabled: bool = True
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
    status: Literal["Runing", "Finish", "Stopped", "Failed"]
    submit_time: str
    end_time: str | None = None
    message: str = ""
    stop_confirmed: bool = False
    stop_confirm_time: str | None = None
    run_token: int = 0
    process: subprocess.Popen[str] | None = field(default=None, repr=False)
    uart_processes: list[subprocess.Popen[str]] = field(default_factory=list, repr=False)


@dataclass
class WaitingJobRecord:
    id: str
    payload: dict[str, Any]
    submit_time: str


class JobManager:
    MAX_RECENT_JOBS = 10
    STOP_CONFIRM_REMINDER_MINUTES = 5
    STOP_GRACE_MINUTES = 5
    UART_LOG_DIR = APP_ROOT / "runtime_uart_logs"

    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._order: list[str] = []
        self._waiting_jobs: dict[str, WaitingJobRecord] = {}
        self._waiting_order: list[str] = []
        self._lock = threading.Lock()

    def _start_job(self, payload: dict[str, Any]) -> JobRecord:
        now = datetime.now().isoformat(timespec="seconds")
        job = JobRecord(
            id=str(uuid.uuid4()),
            payload=payload,
            status="Runing",
            submit_time=now,
            message="job started",
        )
        self._launch_job_process_locked(job)

        self._jobs[job.id] = job
        self._order.insert(0, job.id)
        self._prune_jobs_locked()

        return job

    def _launch_job_process_locked(self, job: JobRecord) -> None:
        command = self._build_job_command(job.payload)
        log_path = (job.payload or {}).get("log_path", "").strip()
        if log_path:
            path = Path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            log_file = path.open("a", encoding="utf-8")
        else:
            log_file = subprocess.DEVNULL

        job.run_token += 1
        process = subprocess.Popen(
            ["bash", "-lc", command],
            stdout=log_file,
            stderr=log_file,
            text=True,
        )
        job.process = process
        self._start_uart_capture_locked(job)
        threading.Thread(target=self._watch_job, args=(job.id, job.run_token), daemon=True).start()

    def _start_uart_capture_locked(self, job: JobRecord) -> None:
        self._stop_uart_capture_locked(job)
        payload = job.payload or {}
        uart_paths = [str(path or "").strip() for path in payload.get("uart_paths") or []]
        uart_paths = [path for path in uart_paths if path]
        if not uart_paths:
            payload["uart_streams"] = []
            return

        self.UART_LOG_DIR.mkdir(parents=True, exist_ok=True)
        streams: list[dict[str, Any]] = []
        for index, path in enumerate(uart_paths):
            log_path = self.UART_LOG_DIR / f"{job.id}_{index}.log"
            try:
                log_path.touch(exist_ok=True)
            except OSError:
                continue

            cat_command = f"stdbuf -oL cat {shlex.quote(path)} >> {shlex.quote(str(log_path))}"
            process = subprocess.Popen(
                ["bash", "-lc", cat_command],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            job.uart_processes.append(process)
            streams.append({"index": index, "path": path, "log": str(log_path)})

        payload["uart_streams"] = streams

    @staticmethod
    def _stop_uart_capture_locked(job: JobRecord) -> None:
        for process in job.uart_processes:
            if process.poll() is not None:
                continue
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        job.uart_processes = []


    @staticmethod
    def _duration_minutes(payload: dict[str, Any]) -> int:
        try:
            return max(0, int(payload.get("duration_minutes") or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _active_running_for_platform(jobs: dict[str, JobRecord], order: list[str], platform: str) -> JobRecord | None:
        for job_id in order:
            job = jobs.get(job_id)
            if not job or job.status != "Runing":
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
            sleep_seconds = 20
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
            # If timeout/manual handlers already finalized this job, preserve that status.
            if current.status != "Runing":
                return
            current.end_time = datetime.now().isoformat(timespec="seconds")
            self._stop_uart_capture_locked(current)
            if rc == 0:
                current.status = "Finish"
                current.message = "job finished"
                self._promote_waiting_locked()
            else:
                current.status = "Failed"
                current.message = f"job failed (exit={rc})"
                self._promote_waiting_locked()

    def stop(self, job_id: str) -> JobRecord:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            if job.status != "Runing":
                return job
            process = job.process

        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        with self._lock:
            job = self._jobs[job_id]
            self._stop_uart_capture_locked(job)
            job.status = "Finish"
            job.end_time = datetime.now().isoformat(timespec="seconds")
            job.message = "job manually finished"
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
            if job.status != "Runing":
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
            if job.status != "Runing":
                raise ValueError("job is not running")
            process = job.process
            # Immediately invalidate old watcher callbacks to guarantee resubmit priority
            # over waiting queue promotion while old process exits.
            job.run_token += 1
            job.process = None
            job.message = "job resubmitting"

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
            if current.status != "Runing":
                raise ValueError("job is not running")
            current.end_time = None
            current.message = "job stopped and resubmitted with original timer"
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
        job.status = "Finish"
        job.end_time = datetime.now().isoformat(timespec="seconds")
        job.message = message
        self._stop_uart_capture_locked(job)

    def _apply_timeouts_locked(self) -> None:
        now = datetime.now()
        for job_id in list(self._order):
            job = self._jobs.get(job_id)
            if not job or job.status != "Runing":
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

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            self._apply_timeouts_locked()
            self._promote_waiting_locked()
            self._prune_jobs_locked()
            return [self._to_api(self._jobs[job_id]) for job_id in self._order]

    def _prune_jobs_locked(self) -> None:
        self._order = [job_id for job_id in self._order if job_id in self._jobs]
        overflow = self._order[self.MAX_RECENT_JOBS :]
        if not overflow:
            return

        for job_id in overflow:
            self._jobs.pop(job_id, None)
        del self._order[self.MAX_RECENT_JOBS :]


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
        overdue = bool(start_time and now >= start_time and running and running.status == "Runing")
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
    def _to_api(job: JobRecord) -> dict[str, Any]:
        return {
            "id": job.id,
            "status": job.status,
            "submit_time": job.submit_time,
            "end_time": job.end_time,
            "message": job.message,
            "stop_confirmed": job.stop_confirmed,
            "stop_confirm_time": job.stop_confirm_time,
            "payload": job.payload,
        }

    def read_uart_output(self, job_id: str, uart_index: int, lines: int = 200) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            streams = (job.payload or {}).get("uart_streams") or []
            target = next((item for item in streams if int(item.get("index", -1)) == uart_index), None)
            if not target:
                raise IndexError(uart_index)
            log_path = Path(str(target.get("log") or ""))
            uart_path = str(target.get("path") or "")

        if not log_path.exists() or not log_path.is_file():
            return {"job_id": job_id, "uart_index": uart_index, "path": uart_path, "text": ""}

        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                content = handle.readlines()
        except OSError:
            content = []

        safe_lines = max(10, min(2000, int(lines)))
        return {
            "job_id": job_id,
            "uart_index": uart_index,
            "path": uart_path,
            "text": "".join(content[-safe_lines:]),
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

def build_jobs_id(jobs_id: str, user_id: str = "") -> str:
    if jobs_id.strip():
        return jobs_id
    user = (user_id or "").strip() or os.getenv("USER") or "user"
    ts = datetime.now().strftime("%y%m%d%H%M%S")
    return f"{user}_{ts}"


def _uid_to_username(uid: int | None) -> str | None:
    if uid is None:
        return None
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


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
manager = JobManager()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(APP_ROOT / "static" / "index.html")




@app.get("/api/session")
def get_session(request: Request) -> dict[str, str]:
    return {
        "user": get_system_user(request),
        "user_id": get_system_user_id(request),
    }




@app.get("/api/directories")
def get_directories() -> dict[str, list[str]]:
    bases = [Path.home(), APP_ROOT]
    found: list[str] = []
    for base in bases:
        if not base.exists() or not base.is_dir():
            continue
        found.append(str(base))
        for child in sorted(base.iterdir()):
            if child.is_dir():
                found.append(str(child))
            if len(found) >= 20:
                break
        if len(found) >= 20:
            break
    # de-duplicate while keeping order
    seen = set()
    dedup = []
    for item in found:
        if item not in seen:
            seen.add(item)
            dedup.append(item)
    return {"directories": dedup[:20]}


@app.get("/api/fs")
def get_fs_entries(path: str = "", mode: str = "file") -> dict[str, Any]:
    target = Path(path).expanduser() if path else Path.home()
    try:
        resolved = target.resolve()
    except OSError:
        raise HTTPException(status_code=400, detail="invalid path")

    if not resolved.exists() or not resolved.is_dir():
        raise HTTPException(status_code=400, detail="path is not a directory")

    entries: list[dict[str, str]] = []
    try:
        for entry in sorted(resolved.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            if entry.is_dir():
                entries.append({"name": entry.name, "path": str(entry), "type": "directory"})
            elif mode == "file" and entry.is_file():
                entries.append({"name": entry.name, "path": str(entry), "type": "file"})
            if len(entries) >= 200:
                break
    except PermissionError:
        raise HTTPException(status_code=403, detail="permission denied")

    parent = str(resolved.parent) if resolved.parent != resolved else ""
    return {
        "cwd": str(resolved),
        "parent": parent,
        "mode": mode,
        "entries": entries,
    }


@app.get("/api/jobs")
def get_jobs() -> dict[str, Any]:
    return {"jobs": manager.list_jobs()}


@app.post("/api/jobs")
def submit_jobs(payload: SubmitJobsRequest, request: Request) -> dict[str, Any]:
    if not payload.jobs:
        raise HTTPException(status_code=400, detail="jobs cannot be empty")

    created: list[dict[str, Any]] = []
    system_user = get_system_user_id(request)
    for item in payload.jobs:
        data = json.loads(item.model_dump_json())
        data["user_id"] = system_user
        data["jobs_id"] = build_jobs_id(data.get("jobs_id", ""), data["user_id"])
        data["log_info"] = build_log_info(data.get("log_path", ""))
        try:
            result = manager.submit(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        created.append(result)

    return {"created": created}


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str) -> dict[str, Any]:
    try:
        job = manager.stop(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    return manager._to_api(job)


@app.post("/api/jobs/{job_id}/confirm-stop")
def confirm_stop(job_id: str, request: Request) -> dict[str, Any]:
    user_id = get_system_user_id(request)
    try:
        job = manager.confirm_stop(job_id, user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return manager._to_api(job)


@app.post("/api/jobs/{job_id}/stop-and-resubmit")
def stop_and_resubmit(job_id: str, request: Request) -> dict[str, Any]:
    user_id = get_system_user_id(request)
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
def get_waiting_jobs() -> dict[str, Any]:
    return {"jobs": manager.list_waiting_jobs()}


@app.delete("/api/waiting-jobs/{waiting_id}")
def cancel_waiting_job(waiting_id: str, user_id: str) -> dict[str, bool]:
    try:
        manager.cancel_waiting(waiting_id, user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="waiting job not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/jobs/{job_id}/uart/{uart_index}")
def get_uart_output(job_id: str, uart_index: int, lines: int = 200) -> dict[str, Any]:
    try:
        return manager.read_uart_output(job_id, uart_index, lines)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    except IndexError as exc:
        raise HTTPException(status_code=404, detail="uart not found") from exc
