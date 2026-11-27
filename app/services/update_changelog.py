from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MAX_CHANGELOG_BYTES = 64 * 1024
MAX_CHANGELOG_ENTRIES = 100
MAX_CHANGELOG_DETAILS = 4
MAX_CHANGELOG_DETAIL_LENGTH = 300
CHANGE_START = "@@CHANGE@@"
CHANGE_END = "@@END@@"
CHANGE_LINE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<commit>[0-9a-fA-F]{7,40})\s+(?P<summary>.+)$"
)
COMMIT_HASH = re.compile(r"^[0-9a-fA-F]{7,40}$")


@dataclass(frozen=True, slots=True)
class UpdateChange:
    date: str | None
    commit: str | None
    summary: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UpdateChangelog:
    title: str
    changes: tuple[UpdateChange, ...]


def _clean(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _detail(value: str) -> str:
    cleaned = _clean(value, MAX_CHANGELOG_DETAIL_LENGTH)
    return re.sub(r"^(?:[-*•]\s+|\d+[.)]\s+)", "", cleaned)


def _structured_changes(text: str) -> list[UpdateChange]:
    changes: list[UpdateChange] = []
    for raw_block in text.split(CHANGE_START)[1 : MAX_CHANGELOG_ENTRIES + 1]:
        block = raw_block.split(CHANGE_END, 1)[0]
        lines = block.splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        if len(lines) < 3:
            continue
        date = _clean(lines[0], 10)
        commit = _clean(lines[1], 40).lower()
        summary = _clean(lines[2], 500)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or not COMMIT_HASH.fullmatch(commit):
            continue
        details = tuple(
            detail for detail in (_detail(line) for line in lines[3:] if line.strip()) if detail
        )[:MAX_CHANGELOG_DETAILS]
        changes.append(
            UpdateChange(
                date=date,
                commit=commit,
                summary=summary or "Untitled change",
                details=details,
            )
        )
    return changes


def _legacy_changes(text: str) -> list[UpdateChange]:
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    if lines and lines[0].casefold().startswith("osint workbench changes installed"):
        lines = lines[1:]
    changes: list[UpdateChange] = []
    for line in lines[:MAX_CHANGELOG_ENTRIES]:
        match = CHANGE_LINE.fullmatch(line)
        if match:
            changes.append(
                UpdateChange(
                    date=match.group("date"),
                    commit=match.group("commit").lower(),
                    summary=match.group("summary")[:500],
                )
            )
        else:
            changes.append(UpdateChange(date=None, commit=None, summary=line[:500]))
    return changes


def load_update_changelog(path: Path) -> UpdateChangelog | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_CHANGELOG_BYTES:
            return None
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None
    if not text.strip():
        return None

    changes = _structured_changes(text) if CHANGE_START in text else _legacy_changes(text)
    return (
        UpdateChangelog(title="Latest installed changes", changes=tuple(changes))
        if changes
        else None
    )
