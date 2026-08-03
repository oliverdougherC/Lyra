"""The single token estimator, used for both the chunk ceiling and the context budget.

There is deliberately no real tokenizer here. The chunk ceiling and the context budget
have to agree, and the context budget is spent against a tutor endpoint the user supplies,
so its tokenizer is unknowable at build time. A shared approximation that is consistently
wrong in the same direction everywhere is safer than two estimators that disagree, and the
25 percent generation reserve in the budget absorbs the error.
"""

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Approximate the token count of `text` at four characters per token.

    Args:
        text: The text to measure.

    Returns:
        The estimated token count, never below 1 so that a non-empty budget always
        admits at least one unit of text.
    """
    return max(1, len(text) // CHARS_PER_TOKEN)
