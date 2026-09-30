"""tft-league-v1: rank, tier and LP (platform routing)."""

from __future__ import annotations

from ..client import RiotClient

RANKED_QUEUE = "RANKED_TFT"


class LeagueService:
    def __init__(self, client: RiotClient):
        self._client = client

    def entries_by_puuid(self, puuid: str) -> list[dict]:
        """One entry per ranked queue the player has played (may be empty)."""
        return self._client.league_entries_by_puuid(puuid)

    def ranked_entry(self, puuid: str) -> dict | None:
        """The standard ranked TFT entry, or None if unranked."""
        for entry in self.entries_by_puuid(puuid):
            if entry.get("queueType") == RANKED_QUEUE:
                return entry
        return None

    def challenger(self) -> dict:
        return self._client.challenger_league()

    def grandmaster(self) -> dict:
        return self._client.grandmaster_league()

    def master(self) -> dict:
        return self._client.master_league()


def format_rank(entry: dict | None) -> str:
    if not entry:
        return "Unranked"
    tier = entry.get("tier", "").title()
    if tier in ("Master", "Grandmaster", "Challenger"):
        return f"{tier} {entry.get('leaguePoints', 0)} LP"
    return f"{tier} {entry.get('rank', '')} {entry.get('leaguePoints', 0)} LP".replace("  ", " ")
