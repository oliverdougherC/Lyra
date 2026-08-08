"""Math delimiter normalization: what the models write becomes what the stack reads.

The two guards matter more than the conversions. `\\[TODO:` is the Milkdown editor's
escaping of a section's intent marker, and turning one into display math would break the
emptiness rule the whole drafting pipeline is built on; `\\$` is a dollar sign someone
wanted.
"""

from backend.core import mathnorm


def test_latex_inline_delimiters_become_dollars() -> None:
    assert mathnorm.normalize(r"The set \(\{u_1, u_k\}\) is independent.") == (
        "The set $\\{u_1, u_k\\}$ is independent."
    )


def test_latex_display_delimiters_become_double_dollars() -> None:
    normalized = mathnorm.normalize(r"Solve \[ A\mathbf{x} = \mathbf{0} \] for x.")

    assert "$$\nA\\mathbf{x} = \\mathbf{0}\n$$" in normalized
    assert "\\[" not in normalized


def test_two_spans_on_one_line_stay_two_spans() -> None:
    """Non-greedy, or everything between the first and last delimiter becomes one blob."""
    assert mathnorm.normalize(r"\(a\) and \(b\)") == "$a$ and $b$"


def test_a_bare_environment_is_promoted_to_display() -> None:
    source = "Then:\n\n\\begin{align}\nx &= 1 \\\\\ny &= 2\n\\end{align}\n\nwhich follows."

    normalized = mathnorm.normalize(source)

    assert "$$\n\\begin{align}" in normalized
    assert "\\end{align}\n$$" in normalized


def test_a_todo_marker_is_never_math() -> None:
    """The editor writes `\\[TODO:` for `[TODO:`, and the section index depends on it.

    Reading one as an opening display delimiter would also swallow the rest of the
    document looking for a `\\]` that is not coming.
    """
    source = "## Methods\n\n\\[TODO: describe the rig]\n\n## Results\n\nProse here.\n"

    assert mathnorm.normalize(source) == source


def test_an_escaped_dollar_is_left_alone() -> None:
    source = "It cost \\$5, which is \\(x\\) dollars."

    normalized = mathnorm.normalize(source)

    assert "\\$5" in normalized
    assert "$x$" in normalized


def test_fenced_code_is_verbatim() -> None:
    """A snippet showing the delimiters is not using them."""
    source = "Prose \\(a\\).\n\n```latex\n\\(not math here\\)\n```\n\nMore \\(b\\).\n"

    normalized = mathnorm.normalize(source)

    assert "```latex\n\\(not math here\\)\n```" in normalized
    assert "$a$" in normalized
    assert "$b$" in normalized


def test_normalization_is_idempotent() -> None:
    """Bodies are normalized at every landing, so twice must equal once."""
    source = "Inline \\(x^2\\) and display \\[ \\frac{a}{b} \\] and $already$ and $$done$$."

    once = mathnorm.normalize(source)

    assert mathnorm.normalize(once) == once


def test_text_with_no_backslashes_is_returned_unchanged() -> None:
    source = "Plain prose with $x$ and $$y$$ and nothing to fix."

    assert mathnorm.normalize(source) is source


def test_an_environment_already_in_display_math_is_not_wrapped_again() -> None:
    """The idempotence case that actually bit: nesting on every load.

    Bodies are normalized server-side when AI text lands and client-side when the draft is
    opened, so a rule that re-wraps its own output adds one more `$$` pair every visit.
    The editor rendered the result as an empty code block with the equation spilled below
    it as literal prose.
    """
    source = "A bare environment:\n\n$$\n\\begin{align}\nu &= 1 \\\\\nv &= 2\n\\end{align}\n$$\n"

    assert mathnorm.normalize(source) == source


def test_delimiters_inside_display_math_are_left_alone() -> None:
    """What is already display math is already correct, whatever it contains."""
    source = "$$\n\\begin{cases} a \\\\ b \\end{cases}\n$$\n"

    assert mathnorm.normalize(source) == source


def test_undelimited_math_tokens_are_wrapped() -> None:
    source = "From X_1 and λ_1, with R^n and \\frac{a}{b}."

    normalized = mathnorm.normalize(source)

    assert "$X_1$" in normalized
    assert "$R^n$" in normalized
    assert "$\\frac{a}{b}$" in normalized


def test_editor_escaped_subscripts_are_restored_without_wrapping_identifiers() -> None:
    source = r"Use X\_1 and λ\_1, but keep snake_case as prose."

    assert mathnorm.normalize(source) == ("Use $X_1$ and $λ_1$, but keep snake_case as prose.")


def test_leading_four_spaces_in_prose_are_dedented() -> None:
    source = (
        "The result:\n"
        "    X_1 converges through every iteration because the transformation preserves "
        "the relevant direction while changing only its magnitude across the complete basis."
    )

    normalized = mathnorm.normalize(source)

    assert normalized == (
        "The result:\n"
        "$X_1$ converges through every iteration because the transformation preserves "
        "the relevant direction while changing only its magnitude across the complete basis."
    )


def test_short_indented_code_is_preserved() -> None:
    source = "Example:\n    X_1 = transform(vector)\n"

    assert mathnorm.normalize(source) == source


def test_space_entity_prose_is_dedented() -> None:
    source = (
        "&#x20;   Linear transformations preserve addition and scalar multiplication while "
        "changing the direction and magnitude of ordinary vectors throughout the transformed space."
    )

    assert mathnorm.normalize(source).startswith("Linear transformations preserve")


def test_undelimited_tokens_inside_inline_and_display_math_are_preserved() -> None:
    source = "Existing $X_1$ and $$R^n + X_2$$ stay exactly as written."

    assert mathnorm.normalize(source) == source


def test_promotion_still_happens_outside_display_math() -> None:
    """The guard must not cost the rule its job."""
    source = "Before.\n\n\\begin{align}\nx &= 1\n\\end{align}\n\nAfter.\n"

    normalized = mathnorm.normalize(source)

    assert "$$\n\\begin{align}" in normalized
    assert mathnorm.normalize(normalized) == normalized
