"""Bounded, unauthenticated exact-profile lookups for a username."""

from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from app.adapters.base import Adapter, ExecutionResult, HealthResult, LogCallback
from app.models.schemas import FindingRecord, FindingStatus, ProfileEvidence, QueryType
from app.services.egress_policy import EgressPolicyError, read_bounded_response, safe_client

MAX_BYTES = 1024 * 1024
NEW = frozenset({"keybase", "devto", "chesscom", "lichess", "codeberg", "dockerhub"})
# DEV Community parsing is kept, but it is not queried: on 2026-11-13 dev.to answered this
# collector's own User-Agent with HTTP 403, and Corvus does not disguise itself to get past that.
PROVIDERS = ("github", "gitlab", "hackernews", "bluesky", "keybase", "chesscom", "lichess", "codeberg", "dockerhub")
NAMES = {"github": "GitHub", "gitlab": "GitLab", "hackernews": "Hacker News", "bluesky": "Bluesky", "keybase": "Keybase",
         "devto": "DEV Community", "chesscom": "Chess.com", "lichess": "Lichess", "codeberg": "Codeberg",
         "dockerhub": "Docker Hub"}


def lookup_spec(provider: str, username: str) -> tuple[str, str] | None:
    if provider == "github" and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", username):
        return f"https://api.github.com/users/{quote(username, safe='')}", username
    if provider == "gitlab" and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", username):
        return "https://gitlab.com/api/v4/users?" + urlencode({"username": username, "per_page": 5}), username
    if provider == "hackernews" and re.fullmatch(r"[A-Za-z0-9_-]{2,15}", username):
        return f"https://hacker-news.firebaseio.com/v0/user/{quote(username, safe='')}.json", username
    if provider == "bluesky" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,63}", username):
        handle = username if "." in username else username + ".bsky.social"
        return "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?" + urlencode({"actor": handle}), handle
    if provider == "keybase" and re.fullmatch(r"[A-Za-z0-9_]{2,16}", username):
        return "https://keybase.io/_/api/1.0/user/lookup.json?" + urlencode({"usernames": username}), username
    if provider == "devto" and re.fullmatch(r"[A-Za-z0-9_]{1,30}", username):
        return "https://dev.to/api/users/by_username?" + urlencode({"url": username}), username
    if provider == "chesscom" and re.fullmatch(r"[A-Za-z0-9_-]{3,25}", username):
        return f"https://api.chess.com/pub/player/{quote(username.lower(), safe='')}", username.lower()
    if provider == "lichess" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,28}", username):
        return f"https://lichess.org/api/user/{quote(username, safe='')}", username
    if provider == "codeberg" and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,38}", username):
        return f"https://codeberg.org/api/v1/users/{quote(username, safe='')}", username
    if provider == "dockerhub" and re.fullmatch(r"[A-Za-z0-9]{4,30}", username):
        return f"https://hub.docker.com/v2/users/{quote(username.lower(), safe='')}/", username.lower()
    return None


def exact_profile(provider: str, account: str, payload: Any) -> dict[str, Any] | None:
    if provider == "keybase":
        them = payload.get("them") if isinstance(payload, dict) else None
        payload = them[0] if isinstance(them, list) and them else None
        identifier = _dig(payload, "basics", "username")
        return payload if isinstance(identifier, str) and identifier.casefold() == account.casefold() else None
    if provider == "lichess" and isinstance(payload, dict) and payload.get("disabled") is True:
        return None
    if provider == "gitlab" and isinstance(payload, list):
        payload = next((row for row in payload[:5] if isinstance(row, dict)
                        and str(row.get("username", "")).casefold() == account.casefold()), None)
    if not isinstance(payload, dict):
        return None
    field = {"github": "login", "gitlab": "username", "hackernews": "id", "bluesky": "handle", "devto": "username",
             "chesscom": "username", "lichess": "username", "codeberg": "login", "dockerhub": "username"}[provider]
    identifier = payload.get(field) if provider in NEW else str(payload.get(field) or "")
    if not isinstance(identifier, str):
        return None
    matches = identifier == account if provider == "hackernews" else identifier.casefold() == account.casefold()
    return payload if matches else None


