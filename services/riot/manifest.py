"""Collection metadata for the raw match archive, kept beside the raw files.

The raw match JSON is never modified. Everything we know about how and when a
match was collected goes in storage/raw/riot/manifest.jsonl instead, one line
per match:

    {"match_id": "NA1_123", "collected_at": "...", "source_puuid": "...",
     "patch": "14.23", "queue_id": 1100, ...}

Fields read out of the match (queue, set, version) are copied as Riot sent
them; nothing is interpreted beyond pulling a patch number from game_version.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

# "Version 14.23.636.0137 (Nov 19 2024/...) [PUBLIC]" -> "14.23". Some matches
# report "TFT Unreal Version ?.?.?.?", which has no patch; those get None.
_PATCH_RE = re.compile(r"(\d+)\.(\d+)\.")

QUEUE_NAMES = {1090: "normal", 1100: "ranked", 1130: "hyper roll", 1160: "double up"}


def patch_from_version(game_version: str | None) -> str | None:
    m = _PATCH_RE.search(game_version or "")
    return f"{m.group(1)}.{m.group(2)}" if m else None


def match_metadata(body: bytes) -> dict:
    """Read-only facts about a raw match body. Unparseable bodies give None fields."""
    try:
        data = json.loads(body)
        info, meta = data.get("info") or {}, data.get("metadata") or {}
    except (ValueError, AttributeError):
        info, meta = {}, {}
    version = info.get("game_version")
    return {
        "patch": patch_from_version(version),
        "game_version": version,
        "queue_id": info.get("queue_id", info.get("queueId")),
        "tft_set_number": info.get("tft_set_number"),
        "tft_game_type": info.get("tft_game_type"),
        "game_datetime": info.get("game_datetime"),
        "data_version": meta.get("data_version"),
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


class MatchManifest:
    def __init__(self, path: str | os.PathLike = "storage/raw/riot/manifest.jsonl"):
        self.path = Path(path)

    def records(self) -> list[dict]:
        """Every complete line. A line cut short by a crash mid-append is skipped."""
        if not self.path.is_file():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def match_ids(self) -> set[str]:
        return {r["match_id"] for r in self.records() if "match_id" in r}

    def append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, separators=(",", ":")) + "\n"
        if self.path.is_file() and self.path.stat().st_size:
            with self.path.open("rb") as f:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":  # previous append was cut short; don't glue onto it
                    line = "\n" + line
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
