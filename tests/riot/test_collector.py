import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from services.riot import RawMatchStore, RiotClient, RiotConfig, RiotExpiredKey
from services.riot.collector import CollectSettings, Collector
from services.riot.manifest import MatchManifest, patch_from_version

from .fakes import TEST_KEY, FakeRiot, fixture_bytes

# Ladder: two Challengers, two Grandmasters (one listed only by summonerId, as older responses were).
LADDER = {
    "CHALLENGER": [{"puuid": "p-chal-low", "leaguePoints": 900}, {"puuid": "p-chal-high", "leaguePoints": 1500}],
    "GRANDMASTER": [{"summonerId": "s-gm", "leaguePoints": 700}, {"puuid": "p-gm-low", "leaguePoints": 600}],
}
# Recent matches per player. Players in the same lobby share match IDs.
MATCH_IDS = {
    "p-chal-high": ["NA1_1", "NA1_2", "NA1_3"],
    "p-chal-low": ["NA1_2", "NA1_3", "NA1_4"],
    "p-gm": ["NA1_3", "NA1_5"],
    "p-gm-low": ["NA1_6"],
}


def match_body(match_id: str) -> bytes:
    return fixture_bytes("match_NA1_5000000003.json").replace(b"NA1_5000000003", match_id.encode())


def ladder_riot() -> FakeRiot:
    t = FakeRiot()
    t.add("/tft/league/v1/challenger", {"tier": "CHALLENGER", "entries": LADDER["CHALLENGER"]})
    t.add("/tft/league/v1/grandmaster", {"tier": "GRANDMASTER", "entries": LADDER["GRANDMASTER"]})
    t.add("/tft/summoner/v1/summoners/s-gm", {"id": "s-gm", "puuid": "p-gm"})
    for puuid, ids in MATCH_IDS.items():
        t.add(f"/tft/match/v1/matches/by-puuid/{puuid}/ids", ids)
    for n in range(1, 7):
        t.add(f"/tft/match/v1/matches/NA1_{n}", match_body(f"NA1_{n}"))
    return t


def match_downloads(riot: FakeRiot) -> list[str]:
    return [c["path"].rsplit("/", 1)[1] for c in riot.calls
            if c["path"].startswith("/tft/match/v1/matches/NA1_")]


class CollectorTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = RawMatchStore(self.root / "matches")
        self.manifest = MatchManifest(self.root / "manifest.jsonl")
        self.log: list[str] = []

    def tearDown(self):
        self._tmp.cleanup()

    def collector(self, raise_on=None):
        def progress(msg):
            self.log.append(msg)
            if raise_on and raise_on in msg:
                raise KeyboardInterrupt
        client = RiotClient(RiotConfig(api_key=TEST_KEY), sleep=lambda s: None)
        return Collector(client, store=self.store, manifest=self.manifest, state_dir=self.root / "collections",
                         now=lambda: datetime(2026, 10, 7, 15, 30, tzinfo=timezone.utc), progress=progress)

    def active(self) -> dict:
        return json.loads((self.root / "collections/active.json").read_text())


