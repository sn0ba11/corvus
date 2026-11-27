import json
from functools import lru_cache
from pathlib import Path
from typing import Any, cast


@lru_cache
def upstreams() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "upstreams.lock.json"
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def commit_for(tool_id: str) -> str | None:
    entry = upstreams().get(tool_id)
    return str(entry["commit"]) if entry else None
