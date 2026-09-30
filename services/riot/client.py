"""RiotClient: the single door to the Riot API.

Internally it wraps Riot Watcher (MIT), which sends the X-Riot-Token header
and enforces Riot's app and method rate limits from response headers:

    Our code -> RiotClient -> Riot Watcher -> Riot API

RiotClient adds what Riot Watcher leaves to the caller: shard selection from
RiotConfig, retries for 429/5xx/timeouts, our own error types, and raw
response bodies for storage. Nothing outside this module imports Riot
Watcher, so it can be replaced without touching the rest of the app.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

import requests
from riotwatcher import ApiError, Deserializer, RiotWatcher, TftWatcher
from riotwatcher.Handlers import (DeprecationHandler, DeserializerAdapter, RateLimiterAdapter,
                                  SanitationHandler, ThrowOnErrorHandler, TypeCorrectorHandler)
from riotwatcher.Handlers.RateLimit import BasicRateLimiter
from riotwatcher._apis import BaseApi, Endpoint, NamedEndpoint, UrlConfig

from .config import RiotConfig
from .errors import RiotNetworkError, RiotRateLimited, error_for_status
from .routing import account_region

log = logging.getLogger("riot")


@dataclass(frozen=True)
class RiotResponse:
    body: bytes  # exactly as Riot sent it

    def json(self) -> Any:
        return json.loads(self.body) if self.body else {}


class _RawDeserializer(Deserializer):
    """Keep Riot's response text instead of parsing it, so raw JSON can be stored as received."""

    def deserialize(self, endpoint_name: str, method_name: str, data: str) -> RiotResponse:
        return RiotResponse(body=(data or "").encode("utf-8"))


class _TftExtrasApi(NamedEndpoint):
    """TFT endpoints Riot Watcher 3.3.1 does not wrap yet, sent through the same handler chain."""

    _league_by_puuid = Endpoint(UrlConfig.tft_url + "/tft/league/v1/by-puuid/{puuid}")
    _status = Endpoint(UrlConfig.tft_url + "/tft/status/v1/platform-data")

    def league_by_puuid(self, platform: str, puuid: str):
        return self._request_endpoint("by_puuid", platform, self._league_by_puuid, puuid=puuid)

    def platform_data(self, platform: str):
        return self._request_endpoint("platform_data", platform, self._status)


def _handler_chain(rate_limiter, deserializer):
    # Same chain Riot Watcher builds for its own watchers.
    return [
        SanitationHandler(),
        DeserializerAdapter(deserializer),
        ThrowOnErrorHandler(),
        TypeCorrectorHandler(),
        RateLimiterAdapter(rate_limiter),
        DeprecationHandler(),
    ]


