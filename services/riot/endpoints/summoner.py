"""tft-summoner-v1: summoner profile by PUUID (platform routing)."""

from __future__ import annotations

from ..client import RiotClient


class SummonerService:
    def __init__(self, client: RiotClient):
        self._client = client

    def by_id(self, summoner_id: str) -> dict:
        """Older league entries carry only a summonerId; this resolves it to a profile with a PUUID."""
        return self._client.summoner_by_id(summoner_id)

    def by_puuid(self, puuid: str) -> dict:
        return self._client.summoner_by_puuid(puuid)
