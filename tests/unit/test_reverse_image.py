from urllib.parse import parse_qs, urlsplit

import pytest

from app.services.reverse_image import (
    ReverseImageSearchError,
    google_lens_url,
    reverse_image_target,
)


def test_google_lens_url_encodes_public_avatar_url() -> None:
    source = "https://scontent.example.net/avatar.jpg?token=a+b&size=hd"

    target = google_lens_url(source)

    parsed = urlsplit(target)
    assert parsed.scheme == "https"
    assert parsed.netloc == "lens.google.com"
    assert parsed.path == "/uploadbyurl"
    assert parse_qs(parsed.query) == {"url": [source]}


@pytest.mark.parametrize(
    ("provider", "hostname", "path", "expected_query"),
    [
        ("google", "lens.google.com", "/uploadbyurl", {"url"}),
        ("yandex", "yandex.com", "/images/search", {"rpt", "url"}),
        ("tineye", "tineye.com", "/search", {"url"}),
    ],
)
def test_get_reverse_image_providers_encode_public_url(
    provider: str, hostname: str, path: str, expected_query: set[str]
) -> None:
    source = "https://images.example.org/profile.jpg?size=large&token=a+b"

    target = reverse_image_target(provider, source)

    parsed = urlsplit(target.url)
    assert target.method == "get"
    assert parsed.hostname == hostname
    assert parsed.path == path
    assert set(parse_qs(parsed.query)) == expected_query
    assert parse_qs(parsed.query)["url"] == [source]


def test_bing_reverse_image_provider_uses_form_post() -> None:
    source = "https://images.example.org/profile.jpg"

    target = reverse_image_target("bing", source)

    assert target.method == "post"
    assert target.url.startswith("https://www.bing.com/images/detail/search?")
    assert target.fields == {"imgurl": source}


@pytest.mark.parametrize(
    "source",
    [
        "http://127.0.0.1/avatar.jpg",
        "http://192.168.1.63/avatar.jpg",
        "http://[::1]/avatar.jpg",
        "http://workbench.local/avatar.jpg",
        "http://intranet/avatar.jpg",
        "file:///tmp/avatar.jpg",
        "https://user:password@example.com/avatar.jpg",
    ],
)
def test_reverse_image_providers_reject_private_or_unsafe_sources(source: str) -> None:
    for provider in ("google", "yandex", "bing", "tineye"):
        with pytest.raises(ReverseImageSearchError):
            reverse_image_target(provider, source)


def test_reverse_image_provider_rejects_unknown_provider() -> None:
    with pytest.raises(ReverseImageSearchError, match="unsupported"):
        reverse_image_target("unknown", "https://images.example.org/avatar.jpg")
