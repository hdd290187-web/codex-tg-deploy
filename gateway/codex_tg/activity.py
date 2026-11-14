"""Cross-process coordination: updates exclude Agent turns and deliveries."""
from __future__ import annotations

import fcntl
from pathlib import Path


def acquire_activity(data: Path, *, exclusive: bool = False):
    data.mkdir(parents=True, exist_ok=True)
    handle = (data / 'activity.lock').open('a')
    try:
        fcntl.flock(handle, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle
