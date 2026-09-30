"""Riot configuration.

The app only ever knows about RIOT_API_KEY. Swapping a development key for a
production key means changing that one environment value, nothing else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import RiotConfigError
from .routing import PLATFORMS, REGIONS

DEFAULT_PLATFORM = "na1"
DEFAULT_REGION = "americas"


def load_dotenv(path: str | os.PathLike = ".env", *, override: bool = False) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ.

    Minimal parser so the gateway has no third-party dependencies. Existing
    environment variables win unless override=True, so a deployed secret is
    never replaced by a stray local file.
    """
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class RiotConfig:
    api_key: str = field(repr=False)
    platform: str = DEFAULT_PLATFORM
    region: str = DEFAULT_REGION
    timeout: float = 10.0
    max_retries: int = 3

    def __post_init__(self):
        if not self.api_key or not self.api_key.strip():
            raise RiotConfigError(
                "RIOT_API_KEY is not set. Copy .env.example to .env and paste your Riot key."
            )
        platform = self.platform.lower().strip()
        region = self.region.lower().strip()
        if platform not in PLATFORMS:
            raise RiotConfigError(
                f"Unknown RIOT_PLATFORM '{self.platform}'. Expected one of: {', '.join(sorted(PLATFORMS))}"
            )
        if region not in REGIONS:
            raise RiotConfigError(
                f"Unknown RIOT_REGION '{self.region}'. Expected one of: {', '.join(sorted(REGIONS))}"
            )
        object.__setattr__(self, "api_key", self.api_key.strip())
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "region", region)

    def __repr__(self) -> str:  # never print the key
        return f"RiotConfig(api_key='***', platform='{self.platform}', region='{self.region}')"

    @classmethod
    def from_env(cls, env_file: str | os.PathLike | None = ".env") -> "RiotConfig":
        """Build config from environment variables (after loading .env if present)."""
        if env_file is not None:
            load_dotenv(env_file)
        return cls(
            api_key=os.environ.get("RIOT_API_KEY", ""),
            platform=os.environ.get("RIOT_PLATFORM", DEFAULT_PLATFORM),
            region=os.environ.get("RIOT_REGION", DEFAULT_REGION),
        )
