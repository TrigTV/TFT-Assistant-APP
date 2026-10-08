# TFT-Assistant-APP (TFT AI Coach)

Compartment 1 (Riot API Gateway) is implemented. Everything else in the tree
is an empty placeholder for later compartments.

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

## Collecting a raw match archive (Milestone 2)

Run a small test first, then the full collection:

```
python scripts/collect_matches.py --players 5 --matches 5
python scripts/collect_matches.py                 # 50 players x 20 recent matches
```

It reads the Challenger and Grandmaster ladders and takes the top players by
tier, then LP. It pulls each player's recent match IDs and deduplicates them
before downloading anything, then downloads each match it doesn't already have
exactly once. Nothing is normalized or analyzed.

| Output | Contents |
|---|---|
| `storage/raw/riot/matches/<MATCH_ID>.json` | Riot's response, byte-for-byte |
| `storage/raw/riot/manifest.jsonl` | One line per match: run, collected_at, source player and tier, patch, game_version, queue, set, sha256 |
| `storage/raw/riot/collections/` | `active.json` while a run is unfinished; `<run_id>.json` once it's done |

The run saves its progress after every player and every match. If it stops for
any reason (Ctrl+C, an expired key, a crash), run the same command again to
resume. Matches already saved are never requested twice. `--fresh` sets an
unfinished run aside and starts a new one. A request that keeps failing with a
429, a 5xx or a network error is retried in three places: by the client with
backoff, in a second pass at the end of the run, and on the next run. A 404 is
recorded and not retried.

With a development key (100 requests per 2 minutes), 50 x 20 takes about 15
minutes. Some matches report `game_version` as `TFT Unreal Version ?.?.?.?`,
which has no patch number; for those, `patch` is `null` and `tft_set_number`
is still recorded.

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
| `collector.py` | Milestone 2: ladder players to a deduplicated, resumable raw match archive |
| `manifest.py` | Collection metadata kept beside the raw files (`manifest.jsonl`) |

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
