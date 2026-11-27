import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import routes
from app.db.session import SessionLocal, engine
from app.main import app
from app.models.db import Base, Investigation, ToolRun
from app.models.schemas import QueryType
from app.services import case_identifiers


def parse(text: str):  # type: ignore[no-untyped-def]
    return case_identifiers.parse_additional_identifiers(
        text, routes.detect_query_type, routes.validate_typed_query
    )


def test_parse_detects_types_honours_prefixes_and_deduplicates() -> None:
    seeds = parse("jane@example.org\n\nJane Example\nusername: jane.example\n@jane_example\nJANE@example.org\n")
    assert [(seed.query_type, seed.value) for seed in seeds] == [
        (QueryType.EMAIL, "jane@example.org"),
        (QueryType.NAME, "Jane Example"),
        (QueryType.USERNAME, "jane.example"),
        (QueryType.USERNAME, "jane_example"),
    ]
    with pytest.raises(ValueError, match="no more than"):
        parse("\n".join(f"handle{i}" for i in range(9)))
    with pytest.raises(ValueError, match="could not tell"):
        parse("???")


def test_one_case_searches_every_supplied_identifier(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    Base.metadata.create_all(engine)
    monkeypatch.setattr(routes.job_manager, "start", lambda investigation_id: None)
    with TestClient(app) as client:
        page = client.get("/")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = client.post(
            "/investigations",
            data={"csrf": csrf, "query": "jane_example", "query_type": "username",
                  "additional_identifiers": "jane@example.org\nJane Example\njane_example"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        case_id = response.headers["location"].rsplit("/", 1)[-1]
        with SessionLocal() as db:
            runs = db.scalars(select(ToolRun).where(ToolRun.investigation_id == case_id)).all()
        by_type = {run.query_type for run in runs if run.query_value}
        assert by_type == {"email", "name"}
        assert all(run.pivot_depth == 0 and not run.pivot_automatic for run in runs)
        assert any(run.query_value == "Jane Example" for run in runs)
        assert any(run.query_value is None for run in runs)  # the primary username runs
        email_tools = {run.tool_id for run in runs if run.query_type == "email"}
        assert email_tools == set(case_identifiers.default_tools_for(QueryType.EMAIL))

        with SessionLocal() as db:
            case = db.get(Investigation, case_id)
            case.status = "completed"
            db.commit()
        case_page = client.get(f"/investigations/{case_id}")
        assert "Known identifiers in this case" in case_page.text
        assert "jane@example.org" in case_page.text
        before = len(runs)
        added = client.post(
            f"/investigations/{case_id}/identifiers",
            data={"csrf": csrf, "additional_identifiers": "jane@example.org\nsecond_handle"},
            follow_redirects=False,
        )
        assert added.status_code == 303
        with SessionLocal() as db:
            runs = db.scalars(select(ToolRun).where(ToolRun.investigation_id == case_id)).all()
            assert db.get(Investigation, case_id).status == "queued"
        new_values = {run.query_value for run in runs[before:]}
        assert new_values == {"second_handle"}  # the email was already in the case
        busy = client.post(
            f"/investigations/{case_id}/identifiers",
            data={"csrf": csrf, "additional_identifiers": "third_handle"},
            follow_redirects=False,
        )
        assert busy.status_code == 409
        bad = client.post(
            "/investigations",
            data={"csrf": csrf, "query": "jane_example", "query_type": "username",
                  "additional_identifiers": "???"},
            follow_redirects=False,
        )
        assert bad.status_code == 422
