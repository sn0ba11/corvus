import pytest

from app.core.config import Settings
from app.core.security import canonicalise_url, hash_password, password_matches, redact


def test_url_canonicalisation_retains_meaningful_query() -> None:
    assert (
        canonicalise_url("HTTPS://Example.COM:443/user/?id=7&utm_source=x&fbclid=y#bio")
        == "https://example.com/user?id=7"
    )


def test_distinct_accounts_not_merged_by_display_name() -> None:
    assert canonicalise_url("https://one.example/sam") != canonicalise_url(
        "https://two.example/sam"
    )


def test_secret_and_query_redaction() -> None:
    output = redact(
        "email target@example.org api_key=secret https://user:pass@proxy/", "target@example.org"
    )
    assert "target@example.org" not in output
    assert "secret" not in output
    assert "pass" not in output


def test_local_auth_password_hash_round_trip() -> None:
    encoded = hash_password("controlled-test-password")
    assert password_matches("controlled-test-password", encoded)
    assert not password_matches("wrong-password", encoded)


def test_non_loopback_binding_without_auth_is_rejected() -> None:
    with pytest.raises(ValueError, match="local authentication is required"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            bind_host="0.0.0.0",  # noqa: S104 - deliberate rejection test
            local_auth_enabled=False,
        )
