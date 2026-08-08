"""Deterministic hostile and boundary tests for the web query guard."""

from backend.core import query_guard


def test_guard_normalizes_whitespace_and_preserves_safe_query() -> None:
    result = query_guard.guard_web_query("   Fourier\ttransform   convergence \n theorem  ")

    assert result == query_guard.SafeQuery(
        "Fourier transform convergence theorem", query_guard.SEMANTIC_LIMITATION
    )


def test_guard_refuses_empty_long_and_over_term_queries() -> None:
    assert query_guard.guard_web_query(" \t\n ") == query_guard.QueryRefusal(
        "empty_query",
        "The search query is empty after normalization.",
        query_guard.SEMANTIC_LIMITATION,
    )
    assert query_guard.guard_web_query("word " * 13) == query_guard.QueryRefusal(
        "too_many_terms",
        "The search query exceeds 12 terms after normalization.",
        query_guard.SEMANTIC_LIMITATION,
    )
    assert query_guard.guard_web_query("a" * 501) == query_guard.QueryRefusal(
        "query_too_long",
        "The search query exceeds 500 characters after normalization.",
        query_guard.SEMANTIC_LIMITATION,
    )


def test_guard_refuses_urls_emails_and_local_paths() -> None:
    assert query_guard.guard_web_query("https://example.com evidence") == query_guard.QueryRefusal(
        "contains_url",
        "The search query may not contain URLs.",
        query_guard.SEMANTIC_LIMITATION,
    )
    assert query_guard.guard_web_query("contact ta@example.edu for rubric") == (
        query_guard.QueryRefusal(
            "contains_email",
            "The search query may not contain email addresses.",
            query_guard.SEMANTIC_LIMITATION,
        )
    )
    assert query_guard.guard_web_query("/Users/alex/course/notes.md key lemmas") == (
        query_guard.QueryRefusal(
            "contains_path",
            "The search query may not contain local file paths.",
            query_guard.SEMANTIC_LIMITATION,
        )
    )
    assert query_guard.guard_web_query("read /tmp then explain") == query_guard.QueryRefusal(
        "contains_path",
        "The search query may not contain local file paths.",
        query_guard.SEMANTIC_LIMITATION,
    )
    assert query_guard.guard_web_query(r"C:\Users\alex\notes.txt key lemmas") == (
        query_guard.QueryRefusal(
            "contains_path",
            "The search query may not contain local file paths.",
            query_guard.SEMANTIC_LIMITATION,
        )
    )


def test_guard_refuses_secret_patterns_and_high_entropy_tokens() -> None:
    assert query_guard.guard_web_query("token=sk-abc123def456ghi789jkl") == (
        query_guard.QueryRefusal(
            "contains_secret_pattern",
            "The search query may not contain secrets, credentials, or token-shaped values.",
            query_guard.SEMANTIC_LIMITATION,
        )
    )

    high_entropy = "r4Nd0mABCD1234wxyzUVWX6789"
    assert query_guard.guard_web_query(f"explain {high_entropy}") == query_guard.QueryRefusal(
        "contains_high_entropy_token",
        "The search query may not contain long high-entropy tokens.",
        query_guard.SEMANTIC_LIMITATION,
    )


def test_guard_refuses_long_quoted_passages_but_allows_short_phrases() -> None:
    refused = query_guard.guard_web_query(
        'research "this sentence came directly from the private course handout"'
    )
    assert refused == query_guard.QueryRefusal(
        "contains_quoted_passage",
        "The search query may not contain long quoted passages.",
        query_guard.SEMANTIC_LIMITATION,
    )
    curly = query_guard.guard_web_query(
        "research “this sentence came directly from the private course handout”"
    )
    assert curly == refused

    accepted = query_guard.guard_web_query('research "Fourier transform" history')
    assert accepted == query_guard.SafeQuery(
        'research "Fourier transform" history', query_guard.SEMANTIC_LIMITATION
    )


def test_guard_refuses_significant_verbatim_overlap_with_private_context() -> None:
    private = [
        "The unpublished assignment describes the weighted residual method proof for "
        "nonlinear boundary conditions in detail."
    ]

    result = query_guard.guard_web_query(
        "weighted residual method proof for nonlinear boundary conditions", private_context=private
    )

    assert result == query_guard.QueryRefusal(
        "overlaps_private_context",
        "The search query overlaps too closely with private context available to this turn.",
        query_guard.SEMANTIC_LIMITATION,
    )


def test_guard_allows_short_shared_terms_without_claiming_semantic_safety() -> None:
    private = ["The unpublished notes emphasize contraction mappings for nonlinear solvers."]

    result = query_guard.guard_web_query(
        "nonlinear solver convergence criteria", private_context=private
    )

    assert result == query_guard.SafeQuery(
        "nonlinear solver convergence criteria", query_guard.SEMANTIC_LIMITATION
    )
    assert result.limitation == (
        "This guard blocks verbatim and obviously secret material, but it cannot detect "
        "semantic paraphrase or inferred disclosures."
    )


def test_guard_accepts_boundary_length_and_term_count() -> None:
    query = "one two three four five six seven eight nine ten eleven twelve"

    result = query_guard.guard_web_query(query)

    assert result == query_guard.SafeQuery(query, query_guard.SEMANTIC_LIMITATION)
