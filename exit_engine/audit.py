"""
Structured audit logging. One JSON line per evaluated decision (both
CONTINUE and EXIT are logged, so you can reconstruct exactly what the
engine saw and decided at every tick) -- thread-safe via a module-level
lock around the file write.
"""
import json
import os
import threading
from datetime import datetime, timezone

_LOCK = threading.Lock()
_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "output", "exit_engine_audit.jsonl")


class AuditLogger:
    def __init__(self, path: str = _DEFAULT_PATH):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def log(self, record: dict):
        record = dict(record)
        record["logged_at"] = datetime.now(timezone.utc).isoformat()
        line = json.dumps(record, default=str)
        with _LOCK:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
