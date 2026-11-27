from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

MAX_UPDATE_RANGE_BYTES = 4096
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


@dataclass(frozen=True, slots=True)
class UpdateBuildRange:
    from_commit: str
    to_commit: str
    fallback_used: bool = False


def _normalise_commit(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    commit = value.strip().lower()
    return commit if COMMIT_RE.fullmatch(commit) else None


def load_update_build_range(path: Path) -> UpdateBuildRange | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_UPDATE_RANGE_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    from_commit = _normalise_commit(payload.get("from_commit"))
    to_commit = _normalise_commit(payload.get("to_commit"))
    if from_commit is None or to_commit is None:
        return None
    return UpdateBuildRange(
        from_commit=from_commit,
        to_commit=to_commit,
        fallback_used=payload.get("fallback_used") is True,
    )
