"""Private atomic runtime state, process lock and redacted logs."""

import fcntl
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

SECRET_FIELDS = {
    "token",
    "previoustoken",
    "authorization",
    "api_key",
    "apikey",
    "openrouter_api_key",
    "wallet",
    "privatekey",
    "private_key",
}
SECRET_PATTERN = re.compile(r'(?i)(?:sk-or-v1-|sk-)[a-z0-9_-]{12,}|bearer\s+[^\s"\']+')


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    def path(self, name):
        p = self.root / name
        if p.parent != self.root or p.name in {".", ".."}:
            raise ValueError("State filenames must not contain directories")
        return p

    def read(self, name, default=None):
        try:
            return json.loads(self.path(name).read_text())
        except FileNotFoundError:
            return default

    def save(self, name, data):
        target = self.path(name)
        fd, temp = tempfile.mkstemp(prefix=".state-", dir=self.root)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)
        finally:
            Path(temp).unlink(missing_ok=True)

    def redact(self, value):
        if isinstance(value, dict):
            return {
                k: ("[REDACTED]" if k.lower() in SECRET_FIELDS else self.redact(v))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        if isinstance(value, str):
            return SECRET_PATTERN.sub("[REDACTED]", value)
        return value

    def log(self, kind, **data):
        row = self.redact({"at": time.time(), "kind": kind, **data})
        fd = os.open(self.path("events.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as stream:
            stream.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)

    @contextmanager
    def lock(self):
        fd = os.open(self.path("controller.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(
                    "A controller is already running for this state directory"
                ) from error
            yield
        finally:
            os.close(fd)
