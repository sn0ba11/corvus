import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def _load(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    bridge = ModuleType("session_bridge")
    bridge.validate_session_context = lambda *_a, **_k: {}  # type: ignore[attr-defined]
    bridge.write_instaloader_session_bundle = lambda *_a, **_k: None  # type: ignore[attr-defined]
    bridge.write_netscape_cookie_jar = lambda *_a, **_k: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "session_bridge", bridge)
    monkeypatch.setenv("TOOL_ID", "sherlock")
    monkeypatch.setenv("UPSTREAM_COMMIT", "0" * 40)
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/local/bin/tool")
    monkeypatch.setattr(
        subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="1\n", stderr="")
    )
    path = Path(__file__).parents[2] / "tool_containers" / "runner.py"
    spec = importlib.util.spec_from_file_location("runner_dns_probe", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dns_probe_true_when_second_host_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load(monkeypatch)
    calls: list[str] = []

    def resolver(host: str, port: int) -> list[object]:
        calls.append(host)
        if host == "cloudflare.com":
            return []
        raise OSError("fail")

    assert module.dns_probe(resolver=resolver) is True
    assert calls == ["example.com", "cloudflare.com"]


def test_dns_probe_false_when_all_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load(monkeypatch)

    def resolver(host: str, port: int) -> list[object]:
        raise OSError("fail")

    assert module.dns_probe(resolver=resolver) is False
