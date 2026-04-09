#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import hashlib
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


def _safe_log_path(raw: str | None) -> Path:
    text = str(raw or "").strip()
    if text:
        return Path(text).expanduser().resolve()
    return (Path.home() / "haps_local_logs" / "default").resolve()


def _append_log(log_dir: Path, message: str) -> None:
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "native_host.log"
        with log_file.open("a", encoding="utf-8") as fp:
            fp.write(f"{message}\n")
    except Exception:
        # keep native-host resilient, logging failure should not break core flow.
        pass


def _extract_log_dir(payload: dict[str, Any]) -> Path:
    direct_raw = str(payload.get("log_path") or "").strip()
    if direct_raw:
        return _safe_log_path(direct_raw)
    nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    nested_raw = str(nested.get("log_path") or "").strip()
    if nested_raw:
        return _safe_log_path(nested_raw)
    jobs = payload.get("jobs") if isinstance(payload.get("jobs"), list) else []
    if jobs:
        first = jobs[0] if isinstance(jobs[0], dict) else {}
        first_log = str((first or {}).get("log_path") or "").strip()
        if first_log:
            return _safe_log_path(first_log)
    return _safe_log_path(None)


def _extract_job_log_dirs(payload: dict[str, Any]) -> list[Path]:
    jobs = payload.get("jobs") if isinstance(payload.get("jobs"), list) else []
    dirs: list[Path] = []
    seen: set[str] = set()
    for item in jobs:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("log_path") or "").strip()
        if not raw:
            continue
        path = _safe_log_path(raw)
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        dirs.append(path)
    if not dirs:
        dirs.append(_extract_log_dir(payload))
    return dirs


def _prepare_log_dirs_for_payload(payload: dict[str, Any]) -> list[Path]:
    dirs = _extract_job_log_dirs(payload)
    prepared: list[Path] = []
    for log_dir in dirs:
        result = _handle_prepare_log_dir({"log_path": str(log_dir)})
        if result.get("ok"):
            prepared.append(log_dir)
        else:
            prepared.append(log_dir)
    return prepared


def _handle_prepare_log_dir(payload: dict[str, Any]) -> dict[str, Any]:
    log_dir = _extract_log_dir(payload)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        _append_log(log_dir, f"[INFO] prepare_log_dir ok: {log_dir}")
        return {"ok": True, "detail": "", "log_path": str(log_dir)}
    except Exception as exc:
        return {"ok": False, "detail": f"prepare_log_dir failed: {exc}", "log_path": str(log_dir)}


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
    log_dir = _extract_log_dir(payload)
    terminal = _resolve_terminal_binary(payload)
    if not terminal:
        _append_log(log_dir, "[ERROR] open_terminal failed: no executable terminal found")
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
        _append_log(log_dir, f"[INFO] open_terminal ok: terminal={terminal} cwd={cwd}")
        return {'ok': True, 'detail': ''}
    except Exception as exc:  # pragma: no cover
        _append_log(log_dir, f"[ERROR] open_terminal failed: {exc}")
        return {'ok': False, 'detail': f'failed to open terminal: {exc}'}


