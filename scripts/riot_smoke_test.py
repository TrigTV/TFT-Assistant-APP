"""Check that the Riot API key and gateway work end to end.

    python scripts/riot_smoke_test.py "GameName#TAG"

Authenticates, looks up the player, lists their match IDs, downloads one match
and saves it untouched to storage/raw/riot/matches/. Exit code 0 means
Compartment 1 is working with the current key.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.riot import (AccountService, MatchService, RawMatchStore, RiotClient,  # noqa: E402
                           RiotConfig, RiotError, RiotExpiredKey, StatusService)


def run(riot_id: str, client: RiotClient, store: RawMatchStore, out=sys.stdout) -> bool:
    rows: list[tuple[str, str]] = []

    def step(name, fn):
        try:
            value = fn()
            rows.append((name, "PASS"))
            return value
        except RiotError as e:
            rows.append((name, "FAIL"))
            raise _Stop(e)

    try:
        step("Authentication", StatusService(client).platform_data)
        account = step("Account lookup", lambda: AccountService(client).by_riot_id_string(riot_id))
        matches = MatchService(client)
        ids = step("Match list", lambda: matches.ids_by_puuid(account["puuid"], count=1))
        if not ids:
            rows.append(("Match retrieval", "SKIP"))
            _print(rows, out)
            print(f"\n{riot_id} has no TFT matches; try a player who has played recently.", file=out)
            return False
        resp = step("Match retrieval", lambda: matches.get_response(ids[0]))
        path = store.save_bytes(ids[0], resp.body)
        rows.append(("Raw JSON saved", "PASS"))
    except _Stop as stop:
        _print(rows, out)
        err = stop.error
        print("", file=out)
        print(RiotExpiredKey.HELP if isinstance(err, RiotExpiredKey) else f"{type(err).__name__}: {err}", file=out)
        return False

    _print(rows, out)
    print(f"\nPlayer: {account['gameName']}#{account['tagLine']}", file=out)
    try:
        shown = os.path.relpath(path, ROOT)
    except ValueError:  # Windows: path is on a different drive than the repo
        shown = path
    print(f"Saved:  {shown}", file=out)
    print("\nRiot API ready.", file=out)
    return True


class _Stop(Exception):
    def __init__(self, error: RiotError):
        self.error = error


def _print(rows, out):
    print("RIOT CONNECTION\n", file=out)
    for name, result in rows:
        print(f"{name:<18} {result}", file=out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("riot_id", nargs="?", default=os.environ.get("RIOT_SMOKE_RIOT_ID"),
                   help='Riot ID like "Name#TAG" (or set RIOT_SMOKE_RIOT_ID)')
    p.add_argument("-v", "--verbose", action="store_true", help="log every Riot request")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    if not args.riot_id:
        p.error('give a Riot ID, e.g. riot_smoke_test.py "Name#NA1"')

    try:
        config = RiotConfig.from_env(ROOT / ".env")
    except RiotError as e:
        print(f"Configuration error: {e}")
        return 2
    print(f"Using platform={config.platform} region={config.region}\n")
    ok = run(args.riot_id, RiotClient(config), RawMatchStore(ROOT / "storage/raw/riot/matches"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