class CollectionTests(CollectorTestCase):
    def test_collects_top_players_and_saves_each_unique_match_once(self):
        with ladder_riot() as riot:
            stats = self.collector().run(CollectSettings(players=3, matches_per_player=3))

        # Top 3 by tier then LP: both Challengers, then the best Grandmaster (resolved via summonerId).
        scanned = [c["path"].split("/")[-2] for c in riot.calls if c["path"].endswith("/ids")]
        self.assertEqual(scanned, ["p-chal-high", "p-chal-low", "p-gm"])
        self.assertEqual(riot.calls[2]["path"], "/tft/summoner/v1/summoners/s-gm")
        self.assertEqual(riot.calls[3]["query"]["count"], ["3"])

        # 8 IDs, 3 repeats -> 5 unique matches, each downloaded exactly once.
        self.assertEqual(sorted(match_downloads(riot)), ["NA1_1", "NA1_2", "NA1_3", "NA1_4", "NA1_5"])
        for n in range(1, 6):
            self.assertEqual(self.store.load_bytes(f"NA1_{n}"), match_body(f"NA1_{n}"))  # untouched

        self.assertEqual((stats.status, stats.players_scanned, stats.match_ids_found, stats.duplicates_skipped,
                          stats.unique_matches, stats.matches_saved, stats.failed_requests, stats.pending),
                         ("complete", 3, 8, 3, 5, 5, 0, 0))
        self.assertEqual(stats.ladder, {"CHALLENGER": 2, "GRANDMASTER": 2})
        summary = stats.summary()
        for line in ("Players scanned      3 / 3", "Duplicates skipped   3", "Matches saved        5",
                     "Queues saved: ranked 5"):
            self.assertIn(line, summary)

    def test_manifest_records_metadata_beside_raw_files(self):
        with ladder_riot():
            stats = self.collector().run(CollectSettings(players=3, matches_per_player=3))
        records = {r["match_id"]: r for r in self.manifest.records()}
        self.assertEqual(sorted(records), ["NA1_1", "NA1_2", "NA1_3", "NA1_4", "NA1_5"])
        r = records["NA1_4"]
        self.assertEqual((r["source_puuid"], r["source_tier"]), ("p-chal-low", "CHALLENGER"))
        self.assertEqual(records["NA1_3"]["source_puuid"], "p-chal-high")  # first player it was seen for
        self.assertEqual((r["patch"], r["queue_id"], r["tft_set_number"]), ("15.19", 1100, 99))
        self.assertEqual(r["collected_at"], "2026-10-07T15:30:00Z")
        self.assertEqual((r["run_id"], r["platform"], r["region"]), (stats.run_id, "na1", "americas"))
        self.assertEqual(r["bytes"], len(match_body("NA1_4")))

    def test_finished_run_is_archived(self):
        with ladder_riot():
            stats = self.collector().run(CollectSettings(players=3, matches_per_player=3))
        self.assertFalse((self.root / "collections/active.json").exists())
        archived = json.loads((self.root / f"collections/{stats.run_id}.json").read_text())
        self.assertEqual(archived["status"], "complete")
        self.assertEqual(archived["finished_at"], "2026-10-07T15:30:00Z")

    def test_new_run_skips_matches_already_on_disk(self):
        with ladder_riot():
            self.collector().run(CollectSettings(players=3, matches_per_player=3))
        with ladder_riot() as riot:
            stats = self.collector().run(CollectSettings(players=4, matches_per_player=3))
        self.assertEqual(match_downloads(riot), ["NA1_6"])
        self.assertEqual((stats.already_on_disk, stats.matches_saved), (5, 1))
        self.assertEqual(len(self.manifest.records()), 6)  # no duplicate manifest lines

    def test_backfills_manifest_for_raw_files_it_did_not_record(self):
        self.store.save_bytes("NA1_2", match_body("NA1_2"))  # e.g. saved by the smoke test
        with ladder_riot():
            self.collector().run(CollectSettings(players=1, matches_per_player=3))
        r = {r["match_id"]: r for r in self.manifest.records()}["NA1_2"]
        self.assertTrue(r["backfilled"])
        self.assertEqual(r["patch"], "15.19")


class ResumeTests(CollectorTestCase):
    def test_stop_during_downloads_then_resume(self):
        with ladder_riot():
            with self.assertRaises(KeyboardInterrupt):
                self.collector(raise_on="Downloading").run(CollectSettings(players=3, matches_per_player=3))
        state = self.active()
        self.assertEqual(len(state["scanned"]), 3)
        self.assertEqual(state["done"], {})

        with ladder_riot() as riot:
            stats = self.collector().run(CollectSettings(players=50, matches_per_player=20))
        self.assertTrue(any(m.startswith("Resuming run") for m in self.log))
        self.assertFalse(any(c["path"].endswith("/ids") or "/league/" in c["path"] for c in riot.calls))
        self.assertEqual(len(match_downloads(riot)), 5)
        self.assertEqual((stats.status, stats.players_target, stats.matches_saved), ("complete", 3, 5))

    def test_expired_key_stops_run_and_keeps_progress(self):
        riot = ladder_riot()
        riot.routes["/tft/match/v1/matches/NA1_3"] = [(403, {}, b"")]
        with riot, self.assertRaises(RiotExpiredKey):
            self.collector().run(CollectSettings(players=3, matches_per_player=3))
        self.assertEqual(sorted(self.active()["done"]), ["NA1_1", "NA1_2"])

        with ladder_riot() as riot:
            stats = self.collector().run()
        self.assertEqual(match_downloads(riot), ["NA1_3", "NA1_4", "NA1_5"])
        self.assertEqual((stats.status, stats.matches_saved, stats.duplicates_skipped), ("complete", 5, 3))

    def test_run_stopped_before_choosing_players_takes_new_settings(self):
        with FakeRiot().add("/tft/league/v1/challenger", status=401), self.assertRaises(Exception):
            self.collector().run(CollectSettings(players=1, matches_per_player=1))
        with ladder_riot():
            stats = self.collector().run(CollectSettings(players=3, matches_per_player=3))
        self.assertFalse(any(m.startswith("Resuming run") for m in self.log))
        self.assertEqual((stats.players_target, stats.matches_saved), (3, 5))

    def test_fresh_sets_unfinished_run_aside(self):
        with ladder_riot(), self.assertRaises(KeyboardInterrupt):
            self.collector(raise_on="Downloading").run(CollectSettings(players=1, matches_per_player=3))
        old_id = self.active()["run_id"]
        c = self.collector()
        c.now = lambda: datetime(2026, 10, 8, tzinfo=timezone.utc)
        with ladder_riot():
            stats = c.run(CollectSettings(players=1, matches_per_player=3), fresh=True)
        self.assertNotEqual(stats.run_id, old_id)
        self.assertEqual(json.loads((self.root / f"collections/{old_id}.json").read_text())["status"], "abandoned")


