"""One immutable depth dial for every bounded writer and reviewer loop.

The pipeline may spend the values differently, but it must never maintain a second
quick/standard/deep table. Keeping endpoint capabilities here as well gives callers a
small read-only policy object without teaching pipelines about the settings schema.
"""

import sqlite3
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, cast

from backend.core import classes

Depth = Literal["quick", "standard", "deep"]
DEPTHS: tuple[Depth, ...] = ("quick", "standard", "deep")


@dataclass(frozen=True, slots=True)
class Budget:
    """All ceilings controlled by writer depth.

    ``max_findings`` is the number of targeted rewrites accepted from one evaluation;
    ``evaluation_passes`` is how often a whole document or lens may be evaluated.
    """

    section_retries: int
    max_critique_rounds: int
    max_findings: int
    evaluation_passes: int
    tool_loop_depth: int
    wall_clock_seconds: float

    @property
    def max_revise_rounds(self) -> int:
        """Compatibility name for the pre-convergence writer pipeline."""
        return self.max_critique_rounds

    @property
    def max_revisions_per_round(self) -> int:
        """Compatibility name for the pre-convergence writer pipeline."""
        return self.max_findings


BUDGETS = MappingProxyType(
    {
        "quick": Budget(
            section_retries=1,
            max_critique_rounds=2,
            max_findings=6,
            evaluation_passes=1,
            tool_loop_depth=8,
            wall_clock_seconds=10 * 60,
        ),
        "standard": Budget(
            section_retries=2,
            max_critique_rounds=4,
            max_findings=10,
            evaluation_passes=2,
            tool_loop_depth=16,
            wall_clock_seconds=30 * 60,
        ),
        "deep": Budget(
            section_retries=4,
            max_critique_rounds=8,
            max_findings=16,
            evaluation_passes=4,
            tool_loop_depth=24,
            wall_clock_seconds=90 * 60,
        ),
    }
)


def validate_depth(value: str) -> Depth:
    """Return a normalized depth, raising a useful error for caller input."""
    normalized = value.strip().lower()
    if normalized not in DEPTHS:
        raise ValueError(f"Unknown writer depth: {value}")
    return cast(Depth, normalized)


def get_budget(depth: str) -> Budget:
    """The shared, immutable budget profile for ``depth``."""
    return BUDGETS[validate_depth(depth)]


@dataclass(frozen=True, slots=True)
class WriterCapabilities:
    """Resolved class capabilities after nullable overrides inherit global defaults."""

    allow_web_research: bool
    parallel_requests: bool
    parallel_concurrency: int
    source_content_enabled: bool


_UNSET = object()


def get_writer_capabilities(conn: sqlite3.Connection, class_id: int) -> WriterCapabilities:
    """Resolve global writer settings with a class's tri-state overrides."""
    classes.get_class(conn, class_id)
    global_row = conn.execute(
        "select allow_web_research, parallel_requests, parallel_concurrency, "
        "source_content_enabled "
        "from settings where id = 1"
    ).fetchone()
    if global_row is None:
        raise RuntimeError("The settings row is missing. The database was not migrated.")
    override = conn.execute(
        "select allow_web_research, parallel_requests, parallel_concurrency "
        "from class_writer_capabilities where class_id = ?",
        (class_id,),
    ).fetchone()

    def resolved(column: str) -> object:
        if override is not None and override[column] is not None:
            return override[column]
        return global_row[column]

    concurrency = int(resolved("parallel_concurrency"))
    # Concurrency is inert while parallelism is disabled, but retaining the configured
    # value means enabling it later does not silently reset the student's choice.
    return WriterCapabilities(
        allow_web_research=bool(resolved("allow_web_research")),
        parallel_requests=bool(resolved("parallel_requests")),
        parallel_concurrency=concurrency,
        source_content_enabled=bool(global_row["source_content_enabled"]),
    )


def get_class_capability_overrides(
    conn: sqlite3.Connection, class_id: int
) -> dict[str, object | None]:
    """Read raw nullable overrides, returning all-null inheritance when absent."""
    classes.get_class(conn, class_id)
    row = conn.execute(
        "select allow_web_research, parallel_requests, parallel_concurrency, updated_at "
        "from class_writer_capabilities where class_id = ?",
        (class_id,),
    ).fetchone()
    if row is None:
        return {
            "allow_web_research": None,
            "parallel_requests": None,
            "parallel_concurrency": None,
            "updated_at": None,
        }
    return dict(row)


def update_class_capability_overrides(
    conn: sqlite3.Connection,
    class_id: int,
    *,
    allow_web_research: bool | None | object = _UNSET,
    parallel_requests: bool | None | object = _UNSET,
    parallel_concurrency: int | None | object = _UNSET,
) -> WriterCapabilities:
    """Patch per-class overrides and return the resulting effective capabilities.

    ``None`` means inherit. An omitted keyword leaves the prior override untouched.
    When every value becomes null the redundant override row is removed.
    """
    classes.get_class(conn, class_id)
    current = get_class_capability_overrides(conn, class_id)
    values: dict[str, object | None] = {
        "allow_web_research": current["allow_web_research"],
        "parallel_requests": current["parallel_requests"],
        "parallel_concurrency": current["parallel_concurrency"],
    }
    # SQLite returns stored booleans as 0/1 integers. Normalize the existing row before
    # validating this patch so leaving one field omitted is genuinely a no-op.
    for key in ("allow_web_research", "parallel_requests"):
        if values[key] is not None:
            values[key] = bool(values[key])
    supplied = {
        "allow_web_research": allow_web_research,
        "parallel_requests": parallel_requests,
        "parallel_concurrency": parallel_concurrency,
    }
    for key, value in supplied.items():
        if value is not _UNSET:
            values[key] = value

    for key in ("allow_web_research", "parallel_requests"):
        value = values[key]
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"{key} must be true, false, or null")
    concurrency = values["parallel_concurrency"]
    if concurrency is not None and (
        isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1
    ):
        raise ValueError("parallel_concurrency must be a positive integer or null")

    if all(value is None for value in values.values()):
        conn.execute("delete from class_writer_capabilities where class_id = ?", (class_id,))
    else:
        conn.execute(
            "insert into class_writer_capabilities "
            "(class_id, allow_web_research, parallel_requests, parallel_concurrency) "
            "values (?, ?, ?, ?) "
            "on conflict (class_id) do update set "
            "allow_web_research = excluded.allow_web_research, "
            "parallel_requests = excluded.parallel_requests, "
            "parallel_concurrency = excluded.parallel_concurrency, "
            "updated_at = datetime('now')",
            (
                class_id,
                values["allow_web_research"],
                values["parallel_requests"],
                values["parallel_concurrency"],
            ),
        )
    conn.commit()
    return get_writer_capabilities(conn, class_id)


# Short aliases for settings routes and older call sites.
resolve_writer_capabilities = get_writer_capabilities
update_writer_capabilities = update_class_capability_overrides
