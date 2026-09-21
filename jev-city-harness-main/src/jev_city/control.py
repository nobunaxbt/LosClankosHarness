"""Cooperative cancellation. An explicit stop never waits for model inference."""

import os
import threading
import time
import uuid


class Control:
    def __init__(self, store):
        self.store = store
        self.stopped = threading.Event()

    def stop(self, *_):
        self.stopped.set()

    def check(self):
        if self.stopped.is_set() or self.store.path("STOP").exists():
            raise KeyboardInterrupt

    def pause(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check()
            self.stopped.wait(min(0.1, max(0, deadline - time.monotonic())))


def submit(store, text):
    text = text.strip()
    if not text or len(text) > 1000:
        raise ValueError("Order must contain 1–1000 characters")
    directory = store.root / "orders"
    directory.mkdir(mode=0o700, exist_ok=True)
    row = {"id": str(uuid.uuid4()), "createdAt": time.time(), "text": text, "source": "local-human"}
    temporary = directory / (row["id"] + ".tmp")
    import json

    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(row, stream)
    temporary.replace(directory / (row["id"] + ".json"))
    if text.lower().strip(" .!") in {"stop", "stop playing", "stop the agent", "stop running"}:
        store.save("STOP", {"at": time.time()})
    return row
