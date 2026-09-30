import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.riot import RiotConfig, RiotConfigError, platform_url, regional_url
from services.riot.config import load_dotenv
from services.riot.routing import account_region

from .fakes import TEST_KEY


class ConfigTests(unittest.TestCase):
    def test_missing_key_is_a_clear_error(self):
        with self.assertRaises(RiotConfigError) as cm:
            RiotConfig(api_key="  ")
        self.assertIn("RIOT_API_KEY", str(cm.exception))

    def test_repr_and_str_hide_key(self):
        c = RiotConfig(api_key=TEST_KEY)
        self.assertNotIn(TEST_KEY, repr(c))
        self.assertNotIn(TEST_KEY, str(c))

    def test_normalizes_case_and_rejects_unknown_values(self):
        c = RiotConfig(api_key=TEST_KEY, platform="EUW1", region="Europe")
        self.assertEqual((c.platform, c.region), ("euw1", "europe"))
        with self.assertRaises(RiotConfigError):
            RiotConfig(api_key=TEST_KEY, platform="na")
        with self.assertRaises(RiotConfigError):
            RiotConfig(api_key=TEST_KEY, region="na1")

    def test_from_env_reads_dotenv_and_real_env_wins(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text('# comment\nRIOT_API_KEY="RGAPI-from-file"\nexport RIOT_PLATFORM=kr\nRIOT_REGION=asia\n')
            with mock.patch.dict(os.environ, {}, clear=True):
                c = RiotConfig.from_env(env)
                self.assertEqual((c.api_key, c.platform, c.region), ("RGAPI-from-file", "kr", "asia"))
            with mock.patch.dict(os.environ, {"RIOT_API_KEY": "RGAPI-deployed-secret"}, clear=True):
                c = RiotConfig.from_env(env)
                self.assertEqual(c.api_key, "RGAPI-deployed-secret")

    def test_missing_dotenv_is_fine(self):
        with mock.patch.dict(os.environ, {"RIOT_API_KEY": TEST_KEY}, clear=True):
            load_dotenv("/nonexistent/.env")
            self.assertEqual(RiotConfig.from_env("/nonexistent/.env").platform, "na1")


class RoutingTests(unittest.TestCase):
    def test_hosts(self):
        self.assertEqual(platform_url("na1"), "https://na1.api.riotgames.com")
        self.assertEqual(regional_url("americas"), "https://americas.api.riotgames.com")

    def test_account_has_no_sea_cluster(self):
        self.assertEqual(account_region("sea"), "asia")
        self.assertEqual(account_region("europe"), "europe")
