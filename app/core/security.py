import hashlib
import hmac
import re
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|password|proxy-authorization)\s*[:=]\s*\S+"),
    re.compile(r"(?i)https?://[^\s:@/]+:[^\s@/]+@"),
]


def make_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def verify_csrf(expected: str | None, supplied: str | None) -> bool:
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


def redact(text: str, query: str | None = None) -> str:
    clean = text.replace(query, "[QUERY]") if query else text
    for pattern in SECRET_PATTERNS:
        clean = pattern.sub("[REDACTED]", clean)
    return clean[:8000]


def canonicalise_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    host = parts.hostname.lower().rstrip(".")
    default_port = (parts.scheme.lower() == "http" and port == 80) or (
        parts.scheme.lower() == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = (parts.path or "/").rstrip("/") or "/"
    pairs = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMS
    ]
    pairs.sort(key=lambda item: (item[0], item[1]))
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(pairs), ""))


def password_matches(password: str, encoded: str) -> bool:
    try:
        _, n, r, p, salt, digest = encoded.split("$")
        actual = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p)
        ).hex()
        return hmac.compare_digest(actual, digest)
    except (ValueError, TypeError):
        return False


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("local authentication password must be at least 12 characters")
    n, r, p = 16384, 8, 1
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p).hex()
    return f"scrypt${n}${r}${p}${salt.hex()}${digest}"
