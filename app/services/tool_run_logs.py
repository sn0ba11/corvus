from dataclasses import dataclass
from pathlib import Path

from app.models.db import Investigation, ToolRun

MAX_TOOL_LOG_BYTES = 5 * 1024 * 1024


class ToolRunLogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ToolRunLog:
    content: str
    size_bytes: int
    filename: str = "process-output.txt"


def load_tool_run_log(
    data_dir: Path,
    investigation: Investigation,
    run: ToolRun,
) -> ToolRunLog:
    if run.investigation_id != investigation.id:
        raise ToolRunLogError("tool run does not belong to this investigation")
    raw_root = (data_dir / "raw").resolve()
    try:
        root = (raw_root / investigation.id / run.id).resolve()
        root.relative_to(raw_root)
        path = (root / "process-output.txt").resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ToolRunLogError("no execution log was retained for this tool run") from exc
    if not path.is_file():
        raise ToolRunLogError("no execution log was retained for this tool run")
    size = path.stat().st_size
    if size > MAX_TOOL_LOG_BYTES:
        raise ToolRunLogError("execution log exceeds the 5 MiB viewing limit")
    return ToolRunLog(
        content=path.read_text(encoding="utf-8", errors="replace"),
        size_bytes=size,
    )
