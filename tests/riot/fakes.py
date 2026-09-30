"""A fake Riot server for tests.

Patches requests' HTTP adapter, so every call runs through the real
RiotClient -> Riot Watcher -> requests stack and only the network is faked.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

import requests
from requests.structures import CaseInsensitiveDict
from riotwatcher.Handlers.RateLimit import BasicRateLimiter

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
TEST_KEY = "RGAPI-test-key-not-real"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeRiot:
    """Maps URL paths to queued (status, headers, body) responses.

    A route with several queued responses returns them in order and then
    repeats the last one. Use as a context manager.
    """

    def __init__(self):
        self.routes: dict[str, list[tuple[int, dict, bytes]]] = {}
        self.calls: list[dict] = []
        self.raise_errors: list[BaseException] = []
        self.riotwatcher_sleeps: list[float] = []

    def add(self, path: str, body=b"", status: int = 200, headers: dict | None = None):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.routes.setdefault(path, []).append((status, headers or {}, body))
        return self

    def add_fixture(self, path: str, fixture: str, **kw):
        return self.add(path, fixture_bytes(fixture), **kw)

    def _send(self, adapter, request, **kwargs):
        parts = urlsplit(request.url)
        self.calls.append({"url": request.url, "host": parts.netloc, "path": unquote(parts.path),
                           "raw_path": parts.path, "query": parse_qs(parts.query),
                           "headers": dict(request.headers), "timeout": kwargs.get("timeout")})
        if self.raise_errors:
            raise self.raise_errors.pop(0)
        queue = self.routes.get(unquote(parts.path))
        if not queue:
            status, headers, body = 404, {}, b'{"status":{"message":"Data not found","status_code":404}}'
        else:
            status, headers, body = queue.pop(0) if len(queue) > 1 else queue[0]
        r = requests.Response()
        r.status_code = status
        r.reason = "fake"
        r.headers = CaseInsensitiveDict(headers)
        r._content = body
        r.encoding = "utf-8"
        r.url = request.url
        r.request = request
        return r

    def __enter__(self):
        # Riot Watcher keeps its application limiter at class level; start each test clean.
        BasicRateLimiter._BasicRateLimiter__application_rate_limiter._limits.clear()
        fake = self
        self._patches = [
            mock.patch("requests.adapters.HTTPAdapter.send",
                       lambda adapter, request, **kw: fake._send(adapter, request, **kw)),
            mock.patch("riotwatcher.Handlers.RateLimiterAdapter.time",
                       mock.Mock(sleep=self.riotwatcher_sleeps.append)),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        BasicRateLimiter._BasicRateLimiter__application_rate_limiter._limits.clear()


def standard_riot() -> FakeRiot:
    t = FakeRiot()
    t.add_fixture("/riot/account/v1/accounts/by-riot-id/Aman/NA1", "account_by_riot_id.json")
    puuid = json.loads(fixture_bytes("account_by_riot_id.json"))["puuid"]
    t.add_fixture(f"/tft/match/v1/matches/by-puuid/{puuid}/ids", "match_ids.json")
    t.add_fixture(f"/tft/league/v1/by-puuid/{puuid}", "league_entries.json")
    t.add_fixture("/tft/status/v1/platform-data", "status_platform_data.json")
    for mid in json.loads(fixture_bytes("match_ids.json")):
        # serve the one real-shaped fixture for every ID, with its ID swapped in
        body = fixture_bytes("match_NA1_5000000003.json").replace(b"NA1_5000000003", mid.encode())
        t.add(f"/tft/match/v1/matches/{mid}", body)
    return t
