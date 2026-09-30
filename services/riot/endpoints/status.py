"""tft-status-v1: a cheap authenticated call used to check the key works."""

from __future__ import annotations

from ..client import RiotClient


class StatusService:
    def __init__(self, client: RiotClient):
        self._client = client

    def platform_data(self) -> dict:
        return self._client.platform_status()
