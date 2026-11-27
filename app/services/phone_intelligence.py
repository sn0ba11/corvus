from __future__ import annotations

import re
from dataclasses import dataclass

E164_MAX_DIGITS = 15
E164_MIN_DIGITS = 7

# Longest-prefix matching over common international calling codes. This deliberately
# describes numbering-plan territory, not inferred residence or physical location.
CALLING_CODE_REGION: dict[str, str] = {
    "1": "North American Numbering Plan",
    "20": "Egypt",
    "27": "South Africa",
    "30": "Greece",
    "31": "Netherlands",
    "32": "Belgium",
    "33": "France",
    "34": "Spain",
    "36": "Hungary",
    "39": "Italy / Vatican City",
    "40": "Romania",
    "41": "Switzerland",
    "43": "Austria",
    "44": "United Kingdom",
    "45": "Denmark",
    "46": "Sweden",
    "47": "Norway",
    "48": "Poland",
    "49": "Germany",
    "51": "Peru",
    "52": "Mexico",
    "53": "Cuba",
    "54": "Argentina",
    "55": "Brazil",
    "56": "Chile",
    "57": "Colombia",
    "58": "Venezuela",
    "60": "Malaysia",
    "61": "Australia",
    "62": "Indonesia",
    "63": "Philippines",
    "64": "New Zealand",
    "65": "Singapore",
    "66": "Thailand",
    "81": "Japan",
    "82": "South Korea",
    "84": "Vietnam",
    "86": "China",
    "90": "Turkey",
    "91": "India",
    "92": "Pakistan",
    "93": "Afghanistan",
    "94": "Sri Lanka",
    "95": "Myanmar",
    "98": "Iran",
    "211": "South Sudan",
    "212": "Morocco / Western Sahara",
    "213": "Algeria",
    "216": "Tunisia",
    "218": "Libya",
    "220": "Gambia",
    "221": "Senegal",
    "222": "Mauritania",
    "223": "Mali",
    "224": "Guinea",
    "225": "Côte d'Ivoire",
    "226": "Burkina Faso",
    "227": "Niger",
    "228": "Togo",
    "229": "Benin",
    "230": "Mauritius",
    "231": "Liberia",
    "232": "Sierra Leone",
    "233": "Ghana",
    "234": "Nigeria",
    "235": "Chad",
    "236": "Central African Republic",
    "237": "Cameroon",
    "238": "Cabo Verde",
    "239": "São Tomé and Príncipe",
    "240": "Equatorial Guinea",
    "241": "Gabon",
    "242": "Republic of the Congo",
    "243": "DR Congo",
    "244": "Angola",
    "245": "Guinea-Bissau",
    "246": "British Indian Ocean Territory",
    "248": "Seychelles",
    "249": "Sudan",
    "250": "Rwanda",
    "251": "Ethiopia",
    "252": "Somalia",
    "253": "Djibouti",
    "254": "Kenya",
    "255": "Tanzania",
    "256": "Uganda",
    "257": "Burundi",
    "258": "Mozambique",
    "260": "Zambia",
    "261": "Madagascar",
    "262": "Réunion / Mayotte",
    "263": "Zimbabwe",
    "264": "Namibia",
    "265": "Malawi",
    "266": "Lesotho",
    "267": "Botswana",
    "268": "Eswatini",
    "269": "Comoros",
    "290": "Saint Helena / Tristan da Cunha",
    "291": "Eritrea",
    "297": "Aruba",
    "298": "Faroe Islands",
    "299": "Greenland",
    "350": "Gibraltar",
    "351": "Portugal",
    "352": "Luxembourg",
    "353": "Ireland",
    "354": "Iceland",
    "355": "Albania",
    "356": "Malta",
    "357": "Cyprus",
    "358": "Finland / Åland",
    "359": "Bulgaria",
    "370": "Lithuania",
    "371": "Latvia",
    "372": "Estonia",
    "373": "Moldova",
    "374": "Armenia",
    "375": "Belarus",
    "376": "Andorra",
    "377": "Monaco",
    "378": "San Marino",
    "380": "Ukraine",
    "381": "Serbia",
    "382": "Montenegro",
    "383": "Kosovo",
    "385": "Croatia",
    "386": "Slovenia",
    "387": "Bosnia and Herzegovina",
    "389": "North Macedonia",
    "420": "Czechia",
    "421": "Slovakia",
    "423": "Liechtenstein",
    "500": "Falkland Islands",
    "501": "Belize",
    "502": "Guatemala",
    "503": "El Salvador",
    "504": "Honduras",
    "505": "Nicaragua",
    "506": "Costa Rica",
    "507": "Panama",
    "508": "Saint Pierre and Miquelon",
    "509": "Haiti",
    "590": "Guadeloupe / Saint Barthélemy / Saint Martin",
    "591": "Bolivia",
    "592": "Guyana",
    "593": "Ecuador",
    "594": "French Guiana",
    "595": "Paraguay",
    "596": "Martinique",
    "597": "Suriname",
    "598": "Uruguay",
    "599": "Caribbean Netherlands / Curaçao",
    "670": "Timor-Leste",
    "672": "Australian external territories",
    "673": "Brunei",
    "674": "Nauru",
    "675": "Papua New Guinea",
    "676": "Tonga",
    "677": "Solomon Islands",
    "678": "Vanuatu",
    "679": "Fiji",
    "680": "Palau",
    "681": "Wallis and Futuna",
    "682": "Cook Islands",
    "683": "Niue",
    "685": "Samoa",
    "686": "Kiribati",
    "687": "New Caledonia",
    "688": "Tuvalu",
    "689": "French Polynesia",
    "690": "Tokelau",
    "691": "Micronesia",
    "692": "Marshall Islands",
    "850": "North Korea",
    "852": "Hong Kong",
    "853": "Macao",
    "855": "Cambodia",
    "856": "Laos",
    "880": "Bangladesh",
    "886": "Taiwan",
    "960": "Maldives",
    "961": "Lebanon",
    "962": "Jordan",
    "963": "Syria",
    "964": "Iraq",
    "965": "Kuwait",
    "966": "Saudi Arabia",
    "967": "Yemen",
    "968": "Oman",
    "970": "Palestinian territories",
    "971": "United Arab Emirates",
    "972": "Israel",
    "973": "Bahrain",
    "974": "Qatar",
    "975": "Bhutan",
    "976": "Mongolia",
    "977": "Nepal",
    "992": "Tajikistan",
    "993": "Turkmenistan",
    "994": "Azerbaijan",
    "995": "Georgia",
    "996": "Kyrgyzstan",
    "998": "Uzbekistan",
}

