from pathlib import Path

from app.services.update_build_range import MAX_UPDATE_RANGE_BYTES, load_update_build_range


def test_update_build_range_parses_valid_manifest(tmp_path: Path) -> None:
    path = tmp_path / "last-update-range.json"
    path.write_text(
        '{"from_commit":"0000000","to_commit":"facefeed01234567890123456789012345678901","fallback_used":true}\n',
        encoding="utf-8",
    )

    build_range = load_update_build_range(path)

    assert build_range is not None
    assert build_range.from_commit == "0000000"
    assert build_range.to_commit == "facefeed01234567890123456789012345678901"
    assert build_range.fallback_used


def test_update_build_range_rejects_invalid_or_oversized_manifest(tmp_path: Path) -> None:
    path = tmp_path / "last-update-range.json"
    path.write_text('{"from_commit":"not-a-commit","to_commit":"f122f81"}', encoding="utf-8")
    assert load_update_build_range(path) is None

    path.write_bytes(b"x" * (MAX_UPDATE_RANGE_BYTES + 1))
    assert load_update_build_range(path) is None


def test_update_build_range_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert load_update_build_range(tmp_path / "missing.json") is None
