"""Deterministic personal-name variants for analyst-controlled search packs.

The variation categories follow the MIT-licensed Bellingcat Name Variant Search /
``@bellingcat/alias-generator`` approach: omit or initialise middle names, initialise
given names, transpose surname order and account for selected surname particles. This
implementation is local, bounded and deliberately does not guess culturally specific
nicknames. Analysts can supply known aliases explicitly in the search workbench.
"""

import re
import unicodedata
from dataclasses import dataclass

MAX_NAME_VARIANTS = 32
MAX_ANALYST_ALIASES = 12
MAX_NAME_VALUE_LENGTH = 120
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_ALLOWED_ALIAS_PUNCTUATION = frozenset(" .,'’-–")
_SURNAME_PARTICLES = frozenset(
    {
        "al",
        "ap",
        "ben",
        "bin",
        "da",
        "dal",
        "de",
        "del",
        "della",
        "den",
        "der",
        "di",
        "dos",
        "du",
        "el",
        "la",
        "le",
        "st",
        "st.",
        "van",
        "von",
    }
)
_ASCII_OVERRIDES = str.maketrans(
    {
        "Æ": "AE",
        "æ": "ae",
        "Ð": "D",
        "ð": "d",
        "Ł": "L",
        "ł": "l",
        "Ø": "O",
        "ø": "o",
        "Œ": "OE",
        "œ": "oe",
        "Þ": "Th",
        "þ": "th",
        "ß": "ss",
    }
)
_DUTCH_PARTICLE_PREFIXES = ("van der ", "van den ", "van dem ", "van de ")


@dataclass(frozen=True, slots=True)
class ParsedName:
    first: str
    middle: tuple[str, ...]
    last: str


@dataclass(frozen=True, slots=True)
class NameVariant:
    value: str
    kind: str
    label: str
    explanation: str
    source_name: str


def normalise_person_name(value: str) -> str:
    """Return a printable, whitespace-normalised name without changing its case."""
    value = unicodedata.normalize("NFKC", value).replace("’", "'").strip()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s*,\s*", ", ", value)
    if not value or len(value) > MAX_NAME_VALUE_LENGTH or _CONTROL_CHARACTER_RE.search(value):
        raise ValueError("name must contain 1 to 120 printable characters")
    if not _LETTER_RE.search(value):
        raise ValueError("name must contain at least one letter")
    return value


