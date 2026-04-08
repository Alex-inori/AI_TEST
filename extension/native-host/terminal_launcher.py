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
    _send_message({'ok': False, 'error': 'unsupported action'})
