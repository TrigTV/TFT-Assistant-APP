import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts import riot_smoke_test
from services.riot import RawMatchStore, RiotClient, RiotConfig
from services.riot.pipeline import fetch_player_matches

from .fakes import TEST_KEY, FakeRiot, fixture_bytes, standard_riot


def client():
    return RiotClient(RiotConfig(api_key=TEST_KEY))


class RawStoreTests(unittest.TestCase):
    def test_round_trip_and_id_validation(self):
        with tempfile.TemporaryDirectory() as d:
            store = RawMatchStore(d)
            self.assertFalse(store.has("NA1_1"))
            path = store.save_bytes("NA1_1", b'{"metadata": {}, "info": {}}')
            self.assertEqual(path.name, "NA1_1.json")
            self.assertEqual(store.load_bytes("NA1_1"), b'{"metadata": {}, "info": {}}')
            self.assertEqual(list(Path(d).iterdir()), [path])  # no temp files left
            for bad in ("../etc/passwd", "NA1_12/../x", "na1_1", ""):
                with self.assertRaises(ValueError):
                    store.path_for(bad)


class FirstDataFlowTests(unittest.TestCase):
    """Milestone 1: Riot ID -> PUUID -> match IDs -> match -> raw JSON on disk."""

    def test_end_to_end_saves_raw_json_untouched(self):
        with tempfile.TemporaryDirectory() as d, standard_riot():
            store = RawMatchStore(d)
            result = fetch_player_matches(client(), "Aman#NA1", store=store)
            self.assertEqual(result.rank, "Diamond II 42 LP")
            self.assertEqual(len(result.match_ids), 3)
            self.assertEqual(result.saved, result.match_ids)
            self.assertEqual(store.load_bytes("NA1_5000000003"), fixture_bytes("match_NA1_5000000003.json"))
            self.assertEqual(json.loads(store.load_bytes("NA1_5000000001"))["metadata"]["match_id"], "NA1_5000000001")
            self.assertIn("Matches saved:\n3", result.summary())

    def test_second_run_uses_cache(self):
        with tempfile.TemporaryDirectory() as d:
            store = RawMatchStore(d)
            with standard_riot():
                fetch_player_matches(client(), "Aman#NA1", store=store)
            with standard_riot() as riot:
                result = fetch_player_matches(client(), "Aman#NA1", store=store)
            self.assertEqual(result.saved, [])
            self.assertEqual(len(result.cached), 3)
            self.assertFalse(any("/tft/match/v1/matches/NA1_" in c["path"] for c in riot.calls))

    def test_one_failed_match_does_not_stop_the_rest(self):
        riot = standard_riot()
        riot.routes["/tft/match/v1/matches/NA1_5000000002"] = [(404, {}, b"")]
        with tempfile.TemporaryDirectory() as d, riot:
            result = fetch_player_matches(client(), "Aman#NA1", store=RawMatchStore(d))
        self.assertEqual(len(result.saved), 2)
        self.assertIn("NA1_5000000002", result.failed)


class SmokeTestScriptTests(unittest.TestCase):
    def run_smoke(self, riot):
        with tempfile.TemporaryDirectory() as d, riot:
            out = io.StringIO()
            ok = riot_smoke_test.run("Aman#NA1", client(), RawMatchStore(d), out=out)
            return ok, out.getvalue(), sorted(p.name for p in Path(d).iterdir())

    def test_pass(self):
        ok, out, files = self.run_smoke(standard_riot())
        self.assertTrue(ok)
        for line in ("Authentication     PASS", "Account lookup     PASS", "Match list         PASS",
                     "Match retrieval    PASS", "Riot API ready."):
            self.assertIn(line, out)
        self.assertEqual(files, ["NA1_5000000003.json"])

    def test_expired_key(self):
        ok, out, files = self.run_smoke(FakeRiot().add("/tft/status/v1/platform-data", status=403))
        self.assertFalse(ok)
        self.assertIn("Authentication     FAIL", out)
        self.assertIn("Your development API key may have expired.", out)
        self.assertNotIn("Account lookup", out)
        self.assertEqual(files, [])
