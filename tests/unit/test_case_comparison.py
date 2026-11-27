from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models.db import Finding, Investigation
from app.services.case_comparison import compare_cases, safe_url


def case(*findings: Finding) -> Investigation:
    item = Investigation(id=str(uuid4()), title="Case", query="shared-name", query_type="username")
    item.findings = list(findings)
    return item


def finding(**overrides):  # type: ignore[no-untyped-def]
    values = {"id": str(uuid4()), "tool_run_id": str(uuid4()), "status": "found",
              "finding_type": "profile", "platform": "GitHub", "source_tool": "fixture",
              "source_tools": ["fixture"], "observed_at": datetime.now(UTC), "extra": {}}
    return Finding(**(values | overrides))


def test_shared_signals_keep_exact_provenance_and_distinct_signal_types():
    extra = {"profile": {"account_id": "12345"}, "public_email": "person@EXAMPLE.org", "image_cache": {"sha256": "a" * 64}}
    first = finding(canonical_url="https://EXAMPLE.org/profile?id=1#bio", extra=extra)
    second = finding(canonical_url="https://example.org/profile?id=1", extra=extra)
    report = compare_cases([case(first), case(second)])
    assert {signal.kind for signal in report.signals} == {"url", "email", "account", "image"}
    assert len(report.signals) == 4
    for signal in report.signals:
        assert {e.finding_id for e in signal.evidence} == {first.id, second.id}
        assert all(e.sources == ("fixture",) for e in signal.evidence)


def test_names_usernames_providers_errors_masked_emails_and_different_paths_do_not_match():
    first = case(finding(value="same-user", canonical_url="https://example.org/Alice", extra={"public_email": "one@example.org"}),
                 finding(status="error", canonical_url="https://example.org/shared"),
                 finding(extra={"public_email": "a***@example.org"}))
    second = case(finding(value="same-user", canonical_url="https://example.org/alice", extra={"public_email": "two@example.org"}),
                  finding(canonical_url="https://example.org/shared"),
                  finding(extra={"public_email": "a***@example.org"}))
    assert compare_cases([first, second]).signals == ()


def test_repeated_observations_in_only_one_case_are_not_cross_case_matches():
    same = {"canonical_url": "https://example.org/shared"}
    assert compare_cases([case(finding(**same), finding(**same)), case(finding())]).signals == ()


def test_comparison_uses_real_developer_identity_metadata_contracts():
    first = case(finding(finding_type="github_commit_identity", extra={"email": "developer@example.org"}),
                 finding(finding_type="github_ssh_signing_key", extra={"fingerprint": "SHA256:" + "A" * 43}))
    second = case(finding(finding_type="github_profile_discovery", extra={"source_metadata": {"github_discovery": {"email": "developer@example.org"}}}),
                  finding(finding_type="github_ssh_signing_key", extra={"fingerprint": "SHA256:" + "A" * 43}))
    report = compare_cases([first, second])
    assert {signal.kind for signal in report.signals} == {"email", "key"}


def test_comparison_bounds_are_explicit(monkeypatch):  # type: ignore[no-untyped-def]
    from app.services import case_comparison

    extra = {"public_email": "person@example.org"}
    first = case(finding(canonical_url="https://example.org/", extra=extra))
    second = case(finding(canonical_url="https://example.org/", extra=extra))
    monkeypatch.setattr(case_comparison, "MAX_SIGNALS", 1)
    report = compare_cases([first, second])
    assert report.truncated and report.total_shared_signals == 2
    assert len(report.signals) == 1
    monkeypatch.setattr(case_comparison, "MAX_FINDINGS", 1)
    with pytest.raises(ValueError, match="at most"):
        compare_cases([first, second])


@pytest.mark.parametrize("value", ["javascript:alert(1)", "https://user:password@example.org/", "https://example.org:invalid/a", "https://example.org/\nfile"])
def test_comparison_never_renders_unsafe_source_links(value):  # type: ignore[no-untyped-def]
    assert safe_url(value) is None
