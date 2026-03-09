from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
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
    bitfile_mode: Literal["latest", "path"] = "path"
    bitfile: str = ""
    binfile: str = ""
    log_path: str = ""
    openocd_cfg: OpenOcdCfgInput = Field(default_factory=OpenOcdCfgInput)
    uart_paths: list[str] = Field(default_factory=list)


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
    process: subprocess.Popen[str] | None = field(default=None, repr=False)


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def submit(self, payload: dict[str, Any]) -> JobRecord:
        now = datetime.now().isoformat(timespec="seconds")
        job = JobRecord(
            id=str(uuid.uuid4()),
            payload=payload,
            status="Runing",
            submit_time=now,
            message="job started",
        )

        command = "python3 -c \"import time; time.sleep(20); print('job done')\""

        log_path = payload.get("log_path", "").strip()
        if log_path:
            path = Path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            log_file = path.open("a", encoding="utf-8")
        else:
            log_file = subprocess.DEVNULL

        process = subprocess.Popen(
            ["bash", "-lc", command],
            stdout=log_file,
            stderr=log_file,
            text=True,
        )
        job.process = process

        with self._lock:
            self._jobs[job.id] = job
            self._order.insert(0, job.id)

        threading.Thread(target=self._watch_job, args=(job.id,), daemon=True).start()
        return job

    def _watch_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job or not job.process:
            return

        rc = job.process.wait()
        with self._lock:
            current = self._jobs.get(job_id)
            if not current or current.status == "Stopped":
                return
            current.end_time = datetime.now().isoformat(timespec="seconds")
            if rc == 0:
                current.status = "Finish"
                current.message = "job finished"
            else:
                current.status = "Failed"
                current.message = f"job failed (exit={rc})"

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
            job.status = "Finish"
            job.end_time = datetime.now().isoformat(timespec="seconds")
            job.message = "job manually finished"
            return job

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._to_api(self._jobs[job_id]) for job_id in self._order]

    @staticmethod
    def _to_api(job: JobRecord) -> dict[str, Any]:
        payload = dict(job.payload)
        return {
            "id": job.id,
            "status": job.status,
            "submit_time": job.submit_time,
            "end_time": job.end_time,
            "message": job.message,
            "payload": payload,
            "log_info": get_log_info(str(payload.get("log_path", ""))),
        }




def get_log_info(log_path_value: str) -> str:
    log_path_value = log_path_value.strip()
    if not log_path_value:
        return "no log path"

    path = Path(log_path_value)
    if path.is_file():
        try:
            content = path.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
            last_line = content[-1] if content else "empty"
            return f"file={path.name}, size={path.stat().st_size}, last={last_line[:120]}"
        except OSError:
            return f"file={path.name}, unreadable"

    folder = path if path.is_dir() else path.parent
    if not folder.exists():
        return f"dir={folder}, not found"

    files = sorted(
        [f for f in folder.iterdir() if f.is_file()],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )[:3]

    if not files:
        return f"dir={folder}, no files"

    infos = []
    for f in files:
        try:
            content = f.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
            last_line = content[-1] if content else "empty"
            infos.append(f"{f.name}(last={last_line[:60]})")
        except OSError:
            infos.append(f"{f.name}(unreadable)")
    return "; ".join(infos)

def build_jobs_id(jobs_id: str) -> str:
    if jobs_id.strip():
        return jobs_id
    user = os.getenv("USER") or "user"
    ts = datetime.now().strftime("%y%m%d%H%M%S")
    return f"{user}_{ts}"


app = FastAPI(title="HAPS Jobs Console Platform")
app.mount("/static", StaticFiles(directory=APP_ROOT / "static"), name="static")
manager = JobManager()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(APP_ROOT / "static" / "index.html")




@app.get("/api/session")
def get_session() -> dict[str, str]:
    return {"user": os.getenv("USER") or "user"}


@app.get("/api/jobs")
def get_jobs() -> dict[str, Any]:
    return {"jobs": manager.list_jobs()}


@app.post("/api/jobs")
def submit_jobs(request: SubmitJobsRequest) -> dict[str, Any]:
    if not request.jobs:
        raise HTTPException(status_code=400, detail="jobs cannot be empty")

    created: list[dict[str, Any]] = []
    for item in request.jobs:
        data = json.loads(item.model_dump_json())
        data["jobs_id"] = build_jobs_id(data.get("jobs_id", ""))

        bitfile_mode = data.get("bitfile_mode", "path")
        bitfile_value = data.get("bitfile", "").strip()
        if bitfile_mode == "path" and not bitfile_value:
            continue

        if bitfile_mode == "latest":
            data["bitfile"] = "GET_LATEST"

        created.append(manager._to_api(manager.submit(data)))

    if not created:
        raise HTTPException(status_code=400, detail="at least one job needs valid bitfile")

    return {"created": created}


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str) -> dict[str, Any]:
    try:
        job = manager.stop(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    return manager._to_api(job)
