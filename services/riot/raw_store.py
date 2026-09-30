"""Raw Riot data on disk, byte-for-byte as received.

The match ID is the cache key: storage/raw/riot/matches/NA1_123456789.json.
If a match is already stored, callers should not request it again.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

MATCH_ID_RE = re.compile(r"^[A-Z0-9]{2,6}_\d+$")


class RawMatchStore:
    def __init__(self, root: str | os.PathLike = "storage/raw/riot/matches"):
        self.root = Path(root)

    def path_for(self, match_id: str) -> Path:
        if not MATCH_ID_RE.match(match_id):
            raise ValueError(f"Unexpected match ID format: {match_id!r}")
        return self.root / f"{match_id}.json"

    def has(self, match_id: str) -> bool:
        return self.path_for(match_id).is_file()

    def load_bytes(self, match_id: str) -> bytes:
        return self.path_for(match_id).read_bytes()

    def save_bytes(self, match_id: str, body: bytes) -> Path:
        """Write atomically so a crash never leaves a half-written match behind."""
        path = self.path_for(match_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{match_id}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path
