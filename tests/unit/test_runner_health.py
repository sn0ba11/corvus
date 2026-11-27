import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.mark.parametrize(
    ("tool_id", "package_name"),
    (("sherlock", "sherlock-project"), ("theharvester", "theHarvester")),
)
def test_slow_python_tools_use_fast_package_probe_and_retry(
    monkeypatch: pytest.MonkeyPatch,
    tool_id: str,
    package_name: str,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        if len(calls) == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="cold start")
        return SimpleNamespace(returncode=0, stdout="1.0\n", stderr="")

    bridge = ModuleType("session_bridge")
    bridge.validate_session_context = lambda *_args, **_kwargs: {}  # type: ignore[attr-defined]
    bridge.write_instaloader_session_bundle = lambda *_args, **_kwargs: None  # type: ignore[attr-defined]
    bridge.write_netscape_cookie_jar = lambda *_args, **_kwargs: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "session_bridge", bridge)
    monkeypatch.setenv("TOOL_ID", tool_id)
    monkeypatch.setenv("UPSTREAM_COMMIT", "0" * 40)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/tool")
    monkeypatch.setattr(subprocess, "run", fake_run)

    path = Path(__file__).parents[2] / "tool_containers" / "runner.py"
    spec = importlib.util.spec_from_file_location(f"runner_health_{tool_id}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.PROBE_HEALTHY is False
    assert calls[0][:2] == ["python", "-c"]
    assert f"version('{package_name}')" in calls[0][2]

    module.PROBE_CHECKED_AT -= module.PROBE_RETRY_SECONDS + 1
    healthy, message = module.current_probe_health()

    assert healthy is True
    assert message == "ready"
    assert len(calls) == 2
