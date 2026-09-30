import hashlib
import io
import json
import re
import tempfile
import unittest

from scripts import collect_high_elo
from services.riot import MatchArchive, RiotClient, RiotConfig, RiotExpiredKey
from services.riot.archive import patch_from_version
from services.riot.collector import (MAX_ATTEMPTS, CollectionRun, CollectSettings, collect,
                                     collection_stats, open_run, select_players)

from .fakes import TEST_KEY, FakeRiot, fixture_bytes

SETTINGS = CollectSettings(players=5, matches_per_player=10)
MATCH_IDS = [f"NA1_10{i}" for i in range(1, 8)]


def entry(puuid, lp, **kw):
    e = {"puuid": puuid, "leaguePoints": lp, "rank": "I", "wins": 40, "losses": 30,
         "veteran": False, "inactive": False, "freshBlood": False, "hotStreak": False}
    e.update(kw)
    return e


def match_bytes(match_id, version="15.19", queue=1100):
    body = fixture_bytes("match_NA1_5000000003.json").replace(b"NA1_5000000003", match_id.encode())
    body = body.replace(b"Version 15.19.", f"Version {version}.".encode())
    return body.replace(b'"queue_id": 1100', f'"queue_id": {queue}'.encode())


class HighEloRiot(FakeRiot):
    """A small ladder whose players share lobbies, the way high Elo players do.

    Picked, best first: c1 1500, c2 1400, c3 1300 (Challenger), g1 700, g2 650
    (Grandmaster; g2 has only a summonerId). g3 has more LP but is inactive.
    11 match IDs are listed, 7 unique.
    """

    LISTS = {
        "p-c1": ["NA1_101", "NA1_102", "NA1_103"],
        "p-c2": ["NA1_101", "NA1_104"],
        "p-c3": ["NA1_102", "NA1_105"],
        "p-g1": ["NA1_103", "NA1_106"],
        "p-g2": ["NA1_107", "NA1_101"],
        "p-g3": ["NA1_199"],
    }

    def __init__(self):
        super().__init__()
        self.challenger = json.dumps({"tier": "CHALLENGER", "leagueId": "l1", "queue": "RANKED_TFT", "entries": [
            entry("p-c2", 1400), entry("p-c1", 1500), entry("p-c3", 1300)]}).encode()
        self.grandmaster = json.dumps({"tier": "GRANDMASTER", "leagueId": "l2", "queue": "RANKED_TFT", "entries": [
            entry("p-g1", 700), entry(None, 650, summonerId="s-g2"), entry("p-g3", 900, inactive=True)]}).encode()
        self.add("/tft/league/v1/challenger", self.challenger)
        self.add("/tft/league/v1/grandmaster", self.grandmaster)
        self.add("/tft/summoner/v1/summoners/s-g2", {"puuid": "p-g2", "id": "s-g2"})
        for puuid, ids in self.LISTS.items():
            self.add(f"/tft/match/v1/matches/by-puuid/{puuid}/ids", ids)
        self.bodies = {m: match_bytes(m) for m in MATCH_IDS}
        self.bodies["NA1_105"] = match_bytes("NA1_105", version="15.18")
        self.bodies["NA1_106"] = match_bytes("NA1_106", queue=1090)
        for m, body in self.bodies.items():
            self.add(f"/tft/match/v1/matches/{m}", body)

    def paths(self, prefix):
        return [c["path"] for c in self.calls if c["path"].startswith(prefix)]

    def match_downloads(self):
        return [p.rsplit("/", 1)[1] for p in self.paths("/tft/match/v1/matches/NA1_")]


class Env:
    """A temp archive plus a client whose waits are recorded instead of slept."""

    def __init__(self, max_retries=3):
        self._dir = tempfile.TemporaryDirectory()
        self.archive = MatchArchive(self._dir.name)
        self.client_sleeps, self.collect_sleeps, self.progress = [], [], []
        self.max_retries = max_retries

    def client(self):
        return RiotClient(RiotConfig(api_key=TEST_KEY, max_retries=self.max_retries), sleep=self.client_sleeps.append)

    def collect(self, settings=SETTINGS, new=False, client=None, on_progress=None):
        run, _ = open_run(self.archive, settings, "na1", "americas", new=new)
        return run, collect(client or self.client(), self.archive, run, sleep=self.collect_sleeps.append,
                            on_progress=on_progress or self.progress.append)

    def cleanup(self):
        self._dir.cleanup()


