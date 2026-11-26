"""Single-process formal asset locks. Not a multi-process locking protocol."""

import threading
from pathlib import Path

_guard = threading.Lock()
_locks: dict[tuple[str, str], threading.RLock] = {}


def case_lock(root: Path, case_id: str) -> threading.RLock:
    key = (str(root.resolve()).casefold(), case_id)
    with _guard:
        return _locks.setdefault(key, threading.RLock())
