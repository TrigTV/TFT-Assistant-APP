"""Milestone 2: a raw match library built from high Elo players.

    Challenger + Grandmaster ladders -> PUUIDs -> match IDs -> dedupe
        -> each unseen match downloaded once -> MatchArchive (raw JSON + metadata)

Each run keeps the ladders it picked players from (Riot's bytes) and its
progress in storage/raw/riot/runs/<run_id>/. Stopping a run (Ctrl+C, an expired
dev key, a crash) loses nothing: opening a run with the same settings resumes
it. Nothing is normalized or analyzed here.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .archive import MatchArchive, utc_now
from .client import RiotClient
from .endpoints import LeagueService, MatchService, SummonerService
from .errors import RiotBadRequest, RiotError, RiotNotFound, RiotUnauthorized
from .raw_store import atomic_write

APEX_TIERS = ("challenger", "grandmaster", "master")  # best first
QUEUE_NAMES = {1090: "normal", 1100: "ranked", 1130: "hyper roll", 1160: "double up"}
# Asking again won't help: 404, 400, or a body that isn't the requested match.
PERMANENT_ERRORS = (RiotNotFound, RiotBadRequest, ValueError)
MAX_ATTEMPTS = 3  # per player or match, across retry rounds and resumes
CHECKPOINT_EVERY = 25  # matches between state saves; saved matches are on disk immediately


@dataclass(frozen=True)
class CollectSettings:
    players: int = 50
    matches_per_player: int = 20
    tiers: tuple[str, ...] = ("challenger", "grandmaster")

    def __post_init__(self):
        tiers = tuple(self.tiers)
        if not tiers or set(tiers) - set(APEX_TIERS):
            raise ValueError(f"tiers must be some of {', '.join(APEX_TIERS)}; got {', '.join(tiers) or 'none'}")
        if self.players < 1 or self.matches_per_player < 1:
            raise ValueError("players and matches_per_player must be at least 1")
        object.__setattr__(self, "tiers", tuple(t for t in APEX_TIERS if t in tiers))


class CollectionRun:
    """One run's resume point: settings, the players picked, their match IDs, failures, counters."""

    def __init__(self, directory: str | Path, state: dict):
        self.dir = Path(directory)
        self.state = state

    @property
    def run_id(self) -> str:
        return self.state["run_id"]

    @property
    def settings(self) -> CollectSettings:
        s = self.state["settings"]
        return CollectSettings(players=s["players"], matches_per_player=s["matches_per_player"],
                               tiers=tuple(s["tiers"]))

    @property
    def finished(self) -> bool:
        return self.state["finished_at"] is not None

    @classmethod
    def start(cls, runs_dir: str | Path, settings: CollectSettings, platform: str, region: str) -> "CollectionRun":
        stamp = utc_now().replace("-", "").replace(":", "")
        run_id, n = stamp, 1
        while (Path(runs_dir) / run_id).exists():
            n += 1
            run_id = f"{stamp}-{n}"
        run = cls(Path(runs_dir) / run_id, {
            "run_id": run_id, "platform": platform, "region": region,
            "settings": {"players": settings.players, "matches_per_player": settings.matches_per_player,
                         "tiers": list(settings.tiers)},
            "started_at": utc_now(), "updated_at": None, "finished_at": None,
            "players": None,  # picked from the ladder once, then fixed for the run
            "match_failures": {},
            "counters": {"api_requests": 0, "api_retries": 0, "puuid_lookups": 0,
                         "failed_requests": 0, "recovered": 0, "seconds": 0.0},
        })
        run.save()
        return run

    @classmethod
    def load(cls, directory: str | Path) -> "CollectionRun":
        return cls(directory, json.loads((Path(directory) / "state.json").read_bytes()))

    @classmethod
    def find_unfinished(cls, runs_dir: str | Path, settings: CollectSettings, platform: str,
                        region: str) -> "CollectionRun | None":
        """The newest unfinished run with exactly these settings, if any."""
        runs_dir = Path(runs_dir)
        if not runs_dir.is_dir():
            return None
        for d in sorted((p for p in runs_dir.iterdir() if p.is_dir()), reverse=True):
            try:
                run = cls.load(d)
                same = (run.state["platform"], run.state["region"], run.settings) == (platform, region, settings)
            except (OSError, ValueError, KeyError, TypeError):
                continue  # not a run directory
            if same and not run.finished:
                return run
        return None

    def save(self) -> None:
        self.state["updated_at"] = utc_now()
        atomic_write(self.dir / "state.json", json.dumps(self.state, indent=1).encode("utf-8"))

    def save_file(self, name: str, data: bytes) -> Path:
        return atomic_write(self.dir / name, data)