class RetryTests(CollectorTestCase):
    def test_transient_failure_is_retried_in_a_second_pass(self):
        riot = ladder_riot()
        # RiotClient makes 1 + 3 attempts per call; the 5th attempt (second pass) succeeds.
        riot.routes["/tft/match/v1/matches/NA1_2"] = [(503, {}, b"")] * 4 + [(200, {}, match_body("NA1_2"))]
        with riot:
            stats = self.collector().run(CollectSettings(players=3, matches_per_player=3))
        self.assertEqual((stats.status, stats.matches_saved, stats.failed_requests), ("complete", 5, 1))
        self.assertTrue(any("Retrying 1 failed request" in m for m in self.log))

    def test_persistent_failure_stays_pending_for_next_run(self):
        riot = ladder_riot()
        riot.routes["/tft/match/v1/matches/NA1_2"] = [(503, {}, b"")]
        with riot:
            stats = self.collector().run(CollectSettings(players=3, matches_per_player=3))
        self.assertEqual((stats.status, stats.matches_saved, stats.pending), ("incomplete", 4, 1))
        self.assertIn("Pending              1 (run again to retry)", stats.summary())
        self.assertEqual(self.active()["status"], "incomplete")

        with ladder_riot() as riot:
            stats = self.collector().run()
        self.assertEqual(match_downloads(riot), ["NA1_2"])
        self.assertEqual((stats.status, stats.matches_saved, stats.failed_requests), ("complete", 5, 2))

    def test_not_found_is_not_retried(self):
        riot = ladder_riot()
        riot.routes["/tft/match/v1/matches/NA1_2"] = [(404, {}, b"")]
        riot.routes["/tft/match/v1/matches/by-puuid/p-gm/ids"] = [(404, {}, b"")]
        with riot:
            stats = self.collector().run(CollectSettings(players=3, matches_per_player=3))
        self.assertEqual(match_downloads(riot).count("NA1_2"), 1)
        self.assertEqual((stats.status, stats.players_scanned, stats.matches_saved, stats.failed_requests),
                         ("complete", 2, 3, 2))
        self.assertEqual(sorted(stats.permanent_failures), ["NA1_2", "p-gm"])


class ManifestTests(unittest.TestCase):
    def test_patch_from_version(self):
        self.assertEqual(patch_from_version("Version 14.23.636.0137 (Nov 19 2024/18:04:02) [PUBLIC] "), "14.23")
        self.assertEqual(patch_from_version("Linux Version 15.19.000.0000"), "15.19")
        self.assertIsNone(patch_from_version("TFT Unreal Version ?.?.?.?"))
        self.assertIsNone(patch_from_version(None))

    def test_line_cut_short_by_a_crash_is_skipped_and_not_glued_to(self):
        with tempfile.TemporaryDirectory() as d:
            m = MatchManifest(Path(d) / "manifest.jsonl")
            m.append({"match_id": "NA1_1"})
            with m.path.open("a") as f:
                f.write('{"match_id": "NA1_')
            m.append({"match_id": "NA1_3"})
            self.assertEqual(m.match_ids(), {"NA1_1", "NA1_3"})


class ScriptTests(CollectorTestCase):
    def test_cli_prints_summary(self):
        from scripts import collect_matches
        with ladder_riot(), redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(collect_matches, "RAW", self.root), \
                mock.patch.object(RiotConfig, "from_env", lambda *a: RiotConfig(api_key=TEST_KEY)):
            code = collect_matches.main(["--players", "3", "--matches", "3"])
        self.assertEqual(code, 0)
        self.assertIn("Matches saved        5", out.getvalue())
        self.assertEqual(len(list((self.root / "matches").iterdir())), 5)


if __name__ == "__main__":
    unittest.main()
