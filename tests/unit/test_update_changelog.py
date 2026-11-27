from pathlib import Path

from app.services.update_changelog import MAX_CHANGELOG_BYTES, load_update_changelog


def test_update_changelog_parses_installed_commits(tmp_path: Path) -> None:
    path = tmp_path / ".last-update-changes.txt"
    path.write_text(
        "\ufeffOSINT Workbench changes installed:\n"
        "@@CHANGE@@\n"
        "2026-08-06\n"
        "abc1234567890\n"
        "Improve managed browser parsing\n"
        "- Normalise extracted profile fields into the ordinary evidence model.\n"
        "- Preserve authenticated-session provenance and detailed platform metrics.\n"
        "- Show the result in the normal case view.\n"
        "- Keep the capture evidence and hashes attached.\n"
        "- This fifth line should be ignored.\n"
        "@@END@@\n"
        "@@CHANGE@@\n"
        "2026-08-05\n"
        "DEADBEEF\n"
        "Fix updater tracking\n"
        "Keep origin/main current after every successful source fetch.\n"
        "@@END@@\n",
        encoding="utf-8",
    )

    changelog = load_update_changelog(path)

    assert changelog is not None
    assert [item.commit for item in changelog.changes] == ["abc1234567890", "deadbeef"]
    assert changelog.changes[0].date == "2026-08-06"
    assert changelog.changes[0].summary == "Improve managed browser parsing"
    assert changelog.changes[0].details == (
        "Normalise extracted profile fields into the ordinary evidence model.",
        "Preserve authenticated-session provenance and detailed platform metrics.",
        "Show the result in the normal case view.",
        "Keep the capture evidence and hashes attached.",
    )


def test_update_changelog_remains_compatible_with_legacy_one_line_entries(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".last-update-changes.txt"
    path.write_text(
        "OSINT Workbench changes installed:\n"
        "2026-08-06  abc1234  Improve managed browser parsing\n",
        encoding="utf-8",
    )

    changelog = load_update_changelog(path)

    assert changelog is not None
    assert changelog.changes[0].summary == "Improve managed browser parsing"
    assert changelog.changes[0].details == ()


def test_update_changelog_accepts_initial_install_and_rejects_oversized_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".last-update-changes.txt"
    path.write_text(
        "OSINT Workbench changes installed:\nInitial installation.\n",
        encoding="utf-8",
    )
    changelog = load_update_changelog(path)
    assert changelog is not None
    assert changelog.changes[0].commit is None
    assert changelog.changes[0].summary == "Initial installation."

    path.write_bytes(b"x" * (MAX_CHANGELOG_BYTES + 1))
    assert load_update_changelog(path) is None


def test_update_changelog_returns_none_for_missing_or_empty_file(tmp_path: Path) -> None:
    path = tmp_path / ".last-update-changes.txt"
    assert load_update_changelog(path) is None
    path.write_text("\n", encoding="utf-8")
    assert load_update_changelog(path) is None
