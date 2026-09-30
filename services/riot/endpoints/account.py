"""account-v1: Riot ID <-> PUUID. The PUUID is our primary player identifier."""

from __future__ import annotations

from ..client import RiotClient


def parse_riot_id(riot_id: str) -> tuple[str, str]:
    """Split "Name#TAG" into ("Name", "TAG")."""
    name, sep, tag = riot_id.strip().rpartition("#")
    if not sep or not name.strip() or not tag.strip():
        raise ValueError(f"Riot ID must look like Name#TAG, got {riot_id!r}")
    return name.strip(), tag.strip()


class AccountService:
    def __init__(self, client: RiotClient):
        self._client = client

    def by_riot_id(self, game_name: str, tag_line: str) -> dict:
        """Returns {"puuid", "gameName", "tagLine"}."""
        return self._client.account_by_riot_id(game_name, tag_line)

    def by_riot_id_string(self, riot_id: str) -> dict:
        return self.by_riot_id(*parse_riot_id(riot_id))

    def by_puuid(self, puuid: str) -> dict:
        return self._client.account_by_puuid(puuid)
