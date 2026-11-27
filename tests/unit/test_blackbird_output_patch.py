import ast
from hashlib import sha256

import pytest

from tool_containers import patch_blackbird_output


def test_patch_replaces_only_the_expected_assignment(monkeypatch):
    statement = "strPath = default_output_root()"
    digest = sha256(ast.dump(ast.parse(statement).body[0]).encode()).hexdigest()
    monkeypatch.setattr(patch_blackbird_output, "ORIGINAL_ASSIGNMENT_DIGEST", digest)
    source = f"def test_output(folderName):\n    {statement}\n    return strPath\n"

    patched = patch_blackbird_output.patch_source(source)

    assert "BLACKBIRD_RESULTS_DIR" in patched
    assert statement not in patched
    assert "    return strPath" in patched
    ast.parse(patched)


def test_patch_rejects_unexpected_upstream_code():
    with pytest.raises(ValueError, match="no longer matches"):
        patch_blackbird_output.patch_source("strPath = unexpected_root()")