def _text(value: Any, limit: int = 2000) -> str | None:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", value)).split())[:limit] or None if isinstance(value, str) else None


def _dig(value: Any, *path: str) -> Any:
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _iso(value: Any, millis: bool = False) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(value / 1000 if millis else value, UTC).isoformat()
        except (ValueError, OverflowError, OSError):
            return None
    text = _text(value, 64)
    if text is None or text.startswith("0001-01-01"):
        return None
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC).isoformat()
        except ValueError:
            pass
    return text


def _link(platform: str, username: Any, url: Any, source: str) -> dict[str, Any] | None:
    name = _text(username, 64)
    if name is None or not re.fullmatch(r"[\w.\-]{3,64}", name):
        return None
    link = _text(url, 2048)
    return {"platform": platform, "username": name, "url": link if link and link.startswith("https://") else None, "source": source}


def _site(value: Any) -> str | None:
    text = _text(value, 2048)
    if text is None or " " in text:
        return None
    return text if re.match(r"https?://", text) else "https://" + text


def _extract(provider: str, p: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Flatten a provider payload into the generic profile keys plus bounded metadata extras."""
    flat: dict[str, Any] = {}
    linked: list[dict[str, Any] | None] = []
    sites: list[str | None] = []
    details: list[tuple[str, Any]] = []
    extra: dict[str, Any] = {}
    if provider == "keybase":
        flat = {"name": _dig(p, "profile", "full_name"), "bio": _dig(p, "profile", "bio"),
                "location": _dig(p, "profile", "location"), "avatar": _dig(p, "pictures", "primary", "url"),
                "id": p.get("id"), "created_at": _iso(_dig(p, "basics", "ctime"))}
        proofs = _dig(p, "proofs_summary", "all")
        for proof in (proofs if isinstance(proofs, list) else [])[:50]:
            kind = _dig(proof, "proof_type")
            if kind in {"twitter", "github", "reddit", "hackernews", "mastodon"}:
                linked.append(_link(str(kind).title() if kind != "hackernews" else "Hacker News",
                                    _dig(proof, "nametag"), _dig(proof, "service_url"), "proofs_summary.all"))
            elif kind in {"dns", "https", "http", "generic_web_site"}:
                sites.append(_site(_dig(proof, "service_url") or _dig(proof, "nametag")))
        details.append(("PGP fingerprint", _dig(p, "public_keys", "primary", "key_fingerprint")))
        coins = p.get("cryptocurrency_addresses")
        for coin, rows in (list(coins.items())[:5] if isinstance(coins, dict) else []):
            for row in (rows if isinstance(rows, list) else [])[:2]:
                details.append((f"{_text(coin, 30) or 'Cryptocurrency'} address", _dig(row, "address")))
    elif provider == "devto":
        flat = {"name": p.get("name"), "bio": p.get("summary"), "location": p.get("location"),
                "avatar": p.get("profile_image"), "id": p.get("id"), "created_at": _iso(p.get("joined_at")),
                "website_url": p.get("website_url")}
        linked = [_link("GitHub", p.get("github_username"), f"https://github.com/{quote(str(p.get('github_username')), safe='')}", "github_username"),
                  _link("Twitter", p.get("twitter_username"), f"https://x.com/{quote(str(p.get('twitter_username')), safe='')}", "twitter_username")]
    elif provider == "chesscom":
        country = _text(p.get("country"), 300)
        flat = {"name": p.get("name"), "avatar": p.get("avatar"), "location": p.get("location"),
                "followers": p.get("followers"), "id": p.get("player_id"), "created_at": _iso(p.get("joined"))}
        extra["last_seen_at"] = _iso(p.get("last_online"))
        twitch = _text(p.get("twitch_url"), 2048)
        if twitch:
            linked.append(_link("Twitch", twitch.rstrip("/").rsplit("/", 1)[-1], twitch, "twitch_url"))
        details += [("Country", country.rstrip("/").rsplit("/", 1)[-1] if country else None), ("Title", p.get("title")),
                    ("Account status", p.get("status")), ("Verified", p.get("verified")), ("League", p.get("league"))]
    elif provider == "lichess":
        prof: dict[str, Any] = _dig(p, "profile") if isinstance(p.get("profile"), dict) else {}
        real = prof.get("realName") or " ".join(x for x in (prof.get("firstName"), prof.get("lastName")) if isinstance(x, str))
        flat = {"name": real, "bio": prof.get("bio"), "location": prof.get("location"), "id": p.get("id"),
                "created_at": _iso(p.get("createdAt"), True)}
        extra["last_seen_at"] = _iso(p.get("seenAt"), True)
        links = prof.get("links")
        sites = [_site(line) for line in (links.splitlines()[:20] if isinstance(links, str) else [])]
        details += [("Country", prof.get("flag")), ("Title", p.get("title")), ("Verified", p.get("verified")),
                    ("Patron", p.get("patron"))]
    elif provider == "codeberg":
        flat = {"name": p.get("full_name"), "bio": p.get("description"), "avatar_url": p.get("avatar_url"),
                "location": p.get("location"), "followers": p.get("followers_count"), "following": p.get("following_count"),
                "id": p.get("id"), "created_at": _iso(p.get("created")), "website_url": p.get("website")}
        extra["last_seen_at"] = _iso(p.get("last_login"))
        details.append(("Pronouns", p.get("pronouns")))
    elif provider == "dockerhub":
        flat = {"name": p.get("full_name"), "avatar": p.get("gravatar_url"), "location": p.get("location"),
                "id": p.get("id"), "created_at": _iso(p.get("date_joined")), "website_url": p.get("profile_url"), "type": p.get("type")}
        details.append(("Company", p.get("company")))
    email = _text(p.get("email"), 254)
    if email and "noreply" not in email.casefold():
        flat["email"] = email
    extra["linked_accounts"] = [x for x in linked if x][:20]
    extra["linked_sites"] = list(dict.fromkeys(x for x in sites if x))[:20]
    extra["platform_details"] = [{"label": label, "value": text} for label, value in details
                                 if (text := _text(str(value), 300) if value is not None and value != "" else None)][:20]
    return flat, extra


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class UsernamePublicProfilesAdapter(Adapter):
    tool_id = "username_public_profiles"
    display_name = "Public Username Profiles"
    executable = "public-username-profiles"
    supported_query_types = frozenset({QueryType.USERNAME})
    capabilities = frozenset({"username_search", "exact_public_profile", "public_api", "profile_metadata"})
    default_timeout = 45
    concurrency_limit = 1

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.transport = transport

    async def healthcheck(self) -> HealthResult:
        return HealthResult(True, "ready", "bounded public profile APIs")

    def build_command(self, query: str, query_type: QueryType, output_dir: Path,
                      timeout: int, proxy: str | None = None) -> list[str]:
        username = query.strip().lstrip("@")
        if query_type != QueryType.USERNAME or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
            raise ValueError("Public profile lookup requires one valid username")
        command = [self.executable, "--username", username]
        if proxy:
            command.extend(["--proxy", proxy])
        return command

    async def execute(self, command: list[str], output_dir: Path, timeout: int,
                      query: str, log: LogCallback | None = None) -> ExecutionResult:
        expected = query.strip().lstrip("@")
        if command[:3] != [self.executable, "--username", expected] or len(command) not in {3, 5} or (
            len(command) == 5 and command[3] != "--proxy"
        ):
            return ExecutionResult(None, "", "", error_code="invalid_command", error_message="Public profile command did not match its query")
        proxy = command[4] if len(command) == 5 else None
        context = httpx.AsyncClient(transport=self.transport, timeout=min(timeout, 10), follow_redirects=False,
                                    trust_env=False) if self.transport else safe_client(
                                        timeout=min(timeout, 10), user_agent="Corvus public username profiles", proxy=proxy)
        semaphore = asyncio.Semaphore(2)
        results: dict[str, dict[str, Any]] = {}
        async with context as client:
            async def lookup(provider: str) -> None:
                spec = lookup_spec(provider, expected)
                if spec is None:
                    results[provider] = {"status": "unsupported_username"}
                    return
                endpoint, account = spec
                entry: dict[str, Any] = {"endpoint": endpoint, "account_identifier": account}
                results[provider] = entry
                async with semaphore:
                    try:
                        async with client.stream("GET", endpoint, headers={"Accept": "application/json"}) as response:
                            entry["http_status"] = response.status_code
                            if response.status_code == 429 or (response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"):
                                entry["status"] = "rate_limited"
                                return
                            if response.status_code in {401, 403}:
                                entry["status"] = "access_blocked"
                                return
                            if response.status_code == 404:
                                entry["status"] = "not_found"
                                return
                            raw = await read_bounded_response(response, max_bytes=MAX_BYTES)
                            if response.is_redirect:
                                entry["status"] = "redirect_not_followed"
                                return
                            payload = json.loads(raw)
                            if provider == "bluesky" and response.status_code == 400 and isinstance(payload, dict) and payload.get("error") in {"ProfileNotFound", "AccountNotFound"}:
                                entry["status"] = "not_found"
                                return
                            response.raise_for_status()
                        entry["response"] = payload
                        profile = exact_profile(provider, account, payload)
                        entry["status"] = "exact_match" if profile is not None else (
                            "not_found" if payload is None or payload == [] else "identifier_mismatch"
                        )
                    except (httpx.TimeoutException, TimeoutError):
                        entry["status"] = "timed_out"
                    except (httpx.HTTPError, EgressPolicyError, ValueError):
                        entry["status"] = "source_error"
            try:
                async with asyncio.timeout(timeout):
                    await asyncio.gather(*(lookup(provider) for provider in PROVIDERS))
            except TimeoutError:
                for provider in PROVIDERS:
                    results.setdefault(provider, {}).setdefault("status", "timed_out")
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "public-username-profiles.json"
        payload = {"query": expected, "observed_at": datetime.now(UTC).isoformat(), "providers": results}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        path.chmod(0o600)
        count = sum(entry.get("status") == "exact_match" for entry in results.values())
        if log:
            await log("stdout", f"Public profile APIs retained {count} exact profiles; per-source outcomes are in raw evidence")
        return ExecutionResult(0, json.dumps({"exact_profiles": count}), "", output_files=[path])

    def parse_results(self, result: ExecutionResult, output_dir: Path) -> list[dict[str, Any]]:
        path = output_dir / "public-username-profiles.json"
        if not path.is_file() or path.stat().st_size > 5 * MAX_BYTES:
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [payload] if isinstance(payload, dict) else []

    def normalise_results(self, records: list[dict[str, Any]], query: str, query_type: QueryType,
                          raw_reference: str | None) -> list[FindingRecord]:
        if not records or records[0].get("query") != query.strip().lstrip("@"):
            return []
        providers = records[0].get("providers", {})
        if not isinstance(providers, dict):
            return []
        findings = []
        for provider in PROVIDERS:
            entry = providers.get(provider)
            if not isinstance(entry, dict):
                continue
            spec = lookup_spec(provider, query.strip().lstrip("@"))
            profile = exact_profile(provider, spec[1], entry.get("response")) if spec else None
            metadata = {"source_scope": "unauthenticated_public_profile_api", "lookup_status": entry.get("status"),
                        "provider": provider, "account_identifier": spec[1] if spec else None,
                        "source_endpoint": spec[0] if spec else None}
            if entry.get("status") != "exact_match" or profile is None or spec is None:
                state = FindingStatus.NOT_FOUND if entry.get("status") == "not_found" else (
                    FindingStatus.RATE_LIMITED if entry.get("status") == "rate_limited" else FindingStatus.UNKNOWN)
                findings.append(FindingRecord(query_type=query_type, queried_value=query,
                    finding_type="username_lookup_status", platform=NAMES[provider], value=query,
                    status=state, title=f"{NAMES[provider]}: {str(entry.get('status', 'unknown')).replace('_', ' ')}",
                    metadata=metadata, source_tool=self.tool_id, source_record_id=f"{provider}:{query}:outcome",
                    raw_output_reference=raw_reference))
                continue
            account = spec[1]
            url = {"github": f"https://github.com/{quote(account, safe='')}",
                   "gitlab": f"https://gitlab.com/{quote(account, safe='')}",
                   "hackernews": "https://news.ycombinator.com/user?" + urlencode({"id": account}),
                   "bluesky": f"https://bsky.app/profile/{quote(account, safe='')}",
                   "keybase": f"https://keybase.io/{quote(account, safe='')}",
                   "devto": f"https://dev.to/{quote(account, safe='')}",
                   "chesscom": f"https://www.chess.com/member/{quote(account, safe='')}",
                   "lichess": f"https://lichess.org/@/{quote(account, safe='')}",
                   "codeberg": f"https://codeberg.org/{quote(account, safe='')}",
                   "dockerhub": f"https://hub.docker.com/u/{quote(account, safe='')}"}[provider]
            assert profile is not None
            extra: dict[str, Any] = {}
            if provider in NEW:
                profile, extra = _extract(provider, profile)
            elif provider == "github":
                # GitHub publishes the owner's own X handle and employer alongside the profile.
                handle = profile.get("twitter_username")
                link = _link("X", handle, f"https://x.com/{quote(str(handle), safe='')}", "twitter_username")
                extra = {"linked_accounts": [link] if link else [],
                         "platform_details": [{"label": "Company", "value": company}]
                         if (company := _text(profile.get("company"), 300)) else []}
            created = profile.get("created_at") or profile.get("createdAt")
            if provider == "hackernews" and isinstance(profile.get("created"), int):
                try:
                    created = datetime.fromtimestamp(profile["created"], UTC).isoformat()
                except (ValueError, OverflowError, OSError):
                    created = None
            metadata.update({"created_at": created, "public_email": _text(profile.get("email"), 254),
                             "external_url": _text(profile.get("blog") or profile.get("website_url"), 2048),
                             "account_type": _text(profile.get("type"), 100)})
            if provider in NEW and (provider_id := _text(str(profile.get("id") or ""), 100)):
                extra["platform_details"] = [*extra.get("platform_details", []),
                                             {"label": "Provider account ID", "value": provider_id}][:20]
            metadata.update(extra)
            findings.append(FindingRecord(
                query_type=query_type, queried_value=query, finding_type="profile", platform=NAMES[provider],
                value=account, canonical_url=url, display_url=url, status=FindingStatus.FOUND, confidence=0.98,
                title=_text(profile.get("name") or profile.get("displayName"), 300) or account,
                description="Exact public profile API match; a matching handle does not establish the subject's identity.",
                metadata=metadata, profile=ProfileEvidence(
                    # The new providers' internal IDs are opaque hex or numbers, not handles;
                    # they are kept as a detail so they are never offered as a username.
                    account_id=account if provider in NEW else str(profile.get("did") or profile.get("id") or account),
                    display_name=_text(profile.get("name") or profile.get("displayName"), 300) or account,
                    biography=_text(profile.get("bio") or profile.get("description") or profile.get("about")),
                    avatar_url=_text(profile.get("avatar_url") or profile.get("avatar"), 2048),
                    location=_text(profile.get("location"), 300),
                    followers=_count(profile.get("followers", profile.get("followersCount"))),
                    following=_count(profile.get("following", profile.get("followsCount"))),
                ), source_tool=self.tool_id, source_record_id=f"{provider}:{account}", raw_output_reference=raw_reference,
            ))
        return findings
