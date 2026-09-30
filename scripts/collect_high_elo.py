"""Collect raw TFT matches from the Challenger and Grandmaster ladders (Milestone 2).

    python scripts/collect_high_elo.py --players 5 --matches-per-player 5   # small test first
    python scripts/collect_high_elo.py                                     # 50 players x 20 matches

Each match is saved once, untouched, to storage/raw/riot/matches/, with a
metadata record (patch, queue, collection date, source player) in
storage/raw/riot/match_meta/. Matches already in the archive are never
downloaded again. Stop at any time; the same command resumes the run.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.riot import MatchArchive, RiotClient, RiotConfig, RiotError, RiotExpiredKey  # noqa: E402
from services.riot.collector import (APEX_TIERS, CollectSettings, collect,  # noqa: E402
                                     collection_stats, open_run)

RESUME_HINT = "Run the same command again to resume."


def run(client: RiotClient, archive: MatchArchive, settings: CollectSettings, *, new: bool = False,
        out=sys.stdout, sleep=time.sleep) -> int:
    def say(msg: str) -> None:
        print(msg, file=out, flush=True)

    config = client.config
    this_run, resumed = open_run(archive, settings, config.platform, config.region, new=new)
    if resumed:
        before = collection_stats(archive, this_run)
        say(f"Resuming run {this_run.run_id}: {before.players_scanned}/{before.players_selected} players "
            f"scanned, {before.matches_saved} matches saved so far.\n")
    else:
        say(f"Starting run {this_run.run_id}: top {settings.players} "
            f"{' + '.join(t.title() for t in settings.tiers)} players, up to {settings.matches_per_player} "
            f"matches each ({config.platform} / {config.region}).\n")

    code, note = 0, None
    try:
        collect(client, archive, this_run, on_progress=say, sleep=sleep)
    except KeyboardInterrupt:
        code, note = 130, f"Stopped. {RESUME_HINT}"
    except RiotExpiredKey:
        code, note = 1, f"{RiotExpiredKey.HELP}\nThen: {RESUME_HINT}"
    except RiotError as e:
        code, note = 1, f"{type(e).__name__}: {e}\n{RESUME_HINT}"

    summary = collection_stats(archive, this_run).summary()
    this_run.save_file("summary.txt", summary.encode("utf-8"))
    say("\n" + summary)
    if note:
        say("\n" + note)
    return code


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--players", type=int, default=50, help="how many top players to scan (default 50)")
    p.add_argument("--matches-per-player", type=int, default=20,
                   help="recent match IDs to list per player (default 20)")
    p.add_argument("--tiers", default="challenger,grandmaster",
                   help=f"comma-separated ladders to pick from: {','.join(APEX_TIERS)} (default challenger,grandmaster)")
    p.add_argument("--new", action="store_true", help="start a new run instead of resuming an unfinished one")
    p.add_argument("-v", "--verbose", action="store_true", help="log every Riot request")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        settings = CollectSettings(players=args.players, matches_per_player=args.matches_per_player,
                                   tiers=tuple(t.strip().lower() for t in args.tiers.split(",") if t.strip()))
    except ValueError as e:
        p.error(str(e))

    try:
        config = RiotConfig.from_env(ROOT / ".env")
    except RiotError as e:
        print(f"Configuration error: {e}")
        return 2
    return run(RiotClient(config), MatchArchive(ROOT / "storage/raw/riot"), settings, new=args.new)


if __name__ == "__main__":
    sys.exit(main())
