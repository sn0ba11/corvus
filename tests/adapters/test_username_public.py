import json
from pathlib import Path

import httpx
import pytest

from app.adapters import username_public
from app.adapters.username_public import UsernamePublicProfilesAdapter, lookup_spec
from app.models.schemas import QueryType


@pytest.mark.asyncio
async def test_exact_profiles_are_rich_and_partial_failures_do_not_erase_hits(tmp_path: Path):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert "authorization" not in request.headers
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"login": "alice", "id": 123, "name": "Public Alice", "bio": "Public bio", "followers": 12})
        if request.url.host == "gitlab.com":
            return httpx.Response(403)
        if request.url.host == "hacker-news.firebaseio.com":
            return httpx.Response(200, json={"id": "alice", "created": 1600000000, "about": "<p>Public profile</p>"})
        return httpx.Response(200, json={"handle": "alice.bsky.social", "did": "did:plc:fixture", "displayName": "Alice", "followersCount": 42})

    adapter = UsernamePublicProfilesAdapter(httpx.MockTransport(handler))
    result = await adapter.execute(adapter.build_command("@alice", QueryType.USERNAME, tmp_path, 30), tmp_path, 30, "@alice")
    findings = adapter.normalise_results(adapter.parse_results(result, tmp_path), "@alice", QueryType.USERNAME, "raw.json")
    positives = [finding for finding in findings if finding.status == "found"]
    assert len(requests) == 9
    assert {finding.platform for finding in positives} >= {"GitHub", "Hacker News", "Bluesky"}
    assert next(f for f in positives if f.platform == "GitHub").profile.followers == 12
    assert next(f for f in positives if f.platform == "Hacker News").profile.biography == "Public profile"
    assert next(f for f in positives if f.platform == "Bluesky").value == "alice.bsky.social"
    blocked = next(f for f in findings if f.platform == "GitLab")
    assert blocked.status == "unknown"
    assert blocked.metadata["lookup_status"] == "access_blocked"
    assert all(f.raw_output_reference == "raw.json" for f in findings)
    payload = json.loads((tmp_path / "public-username-profiles.json").read_text())
    assert payload["providers"]["github"]["response"]["id"] == 123


@pytest.mark.asyncio
async def test_mismatched_redirected_rate_limited_and_absent_profiles_are_distinct(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"login": "alice-extra", "id": 99})
        if request.url.host == "gitlab.com":
            return httpx.Response(302, headers={"location": "https://gitlab.com/users/sign_in"})
        if request.url.host == "hacker-news.firebaseio.com":
            return httpx.Response(200, content=b"null", headers={"content-type": "application/json"})
        return httpx.Response(429)

    adapter = UsernamePublicProfilesAdapter(httpx.MockTransport(handler))
    result = await adapter.execute(adapter.build_command("alice", QueryType.USERNAME, tmp_path, 30), tmp_path, 30, "alice")
    findings = adapter.normalise_results(adapter.parse_results(result, tmp_path), "alice", QueryType.USERNAME, "raw")
    assert not any(finding.status == "found" for finding in findings)
    by_platform = {finding.platform: finding for finding in findings}
    assert by_platform["GitHub"].metadata["lookup_status"] == "identifier_mismatch"
    assert by_platform["GitLab"].metadata["lookup_status"] == "redirect_not_followed"
    assert by_platform["Hacker News"].status == "not_found"
    assert by_platform["Bluesky"].status == "rate_limited"


