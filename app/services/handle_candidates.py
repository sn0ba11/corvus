from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_HANDLE_CANDIDATES = 24
HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")


@dataclass(frozen=True, slots=True)
class HandleCandidate:
    username: str
    source_name: str
    provenance: str


def _ascii_token(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_value = decomposed.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", ascii_value.casefold())


def _name_parts(name: str) -> tuple[str, ...]:
    parts = tuple(part for part in (_ascii_token(item) for item in name.split()) if part)
    if len(parts) < 2:
        raise ValueError("handle generation requires at least two name parts")
    if any(len(part) > 40 for part in parts):
        raise ValueError("name part is too long for bounded handle generation")
    return parts


def generate_handle_candidates(
    name: str,
    *,
    limit: int = MAX_HANDLE_CANDIDATES,
) -> list[HandleCandidate]:
    """Generate a deterministic, bounded set of reviewable handle candidates.

    The output is deliberately a candidate set, not an ownership assertion. No network
    request occurs here and nothing is followed automatically.
    """

    if not 1 <= limit <= MAX_HANDLE_CANDIDATES:
        raise ValueError(f"handle candidate limit must be 1..{MAX_HANDLE_CANDIDATES}")
    clean_name = " ".join(name.split()).strip()
    if not clean_name or len(clean_name) > 200:
        raise ValueError("invalid source name")
    parts = _name_parts(clean_name)
    first = parts[0]
    last = parts[-1]
    middle = "".join(parts[1:-1])
    first_initial = first[0]
    last_initial = last[0]

    patterns: tuple[tuple[str, str], ...] = (
        (f"{first}{last}", "first+last"),
        (f"{first}.{last}", "first.last"),
        (f"{first}_{last}", "first_last"),
        (f"{first}-{last}", "first-last"),
        (f"{first_initial}{last}", "first-initial+last"),
        (f"{first}{last_initial}", "first+last-initial"),
        (f"{last}{first}", "last+first"),
        (f"{last}.{first}", "last.first"),
        (f"{last}_{first}", "last_first"),
        (f"{first}{middle}{last}", "all-name-parts"),
        (f"{first}.{middle}.{last}" if middle else "", "dot-separated-name-parts"),
        (f"{first}_{middle}_{last}" if middle else "", "underscore-name-parts"),
        (f"{first}{last}1", "first+last+1"),
        (f"{first}{last}01", "first+last+01"),
        (f"{first}{last}x", "first+last+x"),
        (f"x{first}{last}x", "x+first+last+x"),
        (f"{first}{last}xox", "first+last+xox"),
        (f"{first}_xox", "first+xox"),
        (f"its{first}{last}", "its+first+last"),
        (f"real{first}{last}", "real+first+last"),
        (f"the{first}{last}", "the+first+last"),
        (f"{first}.{last}1", "first.last+1"),
        (f"{first}_{last}1", "first_last+1"),
        (f"{first_initial}.{last}", "first-initial.last"),
    )

    results: list[HandleCandidate] = []
    seen: set[str] = set()
    for candidate, provenance in patterns:
        candidate = candidate.strip("._-")[:64]
        if not candidate or not HANDLE_RE.fullmatch(candidate) or candidate in seen:
            continue
        seen.add(candidate)
        results.append(HandleCandidate(candidate, clean_name, provenance))
        if len(results) >= limit:
            break
    return results
