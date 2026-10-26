from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import Lock

from utilities.atomic_files import atomic_write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = PROJECT_ROOT / "System" / "automation_state.json"
CYCLE_LOCK_PATH = PROJECT_ROOT / "System" / "automation_cycle.lock"

DEFAULT_STATE = {
    "enabled": True,
    "interval_minutes": 5,
    "running": False,
    "last_started": None,
    "last_finished": None,
    "last_result": "never",
    "last_message": "Автоматический цикл ещё не запускался",
    "last_duration_seconds": None,
    "trigger": None,
}

_state_lock = Lock()


def _read_cycle_lock_pid() -> int | None:
    try:
        lock_text = CYCLE_LOCK_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None

    for part in lock_text.split():
        if not part.startswith("pid="):
            continue
        try:
            return int(part.removeprefix("pid="))
        except ValueError:
            return None
    return None


def _process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False

    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.windll.kernel32
        process_handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if process_handle:
            kernel32.CloseHandle(process_handle)
            return True

        # ERROR_ACCESS_DENIED означает, что процесс существует,
        # но текущему пользователю запрещено открыть его дескриптор.
        return kernel32.GetLastError() == 5

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_state_unlocked() -> dict:
    try:
        with STATE_PATH.open("r", encoding="utf-8") as stream:
            stored = json.load(stream)
            if not isinstance(stored, dict):
                raise ValueError("automation state must be an object")
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        stored = {}

    state = dict(DEFAULT_STATE)
    state.update(stored)
    state["enabled"] = bool(state.get("enabled", True))
    try:
        interval_minutes = int(state.get("interval_minutes", 5))
    except (TypeError, ValueError):
        interval_minutes = 5
    state["interval_minutes"] = min(60, max(1, interval_minutes))
    state["running"] = bool(state.get("running", False))
    return state


def load_automation_state() -> dict:
    with _state_lock:
        return _read_state_unlocked()


def update_automation_state(**changes) -> dict:
    with _state_lock:
        state = _read_state_unlocked()
        state.update(changes)
        atomic_write_json(STATE_PATH, state)
        return dict(state)


def set_automation_enabled(enabled: bool) -> dict:
    return update_automation_state(enabled=bool(enabled))


def set_automation_interval(interval_minutes: int) -> dict:
    if isinstance(interval_minutes, bool):
        raise ValueError("Интервал должен быть целым числом от 1 до 60 минут")
    try:
        normalized = int(interval_minutes)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "Интервал должен быть целым числом от 1 до 60 минут"
        ) from error
    if not 1 <= normalized <= 60:
        raise ValueError("Интервал должен быть от 1 до 60 минут")
    return update_automation_state(interval_minutes=normalized)


def reset_interrupted_run() -> dict:
    state = load_automation_state()
    if not state["running"]:
        return state
    return update_automation_state(
        running=False,
        last_result="interrupted",
        last_message="Предыдущий цикл был прерван перезапуском приложения",
        last_finished=now_iso(),
    )


@contextmanager
def acquire_cycle_lock(stale_after_seconds: int = 60 * 60):
    """Межпроцессная блокировка от одновременного запуска Web, Cron и main.py."""

    CYCLE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor = None

    for attempt in range(2):
        try:
            descriptor = os.open(
                CYCLE_LOCK_PATH,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            os.write(
                descriptor,
                f"pid={os.getpid()} started={now_iso()}".encode("utf-8"),
            )
            break
        except FileExistsError:
            try:
                lock_age = time.time() - CYCLE_LOCK_PATH.stat().st_mtime
            except FileNotFoundError:
                continue

            owner_pid = _read_cycle_lock_pid()
            owner_is_gone = (
                owner_pid is not None
                and not _process_is_running(owner_pid)
            )

            if attempt == 0 and (
                owner_is_gone or lock_age > stale_after_seconds
            ):
                try:
                    CYCLE_LOCK_PATH.unlink()
                except FileNotFoundError:
                    pass
                continue

            yield False
            return

    if descriptor is None:
        yield False
        return

    try:
        yield True
    finally:
        os.close(descriptor)
        try:
            CYCLE_LOCK_PATH.unlink()
        except FileNotFoundError:
            pass
