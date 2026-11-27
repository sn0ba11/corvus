import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import date
from string import Formatter
from urllib.parse import quote_plus

from app.models.db import SearchTemplate
from app.models.schemas import QueryType
from app.services.name_variants import (
    MAX_NAME_VARIANTS,
    NameVariant,
    collect_name_variants,
    validate_analyst_aliases,
)


@dataclass(frozen=True)
class SearchQuery:
    id: str
    label: str
    category: str
    engine: str
    query: str
    url: str


@dataclass(frozen=True)
class SearchPlatform:
    id: str
    label: str
    category: str
    query: str


@dataclass(frozen=True)
class GoogleDork:
    id: str
    label: str
    category: str
    query: str
    url: str
    reliability: str
    note: str


@dataclass(frozen=True)
class SearchQueryOptions:
    engines: tuple[str, ...] = ("google", "bing")
    platform_ids: tuple[str, ...] = ()
    document_types: tuple[str, ...] = ()
    from_date: date | None = None
    to_date: date | None = None
    include_core: bool = True
    include_name_variants: bool = True
    name_aliases: tuple[str, ...] = ()


ENGINE_URLS = {
    "google": "https://www.google.com/search?q={query}",
    "bing": "https://www.bing.com/search?q={query}",
}

SEARCH_PLATFORMS = (
    SearchPlatform("github", "GitHub", "Social platforms", "site:github.com"),
    SearchPlatform("reddit", "Reddit", "Social platforms", "site:reddit.com"),
    SearchPlatform("instagram", "Instagram", "Social platforms", "site:instagram.com"),
    SearchPlatform("facebook", "Facebook", "Social platforms", "site:facebook.com"),
    SearchPlatform("linkedin", "LinkedIn profiles", "Social platforms", "site:linkedin.com/in"),
    SearchPlatform("x", "X / Twitter", "Social platforms", "(site:x.com OR site:twitter.com)"),
    SearchPlatform("tiktok", "TikTok", "Social platforms", "site:tiktok.com"),
    SearchPlatform("telegram", "Telegram public pages", "Social platforms", "(site:t.me OR site:telegram.me)"),
    SearchPlatform(
        "discord",
        "Discord public pages and invites",
        "Social platforms",
        "(site:discord.com OR site:discord.gg)",
    ),
    SearchPlatform(
        "google-maps",
        "Google Maps contributors",
        "Reviews",
        "site:google.com/maps/contrib",
    ),
    SearchPlatform(
        "tripadvisor", "Tripadvisor profiles", "Reviews", "site:tripadvisor.com/Profile"
    ),
    SearchPlatform("yelp", "Yelp profiles", "Reviews", "site:yelp.com/user_details"),
    SearchPlatform("strava", "Strava athletes", "Fitness platforms", "site:strava.com/athletes"),
    SearchPlatform(
        "alltrails", "AllTrails members", "Fitness platforms", "site:alltrails.com/members"
    ),
    SearchPlatform("komoot", "Komoot users", "Fitness platforms", "site:komoot.com/user"),
    SearchPlatform(
        "garmin-connect",
        "Garmin Connect profiles",
        "Fitness platforms",
        "site:connect.garmin.com/modern/profile",
    ),
)
PLATFORM_BY_ID = {item.id: item for item in SEARCH_PLATFORMS}
PLATFORM_QUERY_IDS = {
    "github": "social-github.com",
    "reddit": "social-reddit.com",
    "instagram": "social-instagram.com",
    "facebook": "social-facebook.com",
    "linkedin": "social-linkedin.com",
    "x": "social-x.com",
    "tiktok": "social-tiktok.com",
    "telegram": "social-telegram",
    "discord": "social-discord",
}
DEFAULT_PLATFORM_IDS = tuple(item.id for item in SEARCH_PLATFORMS)
DOCUMENT_TYPES = ("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "txt")
DEFAULT_DOCUMENT_TYPES = ("pdf", "doc", "docx", "xls", "xlsx")
ALLOWED_TEMPLATE_FIELDS = {
    "value",
    "quoted",
    "local",
    "domain",
    "platforms",
    "filetypes",
    "after",
    "before",
    "date_range",
}
IDENTITY_TEMPLATE_FIELDS = {"value", "quoted", "local", "domain"}
CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
GOOGLE_DORK_DOCUMENT_TYPES = ("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "txt")


def _google_exact(value: str) -> str:
    cleaned = re.sub(r'\s+', ' ', value.replace('"', ' ')).strip()
    return f'"{cleaned}"'


def _google_dork(
    dork_id: str,
    label: str,
    category: str,
    query: str,
    *,
    reliability: str = "documented",
    note: str,
) -> GoogleDork:
    return GoogleDork(
        id=dork_id,
        label=label,
        category=category,
        query=query,
        url=ENGINE_URLS["google"].format(query=quote_plus(query)),
        reliability=reliability,
        note=note,
    )


def generate_google_dorks(
    value: str,
    query_type: QueryType,
    identity_names: tuple[str, ...] = (),
) -> list[GoogleDork]:
    """Build a conservative public-web Google dork pack for one case identifier.

    The default pack favours operators Google currently documents (quotes, site:, exclusion and
    filetype:). intitle:/inurl: are retained only as clearly marked best-effort pivots because
    Google's current public help does not guarantee those query syntaxes even though Advanced
    Search still exposes equivalent title/URL constraints.
    """

    exact = _google_exact(value)
    filetypes = "(" + " OR ".join(f"filetype:{item}" for item in GOOGLE_DORK_DOCUMENT_TYPES) + ")"
    items: list[GoogleDork] = [
        _google_dork(
            "exact",
            "Exact public-web mention",
            "Core",
            exact,
            note="Exact-phrase search. Best first pass before adding more constraints.",
        ),
        _google_dork(
            "documents",
            "Public documents",
            "Documents",
            f"{exact} {filetypes}",
            note="Looks for the case value in common Google-indexable document formats.",
        ),
    ]

    if query_type == QueryType.NAME:
        items.extend(
            [
                _google_dork(
                    "name-profile-context",
                    "Profiles and biographies",
                    "Identity context",
                    f'{exact} (profile OR biography OR bio OR "about me")',
                    note="Adds common public profile/biography context terms.",
                ),
                _google_dork(
                    "name-contact-context",
                    "Staff, directory and contact pages",
                    "Identity context",
                    f"{exact} (staff OR directory OR contact OR team)",
                    note="Useful for official staff, organisation and directory pages.",
                ),
                _google_dork(
                    "name-cv",
                    "CV and résumé documents",
                    "Documents",
                    f'{exact} (CV OR resume OR "curriculum vitae") (filetype:pdf OR filetype:doc OR filetype:docx)',
                    note="Targets public CV/résumé documents without searching for private or credential material.",
                ),
                _google_dork(
                    "name-linkedin",
                    "LinkedIn profiles",
                    "Profiles",
                    f"site:linkedin.com/in {exact}",
                    note="Restricts results to indexed LinkedIn profile URLs.",
                ),
                _google_dork(
                    "name-github",
                    "GitHub identity mentions",
                    "Profiles",
                    f"site:github.com {exact}",
                    note="Finds indexed GitHub pages containing the exact name.",
                ),
                _google_dork(
                    "name-orcid",
                    "ORCID profiles",
                    "Profiles",
                    f"site:orcid.org {exact}",
                    note="Targets public researcher identity records.",
                ),
                _google_dork(
                    "name-researchgate",
                    "ResearchGate profiles",
                    "Profiles",
                    f"site:researchgate.net/profile {exact}",
                    note="Targets indexed ResearchGate profile pages.",
                ),
                _google_dork(
                    "name-intitle",
                    "Name in page title",
                    "Best-effort operators",
                    f"intitle:{exact}",
                    reliability="best_effort",
                    note="Best-effort title constraint; Google does not currently guarantee this syntax in public help.",
                ),
            ]
        )
    elif query_type == QueryType.USERNAME:
        handle = value.lstrip("@").strip()
        handle_exact = _google_exact(handle)
        items.extend(
            [
                _google_dork(
                    "username-social-pack",
                    "Major social platforms",
                    "Profiles",
                    f"{handle_exact} (site:github.com OR site:reddit.com OR site:x.com OR site:instagram.com OR site:tiktok.com)",
                    note="One broad pass across commonly indexed public profile platforms.",
                ),
                _google_dork("username-github", "GitHub", "Profiles", f"site:github.com {handle_exact}", note="Indexed GitHub mentions of the exact username."),
                _google_dork("username-reddit", "Reddit", "Profiles", f"site:reddit.com/user {handle_exact}", note="Indexed Reddit user/profile pages."),
                _google_dork("username-x", "X / Twitter", "Profiles", f"(site:x.com OR site:twitter.com) {handle_exact}", note="Indexed X/Twitter mentions of the exact username."),
                _google_dork("username-instagram", "Instagram", "Profiles", f"site:instagram.com {handle_exact}", note="Indexed Instagram pages containing the exact username."),
                _google_dork("username-tiktok", "TikTok", "Profiles", f"site:tiktok.com {handle_exact}", note="Indexed TikTok pages containing the exact username."),
                _google_dork(
                    "username-inurl",
                    "Username in URL",
                    "Best-effort operators",
                    f"inurl:{handle} {handle_exact}",
                    reliability="best_effort",
                    note="Best-effort URL constraint; useful for profile-shaped URLs but not guaranteed by current public help.",
                ),
            ]
        )
    elif query_type == QueryType.EMAIL:
        local, domain = value.rsplit("@", 1)
        local_exact = _google_exact(local)
        domain_exact = _google_exact(domain)
        items.extend(
            [
                _google_dork(
                    "email-external",
                    "External mentions",
                    "Identity context",
                    f"{exact} -site:{domain}",
                    note="Looks for the exact address outside its own domain.",
                ),
                _google_dork(
                    "email-local-domain",
                    "Local-part plus domain",
                    "Identity context",
                    f"{local_exact} {domain_exact}",
                    note="Finds pages where the local part and domain are present separately.",
                ),
                _google_dork("email-github", "GitHub mentions", "Profiles", f"site:github.com {exact}", note="Indexed GitHub pages containing the exact public email address."),
                _google_dork("email-reddit", "Reddit mentions", "Profiles", f"site:reddit.com {exact}", note="Indexed Reddit pages containing the exact public email address."),
                _google_dork(
                    "email-domain-docs",
                    "Domain documents mentioning address",
                    "Documents",
                    f"site:{domain} {exact} (filetype:pdf OR filetype:doc OR filetype:docx OR filetype:xls OR filetype:xlsx)",
                    note="Looks for public documents on the address domain containing the exact address.",
                ),
            ]
        )
    elif query_type == QueryType.DOMAIN:
        domain = value.casefold().rstrip(".")
        items.extend(
            [
                _google_dork("domain-index", "Indexed pages", "Domain", f"site:{domain}", note="Google-indexed pages under the domain; not an exhaustive index count."),
                _google_dork("domain-external", "External mentions", "Domain", f'{_google_exact(domain)} -site:{domain}', note="Mentions of the domain on other indexed sites."),
                _google_dork("domain-documents", "Domain documents", "Documents", f"site:{domain} {filetypes}", note="Common public document formats indexed under the domain."),
                _google_dork("domain-contacts", "Staff and contact pages", "Identity context", f"site:{domain} (staff OR team OR directory OR contact)", note="Finds likely public people/contact pages under the domain."),
                _google_dork("domain-email-mentions", "Published domain email addresses", "Identity context", f'site:{domain} "@{domain}"', note="Finds indexed pages that publish addresses using the domain."),
                _google_dork(
                    "domain-subdomains",
                    "Subdomain-shaped indexed pages",
                    "Best-effort operators",
                    f"site:*.{domain} -site:www.{domain}",
                    reliability="best_effort",
                    note="Wildcard site scoping is useful but not part of Google's currently documented core operator set.",
                ),
                _google_dork(
                    "domain-intitle",
                    "Domain in page title",
                    "Best-effort operators",
                    f"intitle:{_google_exact(domain)}",
                    reliability="best_effort",
                    note="Best-effort title constraint; useful for mirrors, profiles and references.",
                ),
            ]
        )
    elif query_type == QueryType.PHONE:
        digits = re.sub(r"\D+", "", value)
        items.extend(
            [
                _google_dork("phone-digits", "Digits-only mention", "Identity context", _google_exact(digits), note="Checks the normalised digits in case punctuation differs from the case value."),
                _google_dork("phone-documents", "Phone number in documents", "Documents", f"({_google_exact(value)} OR {_google_exact(digits)}) {filetypes}", note="Looks for either displayed or digits-only forms in public documents."),
                _google_dork("phone-contact", "Contact and directory pages", "Identity context", f"({_google_exact(value)} OR {_google_exact(digits)}) (contact OR directory OR phone OR mobile)", note="Targets public contact/directory context without querying registration or recovery endpoints."),
            ]
        )

    for index, name in enumerate(tuple(dict.fromkeys(identity_names))[:5]):
        clean_name = re.sub(r"\s+", " ", name).strip()
        if not clean_name or clean_name.casefold() == value.casefold():
            continue
        items.append(
            _google_dork(
                f"derived-name-{index}",
                f"Derived full name · {clean_name}",
                "Derived identity pivots",
                f"{_google_exact(clean_name)} {exact}",
                note="Correlates an already-derived case name with the original identifier; it is a lead, not an ownership conclusion.",
            )
        )

    unique: dict[str, GoogleDork] = {}
    for item in items:
        unique.setdefault(item.query, item)
    return list(unique.values())


def default_search_options() -> SearchQueryOptions:
    return SearchQueryOptions(
        platform_ids=DEFAULT_PLATFORM_IDS,
        document_types=DEFAULT_DOCUMENT_TYPES,
    )


def validate_search_options(options: SearchQueryOptions) -> SearchQueryOptions:
    engines = tuple(dict.fromkeys(options.engines))
    platform_ids = tuple(dict.fromkeys(options.platform_ids))
    document_types = tuple(dict.fromkeys(options.document_types))
    if not engines or any(engine not in ENGINE_URLS for engine in engines):
        raise ValueError("select at least one supported search engine")
    if any(platform_id not in PLATFORM_BY_ID for platform_id in platform_ids):
        raise ValueError("unsupported search platform selection")
    if any(document_type not in DOCUMENT_TYPES for document_type in document_types):
        raise ValueError("unsupported document type selection")
    if options.from_date and options.to_date and options.from_date > options.to_date:
        raise ValueError("from date cannot be later than to date")
    return SearchQueryOptions(
        engines=engines,
        platform_ids=platform_ids,
        document_types=document_types,
        from_date=options.from_date,
        to_date=options.to_date,
        include_core=options.include_core,
        include_name_variants=options.include_name_variants,
        name_aliases=validate_analyst_aliases(options.name_aliases),
    )


def _date_terms(options: SearchQueryOptions) -> tuple[str, str, str]:
    after = f"after:{options.from_date.isoformat()}" if options.from_date else ""
    before = f"before:{options.to_date.isoformat()}" if options.to_date else ""
    return after, before, " ".join(part for part in (after, before) if part)


def _apply_date_range(query: str, options: SearchQueryOptions) -> str:
    _, _, date_range = _date_terms(options)
    return " ".join(part for part in (query.strip(), date_range) if part)


def _platform_expression(platform_ids: tuple[str, ...]) -> str:
    selected = [PLATFORM_BY_ID[item].query for item in platform_ids]
    if not selected:
        return ""
    return selected[0] if len(selected) == 1 else f"({' OR '.join(selected)})"


def _filetype_expression(document_types: tuple[str, ...]) -> str:
    selected = [f"filetype:{item}" for item in document_types]
    if not selected:
        return ""
    return selected[0] if len(selected) == 1 else f"({' OR '.join(selected)})"


def name_variants_for_search(
    value: str, query_type: QueryType, options: SearchQueryOptions
) -> tuple[NameVariant, ...]:
    if query_type != QueryType.NAME or not options.include_name_variants:
        return ()
    return collect_name_variants(value, options.name_aliases, limit=MAX_NAME_VARIANTS)


def _name_or_expression(variants: tuple[NameVariant, ...], max_chars: int = 1200) -> str:
    terms: list[str] = []
    for item in variants:
        term = f'"{item.value}"'
        candidate = " OR ".join((*terms, term))
        if terms and len(candidate) > max_chars:
            break
        terms.append(term)
    if not terms:
        return ""
    return terms[0] if len(terms) == 1 else f"({' OR '.join(terms)})"


def _name_variant_queries(
    value: str, query_type: QueryType, options: SearchQueryOptions
) -> list[tuple[str, str, str, str]]:
    variants = name_variants_for_search(value, query_type, options)
    if not variants:
        return []
    items: list[tuple[str, str, str, str]] = []
    for variant in variants:
        variant_id = hashlib.sha256(variant.value.casefold().encode("utf-8")).hexdigest()[:10]
        items.append(
            (
                f"name-variant-{variant_id}",
                f"Name variant: {variant.value}",
                "Name variants",
                f'"{variant.value}"',
            )
        )
    combined = _name_or_expression(variants)
    if combined:
        items.append(
            (
                "name-variants-combined",
                "All name variants",
                "Name variants",
                combined,
            )
        )
        platforms = _platform_expression(options.platform_ids)
        if platforms:
            items.append(
                (
                    "name-variants-platforms",
                    "All variants across selected platforms",
                    "Name variants",
                    f"{combined} {platforms}",
                )
            )
        filetypes = _filetype_expression(options.document_types)
        if filetypes:
            items.append(
                (
                    "name-variants-documents",
                    "All variants in selected documents",
                    "Name variants",
                    f"{combined} {filetypes}",
                )
            )
    return items


def _identity_parts(value: str, query_type: QueryType) -> tuple[str, str]:
    if query_type == QueryType.EMAIL:
        return tuple(value.rsplit("@", 1))  # type: ignore[return-value]
    if query_type == QueryType.DOMAIN:
        return value, value
    return value, ""


def _core_queries(
    value: str, query_type: QueryType, options: SearchQueryOptions
) -> list[tuple[str, str, str, str]]:
    quoted = f'"{value}"'
    items: list[tuple[str, str, str, str]] = []
    if options.include_core:
        items.append(("exact", "Exact match", "Exact", quoted))
    if options.document_types:
        items.append(
            (
                "documents",
                "Selected documents",
                "Documents",
                f"{quoted} {_filetype_expression(options.document_types)}",
            )
        )
    if query_type in {QueryType.USERNAME, QueryType.EMAIL, QueryType.DOMAIN, QueryType.NAME}:
        location_pivot = value.rsplit("@", 1)[0] if query_type == QueryType.EMAIL else value
        for platform_id in options.platform_ids:
            platform = PLATFORM_BY_ID[platform_id]
            items.append(
                (
                    PLATFORM_QUERY_IDS.get(platform.id, f"location-{platform.id}"),
                    platform.label,
                    platform.category,
                    f'"{location_pivot}" {platform.query}',
                )
            )
    if query_type == QueryType.NAME and options.include_core:
        items.extend(
            [
                (
                    "name-profile-context",
                    "Profiles and biographies",
                    "Identity context",
                    f'{quoted} (profile OR biography OR bio OR "about me")',
                ),
                (
                    "name-handles",
                    "Username and handle mentions",
                    "Identity context",
                    f'{quoted} (username OR handle OR "screen name" OR aka)',
                ),
                (
                    "name-contact",
                    "Contact and directory mentions",
                    "Identity context",
                    f"{quoted} (email OR contact OR staff OR directory)",
                ),
            ]
        )
    elif query_type == QueryType.EMAIL and options.include_core:
        local, domain = value.rsplit("@", 1)
        items.extend(
            [
                ("email-local", "Email local part", "Identity pivots", f'"{local}"'),
                ("email-domain", "Email domain", "Identity pivots", f'"{domain}"'),
                (
                    "email-contact",
                    "Contact pages",
                    "Identity pivots",
                    f"{quoted} (contact OR profile OR bio OR staff)",
                ),
            ]
        )
    elif query_type == QueryType.DOMAIN and options.include_core:
        items.extend(
            [
                ("domain-site", "Indexed pages", "Domain", f"site:{value}"),
                (
                    "domain-docs",
                    "Domain documents",
                    "Domain",
                    f"site:{value} {_filetype_expression(options.document_types)}",
                )
                if options.document_types
                else ("domain-site-copy", "Indexed pages", "Domain", f"site:{value}"),
                ("domain-mentions", "External mentions", "Domain", f'"{value}" -site:{value}'),
            ]
        )
    return items


def validate_template_definition(
    *,
    name: str,
    category: str,
    query_template: str,
    engines: list[str] | tuple[str, ...],
    query_types: list[str] | tuple[str, ...],
) -> tuple[str, str, str, list[str], list[str]]:
    clean_name = name.strip()
    clean_category = category.strip() or "Custom"
    clean_template = query_template.strip()
    if not clean_name or len(clean_name) > 120:
        raise ValueError("template name must contain 1 to 120 characters")
    if len(clean_category) > 100:
        raise ValueError("template category is too long")
    if (
        not clean_template
        or len(clean_template) > 1200
        or CONTROL_CHARACTER_RE.search(clean_template)
    ):
        raise ValueError("query template must contain 1 to 1200 printable characters")
    fields: set[str] = set()
    try:
        for _, field_name, format_spec, conversion in Formatter().parse(clean_template):
            if field_name is None:
                continue
            if format_spec or conversion or field_name not in ALLOWED_TEMPLATE_FIELDS:
                raise ValueError("query template contains an unsupported placeholder")
            fields.add(field_name)
    except ValueError as exc:
        raise ValueError(f"invalid query template: {exc}") from exc
    if not fields.intersection(IDENTITY_TEMPLATE_FIELDS):
        raise ValueError("query template must include an identity placeholder")
    clean_engines = list(dict.fromkeys(engines))
    clean_types = list(dict.fromkeys(query_types))
    if not clean_engines or any(engine not in ENGINE_URLS for engine in clean_engines):
        raise ValueError("template requires at least one supported engine")
    supported_types = {item.value for item in QueryType}
    if not clean_types or any(query_type not in supported_types for query_type in clean_types):
        raise ValueError("template requires at least one supported query type")
    return clean_name, clean_category, clean_template, clean_engines, clean_types


def render_search_template(
    template: SearchTemplate,
    value: str,
    query_type: QueryType,
    options: SearchQueryOptions,
) -> str:
    if query_type.value not in template.query_types:
        raise ValueError("template does not support this query type")
    local, domain = _identity_parts(value, query_type)
    after, before, date_range = _date_terms(options)
    context = {
        "value": value,
        "quoted": f'"{value}"',
        "local": local,
        "domain": domain,
        "platforms": _platform_expression(options.platform_ids),
        "filetypes": _filetype_expression(options.document_types),
        "after": after,
        "before": before,
        "date_range": date_range,
    }
    try:
        rendered = template.query_template.format_map(context)
    except (KeyError, ValueError) as exc:
        raise ValueError("saved query template could not be rendered") from exc
    rendered = re.sub(r"\s+", " ", rendered).strip()
    if not any(
        f"{{{field}}}" in template.query_template for field in ("after", "before", "date_range")
    ):
        rendered = _apply_date_range(rendered, options)
    return rendered


def generate_search_queries(
    value: str,
    query_type: QueryType,
    options: SearchQueryOptions | None = None,
    templates: list[SearchTemplate] | tuple[SearchTemplate, ...] = (),
    identity_names: tuple[str, ...] = (),
) -> list[SearchQuery]:
    selected = validate_search_options(options or default_search_options())
    generated: list[SearchQuery] = []
    core_items = [
        *_core_queries(value, query_type, selected),
        *_name_variant_queries(value, query_type, selected),
    ]
    name_items: list[tuple[str, str, str, str]] = []
    for name in tuple(dict.fromkeys(identity_names))[:5]:
        clean_name = re.sub(r"\s+", " ", name).strip()
        if not clean_name:
            continue
        identity_id = hashlib.sha256(clean_name.casefold().encode("utf-8")).hexdigest()[:10]
        quoted_name = f'"{clean_name}"'
        name_items.append(
            (
                f"identity-name-{identity_id}",
                f"Full name: {clean_name}",
                "Derived identity pivots",
                quoted_name,
            )
        )
        if clean_name.casefold() != value.casefold():
            name_items.append(
                (
                    f"identity-link-{identity_id}",
                    f"Name + identifier: {clean_name}",
                    "Derived identity pivots",
                    f'{quoted_name} "{value}"',
                )
            )
        platform_expression = _platform_expression(selected.platform_ids)
        if platform_expression:
            name_items.append(
                (
                    f"identity-platforms-{identity_id}",
                    f"Full name across selected platforms: {clean_name}",
                    "Derived identity pivots",
                    f"{quoted_name} {platform_expression}",
                )
            )
    for engine in selected.engines:
        url_template = ENGINE_URLS[engine]
        for query_id, label, category, query in [*core_items, *name_items]:
            final_query = _apply_date_range(query, selected)
            generated.append(
                SearchQuery(
                    id=f"{engine}-{query_id}",
                    label=label,
                    category=category,
                    engine=engine,
                    query=final_query,
                    url=url_template.format(query=quote_plus(final_query)),
                )
            )
        for template in templates:
            if engine not in template.engines or query_type.value not in template.query_types:
                continue
            final_query = render_search_template(template, value, query_type, selected)
            if not final_query:
                continue
            generated.append(
                SearchQuery(
                    id=f"{engine}-template-{template.id}",
                    label=template.name,
                    category=template.category,
                    engine=engine,
                    query=final_query,
                    url=url_template.format(query=quote_plus(final_query)),
                )
            )
    unique: dict[tuple[str, str], SearchQuery] = {}
    for item in generated:
        unique.setdefault((item.engine, item.query), item)
    return list(unique.values())


def find_search_query(value: str, query_type: QueryType, query_id: str) -> SearchQuery | None:
    return next(
        (item for item in generate_search_queries(value, query_type) if item.id == query_id), None
    )


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if _encode(decoded) != value:
        raise ValueError("non-canonical base64 value")
    return decoded


def sign_search_query(secret: str, investigation_id: str, item: SearchQuery) -> str:
    payload = json.dumps(
        {
            "investigation_id": investigation_id,
            "id": item.id,
            "label": item.label,
            "category": item.category,
            "engine": item.engine,
            "query": item.query,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    encoded = _encode(payload)
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{_encode(signature)}"


def verify_search_query_token(secret: str, investigation_id: str, token: str) -> SearchQuery:
    if len(token) > 8000 or token.count(".") != 1:
        raise ValueError("invalid search-query token")
    encoded, supplied_signature = token.split(".", 1)
    expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    try:
        signature = _decode(supplied_signature)
        payload = json.loads(_decode(encoded))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid search-query token") from exc
    if not hmac.compare_digest(expected, signature):
        raise ValueError("invalid search-query token")
    if not isinstance(payload, dict) or payload.get("investigation_id") != investigation_id:
        raise ValueError("search-query token belongs to another investigation")
    engine = str(payload.get("engine", ""))
    query = str(payload.get("query", ""))
    if (
        engine not in ENGINE_URLS
        or not query
        or len(query) > 2400
        or CONTROL_CHARACTER_RE.search(query)
    ):
        raise ValueError("invalid signed search query")
    query_id = str(payload.get("id", ""))[:200]
    label = str(payload.get("label", "Search query"))[:200]
    category = str(payload.get("category", "Custom"))[:100]
    return SearchQuery(
        id=query_id,
        label=label,
        category=category,
        engine=engine,
        query=query,
        url=ENGINE_URLS[engine].format(query=quote_plus(query)),
    )
