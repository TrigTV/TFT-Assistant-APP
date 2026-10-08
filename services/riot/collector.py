"""Milestone 2: build a raw archive of high-Elo TFT matches.

    Challenger + Grandmaster ladders -> top N players (PUUIDs)
        -> recent match IDs per player -> deduplicate
        -> download each unseen match once -> raw JSON + one manifest line

No normalization or analysis. Raw files are saved byte-for-byte by
RawMatchStore; collection metadata goes in MatchManifest beside them.

Progress lives in a state file that is rewritten after every player and every
match, so stopping the program and running it again carries on where it left
off. Failures are retried at three levels: RiotClient retries 429/5xx/network
errors with backoff; a run makes a second pass over requests that still failed
transiently; anything left after that stays pending for the next run.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .client import RiotClient
from .endpoints import LeagueService, MatchService, SummonerService
from .errors import (RiotConfigError, RiotError, RiotNetworkError, RiotRateLimited, RiotServerError,
                     RiotUnauthorized)
from .manifest import QUEUE_NAMES, MatchManifest, match_metadata
from .raw_store import RawMatchStore

TIERS = ("CHALLENGER", "GRANDMASTER")
# Worth asking again later. Anything else (404, 400) will fail the same way every time.
TRANSIENT = (RiotServerError, RiotNetworkError, RiotRateLimited)
# The key or config is wrong: stop the run (progress is kept) instead of failing every request.
FATAL = (RiotUnauthorized, RiotConfigError)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class CollectSettings:
    players: int = 50
    matches_per_player: int = 20


class CollectionState:
    """One run's progress, saved as JSON after every step."""

    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data

    @classmethod
    def new(cls, path: Path, *, run_id: str, started_at: str, settings: CollectSettings,
            platform: str, region: str) -> "CollectionState":
        return cls(path, {
            "run_id": run_id, "status": "running", "started_at": started_at, "finished_at": None,
            "platform": platform, "region": region,
            "settings": {"players": settings.players, "matches_per_player": settings.matches_per_player},
            "ladder": {},           # tier -> players on that ladder when seeded
            "players": None,        # [{puuid, tier, league_points}], fixed once seeded
            "scanned": {},          # puuid -> number of match IDs returned
            "player_errors": {},    # puuid -> {error, transient}
            "matches": {},          # match_id -> source puuid (first player it was seen for)
            "match_ids_found": 0,   # every ID returned, duplicates included
            "done": {},             # match_id -> "saved" | "on_disk"
            "match_errors": {},     # match_id -> {error, transient}
            "failed_requests": 0,
        })

    @classmethod
    def load(cls, path: Path) -> "CollectionState | None":
        if not path.is_file():
            return None
        return cls(path, json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path | None = None) -> None:
        path = path or self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # --- what is left to do ---------------------------------------------------

    def _pending(self, keys, done, errors) -> list[str]:
        return [k for k in keys if k not in done and errors.get(k, {}).get("transient", True)]

    def pending_players(self) -> list[dict]:
        players = {p["puuid"]: p for p in self.data["players"] or []}
        return [players[k] for k in self._pending(players, self.data["scanned"], self.data["player_errors"])]

    def pending_matches(self) -> list[str]:
        return self._pending(self.data["matches"], self.data["done"], self.data["match_errors"])

    def transient_errors(self) -> int:
        return sum(e["transient"] for errs in (self.data["player_errors"], self.data["match_errors"])
                   for e in errs.values())


@dataclass
class CollectionStats:
    run_id: str
    status: str
    ladder: dict[str, int]
    players_target: int
    players_scanned: int
    match_ids_found: int
    duplicates_skipped: int
    unique_matches: int
    already_on_disk: int
    matches_saved: int
    failed_requests: int
    pending: int
    permanent_failures: dict[str, str] = field(default_factory=dict)
    queues: Counter = field(default_factory=Counter)

    def summary(self) -> str:
        lines = [f"COLLECTION {self.run_id}  {self.status}", ""]
        if self.ladder:
            lines += ["Ladder: " + ", ".join(f"{n} {t.title()}" for t, n in self.ladder.items()), ""]
        rows = [
            ("Players scanned", f"{self.players_scanned} / {self.players_target}"),
            ("Match IDs found", self.match_ids_found),
            ("Duplicates skipped", self.duplicates_skipped),
            ("Unique matches", self.unique_matches),
            ("Already on disk", self.already_on_disk),
            ("Matches saved", self.matches_saved),
            ("Failed requests", self.failed_requests),
        ]
        if self.pending:
            rows.append(("Pending", f"{self.pending} (run again to retry)"))
        lines += [f"{name:<20} {value}" for name, value in rows]
        if self.queues:
            lines += ["", "Queues saved: " + ", ".join(
                f"{QUEUE_NAMES.get(q, f'queue {q}')} {n}" for q, n in self.queues.most_common())]
        if self.permanent_failures:
            lines += ["", f"Gave up on {len(self.permanent_failures)}:"]
            lines += [f"  {k}: {v}" for k, v in list(self.permanent_failures.items())[:10]]
        return "\n".join(lines)


