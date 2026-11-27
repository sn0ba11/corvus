from pathlib import Path

import pytest

from app.adapters.phone_offline import PhoneOfflineAdapter
from app.models.schemas import FindingStatus, QueryType


@pytest.mark.asyncio
async def test_phone_adapter_runs_entirely_offline(tmp_path: Path) -> None:
    adapter = PhoneOfflineAdapter()
    command = adapter.build_command("087 123 4567", QueryType.PHONE, tmp_path, 10)
    result = await adapter.execute(command, tmp_path, 10, "087 123 4567")
    assert result.returncode == 0
    records = adapter.parse_results(result, tmp_path)
    findings = adapter.normalise_results(
        records, "087 123 4567", QueryType.PHONE, "phone-offline.json"
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.status == FindingStatus.FOUND
    assert finding.value == "+353871234567"
    assert finding.platform == "Telephone numbering plan"
    assert finding.metadata["numbering_region"] == "Ireland"
    assert finding.source_tool == "phone_offline"


def test_phone_adapter_rejects_non_phone_query_types(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="phone investigations only"):
        PhoneOfflineAdapter().build_command("example", QueryType.USERNAME, tmp_path, 10)
