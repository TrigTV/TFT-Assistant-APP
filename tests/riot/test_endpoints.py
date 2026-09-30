import json
import unittest

from services.riot import (AccountService, LeagueService, MatchService, RiotClient, RiotConfig,
                           SummonerService, parse_riot_id)
from services.riot.endpoints.league import format_rank

from .fakes import TEST_KEY, FakeRiot, fixture_bytes, standard_riot

PUUID = json.loads(fixture_bytes("account_by_riot_id.json"))["puuid"]


def client(**kw):
    return RiotClient(RiotConfig(api_key=TEST_KEY, **kw))


class AccountTests(unittest.TestCase):
    def test_parse_riot_id(self):
        self.assertEqual(parse_riot_id(" Aman#NA1 "), ("Aman", "NA1"))
        self.assertEqual(parse_riot_id("a#b#EUW"), ("a#b", "EUW"))
        for bad in ("Aman", "#NA1", "Aman#"):
            with self.assertRaises(ValueError):
                parse_riot_id(bad)

    def test_riot_id_to_puuid(self):
        with standard_riot() as riot:
            account = AccountService(client()).by_riot_id_string("Aman#NA1")
        self.assertEqual(account["puuid"], PUUID)
        self.assertEqual(riot.calls[0]["host"], "americas.api.riotgames.com")

    def test_sea_accounts_go_through_asia(self):
        with standard_riot() as riot:
            AccountService(client(platform="oc1", region="sea")).by_riot_id("Aman", "NA1")
        self.assertEqual(riot.calls[0]["host"], "asia.api.riotgames.com")

    def test_names_are_url_encoded(self):
        with FakeRiot().add("/riot/account/v1/accounts/by-riot-id/Big Péngu/?#/KR1", {"puuid": "x"}) as riot:
            AccountService(client()).by_riot_id("Big Péngu/?#", "KR1")
        self.assertEqual(riot.calls[0]["raw_path"],
                         "/riot/account/v1/accounts/by-riot-id/Big%20P%C3%A9ngu%2F%3F%23/KR1")


class MatchTests(unittest.TestCase):
    def test_ids_by_puuid_is_regional_with_params(self):
        with standard_riot() as riot:
            ids = MatchService(client(platform="euw1", region="europe")).ids_by_puuid(
                PUUID, count=5, start_time=1700000000)
        self.assertEqual(ids[0], "NA1_5000000003")
        call = riot.calls[0]
        self.assertEqual(call["host"], "europe.api.riotgames.com")
        self.assertEqual(call["query"], {"start": ["0"], "count": ["5"], "startTime": ["1700000000"]})

    def test_get_match_returns_untouched_bytes(self):
        with standard_riot():
            resp = MatchService(client()).get_response("NA1_5000000003")
        self.assertEqual(resp.body, fixture_bytes("match_NA1_5000000003.json"))
        self.assertEqual(set(resp.json()), {"metadata", "info"})


class LeagueAndSummonerTests(unittest.TestCase):
    def test_ranked_entry_picks_standard_queue_on_platform_host(self):
        with standard_riot() as riot:
            entry = LeagueService(client()).ranked_entry(PUUID)
        self.assertEqual((entry["tier"], entry["rank"], entry["leaguePoints"]), ("DIAMOND", "II", 42))
        self.assertEqual(riot.calls[0]["host"], "na1.api.riotgames.com")
        self.assertEqual(format_rank(entry), "Diamond II 42 LP")
        self.assertEqual(format_rank({"tier": "CHALLENGER", "rank": "I", "leaguePoints": 900}), "Challenger 900 LP")
        self.assertEqual(format_rank(None), "Unranked")

    def test_unranked(self):
        with FakeRiot().add(f"/tft/league/v1/by-puuid/{PUUID}", []):
            self.assertIsNone(LeagueService(client()).ranked_entry(PUUID))

    def test_apex_leagues(self):
        with FakeRiot().add("/tft/league/v1/challenger", {"tier": "CHALLENGER", "entries": []}) as riot:
            self.assertEqual(LeagueService(client(platform="kr", region="asia")).challenger()["tier"], "CHALLENGER")
        self.assertEqual(riot.calls[0]["host"], "kr.api.riotgames.com")

    def test_summoner_route(self):
        with FakeRiot().add(f"/tft/summoner/v1/summoners/by-puuid/{PUUID}", {"puuid": PUUID}) as riot:
            SummonerService(client(platform="kr", region="asia")).by_puuid(PUUID)
        self.assertEqual(riot.calls[0]["host"], "kr.api.riotgames.com")
