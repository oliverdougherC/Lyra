"""How much of a context window a run may spend on its own reply.

The chat turn splits a window into four buckets (docs/rag-pipeline.md, Stage 7); the
generation reserve is the one every caller needs, including the background pipelines that
assemble no history and so have no use for the rest. It lives here rather than in the
chat route because `core/` may not import `api/`, and the drafting pipeline's ceiling has
to be the same number the chat turn reserves - two different answers to "how much room
does the model have" is how a section silently comes back truncated.
"""

# The generation quarter of the Stage 7 budget table. An 8192 window reserves 2048.
GENERATION_SHARE = 0.25

# Below this a reply is not worth making: a section that can only be a sentence long is
# a failed call dressed as a short one. Small enough that no real window trips it.
_FLOOR_TOKENS = 256

# English prose runs about 1.4 tokens a word; 1.6 leaves room for the technical
# vocabulary and the markdown a draft section carries.
TOKENS_PER_WORD = 1.6

# What a word target is allowed to overshoot before the ceiling cuts it off. The target
# is a target, not a limit - a section that wants 400 words and writes 520 is fine, and
# clipping it mid-sentence to enforce the number would be worse than the overrun.
_OVERSHOOT = 1.5


def generation_reserve(context_window: int) -> int:
    """The most any one reply may be allowed to occupy, in tokens."""
    return max(_FLOOR_TOKENS, round(context_window * GENERATION_SHARE))


def tokens_for_words(words: int) -> int:
    """A word target as an output-token ceiling, with room to overshoot it."""
    return max(_FLOOR_TOKENS, round(words * TOKENS_PER_WORD * _OVERSHOOT))