class RiotClient:
    def __init__(self, config: RiotConfig, *, sleep: Callable[[float], None] = time.sleep,
                 backoff_base: float = 1.0):
        self.config = config
        self._sleep = sleep
        self._backoff_base = backoff_base
        limiter = BasicRateLimiter()  # shared by all three so limits are counted together
        deserializer = _RawDeserializer()
        kw = dict(timeout=config.timeout, rate_limiter=limiter, deserializer=deserializer)
        self._riot = RiotWatcher(config.api_key, **kw)
        self._tft = TftWatcher(config.api_key, **kw)
        self._extras = _TftExtrasApi(
            BaseApi(config.api_key, _handler_chain(limiter, deserializer), timeout=config.timeout),
            "LeagueApi",
        )

    @classmethod
    def from_env(cls, env_file: str | None = ".env", **kwargs) -> "RiotClient":
        return cls(RiotConfig.from_env(env_file), **kwargs)

    # --- shards -------------------------------------------------------------

    @property
    def _platform(self) -> str:
        return self.config.platform

    @property
    def _region(self) -> str:
        return self.config.region

    @property
    def _account_region(self) -> str:
        return account_region(self.config.region)

    # --- account-v1 (regional) ------------------------------------------------

    def account_by_riot_id(self, game_name: str, tag_line: str) -> dict:
        # Riot Watcher formats path segments without encoding them.
        return self._call("account-v1.by_riot_id", self._riot.account.by_riot_id,
                          self._account_region, _seg(game_name), _seg(tag_line)).json()

    def account_by_puuid(self, puuid: str) -> dict:
        return self._call("account-v1.by_puuid", self._riot.account.by_puuid,
                          self._account_region, _seg(puuid)).json()

    # --- tft-match-v1 (regional) ----------------------------------------------

    def match_ids_by_puuid(self, puuid: str, *, start: int = 0, count: int = 20,
                           start_time: int | None = None, end_time: int | None = None) -> list[str]:
        return self._call("tft-match-v1.by_puuid", self._tft.match.by_puuid, self._region, _seg(puuid),
                          count=count, start=start, start_time=start_time, end_time=end_time).json()

    def match(self, match_id: str) -> RiotResponse:
        """The untouched match response."""
        return self._call("tft-match-v1.by_id", self._tft.match.by_id, self._region, _seg(match_id))

    # --- tft-league-v1, tft-summoner-v1, tft-status-v1 (platform) --------------

    def league_entries_by_puuid(self, puuid: str) -> list[dict]:
        return self._call("tft-league-v1.by_puuid", self._extras.league_by_puuid,
                          self._platform, _seg(puuid)).json()

    def challenger_league(self) -> dict:
        return self._call("tft-league-v1.challenger", self._tft.league.challenger, self._platform).json()

    def grandmaster_league(self) -> dict:
        return self._call("tft-league-v1.grandmaster", self._tft.league.grandmaster, self._platform).json()

    def master_league(self) -> dict:
        return self._call("tft-league-v1.master", self._tft.league.master, self._platform).json()

    def summoner_by_puuid(self, puuid: str) -> dict:
        return self._call("tft-summoner-v1.by_puuid", self._tft.summoner.by_puuid,
                          self._platform, _seg(puuid)).json()

    def platform_status(self) -> dict:
        return self._call("tft-status-v1.platform_data", self._extras.platform_data, self._platform).json()

    # --- retries and error mapping ---------------------------------------------

    def _call(self, name: str, fn, *args, **kwargs) -> RiotResponse:
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                resp = fn(*args, **kwargs)
                log.debug("riot %s ok in %.0fms", name, (time.monotonic() - started) * 1000)
                return resp
            except ApiError as e:
                r = e.response
                status = r.status_code if r is not None else 0
                url = r.url if r is not None else None
                if status == 429:
                    retry_after = _float(r.headers.get("Retry-After"))
                    if attempt < self.config.max_retries:
                        # With Retry-After, Riot Watcher already holds the next call until then.
                        # Without it (Riot's service-level limit), back off ourselves.
                        if retry_after is None:
                            self._sleep(self._backoff(attempt))
                        log.warning("riot 429 on %s (%s limit); retrying", name,
                                    r.headers.get("X-Rate-Limit-Type", "service"))
                        attempt += 1
                        continue
                    raise RiotRateLimited("Riot API rate limit exceeded after retries.",
                                          status=429, url=url, retry_after=retry_after) from e
                if 500 <= status < 600 and attempt < self.config.max_retries:
                    delay = self._backoff(attempt)
                    log.warning("riot %s on %s; retrying in %.1fs", status, name, delay)
                    attempt += 1
                    self._sleep(delay)
                    continue
                text = r.text if r is not None else ""
                raise error_for_status(status, url or name, text) from e
            except (requests.Timeout, requests.ConnectionError) as e:
                if attempt < self.config.max_retries:
                    delay = self._backoff(attempt)
                    log.warning("riot network error on %s (%s); retrying in %.1fs",
                                name, type(e).__name__, delay)
                    attempt += 1
                    self._sleep(delay)
                    continue
                raise RiotNetworkError(f"Could not reach Riot API ({name}): {type(e).__name__}") from e

    def _backoff(self, attempt: int) -> float:
        return self._backoff_base * (2 ** attempt)


def _seg(value: str) -> str:
    return quote(str(value), safe="")


def _float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None