IRISH_PREFIX_METADATA: tuple[tuple[str, str, str | None], ...] = (
    ("3531", "Dublin fixed line", None),
    ("35321", "Cork fixed line", None),
    ("35361", "Limerick fixed line", None),
    ("35371", "Sligo / Leitrim / North Roscommon fixed line", None),
    ("35374", "Donegal fixed line", None),
    ("35383", "Irish mobile", "083 allocation; current network may differ due to number portability"),
    ("35385", "Irish mobile", "085 allocation; current network may differ due to number portability"),
    ("35386", "Irish mobile", "086 allocation; current network may differ due to number portability"),
    ("35387", "Irish mobile", "087 allocation; current network may differ due to number portability"),
    ("35389", "Irish mobile", "089 allocation; current network may differ due to number portability"),
)

PHONE_ALLOWED_RE = re.compile(r"^[+()\-./\s\d]{7,40}$")


class PhoneNormalisationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PhoneMetadata:
    input_value: str
    e164: str
    calling_code: str
    numbering_region: str
    national_number: str
    line_type: str | None = None
    carrier_hint: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "input_value": self.input_value,
            "e164": self.e164,
            "calling_code": self.calling_code,
            "numbering_region": self.numbering_region,
            "national_number": self.national_number,
            "line_type": self.line_type,
            "carrier_hint": self.carrier_hint,
            "metadata_scope": "offline numbering-plan metadata only",
        }


def looks_like_phone(value: str) -> bool:
    raw = value.strip()
    if not PHONE_ALLOWED_RE.fullmatch(raw):
        return False
    digits = re.sub(r"\D", "", raw)
    return E164_MIN_DIGITS <= len(digits) <= E164_MAX_DIGITS + 2 and (
        raw.startswith("+") or raw.startswith("00") or raw.startswith("0")
    )


def _international_digits(value: str, default_country_code: str) -> str:
    raw = value.strip()
    if not PHONE_ALLOWED_RE.fullmatch(raw):
        raise PhoneNormalisationError("phone number contains unsupported characters")
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        international = digits
    elif raw.startswith("00"):
        international = digits[2:]
    elif raw.startswith("0"):
        # Corvus's deployment default is Ireland; national-format inputs are deliberately
        # deterministic rather than guessed from locale or IP geolocation.
        international = default_country_code + digits[1:]
    else:
        raise PhoneNormalisationError(
            "use an international +/00 number or a national number beginning with 0"
        )
    if not international or international.startswith("0"):
        raise PhoneNormalisationError("invalid international phone number")
    if not E164_MIN_DIGITS <= len(international) <= E164_MAX_DIGITS:
        raise PhoneNormalisationError("phone number is outside E.164 length bounds")
    return international


def _calling_code(digits: str) -> str:
    for length in (3, 2, 1):
        candidate = digits[:length]
        if candidate in CALLING_CODE_REGION:
            return candidate
    raise PhoneNormalisationError("unknown or unsupported country calling code")


def normalise_phone(value: str, *, default_country_code: str = "353") -> PhoneMetadata:
    digits = _international_digits(value, default_country_code)
    code = _calling_code(digits)
    national = digits[len(code) :]
    if len(national) < 4:
        raise PhoneNormalisationError("national number is too short")

    line_type: str | None = None
    carrier_hint: str | None = None
    if code == "353":
        for prefix, candidate_type, candidate_carrier in IRISH_PREFIX_METADATA:
            if digits.startswith(prefix):
                line_type = candidate_type
                carrier_hint = candidate_carrier
                break

    return PhoneMetadata(
        input_value=value.strip(),
        e164=f"+{digits}",
        calling_code=f"+{code}",
        numbering_region=CALLING_CODE_REGION[code],
        national_number=national,
        line_type=line_type,
        carrier_hint=carrier_hint,
    )
