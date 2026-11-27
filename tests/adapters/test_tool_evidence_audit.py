from typing import Any

from app.adapters.base import Adapter, ExecutionResult
from app.adapters.builtin import MaigretAdapter, NaminterAdapter, _is_site_generic_label
from app.models.schemas import FindingStatus, QueryType


def _maigret(record: dict[str, Any], query: str = "torvalds") -> Any:
    base = {"site": "Site", "url": f"https://site.example/{query}", "status": "Claimed"}
    findings = MaigretAdapter().normalise_results(
        [{**base, **record}], query, QueryType.USERNAME, "raw.json"
    )
    assert len(findings) == 1
    return findings[0]


def test_generic_label_helper() -> None:
    assert _is_site_generic_label("VK | VK", "VK")
    assert _is_site_generic_label("Log in on Example", "Example")
    assert not _is_site_generic_label("Linus Torvalds", "VK")
    assert not _is_site_generic_label("", "VK")


def test_maigret_site_name_fullname_is_not_evidence() -> None:
    finding = _maigret({"site": "VK", "fullname": "VK | VK", "username": "torvalds"})
    assert finding.status == FindingStatus.UNKNOWN
    assert finding.confidence == 0.15
    assert finding.finding_type == "unverified_profile_lead"
    assert finding.metadata["maigret_discarded_fields"] == ["fullname"]


def test_maigret_real_fullname_is_kept() -> None:
    finding = _maigret({"site": "VK", "fullname": "Linus Torvalds", "username": "torvalds"})
    assert finding.finding_type == "profile"
    assert finding.confidence == 0.70


def test_maigret_different_account_name_is_a_lead() -> None:
    finding = _maigret({"site": "StackOverflow", "username": "Victor Torvalds", "uid": "1"})
    assert finding.status == FindingStatus.UNKNOWN
    assert finding.finding_type == "unverified_profile_lead"
    assert finding.metadata["maigret_evidence_strength"] == "different_account_name"
    assert finding.metadata["maigret_reported_username"] == "Victor Torvalds"


def test_maigret_username_differing_by_case_or_separator_still_matches() -> None:
    for name in ("Torvalds", "tor_valds", "tor-valds"):
        finding = _maigret({"username": name, "uid": "1"})
        assert finding.finding_type == "profile", name


def test_naminter_partial_is_unverified_lead() -> None:
    findings = NaminterAdapter().normalise_results(
        [{"site_name": "Example", "result_url": "https://e.example/u", "result_status": "partial"}],
        "u",
        QueryType.USERNAME,
        "raw.json",
    )
    assert findings[0].status == FindingStatus.UNKNOWN
    assert findings[0].finding_type == "unverified_profile_lead"
    assert findings[0].confidence == 0.20
    assert findings[0].metadata["naminter_partial_match"] is True


def test_maigret_coverage_warning() -> None:
    adapter = MaigretAdapter()
    dns = ExecutionResult(
        0,
        '[!] Too many errors of type "Connecting failure (DNS)" (87.03%). Dns resolution failed',
        "",
    )
    note = adapter.coverage_warning(dns)
    assert note is not None
    assert "87%" in note and "Connecting failure (DNS)" in note
    bot = ExecutionResult(0, '[!] Too many errors of type "Bot protection" (6.68%).', "")
    assert adapter.coverage_warning(bot) is None
    assert adapter.coverage_warning(ExecutionResult(0, "", "")) is None


def test_base_adapter_has_no_coverage_warning() -> None:
    assert Adapter.coverage_warning(NaminterAdapter(), ExecutionResult(0, "", "")) is None


def test_maigret_site_name_equal_to_searched_handle_is_kept() -> None:
    finding = _maigret({"site": "Duolingo", "fullname": "Duolingo", "username": "duolingo"}, "duolingo")
    assert finding.finding_type == "profile"


def test_naminter_site_name_is_not_the_account_display_name() -> None:
    from app.adapters.builtin import get_adapter
    from app.models.schemas import QueryType

    finding = get_adapter("naminter").normalise_results(
        [{"name": "GitHub", "uri_check": "https://github.com/x1", "status": "exists"}],
        "x1",
        QueryType.USERNAME,
        None,
    )[0]

    assert finding.platform == "GitHub"
    assert finding.profile is not None and finding.profile.display_name is None


def test_network_failure_warning_for_container_tools() -> None:
    from app.adapters.builtin import BlackbirdAdapter, SherlockAdapter

    for adapter, name in ((BlackbirdAdapter(), "Blackbird"), (SherlockAdapter(), "Sherlock")):
        note = adapter.coverage_warning(ExecutionResult(0, "", "", network_ok=False))
        assert note is not None and name in note
        assert adapter.coverage_warning(ExecutionResult(0, "", "", network_ok=True)) is None
        assert adapter.coverage_warning(ExecutionResult(0, "", "", network_ok=None)) is None


def test_sherlock_name_resolution_output_warns() -> None:
    from app.adapters.builtin import SherlockAdapter

    result = ExecutionResult(0, "", "A problem occurred: NameResolutionError")
    note = SherlockAdapter().coverage_warning(result)
    assert note is not None and "hostname lookup failure" in note


def test_maigret_text_is_not_parsed_for_other_tools() -> None:
    from app.adapters.builtin import BlackbirdAdapter

    text = '[!] Too many errors of type "Connecting failure (DNS)" (87.03%). Dns resolution failed'
    assert "Maigret could not reach" in (MaigretAdapter().coverage_warning(ExecutionResult(0, text, "")) or "")
    assert BlackbirdAdapter().coverage_warning(ExecutionResult(0, text, "")) is None
