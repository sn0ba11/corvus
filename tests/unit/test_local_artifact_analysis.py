import hashlib
import zipfile
from pathlib import Path

from app.services.local_artifact_analysis import analyze_retained_artifacts


def test_local_text_extraction_retains_hash_email_and_company_candidates(tmp_path: Path) -> None:
    root = tmp_path / "raw" / "case"
    root.mkdir(parents=True)
    content = b"Contact Jane at jane@example.org regarding Example Holdings Limited."
    path = root / "evidence.txt"
    path.write_bytes(content)

    analyses = analyze_retained_artifacts(root)
    assert len(analyses) == 1
    result = analyses[0]
    assert result.relative_path == "evidence.txt"
    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert result.emails == ("jane@example.org",)
    assert result.companies == ("Example Holdings Limited",)
    assert result.media_type == "text/plain"


def test_office_core_metadata_and_text_are_extracted_without_external_tools(tmp_path: Path) -> None:
    root = tmp_path / "raw" / "case"
    root.mkdir(parents=True)
    path = root / "brief.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "docProps/core.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
            <cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
              xmlns:dc="http://purl.org/dc/elements/1.1/">
              <dc:title>Case Brief</dc:title><dc:creator>Example Analyst</dc:creator>
            </cp:coreProperties>""",
        )
        archive.writestr(
            "word/document.xml",
            """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
            <w:body><w:p><w:r><w:t>support@example.net and Sample Services Ltd</w:t></w:r></w:p></w:body></w:document>""",
        )

    result = analyze_retained_artifacts(root)[0]
    assert result.metadata["title"] == "Case Brief"
    assert result.metadata["creator"] == "Example Analyst"
    assert result.emails == ("support@example.net",)
    assert result.companies == ("Sample Services Ltd",)


def test_artifact_analysis_is_bounded_to_supported_files_and_size(tmp_path: Path) -> None:
    root = tmp_path / "raw" / "case"
    root.mkdir(parents=True)
    (root / "ignored.bin").write_bytes(b"ignored")
    (root / "empty.txt").write_bytes(b"")
    (root / "kept.json").write_text('{"email":"kept@example.org"}', encoding="utf-8")

    results = analyze_retained_artifacts(root)
    assert [item.relative_path for item in results] == ["kept.json"]
    assert results[0].emails == ("kept@example.org",)
