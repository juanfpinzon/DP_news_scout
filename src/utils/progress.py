from __future__ import annotations

from collections.abc import Callable
from datetime import datetime


def build_stdout_progress_callback() -> Callable[[str], None]:
    """Return a simple timestamped stdout progress reporter."""
    def _emit_console_progress(message: str) -> None:
        timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
        print(f"[{timestamp}] {message}", flush=True)

    return _emit_console_progress


def emit_progress(
    progress_callback: Callable[[str], None] | None,
    message: str,
) -> None:
    """Best-effort progress reporting that never interrupts pipeline work."""
    if progress_callback is None:
        return

    try:
        progress_callback(message)
    except Exception:
        return
