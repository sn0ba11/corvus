import pytest
from fastapi import HTTPException

from app.api.routes import detect_query_type, validate_typed_query
from app.models.schemas import QueryType, SearchRequest


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("name_here", QueryType.USERNAME),
        ("me@example.org", QueryType.EMAIL),
        ("example.org", QueryType.DOMAIN),
        ("Nova Example", QueryType.NAME),
        ("Sinéad O'Connor", QueryType.NAME),
        ("Jean-Luc Picard", QueryType.NAME),
    ],
)
def test_safe_detection(value: str, expected: QueryType) -> None:
    assert detect_query_type(value) == expected


def test_typed_validation_rejects_shell_payload() -> None:
    with pytest.raises(HTTPException):
        validate_typed_query("name; rm -rf /", QueryType.USERNAME)
    with pytest.raises(HTTPException):
        validate_typed_query("John Smith; rm -rf /", QueryType.NAME)


def test_name_investigation_requires_a_full_name_and_collector() -> None:
    request = SearchRequest(
        query="Nova Example",
        query_type=QueryType.NAME,
        tools=["bing_name_search"],
    )
    assert request.tools == ["bing_name_search"]
    with pytest.raises(ValueError, match="collection tool"):
        SearchRequest(query="Nova Example", query_type=QueryType.NAME)
    with pytest.raises(ValueError, match="collection tool"):
        SearchRequest(query="corvus_fixture_nq7az", query_type=QueryType.USERNAME)
    with pytest.raises(HTTPException):
        validate_typed_query("Nova", QueryType.NAME)


def test_input_length_limit() -> None:
    with pytest.raises(ValueError):
        SearchRequest(query="x" * 321, query_type=QueryType.USERNAME, tools=["maigret"])
