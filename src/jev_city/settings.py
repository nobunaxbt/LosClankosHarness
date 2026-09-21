"""Configuration without machine-specific paths or player identities."""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    base_url: str = "https://losclankos.com"
    model: str = "~typesafe/jev-latest"
    poll_seconds: int = 1
    world_refresh_seconds: int = 5
    max_failures: int = 5
    replace_on_death: bool = False

    def __post_init__(self):
        parsed = urlparse(self.base_url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Game URL must be an origin without credentials or parameters")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("Use HTTPS, or HTTP only for a local development server")
        if not 1 <= self.poll_seconds <= 20:
            raise ValueError("Poll wait must be between 1 and 20 seconds")

    @classmethod
    def from_env(cls, state_dir=None, replace_on_death=False):
        default = (
            Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "jev-city"
        )
        return cls(
            state_dir=Path(state_dir or os.environ.get("JEV_STATE_DIR", default))
            .expanduser()
            .resolve(),
            base_url=os.environ.get("LOS_CLANKOS_URL", "https://losclankos.com").rstrip("/"),
            model=os.environ.get("JEV_MODEL", "~typesafe/jev-latest"),
            poll_seconds=int(os.environ.get("JEV_POLL_SECONDS", "1")),
            replace_on_death=replace_on_death,
        )
