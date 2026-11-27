from app.services.handle_candidates import MAX_HANDLE_CANDIDATES, generate_handle_candidates


def test_handle_candidates_are_bounded_deterministic_and_provenanced() -> None:
    first = generate_handle_candidates("Sinéad O'Connor")
    second = generate_handle_candidates("Sinéad O'Connor")
    assert first == second
    assert 1 < len(first) <= MAX_HANDLE_CANDIDATES
    assert first[0].username == "sineadoconnor"
    assert first[0].provenance == "first+last"
    assert all(item.source_name == "Sinéad O'Connor" for item in first)
    assert len({item.username for item in first}) == len(first)


def test_handle_candidates_include_manual_review_patterns_without_birthdate_guessing() -> None:
    values = {item.username for item in generate_handle_candidates("Alex Morgan")}
    assert "alex.morgan" in values
    assert "alex_morgan" in values
    assert "alexmorganxox" in values
    assert "realalexmorgan" in values
    assert all("200" not in value and "199" not in value for value in values)


def test_handle_candidate_limit_is_hard_cap() -> None:
    candidates = generate_handle_candidates("William Randolph Hearst", limit=5)
    assert len(candidates) == 5
