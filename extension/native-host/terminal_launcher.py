#!/usr/bin/env python3
import json
import os
import struct
import subprocess
from pathlib import Path


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
    return {
        'ok': True,
        'returncode': int(completed.returncode),
        'stdout': completed.stdout[-2000:],
        'stderr': completed.stderr[-2000:],
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
    if action == 'list_dir':
        _send_message(_handle_list_dir(payload))
        continue
    _send_message({'ok': False, 'error': 'unsupported action'})
