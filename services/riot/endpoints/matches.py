"""tft-match-v1: PUUID -> match IDs -> match JSON (regional routing)."""

from __future__ import annotations

from ..client import RiotClient, RiotResponse


class MatchService:
    def __init__(self, client: RiotClient):
        self._client = client

    def ids_by_puuid(self, puuid: str, *, start: int = 0, count: int = 20,
                     start_time: int | None = None, end_time: int | None = None) -> list[str]:
        """Most recent match IDs first. start_time/end_time are epoch seconds."""
        return self._client.match_ids_by_puuid(puuid, start=start, count=count,
                                               start_time=start_time, end_time=end_time)

    def get_response(self, match_id: str) -> RiotResponse:
        """The untouched response, for saving raw bytes exactly as received."""
        return self._client.match(match_id)

    def get(self, match_id: str) -> dict:
        return self.get_response(match_id).json()
