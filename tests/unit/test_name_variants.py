import pytest

from app.services.name_variants import (
    MAX_NAME_VARIANTS,
    ascii_transliteration,
    collect_name_variants,
    generate_name_variants,
    parse_person_name,
    validate_analyst_aliases,
)


def values(name: str) -> list[str]:
    return [item.value for item in generate_name_variants(name)]


def test_bellingcat_style_common_variations_are_deterministic() -> None:
    generated = values("William Randolph Hearst")

    assert generated[0] == "William Randolph Hearst"
    assert "William Hearst" in generated
    assert "William R Hearst" in generated
    assert "W Hearst" in generated
    assert "W Randolph Hearst" in generated
    assert "W R Hearst" in generated
    assert "WR Hearst" in generated
    assert "Hearst, William" in generated
    assert "Hearst, William Randolph" in generated
    assert generated == values("William Randolph Hearst")
    assert len(generated) == len({item.casefold() for item in generated})


def test_particles_punctuation_and_transliteration_are_bounded() -> None:
    dutch = values("Jan van der Laan")
    irish = values("Sinéad O'Connor")

    assert "Jan vd Laan" in dutch
    assert "Jan v.d. Laan" in dutch
    assert "Sinead O'Connor" in irish
    assert "Sinéad OConnor" in irish
    assert len(irish) <= MAX_NAME_VARIANTS


def test_hyphenated_and_surname_first_forms_are_generated() -> None:
    generated = values("Jean-Luc Picard")

    assert "Picard, Jean-Luc" in generated
    assert "Picard Jean-Luc" in generated
    assert "Jean Luc Picard" in generated
    assert "JeanLuc Picard" in generated


def test_parser_preserves_multiword_surname_particles_and_comma_form() -> None:
    assert parse_person_name("Jan van der Laan").last == "van der Laan"
    parsed = parse_person_name("Hearst, William Randolph")
    assert parsed.first == "William"
    assert parsed.middle == ("Randolph",)
    assert parsed.last == "Hearst"


def test_ascii_transliteration_handles_combining_and_non_decomposing_letters() -> None:
    assert ascii_transliteration("Sinéad Łukasz Østergård") == "Sinead Lukasz Ostergard"


def test_analyst_aliases_are_validated_deduplicated_and_expanded() -> None:
    aliases = validate_analyst_aliases(["Bill Hearst", "bill hearst", "W. R. Hearst"])
    generated = collect_name_variants("William Randolph Hearst", aliases)

    assert aliases == ("Bill Hearst", "W. R. Hearst")
    assert any(
        item.value == "Bill Hearst" and item.source_name == "Bill Hearst" for item in generated
    )
    assert len(generated) <= MAX_NAME_VARIANTS


def test_analyst_aliases_reject_search_syntax_and_excessive_input() -> None:
    with pytest.raises(ValueError, match="alternate names"):
        validate_analyst_aliases(["John Smith site:example.org"])
    with pytest.raises(ValueError, match="no more than"):
        validate_analyst_aliases([f"Alias {index}" for index in range(13)])