def open_run(archive: MatchArchive, settings: CollectSettings, platform: str, region: str, *,
             new: bool = False) -> tuple[CollectionRun, bool]:
    """Resume the newest unfinished run with these settings, else start one. Returns (run, resumed)."""
    if not new:
        run = CollectionRun.find_unfinished(archive.runs_dir, settings, platform, region)
        if run is not None:
            return run, True
    return CollectionRun.start(archive.runs_dir, settings, platform, region), False


def collect(client: RiotClient, archive: MatchArchive, run: CollectionRun, *,
            retry_delay: float = 30.0, sleep: Callable[[float], None] = time.sleep,
            on_progress: Callable[[str], None] = lambda msg: None) -> "CollectionStats":
    """Run or resume `run` until every player and match is collected or out of attempts.

    Progress is saved before any exception leaves: KeyboardInterrupt, or
    RiotUnauthorized/RiotExpiredKey, which stop the run because every further
    call would fail too. Calling collect again on the same run continues it.
    """
    c = _Collector(client, archive, run, on_progress)
    try:
        if run.state["players"] is None:
            c.pick_players()
        while True:
            c.scan_players()
            c.download_matches()
            waiting = c.retryable()
            if not waiting:
                break
            on_progress(f"Retrying {waiting} failed request(s) in {retry_delay:.0f}s")
            sleep(retry_delay)
        run.state["finished_at"] = utc_now()
    finally:
        c.checkpoint()
    return collection_stats(archive, run)


def select_players(entries: list[dict], limit: int) -> list[dict]:
    """Best first: Challenger, then Grandmaster, then Master, each by LP. Inactive players are skipped."""
    order = {t.upper(): i for i, t in enumerate(APEX_TIERS)}
    ranked = sorted((e for e in entries if not e.get("inactive")),
                    key=lambda e: (order.get(e["tier"], len(order)), -e.get("leaguePoints", 0)))
    players, seen = [], set()
    for e in ranked:
        key = e.get("puuid") or e.get("summonerId")
        if not key or key in seen:
            continue
        seen.add(key)
        players.append({"puuid": e.get("puuid"), "summoner_id": None if e.get("puuid") else e["summonerId"],
                        "tier": e["tier"], "league_points": e.get("leaguePoints", 0), "match_ids": None})
        if len(players) == limit:
            break
    return players


def unique_matches(players: list[dict] | None) -> dict[str, dict]:
    """Match ID -> the first (highest ranked) player who listed it, in listing order."""
    out: dict[str, dict] = {}
    for p in players or ():
        for match_id in p["match_ids"] or ():
            out.setdefault(match_id, p)
    return out


def _can_try(failure: dict | None) -> bool:
    return failure is None or (not failure["permanent"] and failure["attempts"] < MAX_ATTEMPTS)