@pytest.mark.asyncio
async def test_gitlab_filters_partial_username_hits_and_provider_timeout_is_isolated(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gitlab.com":
            return httpx.Response(200, json=[{"username": "alice-extra", "id": 1}, {"username": "Alice", "id": 2, "name": "Alice"}])
        raise httpx.ReadTimeout("controlled timeout")

    adapter = UsernamePublicProfilesAdapter(httpx.MockTransport(handler))
    result = await adapter.execute(adapter.build_command("alice", QueryType.USERNAME, tmp_path, 30), tmp_path, 30, "alice")
    findings = adapter.normalise_results(adapter.parse_results(result, tmp_path), "alice", QueryType.USERNAME, "raw")
    positives = [f for f in findings if f.status == "found"]
    assert len(positives) == 1
    assert positives[0].platform == "GitLab"
    assert positives[0].profile.account_id == "2"
    assert all(f.metadata["lookup_status"] == "timed_out" for f in findings if f.status != "found")


def test_provider_queries_do_not_guess_invalid_handles(tmp_path: Path):
    assert lookup_spec("github", "alice.name") is None
    assert lookup_spec("hackernews", "long_username_over_limit") is None
    assert lookup_spec("bluesky", "alice")[1] == "alice.bsky.social"
    assert lookup_spec("bluesky", "bsky.app")[1] == "bsky.app"
    with pytest.raises(ValueError):
        UsernamePublicProfilesAdapter().build_command("../../private", QueryType.USERNAME, tmp_path, 30)


NEW_BODIES = {
    "keybase.io": {"them": [{"id": "uid1", "basics": {"username": "Alice", "ctime": 1391653108},
                             "profile": {"full_name": "Alice K", "bio": "Keybase bio", "location": "Maine"},
                             "pictures": {"primary": {"url": "https://img.example/a.jpg"}},
                             "public_keys": {"primary": {"key_fingerprint": "ABCD1234"}},
                             "cryptocurrency_addresses": {"bitcoin": [{"address": "1BTCADDR"}]},
                             "proofs_summary": {"all": [
                                 {"proof_type": "twitter", "nametag": "alice_tw", "service_url": "https://twitter.com/alice_tw"},
                                 {"proof_type": "dns", "nametag": "alice.example", "service_url": "http://alice.example"},
                                 {"proof_type": "github", "nametag": "ab", "service_url": "https://github.com/ab"}]}}]},
    "dev.to": {"username": "alice", "id": 7, "name": "Alice Dev", "summary": "Dev bio", "location": "Paris",
               "joined_at": "Dec 27, 2015", "profile_image": "https://img.example/d.png",
               "github_username": "alice-gh", "twitter_username": "alice_tw", "website_url": "https://alice.example"},
    "api.chess.com": {"username": "alice", "player_id": 5, "name": "Alice Chess", "avatar": "https://img.example/c.png",
                      "country": "https://api.chess.com/pub/country/US", "location": "Florida", "followers": 9,
                      "joined": 1389043258, "last_online": 1794498995, "twitch_url": "https://twitch.tv/alice_tv",
                      "status": "premium", "verified": False},
    "lichess.org": {"id": "alice", "username": "Alice", "createdAt": 1290415680000, "seenAt": 1794505247601,
                    "profile": {"bio": "Li bio", "realName": "Alice Li", "flag": "CA",
                                "links": "github.com/alice\nhttps://alice.example/me"}, "verified": True},
    "codeberg.org": {"id": 3, "login": "alice", "full_name": "Alice CB", "email": "alice@noreply.codeberg.org",
                     "avatar_url": "https://img.example/cb.png", "location": "Berlin", "website": "https://alice.example",
                     "description": "CB bio", "created": "2022-11-06T07:18:11+01:00", "followers_count": 4, "following_count": 2},
    "hub.docker.com": {"id": "abc", "username": "alice", "full_name": "Alice D", "company": "Acme", "location": "NYC",
                       "date_joined": "2014-05-10T19:02:08Z", "gravatar_url": "https://img.example/g.png",
                       "profile_url": "https://alice.example", "type": "User"},
}
NEW_PLATFORMS = {"Keybase", "DEV Community", "Chess.com", "Lichess", "Codeberg", "Docker Hub"}


async def _run(tmp_path: Path, handler, query: str = "alice"):
    adapter = UsernamePublicProfilesAdapter(httpx.MockTransport(handler))
    result = await adapter.execute(adapter.build_command(query, QueryType.USERNAME, tmp_path, 30), tmp_path, 30, query)
    return adapter.normalise_results(adapter.parse_results(result, tmp_path), query, QueryType.USERNAME, "raw")


@pytest.mark.asyncio
async def test_new_providers_exact_matches_carry_details_and_cross_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # DEV Community is parsed but not queried by default; exercise its parsing here.
    monkeypatch.setattr(username_public, "PROVIDERS", (*username_public.PROVIDERS, "devto"))
    def handler(request: httpx.Request) -> httpx.Response:
        body = NEW_BODIES.get(request.url.host)
        return httpx.Response(200, json=body) if body else httpx.Response(404)

    findings = {f.platform: f for f in await _run(tmp_path, handler) if f.status == "found"}
    assert set(findings) == NEW_PLATFORMS
    urls = {p: f.canonical_url for p, f in findings.items()}
    assert urls == {"Keybase": "https://keybase.io/alice", "DEV Community": "https://dev.to/alice",
                    "Chess.com": "https://www.chess.com/member/alice", "Lichess": "https://lichess.org/@/alice",
                    "Codeberg": "https://codeberg.org/alice", "Docker Hub": "https://hub.docker.com/u/alice"}
    assert all(f.confidence == 0.98 for f in findings.values())

    def details(name):
        return {d["label"]: d["value"] for d in findings[name].metadata["platform_details"]}

    # Opaque provider IDs stay out of account_id, which is offered as a follow-up username.
    assert all(f.profile.account_id == "alice" for f in findings.values())
    assert details("Docker Hub")["Provider account ID"] == "abc"
    kb = findings["Keybase"]
    assert kb.profile.display_name == "Alice K" and kb.profile.location == "Maine"
    assert kb.metadata["created_at"].startswith("2014-")
    assert kb.metadata["linked_accounts"] == [
        {"platform": "Twitter", "username": "alice_tw", "url": "https://twitter.com/alice_tw", "source": "proofs_summary.all"}]
    assert kb.metadata["linked_sites"] == ["http://alice.example"]
    assert details("Keybase")["PGP fingerprint"] == "ABCD1234" and details("Keybase")["bitcoin address"] == "1BTCADDR"
    dev = findings["DEV Community"]
    assert [(x["platform"], x["username"], x["source"]) for x in dev.metadata["linked_accounts"]] == [
        ("GitHub", "alice-gh", "github_username"), ("Twitter", "alice_tw", "twitter_username")]
    assert dev.metadata["external_url"] == "https://alice.example" and dev.metadata["created_at"].startswith("2015-12-27")
    chess = findings["Chess.com"]
    assert chess.metadata["linked_accounts"][0]["username"] == "alice_tv" and chess.profile.followers == 9
    assert details("Chess.com")["Country"] == "US" and chess.metadata["last_seen_at"]
    li = findings["Lichess"]
    assert li.profile.display_name == "Alice Li" and li.metadata["linked_sites"] == ["https://github.com/alice", "https://alice.example/me"]
    assert details("Lichess")["Verified"] == "True" and li.metadata["created_at"].startswith("2010-")
    cb = findings["Codeberg"]
    assert cb.metadata["public_email"] is None and cb.profile.following == 2 and cb.profile.avatar_url
    assert findings["Docker Hub"].profile.biography is None and details("Docker Hub")["Company"] == "Acme"


@pytest.mark.asyncio
async def test_new_providers_reject_mismatches_closed_accounts_and_malformed_bodies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # DEV Community is parsed but not queried by default; exercise its parsing here.
    monkeypatch.setattr(username_public, "PROVIDERS", (*username_public.PROVIDERS, "devto"))
    def mismatch(request: httpx.Request) -> httpx.Response:
        if request.url.host == "keybase.io":
            return httpx.Response(200, json={"them": [None]})
        if request.url.host == "lichess.org":
            return httpx.Response(200, json={"id": "alice", "username": "alice", "disabled": True})
        body = {k: v for k, v in NEW_BODIES.items() if k != "keybase.io"}.get(request.url.host)
        if body:
            body = {**body, **{k: "bob" for k in ("username", "login")}}
        return httpx.Response(200, json=body or {})

    findings = await _run(tmp_path, mismatch)
    assert not [f for f in findings if f.finding_type == "profile" and f.platform in NEW_PLATFORMS]

    junk = {"them": 5, "username": 1, "login": [], "id": {}, "name": 3, "profile": "x", "proofs_summary": [],
            "cryptocurrency_addresses": 7, "links": 1}
    assert not [f for f in await _run(tmp_path, lambda r: httpx.Response(200, json=junk)) if f.status == "found"]
    wrong_types = {"username": "alice", "login": "alice", "id": [], "name": 4, "summary": {}, "profile": {"links": 5, "bio": []},
                   "github_username": 3, "twitter_username": [], "twitch_url": 9, "country": 1, "joined": "x",
                   "createdAt": "y", "followers": "many", "email": 1, "them": [{"basics": {"username": "alice"}, "proofs_summary": {"all": [1, None]}}]}
    found = [f for f in await _run(tmp_path, lambda r: httpx.Response(200, json=wrong_types)) if f.status == "found"]
    assert {f.platform for f in found} >= {"DEV Community", "Chess.com", "Lichess", "Codeberg", "Docker Hub"}
    assert all(f.metadata.get("linked_accounts", []) == [] for f in found)


def test_new_provider_username_validation():
    assert lookup_spec("keybase", "alice")[0].endswith("usernames=alice")
    assert lookup_spec("keybase", "a.b") is None
    assert lookup_spec("chesscom", "Hikaru")[1] == "hikaru"
    assert lookup_spec("dockerhub", "abc") is None and lookup_spec("dockerhub", "Jess1")[1] == "jess1"
    assert lookup_spec("lichess", "x") is None and lookup_spec("codeberg", "a.b-c")[1] == "a.b-c"
    assert lookup_spec("devto", "a-b") is None
