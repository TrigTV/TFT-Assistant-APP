import logging
import unittest

import requests

from services.riot import (RiotClient, RiotConfig, RiotExpiredKey, RiotNetworkError, RiotNotFound,
                           RiotRateLimited, RiotServerError, RiotUnauthorized)

from .fakes import TEST_KEY, FakeRiot

STATUS = "/tft/status/v1/platform-data"


def make_client(max_retries=3, **cfg):
    sleeps = []
    client = RiotClient(RiotConfig(api_key=TEST_KEY, max_retries=max_retries, **cfg), sleep=sleeps.append)
    return client, sleeps


class AuthTests(unittest.TestCase):
    def test_key_sent_in_header_never_in_url(self):
        with FakeRiot().add(STATUS, {"id": "NA1"}) as riot:
            client, _ = make_client()
            self.assertEqual(client.platform_status(), {"id": "NA1"})
        call = riot.calls[0]
        self.assertEqual(call["headers"]["X-Riot-Token"], TEST_KEY)
        self.assertNotIn(TEST_KEY, call["url"])
        self.assertEqual(call["host"], "na1.api.riotgames.com")
        self.assertEqual(call["timeout"], 10.0)

    def test_403_is_expired_key_with_help(self):
        with FakeRiot().add(STATUS, b'{"status":{"status_code":403}}', status=403):
            client, _ = make_client()
            with self.assertRaises(RiotExpiredKey) as cm:
                client.platform_status()
        self.assertIn("Update RIOT_API_KEY in .env", str(cm.exception))
        self.assertIsInstance(cm.exception, RiotUnauthorized)

    def test_401_unauthorized(self):
        with FakeRiot().add(STATUS, status=401):
            client, _ = make_client()
            with self.assertRaises(RiotUnauthorized):
                client.platform_status()

    def test_404_not_retried(self):
        with FakeRiot() as riot:
            client, _ = make_client()
            with self.assertRaises(RiotNotFound):
                client.summoner_by_puuid("nobody")
        self.assertEqual(len(riot.calls), 1)

    def test_key_never_logged(self):
        with FakeRiot().add(STATUS, status=500).add(STATUS, {"ok": 1}):
            client, _ = make_client()
            with self.assertLogs(level=logging.DEBUG) as logs:
                client.platform_status()
        self.assertFalse(any(TEST_KEY in line for line in logs.output))


class RetryTests(unittest.TestCase):
    def test_5xx_retries_with_backoff_then_succeeds(self):
        with FakeRiot().add(STATUS, status=503).add(STATUS, status=500).add(STATUS, {"ok": True}) as riot:
            client, sleeps = make_client()
            self.assertEqual(client.platform_status(), {"ok": True})
        self.assertEqual(len(riot.calls), 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_5xx_gives_up(self):
        with FakeRiot().add(STATUS, status=502) as riot:
            client, _ = make_client(max_retries=2)
            with self.assertRaises(RiotServerError):
                client.platform_status()
        self.assertEqual(len(riot.calls), 3)

    def test_429_with_retry_after_waits_in_riot_watcher(self):
        riot = FakeRiot().add(STATUS, status=429, headers={"Retry-After": "7", "X-Rate-Limit-Type": "application"})
        riot.add(STATUS, {"ok": True})
        with riot:
            client, sleeps = make_client()
            self.assertEqual(client.platform_status(), {"ok": True})
        self.assertEqual(sleeps, [])  # no double wait on our side
        self.assertEqual(len(riot.riotwatcher_sleeps), 1)
        self.assertAlmostEqual(riot.riotwatcher_sleeps[0], 7.0, delta=0.5)

    def test_429_without_retry_after_backs_off(self):
        with FakeRiot().add(STATUS, status=429).add(STATUS, {"ok": True}):
            client, sleeps = make_client()
            client.platform_status()
        self.assertEqual(sleeps, [1.0])

    def test_429_gives_up(self):
        with FakeRiot().add(STATUS, status=429, headers={"Retry-After": "1"}):
            client, _ = make_client(max_retries=1)
            with self.assertRaises(RiotRateLimited) as cm:
                client.platform_status()
        self.assertEqual(cm.exception.retry_after, 1.0)

    def test_network_error_retries_then_raises(self):
        with FakeRiot().add(STATUS, {"ok": True}) as riot:
            client, _ = make_client()
            riot.raise_errors = [requests.Timeout("timed out")]
            self.assertEqual(client.platform_status(), {"ok": True})
            riot.raise_errors = [requests.ConnectionError("dns")] * 4
            with self.assertRaises(RiotNetworkError):
                client.platform_status()


class RateLimitTests(unittest.TestCase):
    def test_riot_watcher_waits_when_app_limit_is_used_up(self):
        # Dev-key limits as Riot reports them, with this second's budget spent.
        headers = {"X-App-Rate-Limit": "20:1,100:120", "X-App-Rate-Limit-Count": "20:1,20:120"}
        with FakeRiot().add(STATUS, {"ok": 1}, headers=headers) as riot:
            client, _ = make_client()
            client.platform_status()
            self.assertEqual(riot.riotwatcher_sleeps, [])
            client.platform_status()
        self.assertEqual(len(riot.riotwatcher_sleeps), 1)
        self.assertGreater(riot.riotwatcher_sleeps[0], 0)

    def test_higher_production_limits_need_no_code_change(self):
        headers = {"X-App-Rate-Limit": "500:10,30000:600", "X-App-Rate-Limit-Count": "1:10,1:600"}
        with FakeRiot().add(STATUS, {"ok": 1}, headers=headers) as riot:
            client, _ = make_client()
            for _ in range(30):
                client.platform_status()
        self.assertEqual(riot.riotwatcher_sleeps, [])
