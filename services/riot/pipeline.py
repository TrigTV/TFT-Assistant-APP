"""First data flow: Riot ID -> PUUID -> match IDs -> match JSON -> raw files.

No normalization, statistics or AI; just retrieval and raw storage.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .client import RiotClient
from .endpoints import AccountService, LeagueService, MatchService
from .endpoints.league import format_rank
from .errors import RiotError
from .raw_store import RawMatchStore


@dataclass
class FetchResult:
    riot_id: str
    puuid: str
    rank: str | None = None
    match_ids: list[str] = field(default_factory=list)
    saved: list[str] = field(default_factory=list)
    cached: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        lines = [f"Player:\n{self.riot_id}", ""]
        if self.rank is not None:
            lines += [f"Rank:\n{self.rank}", ""]
        lines += [f"Matches retrieved:\n{len(self.match_ids)}", "",
                  f"Matches saved:\n{len(self.saved) + len(self.cached)}"]
        if self.cached:
            lines.append(f"({len(self.cached)} already on disk, not re-downloaded)")
        if self.failed:
            lines.append(f"Failed: {len(self.failed)}")
        return "\n".join(lines)


def fetch_player_matches(client: RiotClient, riot_id: str, *, count: int = 20,
                         store: RawMatchStore | None = None, include_rank: bool = True) -> FetchResult:
    store = store or RawMatchStore()
    account = AccountService(client).by_riot_id_string(riot_id)
    result = FetchResult(riot_id=f"{account['gameName']}#{account['tagLine']}", puuid=account["puuid"])

    if include_rank:
        try:
            result.rank = format_rank(LeagueService(client).ranked_entry(result.puuid))
        except RiotError as e:
            result.rank = f"unavailable ({type(e).__name__})"

    matches = MatchService(client)
    result.match_ids = matches.ids_by_puuid(result.puuid, count=count)
    for match_id in result.match_ids:
        if store.has(match_id):
            result.cached.append(match_id)
            continue
        try:
            store.save_bytes(match_id, matches.get_response(match_id).body)
            result.saved.append(match_id)
        except RiotError as e:
            result.failed[match_id] = str(e)
    return result
