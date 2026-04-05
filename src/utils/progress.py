from __future__ import annotations

from collections.abc import Callable


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
