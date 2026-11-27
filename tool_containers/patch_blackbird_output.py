"""Patch the pinned, independently obtained Blackbird's results directory."""

import ast
from hashlib import sha256
from pathlib import Path
from textwrap import indent

TARGET = Path("/opt/tool/src/modules/export/file_operations.py")
ORIGINAL_ASSIGNMENT_DIGEST = "eb04f52872e90a5f46b48474ca470df3a6ed94795394cf8b54e236e1522c42fa"
REPLACEMENT = (
    'results_root = Path(os.environ.get("BLACKBIRD_RESULTS_DIR", "results"))\n'
    'strPath = str(results_root / folderName)\n'
)


def patch_source(source: str) -> str:
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assign) or node.end_lineno is None:
            continue
        if sha256(ast.dump(node).encode()).hexdigest() != ORIGINAL_ASSIGNMENT_DIGEST:
            continue
        lines = source.splitlines(keepends=True)
        line = lines[node.lineno - 1]
        indentation = line[:len(line) - len(line.lstrip())]
        lines[node.lineno - 1:node.end_lineno] = [indent(REPLACEMENT, indentation)]
        return "".join(lines)
    raise ValueError("Blackbird output patch no longer matches the pinned upstream source")


def main() -> None:
    source = TARGET.read_text(encoding="utf-8")
    try:
        patched = patch_source(source)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    TARGET.write_text(patched, encoding="utf-8")


if __name__ == "__main__":
    main()
