"""tft-summoner-v1: summoner profile by PUUID or summoner ID (platform routing)."""

from __future__ import annotations

from ..client import RiotClient


class SummonerService:
    def __init__(self, client: RiotClient):
        self._client = client

    def by_puuid(self, puuid: str) -> dict:
        return self._client.summoner_by_puuid(puuid)

    def by_id(self, summoner_id: str) -> dict:
        """Includes the puuid, for league entries that only carry a summonerId."""
        return self._client.summoner_by_id(summoner_id)
