"""Collect a raw archive of high-Elo TFT matches (Milestone 2).

    python scripts/collect_matches.py --players 5 --matches 5    # small test first
    python scripts/collect_matches.py                            # 50 players x 20 matches

Takes the top players from the Challenger and Grandmaster ladders, pulls their
recent match IDs, deduplicates them, and downloads each unseen match once into
storage/raw/riot/matches/ (untouched) with a line per match in
storage/raw/riot/manifest.jsonl.

Stopping (Ctrl+C, expired key, crash) keeps progress. Running the command again
resumes the unfinished run; --fresh sets it aside and starts a new one.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.riot import RawMatchStore, RiotClient, RiotConfig, RiotError, RiotExpiredKey  # noqa: E402
from services.riot.collector import CollectSettings, Collector  # noqa: E402
from services.riot.manifest import MatchManifest  # noqa: E402

RAW = ROOT / "storage/raw/riot"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--players", type=int, default=50, help="how many top ladder players (default 50)")
    p.add_argument("--matches", type=int, default=20, help="recent match IDs per player (default 20)")
    p.add_argument("--fresh", action="store_true", help="start a new run instead of resuming")
    p.add_argument("-v", "--verbose", action="store_true", help="log every Riot request")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    try:
        config = RiotConfig.from_env(ROOT / ".env")
    except RiotError as e:
        print(f"Configuration error: {e}")
        return 2
    print(f"Using platform={config.platform} region={config.region}\n")

    collector = Collector(RiotClient(config), store=RawMatchStore(RAW / "matches"),
                          manifest=MatchManifest(RAW / "manifest.jsonl"), state_dir=RAW / "collections",
                          progress=lambda msg: print(msg, flush=True))
    try:
        stats = collector.run(CollectSettings(players=args.players, matches_per_player=args.matches),
                              fresh=args.fresh)
    except KeyboardInterrupt:
        print("\nStopped. Progress is saved; run the same command to resume.")
        return 130
    except RiotExpiredKey:
        print(f"\n{RiotExpiredKey.HELP}\nProgress is saved; run the same command to resume.")
        return 1
    except RiotError as e:
        print(f"\n{type(e).__name__}: {e}\nProgress is saved; run the same command to resume.")
        return 1

    print("\n" + stats.summary())
    return 0 if stats.status == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
