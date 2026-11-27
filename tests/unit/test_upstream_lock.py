from pathlib import Path

from app.core.upstreams import upstreams


def test_every_mvp_commit_is_immutable_and_compose_synced() -> None:
    compose = (Path(__file__).parents[2] / "docker-compose.yml").read_text(encoding="utf-8")
    for tool in ("maigret", "sherlock", "blackbird", "holehe", "theharvester", "subfinder"):
        commit = upstreams()[tool]["commit"]
        assert len(commit) == 40
        int(commit, 16)
        assert commit in compose
