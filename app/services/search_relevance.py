"""One rule for whether a web search result concerns a given username.

Shared by every collector that imports search results, so a result is judged the same way
whichever engine or tool returned it.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from app.services.profile_links import profile_account_from_url, social_platform_for_url


def handle_appears_in(handle: str, searchable: str) -> bool:
    """True when the handle appears as a whole token, not inside a longer handle or word."""
    if re.search(rf"(?<![a-z0-9_]){re.escape(handle)}(?![a-z0-9_])", searchable):
        return True
    # A handle with separators is often rendered without them (john.doe -> johndoe).
    compact_handle = "".join(ch for ch in handle if ch.isalnum())
    if compact_handle == handle or len(compact_handle) < 4:
        return False
    if re.search(rf"(?<![a-z0-9_]){re.escape(compact_handle)}(?![a-z0-9_])", searchable):
        return True
    # ...or with a different separator (john_doe -> john-doe). Whitespace is not a separator
    # here: "John Doe" is a name, and a name is not evidence about the handle.
    parts = [re.escape(part) for part in re.split(r"[._\-]+", handle) if part]
    return len(parts) > 1 and bool(
        re.search(r"(?<![a-z0-9_.\-])" + r"[._\-]+".join(parts) + r"(?![a-z0-9_.\-])", searchable)
    )


def username_result_is_relevant(
    handle: str, url: str, title: str | None = None, snippet: str | None = None
) -> bool:
    """Keep a result for a username search only when it concerns that exact handle.

    A page that merely mentions the word is not evidence about the handle. In a live run on
    2026-11-13 a search for one surname-like handle returned 38 results, of which one was the
    handle's own page; the others were videos, pins and accounts with the word in their name.
    """
    handle = handle.strip().lstrip("@").casefold()
    if not handle:
        return False
    original_url = url
    # Judge the address by its path only: a query string such as ?q=<handle> is the search
    # itself, not the page's subject.
    url = unquote(urlsplit(url).path).casefold()
    text = f"{title or ''} {snippet or ''}".casefold()
    mentioned = bool(re.search(rf"(?<![a-z0-9_.])(?:@|u/){re.escape(handle)}(?![a-z0-9_])", f"{url} {text}"))
    # Some sites give each account its own hostname (<handle>.tumblr.com, <handle>.github.io).
    labels = (urlsplit(original_url).hostname or "").casefold().split(".")
    if len(labels) >= 3 and labels[0] == handle:
        return True
    account = profile_account_from_url(original_url)
    if account is not None:
        # An account's own page: the account must be this handle, not one named like it.
        return account[1].casefold() == handle
    if social_platform_for_url(original_url) is not None:
        # A post, video or listing on a social site: the handle must own the path
        # (x.com/<handle>/status/..., reddit.com/user/<handle>/...) or be @-mentioned.
        segments = [part.removeprefix("@") for part in url.split("/") if part]
        owner = segments[1] if len(segments) > 1 and segments[0] in {"u", "user", "in", "add"} else (
            segments[0] if segments else "")
        return owner == handle or mentioned
    if handle.isalpha():
        # A handle that is an ordinary word (a name, a noun) appears in countless titles, so
        # the title alone proves nothing; require it in the address or as an @-mention.
        # In the address it must lead a path segment (/authors/<handle>, /<handle>-cv.pdf);
        # buried in a slug such as /2026/interview-with-linus-<handle> it is just the word again.
        leads_segment = any(
            re.fullmatch(rf"{re.escape(handle)}(?:[.\-].*)?", part) for part in url.split("/") if part
        )
        return leads_segment or mentioned
    return handle_appears_in(handle, f"{url} {title or ''}".casefold())