class Collector:
    def __init__(self, client: RiotClient, *, store: RawMatchStore | None = None,
                 manifest: MatchManifest | None = None,
                 state_dir: str | os.PathLike = "storage/raw/riot/collections",
                 now: Callable[[], datetime] = utc_now, progress: Callable[[str], None] = lambda msg: None):
        self.client = client
        self.store = store or RawMatchStore()
        self.manifest = manifest or MatchManifest()
        self.state_dir = Path(state_dir)
        self.now = now
        self.progress = progress
        self._matches = MatchService(client)

    @property
    def active_path(self) -> Path:
        return self.state_dir / "active.json"

    def run(self, settings: CollectSettings = CollectSettings(), *, fresh: bool = False) -> CollectionStats:
        state = self._open(settings, fresh)
        try:
            if state.data["players"] is None:
                self._seed(state, settings.players)
                state.save()
            for attempt in range(2):  # the second pass retries transient failures once
                if attempt:
                    if not state.transient_errors():
                        break
                    self.progress(f"Retrying {state.transient_errors()} failed request(s)...")
                self._scan_players(state)
                self._download_matches(state)
            finished = not state.pending_players() and not state.pending_matches()
            state.data["status"] = "complete" if finished else "incomplete"
            if finished:
                state.data["finished_at"] = _iso(self.now())
        finally:
            state.save()
        if state.data["status"] == "complete":
            state.save(self.state_dir / f"{state.data['run_id']}.json")
            self.active_path.unlink()
        return self.stats(state)

    # --- run lifecycle ---------------------------------------------------------

    def _open(self, settings: CollectSettings, fresh: bool) -> CollectionState:
        state = CollectionState.load(self.active_path)
        if state is not None and state.data["players"] is None:
            state = None  # stopped before choosing players: nothing to resume, take the new settings
        if state is not None and fresh:
            state.data["status"] = "abandoned"
            state.save(self.state_dir / f"{state.data['run_id']}.json")
            self.progress(f"Set aside unfinished run {state.data['run_id']}.")
            state = None
        if state is not None:
            s = state.data["settings"]
            self.progress(f"Resuming run {state.data['run_id']} "
                          f"({s['players']} players x {s['matches_per_player']} matches; --fresh starts over).")
            state.data["status"] = "running"
            return state
        started = self.now()
        state = CollectionState.new(self.active_path, run_id=started.strftime("%Y%m%dT%H%M%SZ"),
                                    started_at=_iso(started), settings=settings,
                                    platform=self.client.config.platform, region=self.client.config.region)
        state.save()
        return state

    # --- 1-3: ladders -> players -> PUUIDs ----------------------------------------

    def _seed(self, state: CollectionState, count: int) -> None:
        league = LeagueService(self.client)
        ranked = []
        for tier, fetch in zip(TIERS, (league.challenger, league.grandmaster)):
            entries = fetch().get("entries") or []
            state.data["ladder"][tier] = len(entries)
            ranked += [(tier, e) for e in sorted(entries, key=lambda e: -e.get("leaguePoints", 0))]
        self.progress("Ladder: " + ", ".join(f"{n} {t.title()}" for t, n in state.data["ladder"].items()))

        players, seen = [], set()
        for tier, entry in ranked:
            if len(players) >= count:
                break
            puuid = entry.get("puuid") or self._puuid_for_summoner(state, entry.get("summonerId"))
            if puuid and puuid not in seen:
                seen.add(puuid)
                players.append({"puuid": puuid, "tier": tier, "league_points": entry.get("leaguePoints", 0)})
        state.data["players"] = players
        self.progress(f"Selected {len(players)} players.")

    def _puuid_for_summoner(self, state: CollectionState, summoner_id: str | None) -> str | None:
        """Ladder entries used to carry only a summonerId; resolve those through summoner-v1."""
        if not summoner_id:
            return None
        try:
            return SummonerService(self.client).by_id(summoner_id).get("puuid")
        except FATAL:
            raise
        except RiotError as e:
            state.data["failed_requests"] += 1
            self.progress(f"Could not resolve a ladder player ({type(e).__name__}); skipping.")
            return None

    # --- 4-5: match IDs, deduplicated before any download -----------------------

    def _scan_players(self, state: CollectionState) -> None:
        d = state.data
        total = len(d["players"])
        per_player = d["settings"]["matches_per_player"]
        for player in state.pending_players():
            puuid = player["puuid"]
            try:
                ids = self._matches.ids_by_puuid(puuid, count=per_player)
            except FATAL:
                raise
            except RiotError as e:
                self._record_error(state, d["player_errors"], puuid, e)
                state.save()
                continue
            d["player_errors"].pop(puuid, None)
            d["match_ids_found"] += len(ids)
            new = 0
            for match_id in ids:
                if match_id not in d["matches"]:
                    d["matches"][match_id] = puuid
                    new += 1
            d["scanned"][puuid] = len(ids)
            state.save()
            self.progress(f"[{len(d['scanned'])}/{total}] {player['tier'].title()} "
                          f"{player['league_points']} LP: {len(ids)} match IDs, {new} new")

    # --- 6-8: download each unseen match once; raw JSON + manifest ---------------

    def _download_matches(self, state: CollectionState) -> None:
        d = state.data
        players = {p["puuid"]: p for p in d["players"]}
        pending = state.pending_matches()
        if pending:
            self.progress(f"Downloading {len(pending)} match(es)...")
        in_manifest = self.manifest.match_ids()
        for i, match_id in enumerate(pending, 1):
            source = players.get(d["matches"][match_id], {})
            record = {"match_id": match_id, "run_id": d["run_id"],
                      "source_puuid": source.get("puuid"), "source_tier": source.get("tier"),
                      "platform": d["platform"], "region": d["region"]}
            if self.store.has(match_id):
                d["done"][match_id] = "on_disk"
                if match_id not in in_manifest:
                    # On disk with no manifest line: saved by the smoke test, or a stop landed
                    # between writing the file and the manifest. Describe the file we have.
                    path = self.store.path_for(match_id)
                    mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                    self.manifest.append({**record, "collected_at": _iso(mtime), "backfilled": True,
                                          **match_metadata(path.read_bytes())})
                    in_manifest.add(match_id)
            else:
                try:
                    body = self._matches.get_response(match_id).body
                except FATAL:
                    raise
                except RiotError as e:
                    self._record_error(state, d["match_errors"], match_id, e)
                    state.save()
                    continue
                self.store.save_bytes(match_id, body)
                self.manifest.append({**record, "collected_at": _iso(self.now()), **match_metadata(body)})
                in_manifest.add(match_id)
                d["done"][match_id] = "saved"
            d["match_errors"].pop(match_id, None)
            state.save()
            if i % 25 == 0 or i == len(pending):
                self.progress(f"  {i}/{len(pending)} matches")

    def _record_error(self, state: CollectionState, errors: dict, key: str, e: RiotError) -> None:
        state.data["failed_requests"] += 1
        transient = isinstance(e, TRANSIENT)
        errors[key] = {"error": f"{type(e).__name__}: {e}", "transient": transient}
        self.progress(f"  failed {key[:20]}: {type(e).__name__}{'' if transient else ' (not retrying)'}")

    # --- 11: statistics ------------------------------------------------------------

    def stats(self, state: CollectionState) -> CollectionStats:
        d = state.data
        outcomes = Counter(d["done"].values())
        run_queues = Counter(r.get("queue_id") for r in self.manifest.records()
                             if r.get("run_id") == d["run_id"])
        permanent = {k: e["error"] for errs in (d["player_errors"], d["match_errors"])
                     for k, e in errs.items() if not e["transient"]}
        return CollectionStats(
            run_id=d["run_id"], status=d["status"], ladder=dict(d["ladder"]),
            players_target=len(d["players"] or []) or d["settings"]["players"],
            players_scanned=len(d["scanned"]),
            match_ids_found=d["match_ids_found"],
            duplicates_skipped=d["match_ids_found"] - len(d["matches"]),
            unique_matches=len(d["matches"]),
            already_on_disk=outcomes["on_disk"],
            matches_saved=outcomes["saved"],
            failed_requests=d["failed_requests"],
            pending=len(state.pending_players()) + len(state.pending_matches()),
            permanent_failures=permanent,
            queues=run_queues,
        )
