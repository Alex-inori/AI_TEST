#!/usr/bin/env python3
import json
import os
import struct
import subprocess
import re
import select
import time
import pty
import zlib
import uuid
from pathlib import Path

_IMG_SIGNATURES: set[str] = set()
_HAPS_LOCK_SESSIONS: dict[str, dict] = {}


def _read_message() -> dict:
    raw_len = os.read(0, 4)
    if len(raw_len) < 4:
        raise EOFError
    msg_len = struct.unpack('<I', raw_len)[0]
    body = os.read(0, msg_len)
    return json.loads(body.decode('utf-8'))


def _send_message(payload: dict) -> None:
    encoded = json.dumps(payload).encode('utf-8')
    os.write(1, struct.pack('<I', len(encoded)))
    os.write(1, encoded)


def _handle_launch(payload: dict) -> dict:
    terminal_path = str((payload or {}).get('terminal_path') or '').strip()
    cwd = str((payload or {}).get('cwd') or '').strip()

    if not terminal_path:
        return {'ok': False, 'error': 'terminal_path is empty'}
    if not os.path.exists(terminal_path):
        return {'ok': False, 'error': f'terminal_path not found: {terminal_path}'}
    if not os.access(terminal_path, os.X_OK):
        return {'ok': False, 'error': f'terminal_path not executable: {terminal_path}'}

    launch_cwd = cwd if cwd and Path(cwd).exists() else str(Path.home())

    try:
        subprocess.Popen(
            [terminal_path],
            cwd=launch_cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        return {'ok': False, 'error': f'failed to launch terminal: {exc}'}

    return {'ok': True}


def _handle_launch_cfgshell(payload: dict) -> dict:
    cmd = list((payload or {}).get('cmd') or [])
    cwd = str((payload or {}).get('cwd') or '').strip()
    if not cmd:
        return {'ok': False, 'error': 'cmd is empty'}
    if not isinstance(cmd, list) or not all(isinstance(item, str) and item.strip() for item in cmd):
        return {'ok': False, 'error': 'cmd must be a non-empty string list'}

    launch_cwd = cwd if cwd and Path(cwd).exists() else str(Path.home())
    try:
        subprocess.Popen(
            cmd,
            cwd=launch_cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        return {'ok': False, 'error': f'failed to launch cfgshell: {exc}'}
    return {'ok': True}


def _handle_run_cfgshell_sync(payload: dict) -> dict:
    cmd = list((payload or {}).get('cmd') or [])
    cwd = str((payload or {}).get('cwd') or '').strip()
    log_file = str((payload or {}).get('log_file') or '').strip()
    meta = dict((payload or {}).get('meta') or {})
    if not cmd:
        return {'ok': False, 'error': 'cmd is empty'}
    if not isinstance(cmd, list) or not all(isinstance(item, str) and item.strip() for item in cmd):
        return {'ok': False, 'error': 'cmd must be a non-empty string list'}
    launch_cwd = cwd if cwd and Path(cwd).exists() else str(Path.home())
    try:
        completed = subprocess.run(
            cmd,
            cwd=launch_cwd,
            text=True,
            capture_output=True,
        )
    except Exception as exc:
        return {'ok': False, 'error': f'failed to run cfgshell sync: {exc}'}
    if log_file:
        try:
            log_path_obj = Path(log_file).expanduser()
            log_path_obj.parent.mkdir(parents=True, exist_ok=True)
            with log_path_obj.open('a', encoding='utf-8') as handle:
                handle.write(f"[NATIVE_HOST] cmd={' '.join(cmd)} rc={completed.returncode}\n")
                if completed.stdout:
                    handle.write(completed.stdout[-4000:])
                    if not completed.stdout.endswith('\n'):
                        handle.write('\n')
                if completed.stderr:
                    handle.write(completed.stderr[-4000:])
                    if not completed.stderr.endswith('\n'):
                        handle.write('\n')
        except Exception:
            pass

    sw_img_check = ''
    sw_img_file = str(meta.get('sw_img_check_file') or '').strip()
    if sw_img_file:
        try:
            file_path = Path(sw_img_file).expanduser()
            file_size = file_path.stat().st_size
            sample_bytes = 4 * 1024 * 1024
            with file_path.open('rb') as handle:
                head = handle.read(sample_bytes)
                tail = b''
                if file_size > sample_bytes:
                    handle.seek(max(0, file_size - sample_bytes))
                    tail = handle.read(sample_bytes)
            head_crc = zlib.crc32(head) & 0xFFFFFFFF
            tail_crc = zlib.crc32(tail) & 0xFFFFFFFF
            signature = f"{file_size:016x}-{head_crc:08x}-{tail_crc:08x}"
            duplicate = signature in _IMG_SIGNATURES
            if not duplicate:
                _IMG_SIGNATURES.add(signature)
            algo = "size+crc32(head4MB,tail4MB)"
            status = "file is remain unchanged" if duplicate else "file is new"
            sw_img_check = f"algo={algo} signature={signature} {status}: {file_path}"
        except Exception as exc:
            sw_img_check = f"failed: {exc}"

    return {
        'ok': True,
        'returncode': int(completed.returncode),
        'stdout': completed.stdout[-2000:],
        'stderr': completed.stderr[-2000:],
        'sw_img_check': sw_img_check,
    }


def _handle_validate_job_payload(payload: dict) -> dict:
    job = dict((payload or {}).get('payload') or {})
    db_enabled = bool(job.get('database_path_enabled', False))
    database_path = str(job.get('database_path') or '').strip()
    if db_enabled:
        if not database_path:
            return {'ok': False, 'error': 'database_path is enabled but empty'}
        db_path = Path(database_path).expanduser()
        if not db_path.exists():
            return {'ok': False, 'error': f'database_path not found: {db_path}'}

    reset_enabled = bool(job.get('reset_script_enabled', False))
    reset_script = str(job.get('reset_script') or '').strip()
    if reset_enabled:
        reset_path = Path(reset_script).expanduser()
        if not reset_script:
            return {'ok': False, 'error': 'reset_script is enabled but empty'}
        if not reset_path.exists() or not reset_path.is_file() or reset_path.suffix.lower() != '.tcl':
            return {'ok': False, 'error': f'reset_script invalid: {reset_path}'}

    img_enabled = bool(job.get('imgload_script_enabled', False))
    img_script = str(job.get('imgload_script') or '').strip()
    img_file = str(job.get('img_file') or '').strip()
    if img_enabled:
        script_path = Path(img_script).expanduser()
        img_path = Path(img_file).expanduser()
        if not img_script or not script_path.exists() or not script_path.is_file() or script_path.suffix.lower() != '.tcl':
            return {'ok': False, 'error': f'imgload_script invalid: {script_path}'}
        if not img_file or not img_path.exists() or not img_path.is_file() or img_path.suffix.lower() not in {'.img', '.bin'}:
            return {'ok': False, 'error': f'img_file invalid: {img_path}'}
    return {'ok': True}


def _handle_append_log(payload: dict) -> dict:
    log_file = str((payload or {}).get('log_file') or '').strip()
    line = str((payload or {}).get('line') or '').rstrip('\n')
    if not log_file:
        return {'ok': False, 'error': 'log_file is empty'}
    try:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as handle:
            handle.write(f"{line}\n")
    except Exception as exc:
        return {'ok': False, 'error': f'append_log failed: {exc}'}
    return {'ok': True}


def _cfgshell_eval(proc, io_fd: int, command: str, timeout_seconds: float = 20) -> str:
    for _ in range(8):
        readable, _, _ = select.select([io_fd], [], [], 0.05)
        if not readable:
            break
        drained = os.read(io_fd, 4096)
        if not drained:
            break
    os.write(io_fd, f"{command}\n".encode("utf-8"))
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    raw_chunks = []
    saw_output = False
    last_output = time.monotonic()
    while time.monotonic() < deadline:
        if proc.poll() is not None and not saw_output:
            raise RuntimeError("cfgshell exited unexpectedly")
        readable, _, _ = select.select([io_fd], [], [], 0.2)
        if not readable:
            if saw_output and (time.monotonic() - last_output) >= 0.5:
                break
            continue
        chunk = os.read(io_fd, 4096)
        if not chunk:
            continue
        raw_chunks.append(chunk.decode("utf-8", errors="replace"))
        saw_output = True
        last_output = time.monotonic()
    if not saw_output:
        raise TimeoutError(f"cfgshell command timeout: {command}")
    lines = [line.strip("\r") for line in "".join(raw_chunks).strip().splitlines()]
    filtered = [line for line in lines if line.strip() and line.strip() != command.strip()]
    return "\n".join(filtered).strip() if filtered else "".join(raw_chunks).strip()


def _extract_available_device(cfg_scan_output: str, haps_platform: str) -> str | None:
    normalized = " ".join(str(cfg_scan_output or "").split())
    preferred_family = ""
    match = re.search(r"(HAPS\d+)", str(haps_platform or "").upper())
    if match:
        preferred_family = match.group(1)
    fallback = None
    for matched in re.finditer(r"DEVICE\s+(\S+).*?TYPE\s+(\S+).*?STATE\s+(\S+)", normalized):
        device_id, device_type, state = matched.groups()
        if not state.startswith("available"):
            continue
        if fallback is None:
            fallback = device_id
        if preferred_family and preferred_family in device_type.upper():
            return device_id
    return fallback


def _extract_cfg_handle(cfg_open_output: str) -> str | None:
    match = re.search(r"\b(cfg\d+)\b", str(cfg_open_output or ""))
    return match.group(1) if match else None


def _handle_acquire_haps_lock(payload: dict) -> dict:
    cmd = list((payload or {}).get("cmd") or [])
    platform = str((payload or {}).get("haps_platform") or "")
    if not cmd:
        return {"ok": False, "error": "cmd is empty"}
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(cmd, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd)
    os.close(slave_fd)
    try:
        scan = _cfgshell_eval(proc, master_fd, "cfg_scan")
        device_id = _extract_available_device(scan, platform)
        if not device_id:
            raise RuntimeError("no available device from cfg_scan")
        open_out = _cfgshell_eval(proc, master_fd, f"cfg_open {device_id}")
        handle = _extract_cfg_handle(open_out)
        if not handle:
            raise RuntimeError(f"cfg_open failed: {open_out}")
        session_id = str(uuid.uuid4())
        _HAPS_LOCK_SESSIONS[session_id] = {
            "proc": proc,
            "fd": master_fd,
            "handle": handle,
            "device_id": device_id,
        }
        return {"ok": True, "session_id": session_id, "device_id": device_id, "handle": handle}
    except Exception as exc:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            os.close(master_fd)
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}


def _handle_release_haps_lock(payload: dict) -> dict:
    session_id = str((payload or {}).get("session_id") or "").strip()
    session = _HAPS_LOCK_SESSIONS.pop(session_id, None)
    if not session:
        return {"ok": True}
    proc = session.get("proc")
    fd = int(session.get("fd"))
    handle = str(session.get("handle") or "")
    try:
        if handle:
            _cfgshell_eval(proc, fd, f"cfg_close {handle}", timeout_seconds=10)
    except Exception:
        pass
    try:
        os.close(fd)
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        pass
    return {"ok": True}


def _handle_list_dir(payload: dict) -> dict:
    mode = str((payload or {}).get('mode') or 'file').strip() or 'file'
    raw_path = str((payload or {}).get('path') or '').strip()
    target = Path(raw_path).expanduser() if raw_path else Path.home()

    try:
        resolved = target.resolve()
    except OSError:
        return {'ok': False, 'error': 'invalid path'}
    if not resolved.exists() or not resolved.is_dir():
        return {'ok': False, 'error': 'path is not a directory'}

    def _collect_entries(base: Path) -> list[dict]:
        collected = []
        for entry in sorted(base.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            if entry.is_dir():
                collected.append({'name': entry.name, 'path': str(entry), 'type': 'directory'})
            elif mode == 'file' and entry.is_file():
                collected.append({'name': entry.name, 'path': str(entry), 'type': 'file'})
            if len(collected) >= 200:
                break
        return collected

    fallback_from = ''
    try:
        entries = _collect_entries(resolved)
    except PermissionError:
        fallback_target = resolved.parent
        while fallback_target != fallback_target.parent:
            try:
                entries = _collect_entries(fallback_target)
                fallback_from = str(resolved)
                resolved = fallback_target
                break
            except PermissionError:
                fallback_target = fallback_target.parent
        else:
            for candidate in (Path.home(), Path('/')):
                try:
                    entries = _collect_entries(candidate)
                    fallback_from = str(resolved)
                    resolved = candidate.resolve()
                    break
                except Exception:
                    continue
            else:
                entries = []

    parent = str(resolved.parent) if resolved.parent != resolved else ''
    return {
        'ok': True,
        'data': {
            'cwd': str(resolved),
            'parent': parent,
            'mode': mode,
            'entries': entries,
            'fallback_from': fallback_from,
        },
    }


while True:
    try:
        req = _read_message()
    except EOFError:
        break
    except Exception as exc:
        _send_message({'ok': False, 'error': f'invalid request: {exc}'})
        continue

    action = req.get('action')
    payload = req.get('payload') or {}
    if action == 'launch_terminal':
        _send_message(_handle_launch(payload))
        continue
    if action == 'launch_cfgshell':
        _send_message(_handle_launch_cfgshell(payload))
        continue
    if action == 'run_cfgshell_sync':
        _send_message(_handle_run_cfgshell_sync(payload))
        continue
    if action == 'validate_job_payload':
        _send_message(_handle_validate_job_payload(payload))
        continue
    if action == 'append_log':
        _send_message(_handle_append_log(payload))
        continue
    if action == 'acquire_haps_lock':
        _send_message(_handle_acquire_haps_lock(payload))
        continue
    if action == 'release_haps_lock':
        _send_message(_handle_release_haps_lock(payload))
        continue
    if action == 'list_dir':
        _send_message(_handle_list_dir(payload))
        continue
    _send_message({'ok': False, 'error': 'unsupported action'})
