from pathlib import Path

from searxng.bing_relevance import quoted_results_are_relevant

ROOT = Path(__file__).resolve().parents[2]


def test_corvus_bing_override_is_built_and_selected() -> None:
    dockerfile = (ROOT / "searxng" / "Dockerfile").read_text(encoding="utf-8")
    settings = (ROOT / "searxng" / "settings.yml").read_text(encoding="utf-8")
    module = ROOT / "searxng" / "bing.py"
    relevance_module = ROOT / "searxng" / "bing_relevance.py"

    assert module.is_file()
    assert relevance_module.is_file()
    assert (
        "COPY --chown=977:977 searxng/bing.py "
        "/usr/local/searxng/searx/engines/bing_corvus.py"
    ) in dockerfile
    assert (
        "COPY --chown=977:977 searxng/bing_relevance.py "
        "/usr/local/searxng/searx/engines/bing_corvus_relevance.py"
    ) in dockerfile
    assert "  - name: bing\n    engine: bing_corvus\n    disabled: false\n    inactive: false\n" in settings


def test_corvus_bing_parser_matches_organic_class_as_token() -> None:
    source = (ROOT / "searxng" / "bing.py").read_text(encoding="utf-8")

    assert 'normalize-space(@class)' in source
    assert '" b_algo "' in source
    assert 'contains(@class, "b_algo")' not in source


def test_corvus_bing_parser_rejects_off_query_quoted_requery_pages() -> None:
    assert not quoted_results_are_relevant(
        [
            {
                "url": "https://www.example.com/solar-panels",
                "title": "Top 5 solar panels for 2026",
                "content": "Compare high-efficiency bifacial modules.",
            }
        ],
        ("corvusfixturek9",),
    )


def test_quoted_identifier_body_only_hit_is_deliberately_rejected() -> None:
    """A body-only mention is an accepted false negative for precision-first OSINT."""
    # Imagine the fetched forum page contains "corvusfixturek9" halfway through a post.
    # Bing's result card exposes only this generic URL/title/snippet, so Corvus
    # deliberately rejects it rather than claiming an unverified identifier hit.
    assert not quoted_results_are_relevant(
        [
            {
                "url": "https://forum.example.net/thread/48291",
                "title": "General discussion thread",
                "content": "A conversation about local accounts and old usernames.",
            }
        ],
        ("corvusfixturek9",),
    )


def test_quoted_identifier_visible_in_result_card_is_retained() -> None:
    assert quoted_results_are_relevant(
        [
            {
                "url": "https://forum.example.net/users/corvusfixturek9",
                "title": "User profile",
                "content": "Public profile for corvusfixturek9.",
            }
        ],
        ("corvusfixturek9",),
    )
