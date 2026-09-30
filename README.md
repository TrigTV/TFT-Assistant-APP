# TFT-Assistant-APP (TFT AI Coach)

Compartment 1 (Riot API Gateway) and Milestone 2 (high Elo raw match
collection) are implemented. Everything else in the tree is an empty
placeholder for later compartments.

Requires Python 3.10+ and one package, Riot Watcher (MIT):

```
python -m pip install -r requirements.txt
```

## Daily workflow with a development key

1. Get today's key from https://developer.riotgames.com (dev keys expire every 24 hours).
2. `cp .env.example .env` (first time only) and set `RIOT_API_KEY=RGAPI-...`.
3. Run the smoke test with any Riot ID that has recent TFT games:

   ```
   python scripts/riot_smoke_test.py "GameName#TAG"
   ```

   Expected:

   ```
   RIOT CONNECTION

   Authentication     PASS
   Account lookup     PASS
   Match list         PASS
   Match retrieval    PASS
   Raw JSON saved     PASS

   Riot API ready.
   ```

   One match is saved untouched to `storage/raw/riot/matches/<MATCH_ID>.json`.
   Add `-v` to log each request. A `403` prints the "key may have expired" message.

## High Elo collection (Milestone 2)

Builds a library of raw TFT matches from the top of the ladder. Start small,
check the summary, then scale:

```
python scripts/collect_high_elo.py --players 5 --matches-per-player 5   # small test
python scripts/collect_high_elo.py                                     # 50 players x 20 matches
```

```
Challenger + Grandmaster ladders   2 requests, saved as received in the run folder
        |  top N active players: Challenger first, then by LP
PUUIDs                             from the ladder; summonerId -> PUUID only for entries without one
        |
Match IDs                          1 request per player, most recent first
        |
Deduplicate                        across players and against everything already saved
        |
Match details                      1 request per unseen match
        |
Raw archive                        Riot's JSON untouched, plus a metadata record
```

```
storage/raw/riot/
  matches/NA1_123.json        Riot's response, byte for byte (same folder as Milestone 1)
  match_meta/NA1_123.json     patch, queue, set, played_at, collected_at, region, run_id,
                              source player (puuid, tier, LP, platform), sha256 of the raw file
  runs/<run_id>/
    challenger_league.json    the ladders players were picked from, as Riot sent them
    grandmaster_league.json
    state.json                resume point: players, their match IDs, failures, counters
    summary.txt               the statistics printed at the end
```

**Stopping and resuming.** Ctrl+C, an expired key or a crash keeps everything
collected so far. Running the same command again resumes the newest unfinished
run with the same settings, without re-reading the ladder or match lists.
`--new` starts over from a fresh ladder. A match already in the archive is never
downloaded again, in any run.

**Retries.** `RiotClient` already retries 429, 5xx and timeouts with backoff. A
request that still fails is recorded and tried again in a later round, up to 3
attempts per player or match, counting resumes. 404s, 400s and responses that
aren't the requested match are not retried. 401 or 403 stops the run at once,
since every later call would fail too: update the key, then rerun to resume.

**What is kept.** Every match a player listed is saved, whatever its queue
(ranked, normal, Double Up). The queue and patch are in each metadata record,
and filtering them is Milestone 3's job.

**Time with a dev key.** The dev key allows 100 requests every 2 minutes. 50
players x 20 matches is about 600 requests, so expect roughly 12 minutes. Pauses
with no output are Riot Watcher waiting for the limit to reset.

The run ends with a summary. This one is from a simulated NA ladder, so the
numbers are only indicative:

```
HIGH ELO COLLECTION  na1 / americas  run 20260930T045656Z

Players scanned       50 / 50
PUUID lookups               0
Match IDs found          1000
Duplicates skipped        445   listed by more than one player
Unique matches            555
Already in archive          0   saved earlier, not downloaded again
Matches saved             554
Failed requests             3   1 recovered on retry
API requests              616   6 retried by the client

Saved by patch:  15.19: 554
Saved by queue:  ranked (1100): 524, normal (1090): 24, double up (1160): 6

Not collected:
  NA1_5100005790  RiotNotFound: Riot API returned 404 Not Found for ...

Collection complete.
```

## Production key

Replace the value of `RIOT_API_KEY` in the environment. Nothing else changes:
the rate limiter adopts whatever limits Riot reports in its response headers.

## How the gateway is built

```
Our code -> endpoint services -> RiotClient -> Riot Watcher -> Riot API
```

`RiotClient` is the only file that imports Riot Watcher. Riot Watcher sends the
key header and enforces Riot's app and method rate limits from response
headers. `RiotClient` adds shard selection from `RiotConfig`, retries for
429/5xx/timeouts, our error types, and raw response bodies for storage.
Two TFT endpoints Riot Watcher 3.3.1 doesn't wrap (league by PUUID, platform
status) are added in `client.py` through Riot Watcher's own request chain.
Replacing Riot Watcher later means rewriting `client.py` only.

## Layout of `services/riot/`

| Module | Job |
|---|---|
| `config.py` | `RiotConfig` (api_key, platform, region) from env / `.env` |
| `routing.py` | Which shard each call uses (platform, region, account region) |
| `client.py` | `RiotClient`: wraps Riot Watcher; timeouts, retries, error mapping, logging |
| `errors.py` | `RiotUnauthorized`, `RiotExpiredKey`, `RiotNotFound`, `RiotRateLimited`, `RiotServerError`, ... |
| `endpoints/` | `AccountService`, `MatchService`, `LeagueService`, `SummonerService`, `StatusService` |
| `raw_store.py` | Raw match JSON on disk, keyed by match ID (the cache boundary) |
| `pipeline.py` | First data flow: Riot ID to saved raw matches, skipping ones already on disk |
| `archive.py` | `MatchArchive`: raw matches plus a metadata record per match |
| `collector.py` | Milestone 2: ladders to raw archive, with dedupe, retries, resumable runs and statistics |

Usage:

```python
from services.riot import RiotClient
from services.riot.pipeline import fetch_player_matches

client = RiotClient.from_env()
print(fetch_player_matches(client, "GameName#TAG", count=20).summary())
```

Routing: account and TFT match calls use `RIOT_REGION` (americas, europe, asia,
sea); summoner, league and status calls use `RIOT_PLATFORM` (na1, euw1, kr, ...).
Account lookups for `sea` go through `asia`, since account-v1 has no sea cluster.

## Tests

```
python -m unittest discover -s tests -t .
```

Tests run the real RiotClient and Riot Watcher code against synthetic fixtures in
`tests/fixtures/`; only the network layer is faked.