def _is_child(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def _handle_create_jobs_browse(payload: dict[str, Any]) -> dict[str, Any]:
    log_dir = _extract_log_dir(payload)
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
        _append_log(log_dir, f"[ERROR] create_jobs_browse invalid path: {current}")
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


def _validate_job_paths(job: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    job_id = str(job.get('jobs_id') or '-')

    def _check_path_access(
        key: str,
        *,
        enabled: bool = True,
        required: bool = False,
        must_be_file: bool = False,
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
        if not p.exists():
            errors.append(f'{job_id}: {key} not exists -> {p}')
            return
        if must_be_file and not p.is_file():
            errors.append(f'{job_id}: {key} must be file -> {p}')
            return
        if not os.access(str(p), os.R_OK):
            errors.append(f'{job_id}: {key} not accessible -> {p}')
            return
        if suffixes and p.suffix.lower() not in suffixes:
            errors.append(f'{job_id}: {key} suffix invalid -> {p.suffix}')

    database_enabled = bool(job.get('database_path_enabled'))
    reset_enabled = bool(job.get('reset_script_enabled'))
    imgload_enabled = bool(job.get('imgload_script_enabled'))

    _check_path_access('database_path', enabled=database_enabled, required=database_enabled)
    _check_path_access(
        'reset_script',
        enabled=reset_enabled,
        required=reset_enabled,
        must_be_file=True,
        suffixes=('.tcl',),
    )
    _check_path_access(
        'imgload_script',
        enabled=imgload_enabled,
        required=imgload_enabled,
        must_be_file=True,
        suffixes=('.tcl',),
    )
    _check_path_access(
        'img_file',
        enabled=imgload_enabled,
        required=imgload_enabled,
        must_be_file=True,
        suffixes=('.img', '.bin', '.dat'),
    )
    return errors


def _handle_validate_create_jobs(payload: dict[str, Any]) -> dict[str, Any]:
    log_dirs = _prepare_log_dirs_for_payload(payload)
    jobs = payload.get('jobs') if isinstance(payload.get('jobs'), list) else []
    if not jobs:
        for log_dir in log_dirs:
            _append_log(log_dir, "[ERROR] validate_create_jobs failed: jobs is empty")
        return {'ok': False, 'detail': 'jobs is empty', 'errors': ['jobs is empty']}
    errors: list[str] = []
    for item in jobs:
        if not isinstance(item, dict):
            errors.append('invalid job payload item')
            continue
        errors.extend(_validate_job_paths(item))
    if errors:
        reason = ' | '.join(errors[:10])
        for log_dir in log_dirs:
            _append_log(log_dir, f"[ERROR] validate_create_jobs failed: {reason}")
        return {'ok': False, 'detail': 'validation failed', 'errors': errors[:100]}
    for log_dir in log_dirs:
        _append_log(log_dir, f"[INFO] validate_create_jobs ok: jobs={len(jobs)}")
    return {'ok': True, 'detail': '', 'errors': []}


def _handle_run_cfgprosh(payload: dict[str, Any]) -> dict[str, Any]:
    job_payload = payload.get('payload') if isinstance(payload.get('payload'), dict) else {}
    log_dir = _extract_log_dir(payload)
    cfgprosh = str(job_payload.get('cfgprosh') or payload.get('cfgprosh') or '').strip()
    if not cfgprosh:
        cfgprosh = str(os.environ.get('HAPS_CFGPROSH', '')).strip()
    if not cfgprosh:
        _append_log(log_dir, "[ERROR] run_cfgprosh failed: missing cfgprosh binary")
        return {'ok': False, 'detail': 'missing cfgprosh binary (set HAPS_CFGPROSH)'}
    if not Path(cfgprosh).exists():
        _append_log(log_dir, f"[ERROR] run_cfgprosh failed: cfgprosh not found: {cfgprosh}")
        return {'ok': False, 'detail': f'cfgprosh not found: {cfgprosh}'}

    steps: list[tuple[str, list[str]]] = []
    if bool(job_payload.get('database_path_enabled')):
        db = str(job_payload.get('database_path') or '').strip()
        db_loading_tcl = str(job_payload.get('db_loading_tcl') or payload.get('db_loading_tcl') or os.environ.get('HAPS_DB_LOADING_TCL') or '').strip()
        if db:
            if not db_loading_tcl:
                _append_log(log_dir, "[ERROR] run_cfgprosh failed: missing db_loading_tcl")
                return {'ok': False, 'detail': 'missing db_loading_tcl'}
            steps.append(('load_db', [cfgprosh, db_loading_tcl, db]))
    if bool(job_payload.get('imgload_script_enabled')):
        img = str(job_payload.get('img_file') or '').strip()
        imgload_script = str(job_payload.get('imgload_script') or payload.get('imgload_script') or '').strip()
        if img:
            if not imgload_script:
                _append_log(log_dir, "[ERROR] run_cfgprosh failed: missing imgload_script")
                return {'ok': False, 'detail': 'missing imgload_script'}
            steps.append(('load_img', [cfgprosh, imgload_script, img]))
    if bool(job_payload.get('reset_script_enabled')):
        reset_script = str(job_payload.get('reset_script') or '').strip()
        if reset_script:
            steps.append(('reset_env', [cfgprosh, reset_script]))

    if not steps:
        _append_log(log_dir, "[ERROR] run_cfgprosh failed: no executable step")
        return {'ok': False, 'detail': 'no executable cfgprosh step from payload'}

    # SW_IMG_CHK moved from backend to native-host
    if bool(job_payload.get('imgload_script_enabled')):
        img_file = str(job_payload.get('img_file') or '').strip()
        if img_file and Path(img_file).exists():
            digest = hashlib.sha256(Path(img_file).read_bytes()).hexdigest()
            state_file = (Path.home() / '.haps_local_bridge' / 'sw_img_signatures.json').resolve()
            state_file.parent.mkdir(parents=True, exist_ok=True)
            if state_file.exists():
                known = set(json.loads(state_file.read_text(encoding='utf-8')))
            else:
                known = set()
            duplicate = digest in known
            if not duplicate:
                known.add(digest)
                state_file.write_text(json.dumps(sorted(known), ensure_ascii=False), encoding='utf-8')
            status = 'file is remain unchanged' if duplicate else 'file is new'
            _append_log(log_dir, f"[INFO] SW_IMG_CHK algo=sha256 signature={digest} {status}: {img_file}")

    merged_output: list[str] = []
    for stage, cmd in steps:
        rc, out = _run_cmd(cmd)
        merged_output.append(f'[{stage}] rc={rc}\n{out}')
        _append_log(log_dir, f"[INFO] run_cfgprosh stage={stage} rc={rc}")
        if rc != 0:
            _append_log(log_dir, f"[ERROR] run_cfgprosh failed on stage={stage}: {out[-1000:]}")
            return {
                'ok': False,
                'detail': f'cfgprosh step failed: {stage}',
                'output': '\n'.join(merged_output)[-4000:],
            }

    _append_log(log_dir, f"[INFO] run_cfgprosh ok: steps={len(steps)}")
    return {'ok': True, 'detail': '', 'output': '\n'.join(merged_output)[-4000:]}


def _dispatch(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action == 'ping':
        return {'ok': True, 'detail': ''}
    if action == 'native_open_terminal':
        return _handle_open_terminal(payload)
    if action == 'native_prepare_log_dir':
        return _handle_prepare_log_dir(payload)
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
