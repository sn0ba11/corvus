import json
from pathlib import Path

from app.adapters.base import ExecutionResult
from app.adapters.builtin import NaminterAdapter
from app.models.schemas import FindingStatus, QueryType


def test_naminter_uses_pinned_local_wmn_and_browser_impersonation(tmp_path: Path) -> None:
    command = NaminterAdapter().build_command("example_user", QueryType.USERNAME, tmp_path, 120)
    assert command[:3] == ["naminter", "--username", "example_user"]
    assert command[command.index("--local-data") + 1] == "/opt/tool/wmn-data.json"
    assert command[command.index("--impersonate") + 1] == "chrome"
    assert "--json" in command
    assert "--filter-all" in command


def test_naminter_bare_found_result_remains_unverified_lead(tmp_path: Path) -> None:
    (tmp_path / "naminter.json").write_text(
        json.dumps(
            [
                {
                    "username": "example_user",
                    "site_name": "Example",
                    "result_url": "https://example.com/example_user",
                    "result_status": "found",
                }
            ]
        ),
        encoding="utf-8",
    )
    adapter = NaminterAdapter()
    result = ExecutionResult(returncode=0, stdout="", stderr="")
    records = adapter.parse_results(result, tmp_path)
    findings = adapter.normalise_results(records, "example_user", QueryType.USERNAME, "raw.json")

    assert len(findings) == 1
    assert findings[0].status == FindingStatus.UNKNOWN
    assert findings[0].finding_type == "unverified_profile_lead"
    assert findings[0].confidence == 0.20
    assert findings[0].display_url == "https://example.com/example_user"