class _Collector:
    def __init__(self, client: RiotClient, archive: MatchArchive, run: CollectionRun, say):
        self.client, self.archive, self.run, self.say = client, archive, run, say
        self.settings = run.settings
        self.counters = run.state["counters"]
        self.failures = run.state["match_failures"]
        self.league = LeagueService(client)
        self.summoners = SummonerService(client)
        self.matches = MatchService(client)
        self._last = (client.request_count, client.retry_count, time.monotonic())

    def checkpoint(self) -> None:
        requests, retries, since = self._last
        now = time.monotonic()
        self.counters["api_requests"] += self.client.request_count - requests
        self.counters["api_retries"] += self.client.retry_count - retries
        self.counters["seconds"] += now - since
        self._last = (self.client.request_count, self.client.retry_count, now)
        self.run.save()

    def pick_players(self) -> None:
        entries, sizes = [], []
        for tier in self.settings.tiers:
            resp = self.league.apex_response(tier)
            self.run.save_file(f"{tier}_league.json", resp.body)  # the ladder as Riot sent it
            league = resp.json()
            tier_name = league.get("tier", tier.upper())
            entries += [dict(e, tier=tier_name) for e in league.get("entries", [])]
            sizes.append(f"{len(league.get('entries', []))} {tier.title()}")
        self.run.state["players"] = select_players(entries, self.settings.players)
        self.checkpoint()
        self.say(f"Ladder: {', '.join(sizes)}; taking the top {len(self.run.state['players'])} active players")

    def scan_players(self) -> None:
        players = self.run.state["players"]
        listed: set[str] = set()
        for i, p in enumerate(players, 1):
            if p["match_ids"] is None and _can_try(p.get("failure")):
                label = f"[{i}/{len(players)}] {p['tier'].title()} {p['league_points']} LP"
                self._scan(p)
                self.checkpoint()
                if p["match_ids"] is None:
                    self.say(f"{label}: failed ({p['failure']['error']})")
                else:
                    new = sum(1 for m in p["match_ids"] if m not in listed and not self.archive.has(m))
                    self.say(f"{label}: {len(p['match_ids'])} match IDs, {new} new")
            listed.update(p["match_ids"] or ())

    def _scan(self, p: dict) -> None:
        try:
            if not p["puuid"]:
                p["puuid"] = self.summoners.by_id(p["summoner_id"])["puuid"]
                self.counters["puuid_lookups"] += 1
            p["match_ids"] = self.matches.ids_by_puuid(p["puuid"], count=self.settings.matches_per_player)
        except RiotUnauthorized:
            raise
        except RiotError as e:
            p["failure"] = self._failure(p.get("failure"), e)
        else:
            if p.pop("failure", None):
                self.counters["recovered"] += 1

    def download_matches(self) -> None:
        # Deduplicated across players, and nothing already in the archive is requested.
        todo = [(match_id, source) for match_id, source in unique_matches(self.run.state["players"]).items()
                if not self.archive.has(match_id) and _can_try(self.failures.get(match_id))]
        if not todo:
            return
        self.say(f"Downloading {len(todo)} matches")
        state = self.run.state
        for i, (match_id, source) in enumerate(todo, 1):
            try:
                body = self.matches.get_response(match_id).body
                # The match's own platform is its ID prefix; `platform` here is the source player's ladder.
                self.archive.save(match_id, body, {
                    "collected_at": utc_now(), "region": state["region"], "run_id": state["run_id"],
                    "source_player": {"puuid": source["puuid"], "tier": source["tier"],
                                      "league_points": source["league_points"], "platform": state["platform"]},
                })
            except RiotUnauthorized:
                raise
            except (RiotError, ValueError) as e:
                self.failures[match_id] = self._failure(self.failures.get(match_id), e)
                self.say(f"  {match_id}: failed ({type(e).__name__})")
            else:
                if self.failures.pop(match_id, None):
                    self.counters["recovered"] += 1
            if i % CHECKPOINT_EVERY == 0 or i == len(todo):
                self.checkpoint()
                self.say(f"  {i}/{len(todo)} matches")

    def retryable(self) -> int:
        players = sum(1 for p in self.run.state["players"] if p["match_ids"] is None and _can_try(p.get("failure")))
        return players + sum(1 for f in self.failures.values() if _can_try(f))

    def _failure(self, previous: dict | None, error: Exception) -> dict:
        self.counters["failed_requests"] += 1
        return {"error": type(error).__name__, "message": str(error)[:300],
                "attempts": (previous or {}).get("attempts", 0) + 1,
                "permanent": isinstance(error, PERMANENT_ERRORS)}


