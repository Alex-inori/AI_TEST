#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _read_message() -> dict[str, Any] | None:
    raw_length = sys.stdin.buffer.read(4)
    if not raw_length:
        return None
    msg_len = int.from_bytes(raw_length, byteorder="little")
    payload = sys.stdin.buffer.read(msg_len)
    if not payload:
        return None
    return json.loads(payload.decode("utf-8"))


def _write_message(message: dict[str, Any]) -> None:
    encoded = json.dumps(message).encode("utf-8")
    sys.stdout.buffer.write(len(encoded).to_bytes(4, byteorder="little"))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _assert_localhost(url: str) -> None:
    allowed = ("http://127.0.0.1", "http://localhost")
    if not any(str(url or "").startswith(prefix) for prefix in allowed):
        raise ValueError(f"unsupported origin: {url}")


def _validate_path(path_text: str, path_type: str, must_exist: bool) -> dict[str, Any]:
    path = Path(path_text).expanduser()
    exists = path.exists()
    if must_exist and not exists:
        raise ValueError(f"path not found: {path}")
    if exists and path_type == "file" and not path.is_file():
        raise ValueError(f"path is not file: {path}")
    if exists and path_type == "directory" and not path.is_dir():
        raise ValueError(f"path is not directory: {path}")
    return {"path": str(path), "exists": exists}


def _ensure_directory(path_text: str, mode_text: str = "0777") -> dict[str, Any]:
    path = Path(path_text).expanduser()
    mode = int(str(mode_text or "0777"), 8)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)
    return {"path": str(path), "mode": oct(stat.S_IMODE(path.stat().st_mode))}


def _append_file(path_text: str, content: str) -> dict[str, Any]:
    path = Path(path_text).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(content)
    return {"path": str(path), "bytes": len(content.encode("utf-8"))}


def _open_terminal(terminal_path: str, cwd: str) -> dict[str, Any]:
    executable = Path(terminal_path).expanduser()
    if not executable.exists() or not executable.is_file():
        raise ValueError(f"terminal not found: {executable}")
    if not os.access(str(executable), os.X_OK):
        raise ValueError(f"terminal not executable: {executable}")
    launch_cwd = Path(cwd).expanduser() if cwd else Path.home()
    if not launch_cwd.exists() or not launch_cwd.is_dir():
        launch_cwd = Path.home()
    subprocess.Popen(
        [str(executable)],
        cwd=str(launch_cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"terminal": str(executable), "cwd": str(launch_cwd)}


def _run_stage(stage: str, payload: dict[str, Any]) -> dict[str, Any]:
    # Keep this deterministic/demo-friendly. Extend as needed.
    if not re.match(r"^Running::[A-Za-z0-9_ ]+$", stage):
        raise ValueError(f"invalid stage: {stage}")
    time.sleep(0.2)
    return {"stage": stage, "jobs_id": payload.get("jobs_id", "")}


def _list_fs(path_text: str, mode: str = "file") -> dict[str, Any]:
    target = Path(path_text).expanduser() if str(path_text or "").strip() else Path.home()
    resolved = target.resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(f"path is not a directory: {resolved}")
    entries: list[dict[str, str]] = []
    for entry in sorted(resolved.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if entry.is_dir():
            entries.append({"name": entry.name, "path": str(entry), "type": "directory"})
        elif mode == "file" and entry.is_file():
            entries.append({"name": entry.name, "path": str(entry), "type": "file"})
        if len(entries) >= 200:
            break
    parent = str(resolved.parent) if resolved.parent != resolved else ""
    return {"cwd": str(resolved), "parent": parent, "mode": mode, "entries": entries}


def _handle(message: dict[str, Any]) -> dict[str, Any]:
    action = str(message.get("action") or "")
    payload = message.get("payload") or {}
    url = str(message.get("url") or "")
    _assert_localhost(url)

    if action == "validatePath":
        return _validate_path(
            path_text=str(payload.get("path") or ""),
            path_type=str(payload.get("type") or ""),
            must_exist=bool(payload.get("mustExist", False)),
        )
    if action == "ensureDirectory":
        return _ensure_directory(str(payload.get("path") or ""), str(payload.get("mode") or "0777"))
    if action == "appendFile":
        return _append_file(str(payload.get("path") or ""), str(payload.get("content") or ""))
    if action == "openTerminal":
        return _open_terminal(str(payload.get("terminalPath") or ""), str(payload.get("cwd") or ""))
    if action == "runStage":
        return _run_stage(str(payload.get("stage") or ""), payload.get("payload") or {})
    if action == "listFs":
        return _list_fs(str(payload.get("path") or ""), str(payload.get("mode") or "file"))

    raise ValueError(f"unsupported action: {action}")


def main() -> int:
    while True:
        message = _read_message()
        if message is None:
            return 0
        try:
            data = _handle(message)
            _write_message({"ok": True, "data": data})
        except Exception as exc:  # noqa: BLE001
            _write_message({"ok": False, "error": str(exc)})


if __name__ == "__main__":
    raise SystemExit(main())
