"""The raw match library.

    storage/raw/riot/
        matches/NA1_123.json      Riot's match response, byte for byte
        match_meta/NA1_123.json   what we recorded when saving it: patch, queue,
                                  collection date, source player, checksum
        runs/<run_id>/            collection runs (see collector.py)

A match whose raw file exists is never requested again. The metadata record is
written before the raw file, so a crash in between leaves a match that simply
gets downloaded again, never a raw match without metadata.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .raw_store import RawMatchStore, atomic_write

_VERSION_RE = re.compile(r"(\d+)\.(\d+)")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def patch_from_version(game_version: str | None) -> str | None:
    """'Linux Version 15.19.708.8403 (Sep 26 2025/17:40:54) [PUBLIC]' -> '15.19'."""
    m = _VERSION_RE.search(game_version or "")
    return f"{m.group(1)}.{m.group(2)}" if m else None


def describe_match(match_id: str, body: bytes) -> dict:
    """Metadata fields read from a raw match. ValueError if the body is not that match."""
    try:
        data = json.loads(body)
    except ValueError:
        raise ValueError(f"Response for {match_id} is not JSON") from None
    metadata = data.get("metadata") if isinstance(data, dict) else None
    info = data.get("info") if isinstance(data, dict) else None
    if not isinstance(metadata, dict) or not isinstance(info, dict) or metadata.get("match_id") != match_id:
        raise ValueError(f"Response for {match_id} is not that match")
    played = info.get("game_datetime")
    return {
        "patch": patch_from_version(info.get("game_version")),
        "queue_id": info.get("queue_id", info.get("queueId")),
        "tft_set_number": info.get("tft_set_number"),
        "played_at": (datetime.fromtimestamp(played / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                      if isinstance(played, (int, float)) else None),
        "game_version": info.get("game_version"),
        "data_version": metadata.get("data_version"),
    }


class MatchArchive:
    def __init__(self, root: str | os.PathLike = "storage/raw/riot"):
        self.root = Path(root)
        self.raw = RawMatchStore(self.root / "matches")
        self.meta_dir = self.root / "match_meta"
        self.runs_dir = self.root / "runs"

    def has(self, match_id: str) -> bool:
        """True once the raw match is stored; such a match is never requested again."""
        try:
            return self.raw.has(match_id)
        except ValueError:  # not a match ID we would ever store
            return False

    def meta(self, match_id: str) -> dict | None:
        try:
            return json.loads(self._meta_path(match_id).read_bytes())
        except FileNotFoundError:
            return None

    def save(self, match_id: str, body: bytes, meta: dict) -> dict:
        """Store Riot's bytes untouched plus a metadata record; returns the record.

        Raises ValueError, storing nothing, if the body is not the requested match.
        """
        record = {"match_id": match_id, **describe_match(match_id, body), **meta,
                  "raw_sha256": hashlib.sha256(body).hexdigest(), "raw_bytes": len(body)}
        atomic_write(self._meta_path(match_id), json.dumps(record, indent=2).encode("utf-8"))
        self.raw.save_bytes(match_id, body)
        return record

    def _meta_path(self, match_id: str) -> Path:
        return self.meta_dir / self.raw.path_for(match_id).name
