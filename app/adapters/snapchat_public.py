"""Exact Snapchat public profile hydration; no signup or presence probing."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from app.adapters.telegram_public import TelegramPublicAdapter, _TelegramPageParser
from app.models.schemas import FindingRecord, FindingStatus, ProfileEvidence, QueryType


class _SnapchatPageParser(_TelegramPageParser):
    def __init__(self) -> None:
        super().__init__()
        self.capture = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        super().handle_starttag(tag, attrs)
        if tag.casefold() == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.capture = True

    def handle_endtag(self, tag: str) -> None:
        super().handle_endtag(tag)
        if tag.casefold() == "script":
            self.capture = False

    def handle_data(self, data: str) -> None:
        super().handle_data(data)
        if self.capture:
            self.parts.append(data)

    def result(self, username: str) -> dict[str, Any]:
        unknown = {"status": "unknown", "username": username,
                   "limitation": "No matching Snapchat public profile hydration"}
        try:
            root = json.loads("".join(self.parts))
            user_profile = root["props"]["pageProps"]["userProfile"]
        except (ValueError, KeyError, TypeError, RecursionError):
            return unknown
        if not isinstance(user_profile, dict):
            return unknown
        # Creator and business accounts publish publicProfileInfo; ordinary accounts publish
        # the smaller userInfo block (display name, snapcode and Bitmoji only).
        profile, kind = user_profile.get("publicProfileInfo"), "public_profile"
        if not isinstance(profile, dict):
            profile, kind = user_profile.get("userInfo"), "user"
        if not isinstance(profile, dict):
            return unknown
        if str(profile.get("username", "")).casefold() != username.casefold():
            return unknown
        keys = ("username", "title", "displayName", "bio", "subscriberCount", "websiteUrl",
                "profilePictureUrl", "snapcodeImageUrl", "businessProfileId", "hostUserId",
                "categoryStringId", "subcategoryStringId", "badge", "address")
        fields: dict[str, Any] = {
            key: value[:4000] if isinstance(value, str) else value
            for key in keys if isinstance(value := profile.get(key), str | int | bool) and value != ""
        }
        fields["profile_kind"] = kind
        for key, target in (("creationTimestampMs", "created_at"), ("lastUpdateTimestampMs", "updated_at")):
            if stamp := _timestamp(profile.get(key)):
                fields[target] = stamp
        bitmoji = profile.get("bitmoji3d")
        avatar = bitmoji.get("avatarImage") if isinstance(bitmoji, dict) else None
        if isinstance(avatar, dict):
            for key in ("fallbackUrl", "url"):
                if isinstance(avatar.get(key), str) and avatar[key].startswith("https://"):
                    fields["bitmojiImageUrl"] = avatar[key][:2000]
                    break
        links = profile.get("sameAsLinks")
        if isinstance(links, list):
            same_as = [item[:2000] for item in links[:20] if isinstance(item, str) and item.startswith("https://")]
            if same_as:
                fields["sameAsLinks"] = same_as
        related = profile.get("relatedAccountsInfo")
        if isinstance(related, list):
            accounts = []
            for item in related[:20]:
                info = item.get("publicProfileInfo") if isinstance(item, dict) else None
                handle = info.get("username") if isinstance(info, dict) else None
                if isinstance(info, dict) and isinstance(handle, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{2,29}", handle):
                    title = info.get("title")
                    accounts.append({"account": handle, "title": title[:300] if isinstance(title, str) else None})
            if accounts:
                fields["relatedAccounts"] = accounts
        name = fields.get("title") or fields.get("displayName")
        if not name:
            return unknown
        return {"status": "found", "username": username, "profile": fields,
                "source_scope": "snapchat_public_profile_hydration"}


def _timestamp(value: Any) -> str | None:
    raw = value.get("value") if isinstance(value, dict) else value
    try:
        millis = int(str(raw))
        return datetime.fromtimestamp(millis / 1000, UTC).isoformat() if 0 < millis < 10**14 else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class SnapchatPublicAdapter(TelegramPublicAdapter):
    tool_id = "snapchat_public"
    display_name = "Snapchat Public Profile"
    executable = "snapchat-public-profile"
    platform_name = "Snapchat"
    profile_url_template = "https://www.snapchat.com/@{username}"
    result_filename = "snapchat-public.json"
    username_pattern = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{2,29}$")
    page_parser = _SnapchatPageParser
    preview_url_template = None
    capabilities = frozenset({"username_search", "snapchat_public_profile", "profile_image"})
    default_selected = True

    def normalise_results(self, records: list[dict[str, Any]], query: str, query_type: QueryType,
                          raw_reference: str | None) -> list[FindingRecord]:
        findings = []
        for record in records[:1]:
            if record.get("status") == "unsupported_username":
                continue
            username = str(record.get("username") or query).removeprefix("@")
            profile = record.get("profile")
            profile = profile if isinstance(profile, dict) else {}
            positive = record.get("status") == "found" and bool(profile)
            state = FindingStatus.FOUND if positive else FindingStatus.NOT_FOUND if record.get("status") == "not_found" else FindingStatus.UNKNOWN
            count = str(profile.get("subscriberCount", ""))
            followers = int(count) if count.isdigit() and len(count) <= 15 else None
            details = [{"label": label, "value": str(profile[key])} for key, label in (
                ("businessProfileId", "Public profile ID"), ("hostUserId", "Account ID"),
                ("websiteUrl", "Website"), ("address", "Address"), ("created_at", "Profile created"),
                ("updated_at", "Profile last updated"), ("snapcodeImageUrl", "Snapcode image"),
                ("bitmojiImageUrl", "Bitmoji image"),
                ("categoryStringId", "Category"), ("subcategoryStringId", "Subcategory"),
            ) if profile.get(key)]
            if positive:
                details.append({"label": "Profile type", "value": (
                    "Public creator or business profile" if profile.get("profile_kind") == "public_profile"
                    else "Ordinary account (name, snapcode and Bitmoji only)")})
            same_as = [link for link in profile.get("sameAsLinks", []) if isinstance(link, str)]
            details.extend({"label": "Same person elsewhere", "value": link} for link in same_as)
            related = [item for item in profile.get("relatedAccounts", []) if isinstance(item, dict)]
            details.extend({"label": "Related Snapchat account", "value": (
                f"{item.get('title')} (@{item.get('account')})" if item.get("title") else f"@{item.get('account')}"
            )} for item in related)
            name = profile.get("title") or profile.get("displayName")
            metadata = {**record, "platform_details": details}
            if same_as:
                # Same-as links are the account owner's own cross-references.
                metadata["linked_sites"] = same_as
            findings.append(FindingRecord(
                query_type=query_type, queried_value=query, finding_type="profile", platform="Snapchat",
                value=username, canonical_url=self.profile_url_template.format(username=username),
                status=state, confidence=(0.9 if profile.get("profile_kind") == "public_profile" else 0.8) if positive else 0.0,
                title=name or username,
                profile=ProfileEvidence(account_id=username, display_name=name,
                    biography=profile.get("bio"),
                    avatar_url=profile.get("profilePictureUrl") or profile.get("bitmojiImageUrl"),
                    followers=followers),
                metadata=metadata, source_tool=self.tool_id,
                source_record_id=str(profile.get("businessProfileId") or username), raw_output_reference=raw_reference,
            ))
        return findings
