"""Atomic local settings; cookies never enter queue files or logs."""
import json
import os
import threading
from pathlib import Path
from .paths import CONFIG_PATH

_LOCK = threading.RLock()
_DEFAULTS = dict(topic="", cookie="", show_cookie=False, geometry="", show_logs=False)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def load_config():
    with _LOCK:
        data = read_json(CONFIG_PATH, {})
        return {**_DEFAULTS, **(data if isinstance(data, dict) else {})}


def save_config(**values):
    with _LOCK:
        data = load_config()
        data.update(values)
        atomic_json(CONFIG_PATH, data)