class CollectorTestCase(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.cleanup)

    def assertRow(self, summary, name, value, note=""):
        """A summary table row, ignoring column padding."""
        pattern = rf"(?m)^{re.escape(name)}\s+{re.escape(str(value))}" + (rf"\s+{re.escape(note)}" if note else "") + "$"
        self.assertRegex(summary, pattern)


class FullRunTests(CollectorTestCase):
    def test_ladder_to_raw_archive(self):
        with HighEloRiot() as riot:
            run, stats = self.env.collect()
        archive = self.env.archive

        # 1-2: both ladders fetched from the platform host and kept exactly as Riot sent them
        self.assertEqual((run.dir / "challenger_league.json").read_bytes(), riot.challenger)
        self.assertEqual((run.dir / "grandmaster_league.json").read_bytes(), riot.grandmaster)
        self.assertEqual({c["host"] for c in riot.calls if "/league/" in c["path"]}, {"na1.api.riotgames.com"})
        players = run.state["players"]
        self.assertEqual([(p["puuid"], p["tier"], p["league_points"]) for p in players], [
            ("p-c1", "CHALLENGER", 1500), ("p-c2", "CHALLENGER", 1400), ("p-c3", "CHALLENGER", 1300),
            ("p-g1", "GRANDMASTER", 700), ("p-g2", "GRANDMASTER", 650)])

        # 3: the entry without a puuid was converted through tft-summoner-v1
        self.assertEqual(riot.paths("/tft/summoner/"), ["/tft/summoner/v1/summoners/s-g2"])

        # 4: recent match IDs per player, regional host, requested count
        id_calls = [c for c in riot.calls if c["path"].endswith("/ids")]
        self.assertEqual(len(id_calls), 5)
        self.assertEqual({c["host"] for c in id_calls}, {"americas.api.riotgames.com"})
        self.assertEqual({c["query"]["count"][0] for c in id_calls}, {"10"})

        # 5-6: deduplicated before downloading; each match requested exactly once
        self.assertEqual(sorted(riot.match_downloads()), MATCH_IDS)

        # 7: raw JSON byte for byte
        for m in MATCH_IDS:
            self.assertEqual(archive.raw.load_bytes(m), riot.bodies[m])

        # 8: metadata recorded alongside, never inside, the raw file
        meta = archive.meta("NA1_104")
        self.assertEqual(meta["match_id"], "NA1_104")
        self.assertEqual((meta["patch"], meta["queue_id"], meta["tft_set_number"]), ("15.19", 1100, 99))
        self.assertEqual(meta["source_player"],
                         {"puuid": "p-c2", "tier": "CHALLENGER", "league_points": 1400, "platform": "na1"})
        self.assertEqual((meta["region"], meta["run_id"]), ("americas", run.run_id))
        self.assertRegex(meta["collected_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(meta["played_at"], "2025-09-30T03:13:20Z")
        self.assertEqual(meta["raw_sha256"], hashlib.sha256(riot.bodies["NA1_104"]).hexdigest())
        self.assertEqual(archive.meta("NA1_101")["source_player"]["puuid"], "p-c1")  # highest ranked lister
        self.assertEqual(archive.meta("NA1_107")["source_player"]["puuid"], "p-g2")
        self.assertEqual(archive.meta("NA1_105")["patch"], "15.18")

        # 11: statistics
        self.assertTrue(stats.finished)
        self.assertEqual((stats.players_scanned, stats.players_selected, stats.puuid_lookups), (5, 5, 1))
        self.assertEqual((stats.match_ids_found, stats.unique_matches, stats.duplicates_skipped), (11, 7, 4))
        self.assertEqual((stats.matches_saved, stats.already_archived, stats.failed_requests), (7, 0, 0))
        self.assertEqual(stats.api_requests, 2 + 1 + 5 + 7)
        self.assertEqual(dict(stats.by_patch), {"15.19": 6, "15.18": 1})
        self.assertEqual(dict(stats.by_queue), {1100: 6, 1090: 1})
        summary = stats.summary()
        self.assertRow(summary, "Players scanned", "5 / 5")
        self.assertRow(summary, "PUUID lookups", 1, "summoner ID -> PUUID")
        self.assertRow(summary, "Match IDs found", 11)
        self.assertRow(summary, "Duplicates skipped", 4, "listed by more than one player")
        self.assertRow(summary, "Matches saved", 7)
        self.assertRow(summary, "Failed requests", 0)
        self.assertIn("Saved by patch:  15.19: 6, 15.18: 1", summary)
        self.assertIn("Saved by queue:  ranked (1100): 6, normal (1090): 1", summary)
        self.assertTrue(summary.endswith("Collection complete."))

        on_disk = json.loads((run.dir / "state.json").read_bytes())
        self.assertIsNotNone(on_disk["finished_at"])
        self.assertEqual(sorted(p.name for p in archive.raw.root.iterdir()), [f"{m}.json" for m in MATCH_IDS])

    def test_matches_already_in_archive_are_not_requested(self):
        self.env.archive.raw.save_bytes("NA1_101", match_bytes("NA1_101"))  # e.g. saved in Milestone 1
        with HighEloRiot() as riot:
            _, stats = self.env.collect()
        self.assertNotIn("NA1_101", riot.match_downloads())
        self.assertEqual((stats.matches_saved, stats.already_archived), (6, 1))

    def test_a_later_run_downloads_nothing_already_saved(self):
        with HighEloRiot():
            first, _ = self.env.collect()
        with HighEloRiot() as riot:
            second, stats = self.env.collect()
        self.assertNotEqual(first.run_id, second.run_id)  # finished runs are not resumed
        self.assertEqual(riot.match_downloads(), [])
        self.assertEqual((stats.matches_saved, stats.already_archived), (0, 7))
        self.assertEqual(self.env.archive.meta("NA1_101")["run_id"], first.run_id)


class FailureTests(CollectorTestCase):
    def test_transient_failure_is_retried_in_a_later_round(self):
        riot = HighEloRiot()
        riot.routes["/tft/match/v1/matches/NA1_103"] = [(503, {}, b"")] * 4 + [(200, {}, riot.bodies["NA1_103"])]
        with riot:
            _, stats = self.env.collect()
        self.assertEqual(riot.match_downloads().count("NA1_103"), 5)  # 4 inside the client, then the retry round
        self.assertEqual(self.env.client_sleeps, [1.0, 2.0, 4.0])
        self.assertEqual(self.env.collect_sleeps, [30.0])
        self.assertEqual((stats.matches_saved, stats.failed_requests, stats.recovered), (7, 1, 1))
        self.assertEqual(stats.api_retries, 3)
        self.assertEqual(stats.not_collected, {})
        self.assertRow(stats.summary(), "Failed requests", 1, "1 recovered on retry")
        self.assertRow(stats.summary(), "API requests", 19, "3 retried by the client")

    def test_gives_up_after_max_attempts(self):
        self.env.max_retries = 0
        riot = HighEloRiot()
        riot.routes["/tft/match/v1/matches/NA1_103"] = [(500, {}, b"")]
        with riot:
            _, stats = self.env.collect()
        self.assertEqual(riot.match_downloads().count("NA1_103"), MAX_ATTEMPTS)
        self.assertEqual(len(self.env.collect_sleeps), MAX_ATTEMPTS - 1)
        self.assertTrue(stats.finished)
        self.assertEqual((stats.matches_saved, stats.failed_requests, stats.recovered), (6, MAX_ATTEMPTS, 0))
        self.assertEqual(list(stats.not_collected), ["NA1_103"])
        self.assertIn("RiotServerError", stats.not_collected["NA1_103"])
        self.assertIn("Not collected:\n  NA1_103  RiotServerError", stats.summary())

    def test_404_is_not_retried_and_does_not_stop_the_rest(self):
        riot = HighEloRiot()
        riot.routes["/tft/match/v1/matches/NA1_104"] = [(404, {}, b"")]
        with riot:
            _, stats = self.env.collect()
        self.assertEqual(riot.match_downloads().count("NA1_104"), 1)
        self.assertEqual(self.env.collect_sleeps, [])
        self.assertEqual(stats.matches_saved, 6)
        self.assertIn("RiotNotFound", stats.not_collected["NA1_104"])

    def test_body_that_is_not_the_match_is_not_stored(self):
        riot = HighEloRiot()
        riot.routes["/tft/match/v1/matches/NA1_105"] = [(200, {}, match_bytes("NA1_999"))]
        with riot:
            _, stats = self.env.collect()
        self.assertFalse(self.env.archive.has("NA1_105"))
        self.assertIsNone(self.env.archive.meta("NA1_105"))
        self.assertIn("ValueError", stats.not_collected["NA1_105"])

    def test_failed_player_is_retried(self):
        riot = HighEloRiot()
        riot.routes["/tft/match/v1/matches/by-puuid/p-c3/ids"] = [(503, {}, b"")] * 4 + [(200, {}, b'["NA1_105"]')]
        with riot:
            _, stats = self.env.collect()
        self.assertEqual((stats.players_scanned, stats.recovered), (5, 1))
        self.assertIn("NA1_105", riot.match_downloads())


class ResumeTests(CollectorTestCase):
    def test_expired_key_stops_the_run_and_the_next_start_resumes_it(self):
        riot = HighEloRiot()
        riot.routes["/tft/match/v1/matches/NA1_104"] = [(403, {}, b"")]
        with riot, self.assertRaises(RiotExpiredKey):
            self.env.collect()
        self.assertEqual(riot.match_downloads(), ["NA1_101", "NA1_102", "NA1_103", "NA1_104"])

        with HighEloRiot() as riot:
            run, stats = self.env.collect()
        self.assertEqual(riot.paths("/tft/league/"), [])  # same players, not re-picked
        self.assertEqual([c for c in riot.calls if c["path"].endswith("/ids")], [])
        self.assertEqual(riot.match_downloads(), ["NA1_104", "NA1_105", "NA1_106", "NA1_107"])
        self.assertTrue(stats.finished)
        self.assertEqual((stats.matches_saved, stats.already_archived), (7, 0))  # all credited to this run
        self.assertEqual(stats.api_requests, (2 + 1 + 5 + 4) + 4)
        self.assertEqual(len(list(self.env.archive.runs_dir.iterdir())), 1)

    def test_interrupt_keeps_scanned_players(self):
        def stop_after_second_player(msg):
            if msg.startswith("[2/5]"):
                raise KeyboardInterrupt

        with HighEloRiot(), self.assertRaises(KeyboardInterrupt):
            self.env.collect(on_progress=stop_after_second_player)
        run = CollectionRun.find_unfinished(self.env.archive.runs_dir, SETTINGS, "na1", "americas")
        stats = collection_stats(self.env.archive, run)
        self.assertEqual((stats.players_scanned, stats.players_pending, stats.matches_saved), (2, 3, 0))
        self.assertIn("Incomplete: 3 players to scan", stats.summary())

        with HighEloRiot() as riot:
            _, stats = self.env.collect()
        scanned = [p.split("/")[-2] for p in riot.paths("/tft/match/v1/matches/by-puuid/")]
        self.assertEqual(scanned, ["p-c3", "p-g1", "p-g2"])
        self.assertEqual(stats.matches_saved, 7)

    def test_only_a_run_with_the_same_settings_is_resumed(self):
        archive = self.env.archive
        run, resumed = open_run(archive, SETTINGS, "na1", "americas")
        self.assertFalse(resumed)
        again, resumed = open_run(archive, SETTINGS, "na1", "americas")
        self.assertEqual((again.run_id, resumed), (run.run_id, True))
        for settings, platform in ((CollectSettings(players=6, matches_per_player=10), "na1"), (SETTINGS, "kr")):
            other, resumed = open_run(archive, settings, platform, "americas")
            self.assertFalse(resumed)
            self.assertNotEqual(other.run_id, run.run_id)
        fresh, resumed = open_run(archive, SETTINGS, "na1", "americas", new=True)
        self.assertFalse(resumed)
        self.assertNotEqual(fresh.run_id, run.run_id)


class SelectionAndParsingTests(unittest.TestCase):
    def test_select_players(self):
        entries = [dict(entry("gm-high", 999), tier="GRANDMASTER"), dict(entry("c-low", 800), tier="CHALLENGER"),
                   dict(entry("c-high", 1200), tier="CHALLENGER"), dict(entry("c-high", 1200), tier="CHALLENGER"),
                   dict(entry("c-idle", 5000, inactive=True), tier="CHALLENGER"),
                   dict(entry(None, 900, summonerId="s1"), tier="GRANDMASTER"), dict(entry(None, 1), tier="MASTER")]
        picked = select_players(entries, limit=4)
        self.assertEqual([p["puuid"] or p["summoner_id"] for p in picked], ["c-high", "c-low", "gm-high", "s1"])
        self.assertEqual(len(select_players(entries, limit=2)), 2)

    def test_settings(self):
        self.assertEqual(CollectSettings(tiers=("grandmaster", "challenger")).tiers, ("challenger", "grandmaster"))
        for bad in (dict(tiers=("diamond",)), dict(tiers=()), dict(players=0), dict(matches_per_player=0)):
            with self.assertRaises(ValueError):
                CollectSettings(**bad)

    def test_patch_from_version(self):
        self.assertEqual(patch_from_version("Linux Version 15.19.708.8403 (Sep 26 2025/17:40:54) [PUBLIC]"), "15.19")
        self.assertEqual(patch_from_version("Version 14.3.559.3548 (Feb 01 2024/17:40:54) [PUBLIC] <Releases/14.3>"),
                         "14.3")
        self.assertIsNone(patch_from_version(None))
        self.assertIsNone(patch_from_version("unknown"))


class CollectScriptTests(CollectorTestCase):
    def run_script(self, riot, **kw):
        out = io.StringIO()
        with riot:
            code = collect_high_elo.run(self.env.client(), self.env.archive, SETTINGS, out=out,
                                        sleep=self.env.collect_sleeps.append, **kw)
        return code, out.getvalue()

    def test_small_collection(self):
        code, out = self.run_script(HighEloRiot())
        self.assertEqual(code, 0)
        self.assertIn("Starting run", out)
        self.assertIn("top 5 Challenger + Grandmaster players, up to 10 matches each (na1 / americas)", out)
        self.assertRegex(out, r"\[1/5\] Challenger 1500 LP: 3 match IDs, 3 new")
        self.assertRegex(out, r"\[2/5\] Challenger 1400 LP: 2 match IDs, 1 new")
        self.assertIn("Collection complete.", out)
        run_dir = next(self.env.archive.runs_dir.iterdir())
        self.assertRow((run_dir / "summary.txt").read_text(), "Matches saved", 7)

    def test_expired_key_then_resume(self):
        riot = HighEloRiot()
        riot.routes["/tft/match/v1/matches/NA1_102"] = [(403, {}, b"")]
        code, out = self.run_script(riot)
        self.assertEqual(code, 1)
        self.assertIn("Your development API key may have expired.", out)
        self.assertIn("Run the same command again to resume.", out)
        self.assertIn("Incomplete: 0 players to scan and 6 matches to download so far.", out)

        code, out = self.run_script(HighEloRiot())
        self.assertEqual(code, 0)
        self.assertTrue(re.search(r"Resuming run \S+: 5/5 players scanned, 1 matches saved so far", out), out)
        self.assertIn("Collection complete.", out)


if __name__ == "__main__":
    unittest.main()
