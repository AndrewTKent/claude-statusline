#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}")
    temp.write_text(json.dumps(value, sort_keys=True) + "\n")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def main() -> None:
    state_path = os.environ.get("CODEX_ACCOUNT_ROUTER_STATE")
    label = os.environ.get("CODEX_ROUTED_LABEL")
    if not state_path or not label:
        return
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    state = {
        "session_id": session_id,
        "transcript_path": str(payload.get("transcript_path") or ""),
        "cwd": str(payload.get("cwd") or ""),
        "model": str(payload.get("model") or ""),
        "label": label,
        "updated_at": time.time(),
    }
    write_json(Path(state_path), state)
    thread_dir = Path(
        os.environ.get(
            "CODEX_ACCOUNTS_THREAD_DIR",
            Path.home() / ".codex-accounts" / "thread-accounts",
        )
    )
    write_json(thread_dir / f"{session_id}.json", state)


if __name__ == "__main__":
    main()
