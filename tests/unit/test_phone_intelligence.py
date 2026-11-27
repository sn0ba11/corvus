import pytest

from app.services.phone_intelligence import (
    PhoneNormalisationError,
    looks_like_phone,
    normalise_phone,
)


def test_normalises_irish_national_mobile_to_e164() -> None:
    result = normalise_phone("087 123 4567")
    assert result.e164 == "+353871234567"
    assert result.calling_code == "+353"
    assert result.numbering_region == "Ireland"
    assert result.national_number == "871234567"
    assert result.line_type == "Irish mobile"
    assert result.carrier_hint is not None
    assert "portability" in result.carrier_hint


def test_normalises_international_and_00_forms() -> None:
    assert normalise_phone("+44 7700 900123").e164 == "+447700900123"
    assert normalise_phone("0033 6 12 34 56 78").e164 == "+33612345678"


def test_numbering_region_is_not_presented_as_subject_location() -> None:
    result = normalise_phone("+353 1 234 5678")
    assert result.numbering_region == "Ireland"
    assert result.line_type == "Dublin fixed line"
    assert result.as_dict()["metadata_scope"] == "offline numbering-plan metadata only"


@pytest.mark.parametrize(
    "value",
    ["file:///etc/passwd", "+999123456789", "+35312", "+353 87 123 4567;rm", "12345678"],
)
def test_rejects_invalid_phone_values(value: str) -> None:
    with pytest.raises(PhoneNormalisationError):
        normalise_phone(value)


def test_phone_detection_is_conservative() -> None:
    assert looks_like_phone("+353 87 123 4567")
    assert looks_like_phone("087-123-4567")
    assert not looks_like_phone("john.smith")
    assert not looks_like_phone("12345678")
