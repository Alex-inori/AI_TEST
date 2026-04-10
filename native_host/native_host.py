#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any


def _read_message() -> dict[str, Any] | None:
    raw_length = sys.stdin.buffer.read(4)
    if not raw_length:
        return None
    if len(raw_length) != 4:
        raise RuntimeError("invalid native message length")
    message_length = struct.unpack("<I", raw_length)[0]
    message = sys.stdin.buffer.read(message_length)
    if len(message) != message_length:
        raise RuntimeError("incomplete native message")
    return json.loads(message.decode("utf-8"))


def _write_message(payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _resolve_path(path_text: str) -> Path:
    path = Path(str(path_text or "").strip()).expanduser()
    return path.resolve()


def _validate_path(path_text: str, path_type: str = "file") -> dict[str, Any]:
    target = _resolve_path(path_text)
    if not target.exists():
        raise ValueError(f"path not found: {target}")
    if path_type == "directory" and not target.is_dir():
        raise ValueError(f"path must be directory: {target}")
    if path_type == "file" and not target.is_file():
        raise ValueError(f"path must be file: {target}")
    return {"ok": True, "path": str(target)}


def _ensure_log_dir(path_text: str, writable: bool = True) -> dict[str, Any]:
    target = _resolve_path(path_text)
    target.mkdir(parents=True, exist_ok=True)
    if writable and not os.access(target, os.W_OK):
        raise ValueError(f"directory is not writable: {target}")
    return {"ok": True, "path": str(target)}


def _list_directory(path_text: str, mode: str = "file") -> dict[str, Any]:
    target = _resolve_path(path_text) if str(path_text or "").strip() else Path.home().resolve()
    if not target.is_dir():
        raise ValueError(f"path is not directory: {target}")
    entries: list[dict[str, str]] = []
    for entry in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if entry.is_dir():
            entries.append({"name": entry.name, "path": str(entry), "type": "directory"})
        elif mode == "file" and entry.is_file():
            entries.append({"name": entry.name, "path": str(entry), "type": "file"})
        if len(entries) >= 200:
            break
    parent = str(target.parent) if target.parent != target else ""
    return {
        "cwd": str(target),
        "parent": parent,
        "mode": mode,
        "entries": entries,
    }


def _open_terminal(terminal_path: str, cwd: str) -> dict[str, Any]:
    terminal = _resolve_path(terminal_path)
    if not terminal.exists():
        raise ValueError(f"terminal path not found: {terminal}")
    if not os.access(terminal, os.X_OK):
        raise ValueError(f"terminal path is not executable: {terminal}")
    launch_cwd = _resolve_path(cwd) if str(cwd or "").strip() else Path.home().resolve()
    if not launch_cwd.is_dir():
        launch_cwd = Path.home().resolve()
    subprocess.Popen(
        [str(terminal)],
        cwd=str(launch_cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"ok": True}


def _run_stage(stage: str, job: dict[str, Any], defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    data = dict(job or {})
    cfg_defaults = dict(defaults or {})
    confprosh = str(cfg_defaults.get("haps_confprosh") or "").strip()
    if not confprosh:
        raise ValueError("missing defaults.haps_confprosh")

    confprosh_path = _resolve_path(confprosh)
    if not os.access(confprosh_path, os.X_OK):
        raise ValueError(f"HAPS_CONFPROSH not executable: {confprosh_path}")

    command: list[str] = [str(confprosh_path)]
    normalized_stage = str(stage or "").strip()
    if normalized_stage == "load_db":
        db_script = str(cfg_defaults.get("haps_db_loading_tcl") or "").strip()
        if not db_script:
            raise ValueError("missing defaults.haps_db_loading_tcl")
        db_path = str(data.get("database_path") or "").strip()
        if not db_path:
            raise ValueError("database_path is required for load_db")
        command.extend([db_script, db_path])
        if "HAPS100" in str(data.get("haps_platform") or ""):
            hmf = str(data.get("haps_hmf_txt") or cfg_defaults.get("haps_hmf_txt") or "").strip()
            if hmf:
                command.append(hmf)
    elif normalized_stage == "load_img":
        img_script = str(data.get("imgload_script") or cfg_defaults.get("haps_img_loading_tcl") or "").strip()
        img_file = str(data.get("img_file") or "").strip()
        if not img_script or not img_file:
            raise ValueError("imgload_script and img_file are required for load_img")
        command.extend([img_script, img_file])
    elif normalized_stage == "reset":
        reset_script = str(data.get("reset_script") or cfg_defaults.get("haps_reset_tcl") or "").strip()
        if not reset_script:
            raise ValueError("reset_script is required for reset")
        command.append(reset_script)
    else:
        raise ValueError(f"unsupported stage: {normalized_stage}")

    run = subprocess.run(command, text=True, capture_output=True)
    ok = run.returncode == 0
    return {
        "ok": ok,
        "message": run.stdout[-1000:] if ok else (run.stderr[-1000:] or run.stdout[-1000:]),
        "exit_code": run.returncode,
    }


def _handle(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "").strip()
    if action == "validate_path":
        return _validate_path(str(payload.get("path") or ""), str(payload.get("type") or "file"))
    if action == "ensure_log_dir":
        return _ensure_log_dir(str(payload.get("path") or ""), bool(payload.get("writable", True)))
    if action == "list_directory":
        return _list_directory(str(payload.get("path") or ""), str(payload.get("mode") or "file"))
    if action == "open_terminal":
        return _open_terminal(str(payload.get("terminalPath") or ""), str(payload.get("cwd") or ""))
    if action == "run_stage":
        return _run_stage(str(payload.get("stage") or ""), dict(payload.get("job") or {}), dict(payload.get("defaults") or {}))
    raise ValueError(f"unsupported action: {action}")


def main() -> int:
    while True:
        try:
            request = _read_message()
            if request is None:
                return 0
            response = _handle(request)
            _write_message(response)
        except Exception as exc:  # noqa: BLE001
            _write_message({"ok": False, "error": str(exc)})


if __name__ == "__main__":
    raise SystemExit(main())