@dataclass
class CollectionStats:
    run_id: str
    platform: str
    region: str
    finished: bool
    players_selected: int = 0
    players_scanned: int = 0
    players_pending: int = 0
    puuid_lookups: int = 0
    match_ids_found: int = 0
    unique_matches: int = 0
    already_archived: int = 0
    matches_saved: int = 0
    matches_pending: int = 0
    failed_requests: int = 0
    recovered: int = 0
    api_requests: int = 0
    api_retries: int = 0
    seconds: float = 0.0
    not_collected: dict[str, str] = field(default_factory=dict)  # given up on -> last error
    by_patch: Counter = field(default_factory=Counter)  # matches saved by this run
    by_queue: Counter = field(default_factory=Counter)

    @property
    def duplicates_skipped(self) -> int:
        return self.match_ids_found - self.unique_matches

    def summary(self) -> str:
        rows = [
            ("Players scanned", f"{self.players_scanned} / {self.players_selected}", ""),
            ("PUUID lookups", self.puuid_lookups, "summoner ID -> PUUID" if self.puuid_lookups else ""),
            ("Match IDs found", self.match_ids_found, ""),
            ("Duplicates skipped", self.duplicates_skipped, "listed by more than one player"),
            ("Unique matches", self.unique_matches, ""),
            ("Already in archive", self.already_archived, "saved earlier, not downloaded again"),
            ("Matches saved", self.matches_saved, ""),
            ("Failed requests", self.failed_requests,
             f"{self.recovered} recovered on retry" if self.failed_requests else ""),
            ("API requests", self.api_requests, f"{self.api_retries} retried by the client" if self.api_retries else ""),
            ("Time", _duration(self.seconds), ""),
        ]
        lines = [f"HIGH ELO COLLECTION  {self.platform} / {self.region}  run {self.run_id}", ""]
        lines += [f"{name:<20}{value!s:>9}   {note}".rstrip() for name, value, note in rows]
        if self.by_patch:
            lines += ["", "Saved by patch:  " + ", ".join(f"{p or 'unknown'}: {n}" for p, n in self.by_patch.most_common()),
                      "Saved by queue:  " + ", ".join(f"{_queue(q)}: {n}" for q, n in self.by_queue.most_common())]
        if self.not_collected:
            lines += ["", "Not collected:"] + [f"  {k}  {v}" for k, v in self.not_collected.items()]
        lines.append("")
        if self.finished:
            lines.append("Collection complete.")
        else:
            lines.append(f"Incomplete: {self.players_pending} players to scan and "
                         f"{self.matches_pending} matches to download so far.")
        return "\n".join(lines)


def collection_stats(archive: MatchArchive, run: CollectionRun) -> CollectionStats:
    """Recomputed from the run state and the archive, so the numbers stay exact across resumes."""
    state = run.state
    players = state["players"] or []
    unique = unique_matches(players)
    counters = state["counters"]
    stats = CollectionStats(
        run_id=run.run_id, platform=state["platform"], region=state["region"], finished=run.finished,
        players_selected=len(players),
        players_scanned=sum(1 for p in players if p["match_ids"] is not None),
        players_pending=sum(1 for p in players if p["match_ids"] is None and _can_try(p.get("failure"))),
        puuid_lookups=counters["puuid_lookups"],
        match_ids_found=sum(len(p["match_ids"]) for p in players if p["match_ids"] is not None),
        unique_matches=len(unique),
        failed_requests=counters["failed_requests"], recovered=counters["recovered"],
        api_requests=counters["api_requests"], api_retries=counters["api_retries"], seconds=counters["seconds"],
    )
    for match_id in unique:
        if not archive.has(match_id):
            if _can_try(state["match_failures"].get(match_id)):
                stats.matches_pending += 1
            continue
        meta = archive.meta(match_id)
        if meta and meta.get("run_id") == run.run_id:
            stats.matches_saved += 1
            stats.by_patch[meta.get("patch")] += 1
            stats.by_queue[meta.get("queue_id")] += 1
        else:
            stats.already_archived += 1
    for i, p in enumerate(players, 1):
        f = p.get("failure")
        if p["match_ids"] is None and f and not _can_try(f):
            stats.not_collected[f"player {i} ({p['tier'].title()} {p['league_points']} LP)"] = \
                f"{f['error']}: {f['message']}"
    for match_id, f in state["match_failures"].items():
        if not _can_try(f):
            stats.not_collected[match_id] = f"{f['error']}: {f['message']}"
    return stats


def _queue(queue_id) -> str:
    return f"{QUEUE_NAMES[queue_id]} ({queue_id})" if queue_id in QUEUE_NAMES else str(queue_id or "unknown")


def _duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"
