#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any


def _read_native_message() -> dict[str, Any] | None:
    raw_len = sys.stdin.buffer.read(4)
    if not raw_len:
        return None
    if len(raw_len) != 4:
        return None
    msg_len = struct.unpack('<I', raw_len)[0]
    data = sys.stdin.buffer.read(msg_len)
    if len(data) != msg_len:
        return None
    try:
        return json.loads(data.decode('utf-8'))
    except json.JSONDecodeError:
        return None


def _write_native_message(payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    sys.stdout.buffer.write(struct.pack('<I', len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _resolve_terminal_binary(payload: dict[str, Any]) -> str | None:
    candidates = [
        str(payload.get('terminal') or '').strip(),
        os.environ.get('HAPS_TERMINAL', '').strip(),
        '/usr/bin/x-terminal-emulator',
        '/usr/bin/gnome-terminal',
        '/usr/bin/konsole',
        '/usr/bin/xfce4-terminal',
    ]
    for item in candidates:
        if not item:
            continue
        path = Path(item)
        if path.exists() and os.access(str(path), os.X_OK):
            return str(path)
    return None


def _handle_open_terminal(payload: dict[str, Any]) -> dict[str, Any]:
    terminal = _resolve_terminal_binary(payload)
    if not terminal:
        return {'ok': False, 'detail': 'no executable terminal found'}

    cwd = str(payload.get('cwd') or '').strip()
    if not cwd:
        cwd = str(Path.home())
    if not Path(cwd).exists():
        cwd = str(Path.home())

    try:
        subprocess.Popen(  # noqa: S603
            [terminal],  # noqa: S607
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {'ok': True, 'detail': ''}
    except Exception as exc:  # pragma: no cover
        return {'ok': False, 'detail': f'failed to open terminal: {exc}'}


def _is_child(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def _handle_create_jobs_browse(payload: dict[str, Any]) -> dict[str, Any]:
    requested = str(payload.get('path') or '').strip()
    mode = str(payload.get('mode') or 'file').strip().lower()
    allowed_root = Path(os.environ.get('HAPS_BROWSE_ROOT', str(Path.home()))).resolve()
    current = Path(requested or allowed_root).expanduser().resolve()

    if not _is_child(allowed_root, current):
        current = allowed_root

    if current.exists() and current.is_file():
        if mode == 'directory':
            current = current.parent
        else:
            return {
                'ok': True,
                'cwd': str(current.parent),
                'parent': str(current.parent.parent) if current.parent != current.parent.parent else None,
                'entries': [
                    {'name': current.name, 'path': str(current), 'type': 'file'},
                ],
            }

    if not current.exists() or not current.is_dir():
        return {'ok': False, 'detail': f'invalid path: {current}'}

    entries: list[dict[str, str]] = []
    for child in sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if not _is_child(allowed_root, child):
            continue
        entry_type = 'directory' if child.is_dir() else 'file'
        if mode == 'directory' and entry_type != 'directory':
            continue
        entries.append({'name': child.name, 'path': str(child), 'type': entry_type})

    parent = current.parent if current != allowed_root else None
    return {
        'ok': True,
        'cwd': str(current),
        'parent': str(parent) if parent else None,
        'entries': entries,
    }


def _run_cmd(cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)  # noqa: S603,S607
    output = (proc.stdout or '') + (proc.stderr or '')
    return proc.returncode, output[-4000:]


def _validate_job_paths(job: dict[str, Any], allowed_root: Path) -> list[str]:
    errors: list[str] = []
    job_id = str(job.get('jobs_id') or '-')

    def _check_path(
        key: str,
        *,
        enabled: bool = True,
        required: bool = False,
        suffixes: tuple[str, ...] | None = None,
    ) -> None:
        if not enabled:
            return
        raw = str(job.get(key) or '').strip()
        if required and not raw:
            errors.append(f'{job_id}: {key} is required')
            return
        if not raw:
            return
        p = Path(raw).expanduser().resolve()
        if not _is_child(allowed_root, p):
            errors.append(f'{job_id}: {key} is out of allowed root')
            return
        if not p.exists():
            errors.append(f'{job_id}: {key} not exists -> {p}')
            return
        if suffixes and p.is_file() and p.suffix.lower() not in suffixes:
            errors.append(f'{job_id}: {key} suffix invalid -> {p.suffix}')

    database_enabled = bool(job.get('database_path_enabled'))
    reset_enabled = bool(job.get('reset_script_enabled'))
    imgload_enabled = bool(job.get('imgload_script_enabled'))

    _check_path('binfile', enabled=True, required=False, suffixes=('.bin', '.img'))
    _check_path('database_path', enabled=database_enabled, required=database_enabled)
    _check_path('reset_script', enabled=reset_enabled, required=reset_enabled, suffixes=('.tcl',))
    _check_path('imgload_script', enabled=imgload_enabled, required=imgload_enabled, suffixes=('.tcl',))
    _check_path('img_file', enabled=imgload_enabled, required=imgload_enabled, suffixes=('.img', '.bin'))
    return errors


def _handle_validate_create_jobs(payload: dict[str, Any]) -> dict[str, Any]:
    jobs = payload.get('jobs') if isinstance(payload.get('jobs'), list) else []
    if not jobs:
        return {'ok': False, 'detail': 'jobs is empty', 'errors': ['jobs is empty']}
    allowed_root = Path(os.environ.get('HAPS_BROWSE_ROOT', str(Path.home()))).resolve()
    errors: list[str] = []
    for item in jobs:
        if not isinstance(item, dict):
            errors.append('invalid job payload item')
            continue
        errors.extend(_validate_job_paths(item, allowed_root))
    if errors:
        return {'ok': False, 'detail': 'validation failed', 'errors': errors[:100]}
    return {'ok': True, 'detail': '', 'errors': []}


def _handle_run_cfgprosh(payload: dict[str, Any]) -> dict[str, Any]:
    job_payload = payload.get('payload') if isinstance(payload.get('payload'), dict) else {}
    cfgprosh = str(os.environ.get('HAPS_CFGPROSH', '')).strip()
    if not cfgprosh:
        cfgprosh = str(job_payload.get('cfgprosh') or '').strip()
    if not cfgprosh:
        return {'ok': False, 'detail': 'missing cfgprosh binary (set HAPS_CFGPROSH)'}
    if not Path(cfgprosh).exists():
        return {'ok': False, 'detail': f'cfgprosh not found: {cfgprosh}'}

    steps: list[tuple[str, list[str]]] = []
    if bool(job_payload.get('database_path_enabled')):
        db = str(job_payload.get('database_path') or '').strip()
        if db:
            steps.append(('load_db', [cfgprosh, db]))
    if bool(job_payload.get('imgload_script_enabled')):
        img = str(job_payload.get('img_file') or '').strip()
        if img:
            steps.append(('load_img', [cfgprosh, img]))
    if bool(job_payload.get('reset_script_enabled')):
        reset_script = str(job_payload.get('reset_script') or '').strip()
        if reset_script:
            steps.append(('reset_env', [cfgprosh, reset_script]))

    if not steps:
        return {'ok': False, 'detail': 'no executable cfgprosh step from payload'}

    merged_output: list[str] = []
    for stage, cmd in steps:
        rc, out = _run_cmd(cmd)
        merged_output.append(f'[{stage}] rc={rc}\n{out}')
        if rc != 0:
            return {
                'ok': False,
                'detail': f'cfgprosh step failed: {stage}',
                'output': '\n'.join(merged_output)[-4000:],
            }

    return {'ok': True, 'detail': '', 'output': '\n'.join(merged_output)[-4000:]}


def _dispatch(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action == 'ping':
        return {'ok': True, 'detail': ''}
    if action == 'native_open_terminal':
        return _handle_open_terminal(payload)
    if action == 'native_create_jobs_browse':
        return _handle_create_jobs_browse(payload)
    if action == 'native_run_cfgprosh':
        return _handle_run_cfgprosh(payload)
    if action == 'native_validate_create_jobs':
        return _handle_validate_create_jobs(payload)
    return {'ok': False, 'detail': f'unsupported action: {action}'}


def main() -> int:
    while True:
        request = _read_native_message()
        if request is None:
            return 0
        action = str(request.get('action') or '').strip()
        payload = request.get('payload') if isinstance(request.get('payload'), dict) else {}
        response = _dispatch(action, payload)
        _write_native_message(response)


if __name__ == '__main__':
    raise SystemExit(main())
