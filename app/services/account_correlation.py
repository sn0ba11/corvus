import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlsplit

USERNAME_RE = re.compile(r"[^a-z0-9]+")
WORD_RE = re.compile(r"[a-z0-9]{3,}")
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
GENERIC_DOMAINS = {
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "youtube.com",
}


@dataclass(frozen=True, slots=True)
class AccountEvidence:
    finding_id: str
    username: str | None
    display_name: str | None
    biography: str | None
    location: str | None
    domains: frozenset[str]
    urls: frozenset[str]
    image_matches: dict[str, tuple[str, int]]


@dataclass(frozen=True, slots=True)
class AccountMatch:
    left_id: str
    right_id: str
    score: int
    signals: tuple[dict[str, Any], ...]


def normalise_text(value: str | None) -> str:
    if not value:
        return ""
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return " ".join(folded.lower().split())


def normalise_username(value: str | None) -> str:
    return USERNAME_RE.sub("", normalise_text(value))


def extract_domains(value: Any) -> frozenset[str]:
    domains: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, str):
            for match in URL_RE.findall(item):
                host = (urlsplit(match.rstrip(".,);]")).hostname or "").lower()
                if host.startswith("www."):
                    host = host[4:]
                if host and host not in GENERIC_DOMAINS:
                    domains.add(host)
        elif isinstance(item, dict):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return frozenset(domains)


def extract_urls(value: Any) -> frozenset[str]:
    """Return bounded, normalized public-looking HTTP(S) references for correlation."""

    urls: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, str):
            for match in URL_RE.findall(item):
                raw = match.rstrip(".,);]")
                parsed = urlsplit(raw)
                host = (parsed.hostname or "").casefold().rstrip(".")
                if (
                    parsed.scheme.casefold() not in {"http", "https"}
                    or not host
                    or parsed.username is not None
                    or parsed.password is not None
                ):
                    continue
                if host.startswith("www."):
                    host = host[4:]
                if host == "localhost" or host.endswith((".localhost", ".local", ".lan", ".internal", ".home", ".home.arpa")):
                    continue
                try:
                    address = ipaddress.ip_address(host)
                except ValueError:
                    pass
                else:
                    if not address.is_global:
                        continue
                try:
                    port = parsed.port
                except ValueError:
                    continue
                default_port = (parsed.scheme.casefold() == "http" and port == 80) or (
                    parsed.scheme.casefold() == "https" and port == 443
                )
                authority = host if port is None or default_port else f"{host}:{port}"
                path = re.sub(r"/{2,}", "/", parsed.path or "/")
                if path != "/":
                    path = path.rstrip("/")
                normalized = f"{parsed.scheme.casefold()}://{authority}{path}"
                if parsed.query:
                    normalized = f"{normalized}?{parsed.query}"
                urls.add(normalized[:2048])
                if len(urls) >= 100:
                    return
        elif isinstance(item, dict):
            for nested in item.values():
                visit(nested)
                if len(urls) >= 100:
                    return
        elif isinstance(item, list):
            for nested in item[:200]:
                visit(nested)
                if len(urls) >= 100:
                    return

    visit(value)
    return frozenset(urls)


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio() if left and right else 0.0


def _bio_overlap(left: str, right: str) -> tuple[float, list[str]]:
    left_words = set(WORD_RE.findall(left))
    right_words = set(WORD_RE.findall(right))
    shared = sorted(left_words & right_words)
    union = left_words | right_words
    return (len(shared) / len(union) if union else 0.0), shared


def correlate_accounts(
    evidence: dict[str, AccountEvidence], *, minimum_score: int = 35
) -> list[AccountMatch]:
    if not 0 <= minimum_score <= 100:
        raise ValueError("minimum score must be between 0 and 100")
    matches: list[AccountMatch] = []
    ids = sorted(evidence)
    for index, left_id in enumerate(ids):
        left = evidence[left_id]
        for right_id in ids[index + 1 :]:
            right = evidence[right_id]
            score = 0
            signals: list[dict[str, Any]] = []

            left_user, right_user = (
                normalise_username(left.username),
                normalise_username(right.username),
            )
            if left_user and right_user:
                if left_user == right_user:
                    score += 35
                    signals.append({"type": "username_exact", "weight": 35})
                else:
                    ratio = _similarity(left_user, right_user)
                    if ratio >= 0.82:
                        weight = round(20 * ratio)
                        score += weight
                        signals.append(
                            {
                                "type": "username_similar",
                                "weight": weight,
                                "similarity": round(ratio, 3),
                            }
                        )

            left_name, right_name = (
                normalise_text(left.display_name),
                normalise_text(right.display_name),
            )
            name_ratio = _similarity(left_name, right_name)
            if name_ratio >= 0.88:
                weight = round(20 * name_ratio)
                score += weight
                signals.append(
                    {"type": "display_name", "weight": weight, "similarity": round(name_ratio, 3)}
                )

            shared_urls = sorted(left.urls & right.urls)
            if shared_urls:
                weight = min(35, 30 + 2 * (len(shared_urls) - 1))
                score += weight
                signals.append(
                    {"type": "shared_url", "weight": weight, "values": shared_urls[:5]}
                )
            else:
                shared_domains = sorted(left.domains & right.domains)
                if shared_domains:
                    weight = min(30, 20 + 5 * (len(shared_domains) - 1))
                    score += weight
                    signals.append(
                        {"type": "shared_domain", "weight": weight, "values": shared_domains}
                    )

            left_location, right_location = (
                normalise_text(left.location),
                normalise_text(right.location),
            )
            if left_location and left_location == right_location:
                score += 8
                signals.append({"type": "location", "weight": 8, "value": left_location})

            overlap, shared_words = _bio_overlap(
                normalise_text(left.biography), normalise_text(right.biography)
            )
            if len(shared_words) >= 3 and overlap >= 0.25:
                weight = min(15, round(8 + 12 * overlap))
                score += weight
                signals.append(
                    {
                        "type": "biography",
                        "weight": weight,
                        "overlap": round(overlap, 3),
                        "shared_terms": shared_words[:12],
                    }
                )

            image = left.image_matches.get(right_id) or right.image_matches.get(left_id)
            if image:
                match_type, distance = image
                weight = 35 if match_type == "exact" else max(10, 25 - distance)
                score += weight
                signals.append(
                    {
                        "type": "image",
                        "weight": weight,
                        "match_type": match_type,
                        "distance": distance,
                    }
                )

            score = min(score, 100)
            if score >= minimum_score:
                matches.append(AccountMatch(left_id, right_id, score, tuple(signals)))
    return sorted(matches, key=lambda item: (-item.score, item.left_id, item.right_id))
