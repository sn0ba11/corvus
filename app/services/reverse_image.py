import ipaddress
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlencode, urlsplit


class ReverseImageSearchError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReverseImageTarget:
    provider: str
    label: str
    method: Literal["get", "post"]
    url: str
    fields: dict[str, str]


PROVIDER_LABELS = {
    "google": "Google Lens",
    "yandex": "Yandex Images",
    "bing": "Bing Visual Search",
    "tineye": "TinEye",
}

_LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".internal", ".home", ".home.arpa")


def validate_public_image_url(source_url: str) -> str:
    """Reject local, credential-bearing and non-HTTP image URLs before disclosure."""
    if not isinstance(source_url, str) or not source_url or len(source_url) > 4096:
        raise ReverseImageSearchError("invalid image URL")

    parsed = urlsplit(source_url)
    if parsed.scheme not in {"http", "https"}:
        raise ReverseImageSearchError("only HTTP(S) image URLs are supported")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ReverseImageSearchError("invalid image URL")

    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or "." not in host or host.endswith(_LOCAL_SUFFIXES):
        raise ReverseImageSearchError("local image URLs cannot be sent to external providers")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ReverseImageSearchError("private or non-routable image URLs are blocked")

    return source_url


def reverse_image_target(provider: str, source_url: str) -> ReverseImageTarget:
    source_url = validate_public_image_url(source_url)
    label = PROVIDER_LABELS.get(provider)
    if label is None:
        raise ReverseImageSearchError("unsupported reverse-image provider")

    if provider == "google":
        return ReverseImageTarget(
            provider,
            label,
            "get",
            f"https://lens.google.com/uploadbyurl?{urlencode({'url': source_url})}",
            {},
        )
    if provider == "yandex":
        return ReverseImageTarget(
            provider,
            label,
            "get",
            f"https://yandex.com/images/search?{urlencode({'rpt': 'imageview', 'url': source_url})}",
            {},
        )
    if provider == "tineye":
        return ReverseImageTarget(
            provider,
            label,
            "get",
            f"https://tineye.com/search?{urlencode({'url': source_url})}",
            {},
        )

    # Bing's supported browser flow accepts the image URL as a form POST.
    return ReverseImageTarget(
        provider,
        label,
        "post",
        "https://www.bing.com/images/detail/search?iss=sbi&FORM=CHROMI#enterInsights",
        {"imgurl": source_url},
    )


def google_lens_url(source_url: str) -> str:
    """Backwards-compatible helper for callers that only need Google Lens."""
    return reverse_image_target("google", source_url).url
