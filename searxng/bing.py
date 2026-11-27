# SPDX-License-Identifier: AGPL-3.0-or-later
"""Corvus override of SearXNG's Bing web engine.

Keeps the upstream parser but makes the outbound request deterministic for the
Corvus deployment. Bing returned materially different results to SearXNG than
to a browser from the same host; Corvus therefore fixes the market, country,
user-agent and language headers to a normal Irish Windows Chrome request.
"""

import base64
import re
import typing as t
from urllib.parse import parse_qs, urlencode, urlparse

from babel import Locale, UnknownLocaleError
from babel.languages import get_official_languages
from lxml import html
from searx.enginelib.traits import EngineTraits
from searx.engines.bing_corvus_relevance import quoted_results_are_relevant
from searx.locales import region_tag
from searx.utils import eval_xpath, eval_xpath_getindex, eval_xpath_list, extract_text

if t.TYPE_CHECKING:
    from searx.extended_types import SXNG_Response
    from searx.search.processors import OnlineParams

about: dict[str, t.Any] = {
    "website": "https://www.bing.com",
    "wikidata_id": "Q182496",
    "official_api_documentation": "https://github.com/MicrosoftDocs/bing-docs",
    "use_official_api": False,
    "require_api_key": False,
    "results": "HTML",
}

categories = ["general", "web"]
safesearch = True
_safesearch_map: dict[int, str] = {0: "off", 1: "moderate", 2: "strict"}
base_url = "https://www.bing.com"
CORVUS_BING_MARKET = "en-IE"
CORVUS_BING_COUNTRY = "IE"
CORVUS_BING_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)


def get_locale_params(engine_region: str | None) -> dict[str, str] | None:
    if not engine_region or engine_region == "clear":
        return None
    return {"mkt": engine_region}


def override_accept_language(params: "OnlineParams", engine_region: str | None) -> None:
    if not engine_region or engine_region == "clear":
        return
    lang = engine_region.split("-")[0]
    params["headers"]["Accept-Language"] = f"{engine_region},{lang};q=0.9"


def request(query: str, params: "OnlineParams"):
    """Assemble a deterministic browser-like Bing-Web request for Corvus."""
    params["headers"]["User-Agent"] = CORVUS_BING_USER_AGENT
    params["headers"]["Accept-Language"] = "en-IE,en;q=0.9"
    params["headers"]["Accept"] = (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    )
    params["headers"]["Upgrade-Insecure-Requests"] = "1"
    params["headers"].pop("DNT", None)
    params["headers"].pop("Sec-GPC", None)
    params["headers"].pop("Cache-Control", None)

    query_params: dict[str, str | int] = {
        "q": query,
        "adlt": _safesearch_map.get(params.get("safesearch", 0), "off"),
        "mkt": CORVUS_BING_MARKET,
        "cc": CORVUS_BING_COUNTRY,
    }
    params["url"] = f"{base_url}/search?{urlencode(query_params)}"


def _quoted_query_anchors(resp: "SXNG_Response") -> tuple[str, ...]:
    """Return quoted literals from the Bing request URL for slop detection."""
    query = parse_qs(urlparse(str(resp.url)).query).get("q", [""])[0]
    return tuple(anchor.casefold().strip() for anchor in re.findall(r'"([^"\\r\\n]+)"', query) if anchor.strip())


def response(resp: "SXNG_Response") -> list[dict[str, t.Any]]:
    results: list[dict[str, t.Any]] = []
    dom = html.fromstring(resp.text)
    # Bing uses b_algo-like class names for non-organic/filler blocks on sparse
    # queries. Match b_algo as a class token, not as a substring, otherwise a
    # zero-hit search can be surfaced as plausible-but-unrelated Corvus evidence.
    organic_result_xpath = (
        '//ol[@id="b_results"]/li['
        'contains(concat(" ", normalize-space(@class), " "), " b_algo ")'
        ']'
    )
    for item in eval_xpath_list(dom, organic_result_xpath):
        link = eval_xpath_getindex(item, ".//h2/a", 0, None)
        if link is None:
            continue
        href = link.attrib.get("href", "")
        title = extract_text(link)
        if not href or not title:
            continue
        if href.startswith("https://www.bing.com/ck/a?"):
            qs = parse_qs(urlparse(href).query)
            u_values = qs.get("u")
            if u_values:
                u_val = u_values[0]
                if u_val.startswith("a1"):
                    encoded = u_val[2:]
                    encoded += "=" * (-len(encoded) % 4)
                    href = base64.urlsafe_b64decode(encoded).decode("utf-8", errors="replace")
        content_els = eval_xpath(item, ".//p")
        for p in content_els:
            for icon in p.xpath('.//span[@class="algoSlug_icon"]'):
                icon.getparent().remove(icon)
        content = extract_text(content_els)
        results.append({"url": href, "title": title, "content": content})

    # Bing can silently requery sparse quoted searches while leaving the
    # original text in the search box, then return a normal-looking b_algo
    # page for unrelated terms. For Corvus' exact username/name pivots that is
    # worse than an empty result set: it manufactures plausible search slop.
    # Only apply this page-level guard when the caller explicitly quoted a
    # literal; broad/unquoted web searches retain normal Bing semantics.
    anchors = _quoted_query_anchors(resp)
    if not quoted_results_are_relevant(results, anchors):
        return []
    return results


def fetch_traits(engine_traits: EngineTraits) -> None:
    """Retain upstream region-trait discovery for compatibility."""
    from searx.network import get
    from searx.utils import gen_useragent

    headers = {
        "User-Agent": gen_useragent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US;q=0.5,en;q=0.3",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-GPC": "1",
        "Cache-Control": "max-age=0",
    }
    resp = get("https://www.bing.com/account/general", headers=headers, timeout=5)
    if not resp.ok:
        raise RuntimeError("Response from Bing is not OK.")
    dom = html.fromstring(resp.text)
    map_market_codes: dict[str, str] = {"zh-hk": "en-hk"}
    for href in eval_xpath(dom, '//div[@id="region-section-content"]//div[@class="regionItem"]/a/@href'):
        cc_tag = parse_qs(urlparse(href).query)["cc"][0]
        if cc_tag == "clear":
            engine_traits.all_locale = cc_tag
            continue
        for lang_tag in get_official_languages(cc_tag, de_facto=True):
            lang_tag = lang_tag.split("_")[0]
            market_code = map_market_codes.get(f"{lang_tag}-{cc_tag}", f"{lang_tag}-{cc_tag}")
            try:
                sxng_tag = region_tag(Locale.parse(f"{lang_tag}_{cc_tag.upper()}"))
            except UnknownLocaleError:
                continue
            conflict = engine_traits.regions.get(sxng_tag)
            if conflict:
                continue
            engine_traits.regions[sxng_tag] = market_code
