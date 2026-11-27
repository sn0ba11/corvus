# SPDX-License-Identifier: AGPL-3.0-or-later
"""Precision-first relevance policy for Corvus quoted Bing searches."""

import typing as t


def result_matches_anchor(result: dict[str, t.Any], anchors: tuple[str, ...]) -> bool:
    haystack = " ".join(
        str(result.get(field, "")) for field in ("url", "title", "content")
    ).casefold()
    return any(anchor in haystack for anchor in anchors)


def quoted_results_are_relevant(
    results: list[dict[str, t.Any]], anchors: tuple[str, ...]
) -> bool:
    """Require a quoted identifier to be visible in Bing's result card.

    This intentionally accepts a false-negative edge case: if an identifier is
    present only in the destination page body but absent from Bing's URL, title,
    and snippet, Corvus rejects the card. For casework, that precision-over-recall
    tradeoff is preferable to promoting Bing's silent sparse-query requeries as
    evidence-backed hits.
    """
    return not anchors or not results or any(result_matches_anchor(result, anchors) for result in results)