def validate_analyst_aliases(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Validate and deduplicate analyst-supplied aliases used only in signed searches."""
    if len(values) > MAX_ANALYST_ALIASES:
        raise ValueError(f"provide no more than {MAX_ANALYST_ALIASES} alternate names")
    aliases: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not str(raw).strip():
            continue
        alias = normalise_person_name(str(raw))
        if any(
            not character.isalpha() and character not in _ALLOWED_ALIAS_PUNCTUATION
            for character in alias
        ):
            raise ValueError(
                "alternate names may contain letters, spaces, apostrophes, periods, hyphens and commas only"
            )
        key = alias.casefold()
        if key not in seen:
            aliases.append(alias)
            seen.add(key)
    return tuple(aliases)


def parse_person_name(value: str) -> ParsedName:
    """Parse a bounded western-order name while preserving common surname particles."""
    clean = normalise_person_name(value)
    if "," in clean:
        last, remainder = clean.split(",", 1)
        remainder_tokens = remainder.strip().split()
        if not last.strip() or not remainder_tokens:
            raise ValueError("comma-form names require both surname and given name")
        return ParsedName(
            first=remainder_tokens[0],
            middle=tuple(remainder_tokens[1:]),
            last=last.strip(),
        )

    tokens = clean.split()
    if len(tokens) == 1:
        return ParsedName(first=tokens[0], middle=(), last="")

    surname_start = len(tokens) - 1
    while surname_start > 1 and tokens[surname_start - 1].casefold() in _SURNAME_PARTICLES:
        surname_start -= 1
    return ParsedName(
        first=tokens[0],
        middle=tuple(tokens[1:surname_start]),
        last=" ".join(tokens[surname_start:]),
    )


def ascii_transliteration(value: str) -> str:
    """Produce a conservative Latin ASCII search form using only the standard library."""
    expanded = value.translate(_ASCII_OVERRIDES)
    decomposed = unicodedata.normalize("NFKD", expanded)
    folded = "".join(character for character in decomposed if not unicodedata.combining(character))
    return re.sub(r"\s+", " ", folded).strip()


def _initial(value: str) -> str:
    match = _LETTER_RE.search(value)
    return match.group(0) if match else ""


def _join(*parts: str) -> str:
    return " ".join(part.strip() for part in parts if part and part.strip())


def _middle_initials(middle: tuple[str, ...], *, periods: bool = False) -> str:
    initials = [_initial(part) for part in middle]
    initials = [item for item in initials if item]
    if periods:
        return " ".join(f"{item}." for item in initials)
    return " ".join(initials)


def generate_name_variants(
    value: str, *, limit: int = MAX_NAME_VARIANTS
) -> tuple[NameVariant, ...]:
    """Generate stable, deduplicated variants without network calls or nickname guessing."""
    if not 1 <= limit <= MAX_NAME_VARIANTS:
        raise ValueError(f"name variant limit must be between 1 and {MAX_NAME_VARIANTS}")
    source = normalise_person_name(value)
    parsed = parse_person_name(source)
    variants: list[NameVariant] = []
    seen: set[str] = set()

    def add(candidate: str, kind: str, label: str, explanation: str) -> None:
        if len(variants) >= limit:
            return
        candidate = normalise_person_name(candidate)
        key = candidate.casefold()
        if key in seen:
            return
        seen.add(key)
        variants.append(
            NameVariant(
                value=candidate,
                kind=kind,
                label=label,
                explanation=explanation,
                source_name=source,
            )
        )

    first = parsed.first
    middle = parsed.middle
    last = parsed.last
    middle_full = " ".join(middle)
    middle_initials = _middle_initials(middle)
    middle_initials_periods = _middle_initials(middle, periods=True)
    first_initial = _initial(first)

    add(source, "original", "Original form", "The name exactly as entered by the analyst.")
    if last:
        add(_join(first, middle_full, last), "full", "Full name", "Given, middle and family names.")
        add(_join(first, last), "middle-omitted", "Middle name omitted", "Middle names removed.")
        if middle_initials:
            add(
                _join(first, middle_initials, last),
                "middle-initials",
                "Middle initials",
                "Middle names replaced by initials.",
            )
        if middle_initials_periods:
            add(
                _join(first, middle_initials_periods, last),
                "middle-initials-periods",
                "Middle initials with periods",
                "Middle names replaced by punctuated initials.",
            )
        if first_initial:
            add(
                _join(first_initial, last),
                "first-initial",
                "First initial",
                "Given name replaced by its initial.",
            )
            add(
                _join(f"{first_initial}.", last),
                "first-initial-period",
                "First initial with period",
                "Given name replaced by a punctuated initial.",
            )
            if middle_full:
                add(
                    _join(first_initial, middle_full, last),
                    "first-initial-middle",
                    "First initial and middle name",
                    "Given name initialised while middle names remain complete.",
                )
            if middle_initials:
                add(
                    _join(first_initial, middle_initials, last),
                    "all-initials",
                    "Given and middle initials",
                    "Given and middle names replaced by initials.",
                )
                combined = first_initial + middle_initials.replace(" ", "")
                add(
                    _join(combined, last),
                    "combined-initials",
                    "Combined initials",
                    "Given and middle initials combined before the surname.",
                )
        add(
            f"{last}, {first}",
            "surname-first-comma",
            "Surname first",
            "Surname moved before the given name using catalogue punctuation.",
        )
        add(
            _join(last, first),
            "surname-first",
            "Surname-first order",
            "Surname moved before the given name without punctuation.",
        )
        if middle_full:
            add(
                f"{last}, {_join(first, middle_full)}",
                "surname-first-full",
                "Surname first with middle name",
                "Surname-first catalogue form retaining middle names.",
            )
            add(
                _join(last, first, middle_full),
                "surname-first-unpunctuated-full",
                "Surname-first full order",
                "Surname-first order retaining middle names without punctuation.",
            )
        if middle_initials:
            add(
                f"{last}, {_join(first, middle_initials)}",
                "surname-first-initials",
                "Surname first with initials",
                "Surname-first catalogue form with middle initials.",
            )

        lower_last = last.casefold()
        for prefix in _DUTCH_PARTICLE_PREFIXES:
            if lower_last.startswith(prefix):
                remainder = last[len(prefix) :]
                add(
                    _join(first, middle_full, f"vd {remainder}"),
                    "particle-abbreviation",
                    "Abbreviated surname particle",
                    "A common van der/van de particle abbreviation.",
                )
                add(
                    _join(first, middle_full, f"v.d. {remainder}"),
                    "particle-abbreviation-periods",
                    "Punctuated surname particle",
                    "A punctuated van der/van de particle abbreviation.",
                )
                break

    punctuation_seeds = tuple(variants)
    for item in punctuation_seeds:
        if len(variants) >= limit:
            break
        if "-" in item.value:
            add(
                item.value.replace("-", " "),
                "hyphen-spacing",
                "Hyphen replaced by space",
                "A hyphenated name searched with spaces.",
            )
            add(
                item.value.replace("-", ""),
                "hyphen-removed",
                "Hyphen removed",
                "A hyphenated name searched without punctuation.",
            )
        if "'" in item.value:
            add(
                item.value.replace("'", ""),
                "apostrophe-removed",
                "Apostrophe removed",
                "An apostrophised name searched without punctuation.",
            )
            add(
                item.value.replace("'", " "),
                "apostrophe-spacing",
                "Apostrophe replaced by space",
                "An apostrophised name searched with a space.",
            )

    transliteration_seeds = tuple(variants)
    for item in transliteration_seeds:
        if len(variants) >= limit:
            break
        transliterated = ascii_transliteration(item.value)
        if transliterated.casefold() != item.value.casefold():
            add(
                transliterated,
                "ascii-transliteration",
                "ASCII transliteration",
                f"ASCII search form derived from {item.label.lower()}.",
            )

    return tuple(variants)


def collect_name_variants(
    primary_name: str,
    analyst_aliases: tuple[str, ...] = (),
    *,
    limit: int = MAX_NAME_VARIANTS,
) -> tuple[NameVariant, ...]:
    """Generate one bounded variant set from a primary name and known analyst aliases."""
    if not 1 <= limit <= MAX_NAME_VARIANTS:
        raise ValueError(f"name variant limit must be between 1 and {MAX_NAME_VARIANTS}")
    aliases = validate_analyst_aliases(analyst_aliases)
    source_names = (normalise_person_name(primary_name), *aliases)
    source_variants = [generate_name_variants(source_name) for source_name in source_names]
    combined: list[NameVariant] = []
    seen: set[str] = set()

    def append(item: NameVariant) -> bool:
        key = item.value.casefold()
        if key in seen:
            return False
        seen.add(key)
        combined.append(item)
        return len(combined) >= limit

    # Preserve every analyst-confirmed source form before filling the remaining
    # capacity with generated alternatives. This prevents a punctuation-heavy
    # primary name from crowding a known alias out of the bounded pack.
    for variants in source_variants:
        if variants and append(variants[0]):
            return tuple(combined)
    depth = 1
    while len(combined) < limit:
        added = False
        for variants in source_variants:
            if depth >= len(variants):
                continue
            added = True
            if append(variants[depth]):
                return tuple(combined)
        if not added:
            break
        depth += 1
    return tuple(combined)
