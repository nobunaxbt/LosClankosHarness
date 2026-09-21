"""Game HTTP and bounded Jev inference; credentials never enter command arguments."""

import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


class API:
    def __init__(self, settings, store, control):
        self.settings, self.store, self.control = settings, store, control

    def request(self, path, data=None, auth=True, model=False):
        self.control.check()
        if model:
            return self.decide(data)
        if not path.startswith("/api/") or "://" in path:
            raise ValueError("Game requests must use a local /api/ path")
        headers = {"Content-Type": "application/json"}
        if auth:
            identity = self.store.read("identity.json")
            if not identity or not identity.get("token"):
                raise RuntimeError("Register an agent before running")
            headers["Authorization"] = "Bearer " + identity["token"]
        request = urllib.request.Request(
            self.settings.base_url + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as result:
                return json.load(result)
        except urllib.error.HTTPError as error:
            try:
                response = json.load(error)
            except (ValueError, UnicodeDecodeError):
                response = {"ok": False, "error": "HTTP_" + str(error.code)}
            response["_http"] = error.code
            return response

    def decide(self, data):
        self.control.check()
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            shared = os.environ.get("OPENROUTER_KEY_FILE")
            keyfile = Path(shared).expanduser() if shared else self.store.path("openrouter.key")
            if keyfile.exists():
                key = keyfile.read_text().strip()
        if not key:
            raise RuntimeError(
                "Set OPENROUTER_API_KEY, OPENROUTER_KEY_FILE, or a private state-directory openrouter.key"
            )
        if any(char in key for char in '\r\n"\\'):
            raise ValueError("API key contains invalid header characters")
        # curl provides consistent connection setup and a hard wall-clock request limit.
        fd, name = tempfile.mkstemp(prefix=".model-", dir=self.store.root)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(
                    f'header = "Authorization: Bearer {key}"\nheader = "Content-Type: application/json"\n'
                )
            process = subprocess.Popen(
                [
                    "curl",
                    "--silent",
                    "--show-error",
                    "--max-time",
                    "25",
                    "--config",
                    name,
                    "--data-binary",
                    "@-",
                    "https://openrouter.ai/api/alpha/decisions",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            payload = json.dumps(data)
            try:
                while True:
                    self.control.check()
                    try:
                        stdout, _ = process.communicate(input=payload, timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        payload = None
                if process.returncode:
                    raise RuntimeError(f"OpenRouter transport failed ({process.returncode})")
                result = json.loads(stdout)
                if not isinstance(result, dict):
                    raise RuntimeError("Invalid model response")
                return result
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        finally:
            Path(name).unlink(missing_ok=True)
