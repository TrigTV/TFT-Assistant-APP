"""Riot API Gateway: the only part of the app that talks to Riot.

    from services.riot import RiotClient, AccountService, MatchService
    client = RiotClient.from_env()          # reads RIOT_API_KEY etc. from .env
    puuid = AccountService(client).by_riot_id("Aman", "NA1")["puuid"]

High Elo collection (Milestone 2) lives in services.riot.collector.
"""

from .archive import MatchArchive
from .client import RiotClient, RiotResponse
from .config import RiotConfig, load_dotenv
from .endpoints import (AccountService, LeagueService, MatchService, StatusService,
                        SummonerService, parse_riot_id)
from .errors import (RiotBadRequest, RiotConfigError, RiotError, RiotExpiredKey,
                     RiotNetworkError, RiotNotFound, RiotRateLimited, RiotServerError,
                     RiotUnauthorized)
from .raw_store import RawMatchStore
from .routing import platform_url, regional_url

__all__ = [
    "RiotClient", "RiotResponse", "RiotConfig", "load_dotenv",
    "platform_url", "regional_url", "AccountService", "LeagueService", "MatchService",
    "StatusService", "SummonerService", "parse_riot_id", "RawMatchStore", "MatchArchive",
    "RiotError", "RiotConfigError", "RiotUnauthorized", "RiotExpiredKey", "RiotNotFound",
    "RiotRateLimited", "RiotServerError", "RiotBadRequest", "RiotNetworkError",
]
