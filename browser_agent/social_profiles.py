"""Bounded, read-only extraction of profile fields from rendered pages."""

from __future__ import annotations

import json
import re
from typing import Any

PROFILE_KEYS = frozenset({
    "username", "userName", "uniqueId", "alternateName", "vanity", "snap_user_name",
    "id", "user_id", "userid", "account_id", "secUid", "displayName", "display_name",
    "nickname", "full_name", "name", "global_name", "signature", "biography", "bio",
    "description", "avatarLarger", "avatarMedium", "avatarThumb", "avatar_url",
    "profile_pic_url", "profile_pic_url_hd", "image", "url", "profile_url",
    "verified", "is_verified", "isVerified", "privateAccount", "is_private",
    "followerCount", "followingCount", "followers", "following", "followers_count",
    "following_count", "subscriber_count", "subscriberCount", "videoCount", "heartCount",
    "heart", "media_count", "post_count", "location", "city", "hometown", "category",
    "category_name", "business_category_name", "external_url", "website", "createTime",
    "created_at", "public_email", "public_phone_number", "is_business_account",
    "title", "profilePictureUrl", "websiteUrl", "businessProfileId", "hostUserId",
    "snapcodeImageUrl", "categoryStringId", "subcategoryStringId",
})
IDENTITY_KEYS = frozenset({"username", "userName", "uniqueId", "alternateName", "vanity",
                           "snap_user_name", "id", "user_id", "account_id"})


def profile_objects(roots: list[Any]) -> list[dict[str, Any]]:
    """Keep allowlisted profile fields; never export tokens or arbitrary app state."""
    pending = [(root, 0) for root in roots[:100]]
    output: list[dict[str, Any]] = []
    visited = 0
    while pending and visited < 5000 and len(output) < 100:
        value, depth = pending.pop()
        visited += 1
        if depth > 16:
            continue
        if isinstance(value, list):
            pending.extend((child, depth + 1) for child in value[:100])
        elif isinstance(value, dict):
            pending.extend((child, depth + 1) for child in list(value.values())[:100])
            candidates = [value]
            for key in ("user", "profile", "account", "publicProfileInfo"):
                if isinstance(value.get(key), dict):
                    candidates.append({**value[key], **{
                        field: item for stats_key in ("stats", "statsV2", "counts")
                        if isinstance(value.get(stats_key), dict)
                        for field, item in value[stats_key].items()
                    }})
            for candidate in candidates:
                if not IDENTITY_KEYS.intersection(candidate):
                    continue
                clean: dict[str, Any] = {key: item[:4000] if isinstance(item, str) else item
                         for key, item in candidate.items() if key in PROFILE_KEYS
                         and isinstance(item, str | int | float | bool)}
                for key in ("edge_followed_by", "edge_follow", "edge_owner_to_timeline_media"):
                    count = candidate.get(key)
                    if isinstance(count, dict) and isinstance(count.get("count"), int):
                        clean[key] = {"count": count["count"]}
                if clean not in output:
                    output.append(clean)
                if len(output) >= 100:
                    break
    return output


async def collect_profile_objects(page: Any) -> list[dict[str, Any]]:
    nodes = page.locator('script[type="application/json"],script[type="application/ld+json"],script#__NEXT_DATA__,script#__UNIVERSAL_DATA_FOR_REHYDRATION__,script#SIGI_STATE')
    roots: list[Any] = []
    total = 0
    for index in range(min(await nodes.count(), 100)):
        raw = await nodes.nth(index).text_content() or ""
        total += len(raw)
        if total > 2_000_000:
            break
        if len(raw) > 750_000:
            continue
        try:
            roots.append(json.loads(raw))
        except (ValueError, RecursionError):
            continue
    return profile_objects(roots)


async def visible_contact_profile(page: Any, platform: str) -> dict[str, Any]:
    """Only inspect an operator-opened contact/profile panel; never open chats."""
    selector = ('[data-testid="contact-info-drawer"], [data-testid="drawer-right"]'
                if platform == "whatsapp" else '[role="dialog"]')
    panels = page.locator(selector)
    for index in range(min(await panels.count(), 5)):
        panel = panels.nth(index)
        if not await panel.is_visible():
            continue
        text = (await panel.inner_text())[:8000]
        images = panel.locator("img[src]")
        avatar = None
        account_id = None
        for pos in range(min(await images.count(), 10)):
            src = await images.nth(pos).get_attribute("src") or ""
            if platform == "discord":
                match = re.match(r"https://cdn\.discordapp\.com/avatars/(\d{17,20})/", src)
                if match:
                    account_id, avatar = match.group(1), src[:2048]
                    break
            elif src.startswith("https://"):
                avatar = src[:2048]
                break
        if platform == "whatsapp":
            numbers = {re.sub(r"\D", "", number) for number in
                       re.findall(r"\+[1-9][0-9 ()-]{6,24}[0-9]", text)}
            if len(numbers) != 1:
                continue
            account_id = next(iter(numbers))
            if not 7 <= len(account_id) <= 15:
                continue
        if not account_id:
            continue
        headings = panel.locator("h1,h2,h3")
        label = (await headings.first.inner_text())[:300] if await headings.count() else None
        return {"account_id": account_id, "avatar_url": avatar, "panel_label": label,
                "source_scope": "operator_opened_profile_panel"}
    return {}
