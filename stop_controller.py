from __future__ import annotations

import threading
from pathlib import Path
import yaml

_STOP_EVENT = threading.Event()
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"


def request_stop() -> None:
    """Signals all running threads and background pipelines to stop immediately."""
    _STOP_EVENT.set()


def clear_stop() -> None:
    """Resets the cancellation signal for a fresh run."""
    _STOP_EVENT.clear()


def is_stop_requested(config: dict | None = None) -> bool:
    """
    Checks if a cancellation was triggered via the dashboard Kill Switch
    or emergency_stop in config.
    """
    if _STOP_EVENT.is_set():
        return True

    if config and config.get("filters", {}).get("emergency_stop", False):
        return True

    # Check live on disk in case config.yaml was updated dynamically
    try:
        if CONFIG_PATH.exists():
            cfg_data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            if cfg_data.get("filters", {}).get("emergency_stop", False):
                return True
    except Exception:
        pass

    return False
